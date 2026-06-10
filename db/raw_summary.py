from __future__ import annotations

import heapq
import json
import logging
import os
import re
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, TextIO

from labeler.db.raw_tables import CATALOG_TABLE, NODE_TARGET_TABLE, UNKNOWN_COLLECTION

LOGGER = logging.getLogger(__name__)
_NON_DIGIT_RE = re.compile(r"[^0-9]")
_PIPE_ALIAS_RE = re.compile(r"\s*\|\|\s*")
_RELATIONSHIP_ALIAS_PIPE_RE = re.compile(r" \| {1,2}\| ")
_RELATIONSHIP_FIELD_SEPARATOR = " | "
_WHITESPACE_RE = re.compile(r"\s+")
_RELATIONSHIP_METADATA_LINES = {
    "entity_1 | relation | entity_2 | relation_category | confidence",
    "entity_1 | relation | entity_2 | relation_category | confidence | evidence",
}
_JSON_DECODER = json.JSONDecoder()
_SUMMARY_READ_CHUNK_SIZE = 64 * 1024
_SPOOL_MAX_MEMORY_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SummaryEntityRow:
    collection_name: str
    document_id: str
    document_name: str
    entity: str
    top_category: str | None
    specific_category: str | None
    wikipedia_url: str | None
    wikipedia_category: str | None
    confidence: str | None
    witness: str | None
    page_number: int | None


@dataclass(frozen=True, slots=True)
class SummaryEdgeRow:
    collection_name: str
    document_id: str
    document_name: str
    term_1: str
    semantic_category_1: str | None
    term_2: str
    semantic_category_2: str | None
    relationship: str
    relation_category: str | None
    confidence: str | None


@dataclass(frozen=True, slots=True)
class GlobalSummaryResult:
    output_path: Path
    payload: dict[str, Any] | None
    node_rows: list[SummaryEntityRow]
    edge_rows: list[SummaryEdgeRow]
    stats: dict[str, int]


@dataclass(frozen=True, slots=True)
class SummaryDocumentRows:
    doc_id: str
    node_rows: list[SummaryEntityRow]
    edge_rows: list[SummaryEdgeRow]
    document_summary: dict[str, Any] | None
    skipped_document: dict[str, Any] | None


def get_postgres_client():
    from labeler.db.db_connect import get_postgres_client as _get_postgres_client

    return _get_postgres_client()


def _absolute_path(path: Path) -> str:
    return os.path.abspath(path)


def collect_doc_ids(docs_root: Path, limit: int | None) -> list[str]:
    if limit is not None:
        max_count = max(0, int(limit))
        if max_count == 0:
            return []
        return heapq.nsmallest(max_count, _iter_document_ids(docs_root))
    return sorted(_iter_document_ids(docs_root))


def _iter_document_ids(docs_root: Path) -> Iterable[str]:
    with os.scandir(docs_root) as entries:
        for entry in entries:
            if _is_document_entry(entry):
                yield entry.name


def _is_document_entry(entry) -> bool:
    if entry.name.casefold() in {"cache"}:
        return False
    try:
        return entry.is_dir()
    except OSError:
        return False


def load_catalog_metadata(doc_ids: list[str]) -> dict[str, dict[str, Any]]:
    try:
        client = get_postgres_client()
    except Exception as exc:
        LOGGER.warning("Could not initialize PostgreSQL client for catalog lookup: %s", exc)
        return {}
    try:
        rows = client.fetch_all(
            f"""
            SELECT id, collection_name
            FROM {CATALOG_TABLE}
            WHERE id = ANY(%s)
            """,
            (doc_ids,),
        )
    except Exception as exc:
        LOGGER.warning("Could not load catalog metadata from %s: %s", CATALOG_TABLE, exc)
        return {}
    finally:
        client.close()

    return {str(row["id"]): row for row in rows}


def _nullable_text(value: Any) -> str | None:
    if value is None:
        return None
    source = value if isinstance(value, str) else str(value)
    text = _trimmed_text(source)
    if not text or _is_null_literal(text):
        return None
    return text


def _parse_page_number(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int) and value >= 0:
        return value
    source = value if isinstance(value, str) else str(value)
    raw = _trimmed_text(source)
    if not raw or _is_null_literal(raw):
        return None
    if raw.isdecimal():
        return int(raw)
    digits = _NON_DIGIT_RE.sub("", raw)
    if not digits:
        return None
    return int(digits)


def _pick_edge_semantic_category(row: SummaryEntityRow) -> str | None:
    return row.specific_category or row.top_category or row.wikipedia_category


def _normalize_pipe_alias(text: str) -> str:
    if "||" not in text:
        return text
    return _PIPE_ALIAS_RE.sub("||", text)


def _normalize_metadata_line(text: str) -> str:
    trimmed = _trimmed_text(text or "")
    if not trimmed:
        return ""
    return _WHITESPACE_RE.sub(" ", trimmed).casefold()


def _is_relationship_metadata_line(text: str, start: int | None = None) -> bool:
    source = text or ""
    if start is None:
        start = _first_non_whitespace_index(source)
    if start < 0:
        return False
    if _is_document_genre_metadata_candidate(source, start):
        return True
    if not _is_relationship_table_metadata_candidate(source, start):
        return False
    normalized = _normalize_metadata_line(source)
    return normalized in _RELATIONSHIP_METADATA_LINES


def _is_document_genre_metadata_candidate(text: str, start: int) -> bool:
    text_length = len(text)
    if start + 8 >= text_length:
        return False
    if not _span_equals_ascii_lower(text, start, "document"):
        return False
    index = start + 8
    if not text[index].isspace():
        return False
    while index < text_length and text[index].isspace():
        index += 1
    return index + 6 <= text_length and _span_equals_ascii_lower(text, index, "genre:")


def _is_relationship_table_metadata_candidate(text: str, start: int) -> bool:
    text_length = len(text)
    if start + 8 > text_length:
        return False
    if not _span_equals_ascii_lower(text, start, "entity_1"):
        return False
    index = start + 8
    while index < text_length and text[index].isspace():
        index += 1
    return index < text_length and text[index] == "|"


def _is_valid_confidence_label(text: str) -> bool:
    length = len(text)
    if length == 3:
        return _span_equals_ascii_lower(text, 0, "low")
    if length == 4:
        return _span_equals_ascii_lower(text, 0, "high")
    if length == 6:
        return _span_equals_ascii_lower(text, 0, "medium")
    return False


def _is_null_literal(text: str) -> bool:
    return len(text) == 4 and _span_equals_ascii_lower(text, 0, "null")


def _span_equals_ascii_lower(text: str, start: int, expected: str) -> bool:
    for offset, expected_char in enumerate(expected):
        char_code = ord(text[start + offset])
        if 65 <= char_code <= 90:
            char_code += 32
        if char_code != ord(expected_char):
            return False
    return True


def _first_non_whitespace_index(text: str) -> int:
    for index, char in enumerate(text):
        if not char.isspace():
            return index
    return -1


def _trimmed_text(text: str) -> str:
    source = text or ""
    return _trimmed_slice(source, 0, len(source))


def _trimmed_slice(text: str, start: int, end: int) -> str:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if start == end:
        return ""
    if start == 0 and end == len(text):
        return text
    return text[start:end]


def build_entity_category_lookup(rows: list[SummaryEntityRow]) -> tuple[dict[str, str], dict[str, str]]:
    exact: dict[str, str] = {}
    folded: dict[str, str] = {}
    for row in rows:
        semantic_category = _pick_edge_semantic_category(row)
        if not semantic_category:
            continue
        normalized = _normalize_pipe_alias(row.entity)
        exact.setdefault(row.entity, semantic_category)
        folded.setdefault(row.entity.casefold(), semantic_category)
        if normalized != row.entity:
            exact.setdefault(normalized, semantic_category)
            folded.setdefault(normalized.casefold(), semantic_category)
    return exact, folded


_db_node_rows_cache: list[dict[str, Any]] | None = None
_db_category_index_cache: tuple[
    dict[str, str | None],
    dict[str, list[tuple[str, str | None]]],
] | None = None
_db_category_lookup_cache: dict[str, str | None] = {}

NODE_PROPERTIES = [
    "collection_name",
    "document_id",
    "document_name",
    "entity",
    "top_category",
    "specific_category",
    "wikipedia_url",
    "wikipedia_category",
    "confidence",
    "witness",
]
EDGE_PROPERTIES = [
    "collection_name",
    "document_id",
    "document_name",
    "term_1",
    "semantic_category_1",
    "term_2",
    "semantic_category_2",
    "relationship",
    "relation_category",
    "confidence",
]


def _load_db_node_rows() -> list[dict[str, Any]]:
    global _db_node_rows_cache
    if _db_node_rows_cache is not None:
        return _db_node_rows_cache
    _db_node_rows_cache = []
    if not NODE_TARGET_TABLE.strip():
        return _db_node_rows_cache
    client = get_postgres_client()
    try:
        _db_node_rows_cache = client.fetch_all(
            f"""
            SELECT term, specific_category, top_category
            FROM {NODE_TARGET_TABLE}
            """
        )
    finally:
        client.close()
    LOGGER.info("Loaded %s rows from %s for category lookup.", len(_db_node_rows_cache), NODE_TARGET_TABLE)
    return _db_node_rows_cache


def _load_db_category_indexes() -> tuple[
    dict[str, str | None],
    dict[str, list[tuple[str, str | None]]],
]:
    global _db_category_index_cache
    if _db_category_index_cache is not None:
        return _db_category_index_cache

    exact: dict[str, str | None] = {}
    candidates_by_char: dict[str, list[tuple[str, str | None]]] = {}
    for row in _load_db_node_rows():
        raw_term = row.get("term") or ""
        db_term = _trimmed_text(raw_term if isinstance(raw_term, str) else str(raw_term))
        if not db_term:
            continue
        category = row.get("specific_category") or row.get("top_category") or None
        db_term_lower = db_term.casefold()
        candidate = (db_term_lower, category)
        exact.setdefault(db_term_lower, category)
        for char in set(db_term_lower):
            candidates_by_char.setdefault(char, []).append(candidate)
    _db_category_index_cache = (exact, candidates_by_char)
    return _db_category_index_cache


def _resolve_semantic_category_from_db(term: str) -> str | None:
    term_lower = term.casefold()
    if not term_lower:
        return None
    if term_lower in _db_category_lookup_cache:
        return _db_category_lookup_cache[term_lower]
    exact, candidates_by_char = _load_db_category_indexes()
    if term_lower in exact:
        category = exact[term_lower]
        _db_category_lookup_cache[term_lower] = category
        return category
    for db_term_lower, category in candidates_by_char.get(term_lower[0], ()):
        if term_lower in db_term_lower:
            _db_category_lookup_cache[term_lower] = category
            return category
    _db_category_lookup_cache[term_lower] = None
    return None


def resolve_edge_semantic_category(
    term: str,
    exact_categories: dict[str, str],
    folded_categories: dict[str, str],
) -> str | None:
    if term in exact_categories:
        return exact_categories[term]
    folded = folded_categories.get(term.casefold())
    if folded:
        return folded
    return _resolve_semantic_category_from_db(term)


def parse_summary_rows(
    *,
    summary_path: Path,
    collection_name: str,
    document_id: str,
    fallback_document_name: str,
) -> list[SummaryEntityRow]:
    with summary_path.open("r", encoding="utf-8") as fh:
        return _parse_summary_rows_from_stream(
            fh,
            summary_path=summary_path,
            collection_name=collection_name,
            document_id=document_id,
            fallback_document_name=fallback_document_name,
        )


def _parse_summary_rows_from_stream(
    handle: TextIO,
    *,
    summary_path: Path,
    collection_name: str,
    document_id: str,
    fallback_document_name: str,
) -> list[SummaryEntityRow]:
    reader = _JsonStreamReader(handle)
    document_name = fallback_document_name
    document_name_seen = False
    rows: list[SummaryEntityRow] = []
    entities_seen = False

    reader.skip_whitespace()
    if reader.read_char() != "{":
        LOGGER.warning("Skipping %s because summary.json is not an object.", summary_path)
        return []

    while True:
        reader.skip_whitespace()
        char = reader.peek()
        if not char or char == "}":
            if char == "}":
                reader.read_char()
            break

        key = reader.decode_value()
        reader.skip_whitespace()
        if reader.read_char() != ":":
            raise json.JSONDecodeError("Expected ':' after object key", "", 0)

        if key == "document_name":
            updated_document_name = _nullable_text(reader.decode_value()) or fallback_document_name
            document_name_seen = True
            if updated_document_name != document_name:
                document_name = updated_document_name
                if rows:
                    rows = [_copy_entity_row_with_document_name(row, document_name) for row in rows]
        elif key == "entities":
            entities_seen = True
            if reader.peek_non_whitespace() != "[":
                _skip_json_value(reader)
                LOGGER.warning("Skipping %s because entities is not a list.", summary_path)
                rows = []
            else:
                rows = _parse_entity_rows_from_json_array(
                    reader,
                    summary_path=summary_path,
                    collection_name=collection_name,
                    document_id=document_id,
                    document_name=document_name,
                )
                if document_name_seen:
                    return rows
        else:
            _skip_json_value(reader)

        reader.skip_whitespace()
        char = reader.peek()
        if char == ",":
            reader.read_char()
            continue
        if char == "}":
            reader.read_char()
            break
        if not char:
            break
        raise json.JSONDecodeError("Expected ',' or '}' after object value", "", 0)

    if not entities_seen:
        return []
    return rows


class _JsonStreamReader:
    __slots__ = ("_eof", "_handle", "_pos", "_text")

    def __init__(self, handle: TextIO) -> None:
        self._handle = handle
        self._text = ""
        self._pos = 0
        self._eof = False

    def peek(self) -> str:
        self._fill()
        if self._pos >= len(self._text):
            return ""
        return self._text[self._pos]

    def peek_non_whitespace(self) -> str:
        self.skip_whitespace()
        return self.peek()

    def read_char(self) -> str:
        char = self.peek()
        if char:
            self._pos += 1
            self._compact()
        return char

    def skip_whitespace(self) -> None:
        while True:
            char = self.peek()
            if not char or not char.isspace():
                return
            self._pos += 1
            self._compact()

    def decode_value(self) -> Any:
        self.skip_whitespace()
        while True:
            try:
                value, end = _JSON_DECODER.raw_decode(self._text, self._pos)
            except json.JSONDecodeError:
                if self._eof:
                    raise
                self._fill(force=True)
                continue
            self._pos = end
            self._compact()
            return value

    def _fill(self, *, force: bool = False) -> None:
        if self._eof:
            return
        if not force and self._pos < len(self._text):
            return
        chunk = self._handle.read(_SUMMARY_READ_CHUNK_SIZE)
        if not chunk:
            self._eof = True
            return
        if self._pos:
            self._text = self._text[self._pos :] + chunk
            self._pos = 0
        else:
            self._text += chunk

    def _compact(self) -> None:
        if self._pos < _SUMMARY_READ_CHUNK_SIZE:
            return
        self._text = self._text[self._pos :]
        self._pos = 0


