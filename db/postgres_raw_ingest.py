from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable

from labeler.db.db_connect import get_postgres_client
from labeler.db.raw_summary import SummaryEdgeRow, SummaryEntityRow
from labeler.db.raw_tables import EDGE_TARGET_TABLE, NODE_TARGET_TABLE

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PostgresRawIngestResult:
    document_ids: list[str]
    deleted_node_rows: int
    deleted_edge_rows: int
    inserted_node_rows: int
    inserted_edge_rows: int


def replace_raw_data_rows(
    *,
    document_ids: Iterable[str],
    node_rows: Iterable[SummaryEntityRow],
    edge_rows: Iterable[SummaryEdgeRow],
    include_nodes: bool = True,
    include_edges: bool = True,
    client: Any | None = None,
    dry_run: bool = False,
    insert_batch_size: int = 1000,
) -> PostgresRawIngestResult:
    """Replace raw node/edge rows for the supplied document ids.

    Stage 3 re-runs should be idempotent: a document's old extracted raw
    rows are removed in the same transaction before the newly parsed rows
    are inserted.
    """

    doc_ids = _normalize_document_ids(document_ids)
    if not doc_ids:
        raise ValueError("replace_raw_data_rows requires at least one document id.")
    if not include_nodes and not include_edges:
        raise ValueError("replace_raw_data_rows requires at least one target table.")
    if include_nodes and not NODE_TARGET_TABLE.strip():
        raise RuntimeError("Postgres raw node ingest target table is not configured.")
    if include_edges and not EDGE_TARGET_TABLE.strip():
        raise RuntimeError("Postgres raw edge ingest target table is not configured.")

    insert_batch_size = max(1, int(insert_batch_size))
    inserted_node_count = 0
    inserted_edge_count = 0
    owns_client = client is None
    pg = client or get_postgres_client()

    deleted_nodes = 0
    deleted_edges = 0
    try:
        with pg.connection() as conn:
            with conn.cursor() as cur:
                if include_edges:
                    cur.execute(
                        f"DELETE FROM {EDGE_TARGET_TABLE} WHERE document_id = ANY(%s)",
                        (doc_ids,),
                    )
                    deleted_edges = max(int(getattr(cur, "rowcount", 0) or 0), 0)
                if include_nodes:
                    cur.execute(
                        f"DELETE FROM {NODE_TARGET_TABLE} WHERE document_id = ANY(%s)",
                        (doc_ids,),
                    )
                    deleted_nodes = max(int(getattr(cur, "rowcount", 0) or 0), 0)

                if include_nodes:
                    node_insert_sql = f"""
                        INSERT INTO {NODE_TARGET_TABLE}
                            (
                                collection_name,
                                document_id,
                                term,
                                top_category,
                                specific_category,
                                wikipedia_category,
                                wikipedia_url,
                                confidence,
                                witness,
                                page_number
                            )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """
                    for batch in _batched(_iter_node_params(node_rows), insert_batch_size):
                        inserted_node_count += len(batch)
                        cur.executemany(node_insert_sql, batch)

                if include_edges:
                    edge_insert_sql = f"""
                        INSERT INTO {EDGE_TARGET_TABLE}
                            (
                                collection_name,
                                document_id,
                                term_1,
                                semantic_category_1,
                                term_2,
                                semantic_category_2,
                                relationship,
                                relation_category,
                                confidence
                            )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """
                    for batch in _batched(_iter_unique_edge_params(edge_rows), insert_batch_size):
                        inserted_edge_count += len(batch)
                        cur.executemany(edge_insert_sql, batch)

            if dry_run:
                conn.rollback()
            else:
                conn.commit()
    finally:
        if owns_client:
            pg.close()

    LOGGER.info(
        "Postgres raw ingest replaced docs=%s deleted(nodes=%s, edges=%s) inserted(nodes=%s, edges=%s)",
        _format_doc_ids_for_log(doc_ids),
        deleted_nodes,
        deleted_edges,
        inserted_node_count,
        inserted_edge_count,
    )
    return PostgresRawIngestResult(
        document_ids=doc_ids,
        deleted_node_rows=deleted_nodes,
        deleted_edge_rows=deleted_edges,
        inserted_node_rows=inserted_node_count,
        inserted_edge_rows=inserted_edge_count,
    )


def _iter_node_params(rows: Iterable[SummaryEntityRow]) -> Iterable[tuple[Any, ...]]:
    for row in rows:
        yield (
            row.collection_name,
            row.document_id,
            row.entity,
            row.top_category,
            row.specific_category,
            row.wikipedia_category,
            row.wikipedia_url,
            row.confidence,
            row.witness,
            row.page_number,
        )


def _iter_unique_edge_params(rows: Iterable[SummaryEdgeRow]) -> Iterable[tuple[Any, ...]]:
    seen: set[tuple[str | None, ...]] = set()
    for row in rows:
        key = (
            row.collection_name,
            row.document_id,
            row.term_1,
            row.semantic_category_1,
            row.term_2,
            row.semantic_category_2,
            row.relationship,
            row.relation_category,
        )
        if key in seen:
            continue
        seen.add(key)
        yield (
            row.collection_name,
            row.document_id,
            row.term_1,
            row.semantic_category_1,
            row.term_2,
            row.semantic_category_2,
            row.relationship,
            row.relation_category,
            row.confidence,
        )


def _normalize_document_ids(document_ids: Iterable[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] | None = None
    previous: str | None = None
    for doc_id in document_ids:
        text = _trimmed_text(str(doc_id))
        if not text:
            continue
        if seen is None:
            if previous is None or text > previous:
                normalized.append(text)
                previous = text
                continue
            if text == previous:
                continue
            seen = set(normalized)
        seen.add(text)
    if seen is None:
        return normalized
    return sorted(seen)


def _trimmed_text(text: str) -> str:
    source = text or ""
    start = 0
    end = len(source)
    while start < end and source[start].isspace():
        start += 1
    while end > start and source[end - 1].isspace():
        end -= 1
    if start == end:
        return ""
    if start == 0 and end == len(source):
        return source
    return source[start:end]


def _format_doc_ids_for_log(doc_ids: list[str], *, limit: int = 8) -> str:
    if limit <= 0:
        return f"...(+{len(doc_ids)} more)" if doc_ids else ""
    if len(doc_ids) <= limit:
        return ",".join(doc_ids)
    return f"{','.join(doc_ids[:limit])},...(+{len(doc_ids) - limit} more)"


def _batched(items: Iterable[tuple[Any, ...]], batch_size: int) -> Iterable[list[tuple[Any, ...]]]:
    batch: list[tuple[Any, ...]] = []
    for item in items:
        batch.append(item)
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch
