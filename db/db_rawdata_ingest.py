from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
import json
import logging
import tempfile
from pathlib import Path
from typing import TextIO

from labeler.db.postgres_raw_ingest import replace_raw_data_rows
from labeler.db.raw_summary import (
    GlobalSummaryResult,
    SummaryEdgeRow,
    SummaryEntityRow,
    build_global_summary,
    collect_doc_ids,
    iter_summary_document_rows,
    write_overall_summary_from_documents,
    _edge_row_to_dict,
    _entity_row_to_dict,
)
from labeler.db.raw_tables import DEFAULT_DOCS_ROOT, EDGE_TARGET_TABLE, NODE_TARGET_TABLE

LOGGER = logging.getLogger(__name__)
_SPOOL_MAX_MEMORY_BYTES = 8 * 1024 * 1024


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Load docs/*/summary.json into raw Postgres tables. "
            "The CLI rebuilds the aggregate summary, then replaces existing rows for each document."
        )
    )
    parser.add_argument(
        "--docs-root",
        type=Path,
        default=DEFAULT_DOCS_ROOT,
        help=f"Docs directory containing per-document folders (default: {DEFAULT_DOCS_ROOT}).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only process the first N document folders after sorting. Useful for testing.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and preview rows, then roll back instead of committing.",
    )
    parser.add_argument(
        "--ingest-target",
        default="nodes",
        choices=["nodes", "edges", "both", "none"],
        help=(
            "Choose which rows to write to the database after rebuilding the overall summary "
            "(default: nodes)."
        ),
    )
    parser.add_argument(
        "--summary-output",
        type=Path,
        default=None,
        help="Write an overall JSON summary for all docs (default: <docs-root>/summary.json).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO).",
    )
    return parser


def ingest_summary_nodes_to_db(rows: list[SummaryEntityRow], *, dry_run: bool) -> int:
    if not rows:
        return 0
    result = replace_raw_data_rows(
        document_ids=(row.document_id for row in rows),
        node_rows=rows,
        edge_rows=[],
        include_edges=False,
        dry_run=dry_run,
    )
    return result.inserted_node_rows


def ingest_summary_edges_to_db(rows: list[SummaryEdgeRow], *, dry_run: bool) -> int:
    if not rows:
        return 0
    result = replace_raw_data_rows(
        document_ids=(row.document_id for row in rows),
        node_rows=[],
        edge_rows=rows,
        include_nodes=False,
        dry_run=dry_run,
    )
    return result.inserted_edge_rows

def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    docs_root = args.docs_root.resolve()
    if not docs_root.exists() or not docs_root.is_dir():
        parser.error(f"Docs root does not exist or is not a directory: {docs_root}")

    doc_ids = collect_doc_ids(docs_root, args.limit)
    if not doc_ids:
        parser.error(f"No document folders found in: {docs_root}")

    LOGGER.info("Processing %s document folders from %s", len(doc_ids), docs_root)
    summary_output = (args.summary_output or (docs_root / "summary.json")).resolve()
    if args.ingest_target == "none":
        result = build_global_summary(
            docs_root=docs_root,
            doc_ids=doc_ids,
            summary_output=summary_output,
            materialize_payload=False,
            retain_rows=False,
        )
        _log_summary_result(result)
        LOGGER.info("Summary rebuilt only. No database rows were written.")
        return 0

    include_nodes = args.ingest_target in {"nodes", "both"}
    include_edges = args.ingest_target in {"edges", "both"}

    if args.dry_run:
        result = build_global_summary(
            docs_root=docs_root,
            doc_ids=doc_ids,
            summary_output=summary_output,
            materialize_payload=False,
            retain_rows=True,
        )
        _log_summary_result(result)
        _log_preview(result)
        postgres_result = replace_raw_data_rows(
            document_ids=doc_ids,
            node_rows=result.node_rows,
            edge_rows=result.edge_rows,
            include_nodes=include_nodes,
            include_edges=include_edges,
            dry_run=args.dry_run,
        )
    else:
        with _summary_and_row_iterables_for_ingest(
            docs_root=docs_root,
            doc_ids=doc_ids,
            summary_output=summary_output,
            include_nodes=include_nodes,
            include_edges=include_edges,
        ) as (result, node_rows, edge_rows):
            _log_summary_result(result)
            postgres_result = replace_raw_data_rows(
                document_ids=doc_ids,
                node_rows=node_rows,
                edge_rows=edge_rows,
                include_nodes=include_nodes,
                include_edges=include_edges,
                dry_run=False,
            )
    if include_nodes:
        LOGGER.info("Inserted %s rows into %s.", postgres_result.inserted_node_rows, NODE_TARGET_TABLE)
    if include_edges:
        LOGGER.info("Inserted %s rows into %s.", postgres_result.inserted_edge_rows, EDGE_TARGET_TABLE)
    if args.dry_run:
        LOGGER.info("Dry run complete. No database changes committed.")
    return 0


@contextmanager
def _summary_and_row_iterables_for_ingest(
    *,
    docs_root: Path,
    doc_ids: list[str],
    summary_output: Path,
    include_nodes: bool,
    include_edges: bool,
):
    with ExitStack() as stack:
        node_rows_file = _LazyJsonlSpool(stack) if include_nodes else None
        edge_rows_file = _LazyJsonlSpool(stack) if include_edges else None

        def documents_for_summary():
            for document in iter_summary_document_rows(docs_root, doc_ids):
                if document.skipped_document is None:
                    if node_rows_file is not None:
                        for row in document.node_rows:
                            node_rows_file.write_entity_row(row)
                    if edge_rows_file is not None:
                        for row in document.edge_rows:
                            edge_rows_file.write_edge_row(row)
                yield document

        _, _, stats = write_overall_summary_from_documents(
            path=summary_output,
            docs_root=docs_root,
            documents=documents_for_summary(),
            retain_document_details=False,
        )
        node_rows_handle = node_rows_file.open_for_reading() if node_rows_file is not None else None
        edge_rows_handle = edge_rows_file.open_for_reading() if edge_rows_file is not None else None
        result = GlobalSummaryResult(
            output_path=summary_output,
            payload=None,
            node_rows=[],
            edge_rows=[],
            stats=stats,
        )
        node_rows = _iter_entity_rows_from_jsonl(node_rows_handle) if node_rows_handle is not None else iter(())
        edge_rows = _iter_edge_rows_from_jsonl(edge_rows_handle) if edge_rows_handle is not None else iter(())
        yield result, node_rows, edge_rows


class _LazyJsonlSpool:
    def __init__(self, stack: ExitStack) -> None:
        self._stack = stack
        self.handle: TextIO | None = None
        self._entity_payload: dict[str, object] | None = None
        self._edge_payload: dict[str, object] | None = None

    def _ensure_handle(self) -> TextIO:
        if self.handle is None:
            self.handle = self._stack.enter_context(
                tempfile.SpooledTemporaryFile(
                    max_size=_SPOOL_MAX_MEMORY_BYTES,
                    mode="w+",
                    encoding="utf-8",
                )
            )
        return self.handle

    def write_row(self, row: dict[str, object]) -> None:
        _write_jsonl_row(self._ensure_handle(), row)

    def write_entity_row(self, row: SummaryEntityRow) -> None:
        if self._entity_payload is None:
            self._entity_payload = _new_entity_jsonl_payload()
        _write_jsonl_row(
            self._ensure_handle(),
            _fill_entity_jsonl_payload(self._entity_payload, row),
        )

    def write_edge_row(self, row: SummaryEdgeRow) -> None:
        if self._edge_payload is None:
            self._edge_payload = _new_edge_jsonl_payload()
        _write_jsonl_row(
            self._ensure_handle(),
            _fill_edge_jsonl_payload(self._edge_payload, row),
        )

    def open_for_reading(self) -> TextIO | None:
        if self.handle is None:
            return None
        self.handle.seek(0)
        return self.handle


def _write_jsonl_row(handle: TextIO, row: dict[str, object]) -> None:
    handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
    handle.write("\n")


def _new_entity_jsonl_payload() -> dict[str, object]:
    return {
        "collection_name": None,
        "document_id": None,
        "document_name": None,
        "entity": None,
        "top_category": None,
        "specific_category": None,
        "wikipedia_url": None,
        "wikipedia_category": None,
        "confidence": None,
        "witness": None,
        "page_number": None,
    }


def _fill_entity_jsonl_payload(
    payload: dict[str, object],
    row: SummaryEntityRow,
) -> dict[str, object]:
    payload["collection_name"] = row.collection_name
    payload["document_id"] = row.document_id
    payload["document_name"] = row.document_name
    payload["entity"] = row.entity
    payload["top_category"] = row.top_category
    payload["specific_category"] = row.specific_category
    payload["wikipedia_url"] = row.wikipedia_url
    payload["wikipedia_category"] = row.wikipedia_category
    payload["confidence"] = row.confidence
    payload["witness"] = row.witness
    payload["page_number"] = row.page_number
    return payload


def _new_edge_jsonl_payload() -> dict[str, object]:
    return {
        "collection_name": None,
        "document_id": None,
        "document_name": None,
        "term_1": None,
        "semantic_category_1": None,
        "term_2": None,
        "semantic_category_2": None,
        "relationship": None,
        "relation_category": None,
        "confidence": None,
    }


def _fill_edge_jsonl_payload(
    payload: dict[str, object],
    row: SummaryEdgeRow,
) -> dict[str, object]:
    payload["collection_name"] = row.collection_name
    payload["document_id"] = row.document_id
    payload["document_name"] = row.document_name
    payload["term_1"] = row.term_1
    payload["semantic_category_1"] = row.semantic_category_1
    payload["term_2"] = row.term_2
    payload["semantic_category_2"] = row.semantic_category_2
    payload["relationship"] = row.relationship
    payload["relation_category"] = row.relation_category
    payload["confidence"] = row.confidence
    return payload


def _iter_entity_rows_from_jsonl(handle: TextIO):
    for line in handle:
        if _jsonl_line_has_payload(line):
            payload = json.loads(line)
            yield SummaryEntityRow(
                payload["collection_name"],
                payload["document_id"],
                payload["document_name"],
                payload["entity"],
                payload["top_category"],
                payload["specific_category"],
                payload["wikipedia_url"],
                payload["wikipedia_category"],
                payload["confidence"],
                payload["witness"],
                payload["page_number"],
            )


def _iter_edge_rows_from_jsonl(handle: TextIO):
    for line in handle:
        if _jsonl_line_has_payload(line):
            payload = json.loads(line)
            yield SummaryEdgeRow(
                payload["collection_name"],
                payload["document_id"],
                payload["document_name"],
                payload["term_1"],
                payload["semantic_category_1"],
                payload["term_2"],
                payload["semantic_category_2"],
                payload["relationship"],
                payload["relation_category"],
                payload["confidence"],
            )


def _has_non_whitespace(text: str) -> bool:
    for char in text:
        if not char.isspace():
            return True
    return False


def _jsonl_line_has_payload(line: str) -> bool:
    return bool(line) and (line[0] == "{" or _has_non_whitespace(line))


def _log_summary_result(result: GlobalSummaryResult) -> None:
    LOGGER.info("Wrote overall summary JSON to %s", result.output_path)
    LOGGER.info(
        "Prepared %s node rows and %s edge rows from %s processed docs (%s skipped).",
        result.stats["node_rows_prepared"],
        result.stats["edge_rows_prepared"],
        result.stats["processed_docs"],
        result.stats["skipped_docs"],
    )


def _log_preview(result: GlobalSummaryResult) -> None:
    if not LOGGER.isEnabledFor(logging.INFO):
        return
    preview = [_entity_row_to_dict(row) for row in result.node_rows[:5]]
    if preview:
        LOGGER.info("Preview node rows: %s", json.dumps(preview, ensure_ascii=False, separators=(",", ":")))
    edge_preview = [_edge_row_to_dict(row) for row in result.edge_rows[:5]]
    if edge_preview:
        LOGGER.info("Preview edge rows: %s", json.dumps(edge_preview, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    raise SystemExit(main())