def _parse_entity_rows_from_json_array(
    reader: _JsonStreamReader,
    *,
    summary_path: Path,
    collection_name: str,
    document_id: str,
    document_name: str,
) -> list[SummaryEntityRow]:
    rows: list[SummaryEntityRow] = []
    reader.skip_whitespace()
    reader.read_char()

    index = 1
    while True:
        reader.skip_whitespace()
        char = reader.peek()
        if not char:
            return rows
        if char == "]":
            reader.read_char()
            return rows
        item = reader.decode_value()
        if not isinstance(item, dict):
            LOGGER.warning("Skipping malformed entity %s in %s: expected object.", index, summary_path)
        else:
            row = _summary_entity_row_from_payload(
                item,
                index=index,
                summary_path=summary_path,
                collection_name=collection_name,
                document_id=document_id,
                document_name=document_name,
            )
            if row is not None:
                rows.append(row)
        index += 1

        reader.skip_whitespace()
        char = reader.peek()
        if char == ",":
            reader.read_char()
            continue
        if char == "]":
            reader.read_char()
            return rows
        raise json.JSONDecodeError("Expected ',' or ']' after array value", "", 0)


def _summary_entity_row_from_payload(
    item: dict[str, Any],
    *,
    index: int,
    summary_path: Path,
    collection_name: str,
    document_id: str,
    document_name: str,
) -> SummaryEntityRow | None:
    entity = _nullable_text(item.get("entity"))
    if not entity:
        LOGGER.warning("Skipping entity %s in %s: missing entity.", index, summary_path)
        return None
    return SummaryEntityRow(
        collection_name=collection_name,
        document_id=document_id,
        document_name=document_name,
        entity=entity,
        top_category=_nullable_text(item.get("top_category")),
        specific_category=_nullable_text(item.get("specific_category")),
        wikipedia_url=_nullable_text(item.get("wikipedia_url")),
        wikipedia_category=_nullable_text(item.get("wikipedia_category")),
        confidence=_nullable_text(item.get("confidence")),
        witness=_nullable_text(item.get("witness")),
        page_number=_parse_page_number(item.get("page_number")) or 0,
    )


def _copy_entity_row_with_document_name(row: SummaryEntityRow, document_name: str) -> SummaryEntityRow:
    return SummaryEntityRow(
        collection_name=row.collection_name,
        document_id=row.document_id,
        document_name=document_name,
        entity=row.entity,
        top_category=row.top_category,
        specific_category=row.specific_category,
        wikipedia_url=row.wikipedia_url,
        wikipedia_category=row.wikipedia_category,
        confidence=row.confidence,
        witness=row.witness,
        page_number=row.page_number,
    )


def _skip_json_value(reader: _JsonStreamReader) -> None:
    reader.skip_whitespace()
    char = reader.peek()
    if char == '"':
        _skip_json_string(reader)
        return
    if char == "{":
        _skip_json_container(reader, "}")
        return
    if char == "[":
        _skip_json_container(reader, "]")
        return
    while True:
        char = reader.peek()
        if not char or char in ",]}":
            return
        reader.read_char()


def _skip_json_container(reader: _JsonStreamReader, first_closer: str) -> None:
    reader.read_char()
    closers = [first_closer]
    while closers:
        char = reader.peek()
        if not char:
            raise json.JSONDecodeError("Unterminated JSON container", "", 0)
        if char == '"':
            _skip_json_string(reader)
            continue
        reader.read_char()
        if char == "{":
            closers.append("}")
        elif char == "[":
            closers.append("]")
        elif char == closers[-1]:
            closers.pop()


def _skip_json_string(reader: _JsonStreamReader) -> None:
    if reader.read_char() != '"':
        raise json.JSONDecodeError("Expected string", "", 0)
    escaped = False
    while True:
        char = reader.read_char()
        if not char:
            raise json.JSONDecodeError("Unterminated string", "", 0)
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            return
    return rows


def parse_relationship_rows(
    *,
    relationship_path: Path,
    collection_name: str,
    document_id: str,
    document_name: str,
    exact_categories: dict[str, str],
    folded_categories: dict[str, str],
) -> list[SummaryEdgeRow]:
    with relationship_path.open("r", encoding="utf-8") as relationship_file:
        return _parse_relationship_rows_from_lines(
            relationship_lines=relationship_file,
            relationship_path=relationship_path,
            collection_name=collection_name,
            document_id=document_id,
            document_name=document_name,
            exact_categories=exact_categories,
            folded_categories=folded_categories,
        )


def _parse_relationship_rows_from_lines(
    *,
    relationship_lines: Iterable[str],
    relationship_path: Path,
    collection_name: str,
    document_id: str,
    document_name: str,
    exact_categories: dict[str, str],
    folded_categories: dict[str, str],
) -> list[SummaryEdgeRow]:
    rows: list[SummaryEdgeRow] = []
    seen_keys: set[tuple[str, str, str, str]] = set()
    semantic_category_cache: dict[str, str | None] = {}

    def semantic_category(term: str) -> str | None:
        if term not in semantic_category_cache:
            semantic_category_cache[term] = resolve_edge_semantic_category(
                term,
                exact_categories,
                folded_categories,
            )
        return semantic_category_cache[term]

    for line_number, raw_line in enumerate(relationship_lines, start=1):
        line_start = _first_non_whitespace_index(raw_line)
        if line_start < 0:
            continue
        if _is_relationship_metadata_line(raw_line, line_start):
            continue

        line = raw_line
        if " | | " in line or " |  | " in line:
            line = _RELATIONSHIP_ALIAS_PIPE_RE.sub("||", line)
        fields = _parse_relationship_fields(line)
        if fields is None:
            LOGGER.warning(
                "Skipping malformed relationship line %s in %s: %s",
                line_number,
                relationship_path,
                raw_line,
            )
            continue

        term_1, relationship, term_2, relation_category, confidence = fields
        if not term_1 or not relationship or not term_2:
            LOGGER.warning(
                "Skipping incomplete relationship line %s in %s: %s",
                line_number,
                relationship_path,
                raw_line,
            )
            continue
        if not _is_valid_confidence_label(confidence):
            LOGGER.warning(
                "Skipping relationship line %s in %s with invalid confidence %r: %s",
                line_number,
                relationship_path,
                confidence,
                raw_line,
            )
            continue

        edge_key = (term_1, relationship, term_2, relation_category)
        if edge_key in seen_keys:
            continue
        seen_keys.add(edge_key)

        rows.append(
            SummaryEdgeRow(
                collection_name=collection_name,
                document_id=document_id,
                document_name=document_name,
                term_1=term_1,
                semantic_category_1=semantic_category(term_1),
                term_2=term_2,
                semantic_category_2=semantic_category(term_2),
                relationship=relationship,
                relation_category=_nullable_parsed_field(relation_category),
                confidence=confidence,
            )
        )

    return rows


def _parse_relationship_fields(line: str) -> tuple[str, str, str, str, str] | None:
    separator_length = len(_RELATIONSHIP_FIELD_SEPARATOR)
    first = line.find(_RELATIONSHIP_FIELD_SEPARATOR)
    if first < 0:
        return None
    second = line.find(_RELATIONSHIP_FIELD_SEPARATOR, first + separator_length)
    if second < 0:
        return None
    third = line.find(_RELATIONSHIP_FIELD_SEPARATOR, second + separator_length)
    if third < 0:
        return None
    fourth = line.find(_RELATIONSHIP_FIELD_SEPARATOR, third + separator_length)
    if fourth < 0:
        return None
    confidence_start = fourth + separator_length
    fifth = line.find(_RELATIONSHIP_FIELD_SEPARATOR, confidence_start)
    confidence_end = len(line) if fifth < 0 else fifth
    return (
        _trimmed_slice(line, 0, first),
        _trimmed_slice(line, first + separator_length, second),
        _trimmed_slice(line, second + separator_length, third),
        _trimmed_slice(line, third + separator_length, fourth),
        _trimmed_slice(line, confidence_start, confidence_end),
    )


def _nullable_parsed_field(text: str) -> str | None:
    if not text or _is_null_literal(text):
        return None
    return text


def _new_summary_stats() -> dict[str, int]:
    return {
        "processed_docs": 0,
        "skipped_docs": 0,
        "node_rows_prepared": 0,
        "edge_rows_prepared": 0,
    }


def iter_summary_document_rows(
    docs_root: Path,
    doc_ids: list[str],
) -> Iterable[SummaryDocumentRows]:
    catalog_by_id = load_catalog_metadata(doc_ids)
    for doc_id in doc_ids:
        yield _parse_summary_document_rows(docs_root=docs_root, doc_id=doc_id, catalog_by_id=catalog_by_id)


def _parse_summary_document_rows(
    *,
    docs_root: Path,
    doc_id: str,
    catalog_by_id: dict[str, dict[str, Any]],
) -> SummaryDocumentRows:
    doc_dir = docs_root / doc_id
    summary_path = doc_dir / "summary.json"
    relationship_path = doc_dir / "relationship.txt"

    metadata = catalog_by_id.get(doc_id)
    collection_name = _nullable_text((metadata or {}).get("collection_name")) or UNKNOWN_COLLECTION

    try:
        doc_rows = parse_summary_rows(
            summary_path=summary_path,
            collection_name=collection_name,
            document_id=doc_id,
            fallback_document_name=doc_id,
        )
    except FileNotFoundError:
        LOGGER.warning("Skipping %s because summary.json is missing.", doc_id)
        return SummaryDocumentRows(
            doc_id=doc_id,
            node_rows=[],
            edge_rows=[],
            document_summary=None,
            skipped_document={
                "doc_id": doc_id,
                "reason": "summary.json is missing",
                "summary_path": _absolute_path(summary_path),
            },
        )
    if metadata is None:
        LOGGER.warning("No catalog row found for %s in %s. Using %s.", doc_id, CATALOG_TABLE, UNKNOWN_COLLECTION)

    document_name = doc_rows[0].document_name if doc_rows else doc_id
    relationship_path_text: str | None = None
    try:
        relationship_file = relationship_path.open("r", encoding="utf-8")
    except FileNotFoundError:
        doc_edge_rows = []
    else:
        with relationship_file:
            exact_categories, folded_categories = build_entity_category_lookup(doc_rows)
            doc_edge_rows = _parse_relationship_rows_from_lines(
                relationship_lines=relationship_file,
                relationship_path=relationship_path,
                collection_name=collection_name,
                document_id=doc_id,
                document_name=document_name,
                exact_categories=exact_categories,
                folded_categories=folded_categories,
            )
            relationship_path_text = _absolute_path(relationship_path)
    return SummaryDocumentRows(
        doc_id=doc_id,
        node_rows=doc_rows,
        edge_rows=doc_edge_rows,
        document_summary={
            "doc_id": doc_id,
            "collection_name": collection_name,
            "document_name": document_name,
            "node_count": len(doc_rows),
            "edge_count": len(doc_edge_rows),
            "summary_path": _absolute_path(summary_path),
            "relationship_path": relationship_path_text,
        },
        skipped_document=None,
    )


def collect_summary_rows(
    docs_root: Path,
    doc_ids: list[str],
) -> tuple[list[SummaryEntityRow], list[SummaryEdgeRow], list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    node_rows: list[SummaryEntityRow] = []
    edge_rows: list[SummaryEdgeRow] = []
    document_summaries: list[dict[str, Any]] = []
    skipped_documents: list[dict[str, Any]] = []
    stats = _new_summary_stats()

    for document in iter_summary_document_rows(docs_root, doc_ids):
        if document.skipped_document is not None:
            skipped_documents.append(document.skipped_document)
            stats["skipped_docs"] += 1
            continue
        node_rows.extend(document.node_rows)
        edge_rows.extend(document.edge_rows)
        if document.document_summary is not None:
            document_summaries.append(document.document_summary)
        stats["processed_docs"] += 1
        stats["node_rows_prepared"] += len(document.node_rows)
        stats["edge_rows_prepared"] += len(document.edge_rows)

    return node_rows, edge_rows, document_summaries, skipped_documents, stats


def build_overall_summary_payload(
    *,
    docs_root: Path,
    node_rows: list[SummaryEntityRow],
    edge_rows: list[SummaryEdgeRow],
    document_summaries: list[dict[str, Any]],
    skipped_documents: list[dict[str, Any]],
    stats: dict[str, int],
) -> dict[str, Any]:
    return {
        "docs_root": _absolute_path(docs_root),
        "total_documents": stats["processed_docs"],
        "skipped_documents": stats["skipped_docs"],
        "skipped_doc_ids": [item["doc_id"] for item in skipped_documents],
        "total_node_rows": stats["node_rows_prepared"],
        "total_edge_rows": stats["edge_rows_prepared"],
        "node_properties": NODE_PROPERTIES,
        "edge_properties": EDGE_PROPERTIES,
        "documents": document_summaries,
        "skipped_document_details": skipped_documents,
        "nodes": [_entity_row_to_dict(row) for row in node_rows],
        "edges": [_edge_row_to_dict(row) for row in edge_rows],
    }


def _entity_row_to_dict(row: SummaryEntityRow) -> dict[str, Any]:
    return {
        "collection_name": row.collection_name,
        "document_id": row.document_id,
        "document_name": row.document_name,
        "entity": row.entity,
        "top_category": row.top_category,
        "specific_category": row.specific_category,
        "wikipedia_url": row.wikipedia_url,
        "wikipedia_category": row.wikipedia_category,
        "confidence": row.confidence,
        "witness": row.witness,
        "page_number": row.page_number,
    }


def _edge_row_to_dict(row: SummaryEdgeRow) -> dict[str, Any]:
    return {
        "collection_name": row.collection_name,
        "document_id": row.document_id,
        "document_name": row.document_name,
        "term_1": row.term_1,
        "semantic_category_1": row.semantic_category_1,
        "term_2": row.term_2,
        "semantic_category_2": row.semantic_category_2,
        "relationship": row.relationship,
        "relation_category": row.relation_category,
        "confidence": row.confidence,
    }


def write_overall_summary(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
        fh.write("\n")


def write_overall_summary_from_rows(
    *,
    path: Path,
    docs_root: Path,
    node_rows: list[SummaryEntityRow],
    edge_rows: list[SummaryEdgeRow],
    document_summaries: list[dict[str, Any]],
    skipped_documents: list[dict[str, Any]],
    stats: dict[str, int],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        fh.write("{\n")
        _write_json_field(fh, "docs_root", _absolute_path(docs_root))
        _write_json_field(fh, "total_documents", stats["processed_docs"])
        _write_json_field(fh, "skipped_documents", stats["skipped_docs"])
        _write_json_array_field(fh, "skipped_doc_ids", _iter_skipped_doc_ids(skipped_documents))
        _write_json_field(fh, "total_node_rows", stats["node_rows_prepared"])
        _write_json_field(fh, "total_edge_rows", stats["edge_rows_prepared"])
        _write_json_field(fh, "node_properties", NODE_PROPERTIES)
        _write_json_field(fh, "edge_properties", EDGE_PROPERTIES)
        _write_json_field(fh, "documents", document_summaries)
        _write_json_field(fh, "skipped_document_details", skipped_documents)
        _write_row_array(fh, "nodes", node_rows, _entity_row_to_dict)
        _write_row_array(fh, "edges", edge_rows, _edge_row_to_dict, trailing_comma=False)
        fh.write("}\n")


def write_overall_summary_from_documents(
    *,
    path: Path,
    docs_root: Path,
    documents: Iterable[SummaryDocumentRows],
    retain_document_details: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    path.parent.mkdir(parents=True, exist_ok=True)
    document_summaries: list[dict[str, Any]] = []
    skipped_documents: list[dict[str, Any]] = []
    stats = _new_summary_stats()

    with ExitStack() as stack:
        document_summaries_file = _LazySpool(stack)
        skipped_documents_file = _LazySpool(stack)
        skipped_doc_ids_file = _LazySpool(stack)
        node_rows_file = _LazySpool(stack)
        edge_rows_file = _LazySpool(stack)
        document_summary_count = 0
        skipped_document_count = 0
        node_count = 0
        edge_count = 0
        for document in documents:
            if document.skipped_document is not None:
                if retain_document_details:
                    skipped_documents.append(document.skipped_document)
                skipped_documents_file.write_row(document.skipped_document, skipped_document_count)
                skipped_doc_ids_file.write_row(document.skipped_document["doc_id"], skipped_document_count)
                skipped_document_count += 1
                stats["skipped_docs"] += 1
                continue
            if document.document_summary is not None:
                if retain_document_details:
                    document_summaries.append(document.document_summary)
                document_summaries_file.write_row(document.document_summary, document_summary_count)
                document_summary_count += 1
            for row in document.node_rows:
                node_rows_file.write_entity_row(row, node_count)
                node_count += 1
            for row in document.edge_rows:
                edge_rows_file.write_edge_row(row, edge_count)
                edge_count += 1
            stats["processed_docs"] += 1
            stats["node_rows_prepared"] += len(document.node_rows)
            stats["edge_rows_prepared"] += len(document.edge_rows)

        with path.open("w", encoding="utf-8") as fh:
            fh.write("{\n")
            _write_json_field(fh, "docs_root", _absolute_path(docs_root))
            _write_json_field(fh, "total_documents", stats["processed_docs"])
            _write_json_field(fh, "skipped_documents", stats["skipped_docs"])
            _write_spooled_array(fh, "skipped_doc_ids", skipped_doc_ids_file.handle)
            _write_json_field(fh, "total_node_rows", stats["node_rows_prepared"])
            _write_json_field(fh, "total_edge_rows", stats["edge_rows_prepared"])
            _write_json_field(fh, "node_properties", NODE_PROPERTIES)
            _write_json_field(fh, "edge_properties", EDGE_PROPERTIES)
            _write_spooled_array(fh, "documents", document_summaries_file.handle)
            _write_spooled_array(fh, "skipped_document_details", skipped_documents_file.handle)
            _write_spooled_array(fh, "nodes", node_rows_file.handle)
            _write_spooled_array(fh, "edges", edge_rows_file.handle, trailing_comma=False)
            fh.write("}\n")

    return document_summaries, skipped_documents, stats


class _LazySpool:
    def __init__(self, stack: ExitStack) -> None:
        self._stack = stack
        self.handle: TextIO | None = None
        self._entity_payload: dict[str, Any] | None = None
        self._edge_payload: dict[str, Any] | None = None

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

    def write_row(self, row: Any, row_index: int) -> None:
        _write_spooled_row(self._ensure_handle(), row, row_index)

    def write_entity_row(self, row: SummaryEntityRow, row_index: int) -> None:
        if self._entity_payload is None:
            self._entity_payload = _new_entity_spool_payload()
        _write_spooled_row(
            self._ensure_handle(),
            _fill_entity_spool_payload(self._entity_payload, row),
            row_index,
        )

    def write_edge_row(self, row: SummaryEdgeRow, row_index: int) -> None:
        if self._edge_payload is None:
            self._edge_payload = _new_edge_spool_payload()
        _write_spooled_row(
            self._ensure_handle(),
            _fill_edge_spool_payload(self._edge_payload, row),
            row_index,
        )


def _write_spooled_row(handle: TextIO, row: Any, row_index: int) -> None:
    if row_index:
        handle.write(",\n")
    handle.write("    ")
    handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))


def _new_entity_spool_payload() -> dict[str, Any]:
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


def _fill_entity_spool_payload(
    payload: dict[str, Any],
    row: SummaryEntityRow,
) -> dict[str, Any]:
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


def _new_edge_spool_payload() -> dict[str, Any]:
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


def _fill_edge_spool_payload(
    payload: dict[str, Any],
    row: SummaryEdgeRow,
) -> dict[str, Any]:
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


def _write_spooled_array(
    output: TextIO,
    name: str,
    rows_file: TextIO | None,
    *,
    trailing_comma: bool = True,
) -> None:
    output.write(f'  "{name}": [')
    if rows_file is None:
        output.write("]")
        output.write(",\n" if trailing_comma else "\n")
        return
    rows_file.seek(0)
    first_chunk = rows_file.read(1024 * 1024)
    if first_chunk:
        output.write("\n")
        output.write(first_chunk)
        while True:
            chunk = rows_file.read(1024 * 1024)
            if not chunk:
                break
            output.write(chunk)
        output.write("\n  ]")
    else:
        output.write("]")
    output.write(",\n" if trailing_comma else "\n")


def _write_json_field(handle, name: str, value: Any) -> None:
    handle.write(f'  "{name}": ')
    json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
    handle.write(",\n")


def _write_json_array_field(handle, name: str, values: Iterable[Any], *, trailing_comma: bool = True) -> None:
    handle.write(f'  "{name}": [')
    wrote_value = False
    for value in values:
        if wrote_value:
            handle.write(",")
        json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
        wrote_value = True
    handle.write("]")
    handle.write(",\n" if trailing_comma else "\n")


def _iter_skipped_doc_ids(skipped_documents: Iterable[dict[str, Any]]) -> Iterable[Any]:
    for item in skipped_documents:
        yield item["doc_id"]


def _write_row_array(handle, name: str, rows, row_to_dict, *, trailing_comma: bool = True) -> None:
    handle.write(f'  "{name}": [')
    wrote_row = False
    for row in rows:
        if wrote_row:
            handle.write(",")
        handle.write("\n    ")
        json.dump(row_to_dict(row), handle, ensure_ascii=False, separators=(",", ":"))
        wrote_row = True
    if wrote_row:
        handle.write("\n  ]")
    else:
        handle.write("]")
    handle.write(",\n" if trailing_comma else "\n")


def build_global_summary(
    *,
    docs_root: Path,
    doc_ids: list[str],
    summary_output: Path,
    materialize_payload: bool = True,
    retain_rows: bool = True,
) -> GlobalSummaryResult:
    if not retain_rows and materialize_payload:
        raise ValueError("retain_rows=False requires materialize_payload=False.")
    if not retain_rows:
        _, _, stats = write_overall_summary_from_documents(
            path=summary_output,
            docs_root=docs_root,
            documents=iter_summary_document_rows(docs_root, doc_ids),
            retain_document_details=False,
        )
        return GlobalSummaryResult(
            output_path=summary_output,
            payload=None,
            node_rows=[],
            edge_rows=[],
            stats=stats,
        )

    node_rows, edge_rows, document_summaries, skipped_documents, stats = collect_summary_rows(docs_root, doc_ids)
    if materialize_payload:
        payload = build_overall_summary_payload(
            docs_root=docs_root,
            node_rows=node_rows,
            edge_rows=edge_rows,
            document_summaries=document_summaries,
            skipped_documents=skipped_documents,
            stats=stats,
        )
        write_overall_summary(summary_output, payload)
    else:
        payload = None
        write_overall_summary_from_rows(
            path=summary_output,
            docs_root=docs_root,
            node_rows=node_rows,
            edge_rows=edge_rows,
            document_summaries=document_summaries,
            skipped_documents=skipped_documents,
            stats=stats,
        )
    return GlobalSummaryResult(
        output_path=summary_output,
        payload=payload,
        node_rows=node_rows,
        edge_rows=edge_rows,
        stats=stats,
    )
