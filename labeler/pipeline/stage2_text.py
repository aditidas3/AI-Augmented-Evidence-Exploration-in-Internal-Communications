from __future__ import annotations

import io
import re
from typing import Callable, Iterable

_FIELD_SEPARATOR = " | "
_PAGE_NUMBER_RE = re.compile(r"^(?:page\s*)?\d+$", re.IGNORECASE)
_ALIAS_SEPARATOR_RE = re.compile(r"\s*\|\|\s*")
_PIPE_SEPARATOR_RE = re.compile(r"\s*\|\s*")
_WHITESPACE_RE = re.compile(r"\s+")
_BATCHED_OCCURRENCE_THRESHOLD = 16
_NORMALIZE_SEARCH_FAST_PATH_LIMIT = 256
_TRIE_TERMINAL_KEY = "\0"
_ENTITY_INVENTORY_METADATA_LINES = {
    "entity | category | confidence | context",
    "entity | specificcategory | confidence | context",
    "entity | topcategory > specificcategory | confidence | context",
}
_WIKIPEDIA_ENRICHMENT_METADATA_LINES = {
    "entity | wikipedia_url | wikipedia_category",
}
_RELATIONSHIP_METADATA_LINES = {
    "entity_1 | relation | entity_2 | relation_category | confidence",
    "entity_1 | relation | entity_2 | relation_category | confidence | evidence",
}


def collect_relationship_entity_names(raw_relationship_text: str) -> list[str]:
    return list(_iter_relationship_entity_names(raw_relationship_text))


def _iter_relationship_entity_names(raw_relationship_text: str):
    seen: set[str] = set()
    for line in _iter_text_lines(raw_relationship_text):
        parsed = _parse_relationship_record(line)
        if parsed is None:
            continue
        for name in (parsed[0], parsed[2]):
            if not name or name in seen:
                continue
            seen.add(name)
            yield name


def find_missing_entity_names(
    *,
    raw_relationship_text: str,
    entity_list_text: str,
    allowed_entity_keys: set[str] | None = None,
) -> list[str]:
    allowed_keys = allowed_entity_keys
    if allowed_keys is None:
        allowed_keys = build_allowed_entity_keys(entity_list_text)
    missing: list[str] = []
    seen_normalized: set[str] = set()
    for name in _iter_relationship_entity_names(raw_relationship_text):
        if entity_is_allowed(name, allowed_keys):
            continue
        normalized = _normalize_entity_key(name)
        if not normalized or normalized in seen_normalized:
            continue
        seen_normalized.add(normalized)
        missing.append(name)
    return missing


def sanitize_entity_inventory_text(entity_list_text: str) -> str:
    writer = _DedupedLineWriter()
    for line in _iter_text_lines(entity_list_text):
        parsed = _parse_entity_inventory_line(line)
        if parsed is None:
            continue
        entity, category, confidence, context, page_number = parsed
        normalized_line = f"{entity}{_FIELD_SEPARATOR}{category}{_FIELD_SEPARATOR}{confidence}{_FIELD_SEPARATOR}{context}"
        if page_number:
            normalized_line = f"{normalized_line}{_FIELD_SEPARATOR}{page_number}"
        writer.add(normalized_line)
    return writer.text()


def sanitize_wikipedia_enrichment_text(text: str) -> str:
    writer = _DedupedLineWriter()
    for line in _iter_text_lines(text):
        parsed = _parse_wikipedia_enrichment_line(line)
        if parsed is None:
            continue
        entity, wikipedia_url, wikipedia_category = parsed
        normalized_line = f"{entity}{_FIELD_SEPARATOR}{wikipedia_url}{_FIELD_SEPARATOR}{wikipedia_category}"
        writer.add(normalized_line)
    return writer.text()


def sanitize_relationship_output(
    text: str,
    *,
    entity_list_text: str,
    allowed_entity_keys: set[str] | None = None,
) -> str:
    if allowed_entity_keys is None:
        allowed_entity_keys = build_allowed_entity_keys(entity_list_text)
    writer = _DedupedLineWriter()
    for line in _iter_text_lines(text):
        parsed = _parse_relationship_record(line)
        if parsed is None:
            continue
        entity_1, relation, entity_2, relation_category, confidence, evidence, _ = parsed
        if not entity_is_allowed(entity_1, allowed_entity_keys):
            continue
        if not entity_is_allowed(entity_2, allowed_entity_keys):
            continue
        normalized_line = (
            f"{entity_1}{_FIELD_SEPARATOR}"
            f"{relation}{_FIELD_SEPARATOR}"
            f"{entity_2}{_FIELD_SEPARATOR}"
            f"{relation_category}{_FIELD_SEPARATOR}"
            f"{confidence}{_FIELD_SEPARATOR}"
            f"{evidence}"
        )
        writer.add(normalized_line)
    return writer.text()


def sanitize_relationship_output_and_find_missing(
    text: str,
    *,
    entity_list_text: str,
    allowed_entity_keys: set[str] | None = None,
) -> tuple[str, list[str]]:
    if allowed_entity_keys is None:
        allowed_entity_keys = build_allowed_entity_keys(entity_list_text)
    writer = _DedupedLineWriter()
    missing: list[str] = []
    seen_missing_normalized: set[str] = set()
    for line in _iter_text_lines(text):
        parsed = _parse_relationship_record(line)
        if parsed is None:
            continue
        entity_1, relation, entity_2, relation_category, confidence, evidence, _ = parsed
        entity_1_allowed = entity_is_allowed(entity_1, allowed_entity_keys)
        entity_2_allowed = entity_is_allowed(entity_2, allowed_entity_keys)
        if not entity_1_allowed:
            _append_missing_entity(entity_1, missing, seen_missing_normalized)
        if not entity_2_allowed:
            _append_missing_entity(entity_2, missing, seen_missing_normalized)
        if not entity_1_allowed or not entity_2_allowed:
            continue
        normalized_line = (
            f"{entity_1}{_FIELD_SEPARATOR}"
            f"{relation}{_FIELD_SEPARATOR}"
            f"{entity_2}{_FIELD_SEPARATOR}"
            f"{relation_category}{_FIELD_SEPARATOR}"
            f"{confidence}{_FIELD_SEPARATOR}"
            f"{evidence}"
        )
        writer.add(normalized_line)
    return writer.text(), missing


def build_entity_summary_payload(
    *,
    document_name: str,
    entities_text: str,
    wikipedia_enrichment_text: str = "",
) -> dict[str, object]:
    entities: list[dict[str, object]] = []
    seen: set[tuple[str, str, str, str]] = set()
    enrichment_by_entity, enrichment_by_normalized_key = parse_wikipedia_enrichment(wikipedia_enrichment_text)
    enrichment_lookup_cache: dict[str, dict[str, str] | None] = {}

    for line in _iter_text_lines(entities_text):
        parsed = _parse_entity_inventory_line(line)
        if parsed is None:
            continue
        entity, category, confidence, context, page_number = parsed
        key = (entity, category, confidence, context)
        if key in seen:
            continue
        seen.add(key)
        if entity in enrichment_lookup_cache:
            enrichment = enrichment_lookup_cache[entity]
        else:
            enrichment = _find_wikipedia_enrichment_for_entity(
                entity=entity,
                enrichment_by_entity=enrichment_by_entity,
                enrichment_by_normalized_key=enrichment_by_normalized_key,
            )
            enrichment_lookup_cache[entity] = enrichment
        wikipedia_url: str | None = None
        wikipedia_category: str | None = None
        if enrichment is not None:
            wikipedia_url_value = enrichment["wikipedia_url"]
            if wikipedia_url_value and not _is_null_literal(wikipedia_url_value):
                wikipedia_url = wikipedia_url_value
            if _wiki_category_is_present(enrichment["wikipedia_category"]):
                wikipedia_category = enrichment["wikipedia_category"]
        top_category, specific_category = _split_category(category)
        entities.append(
            {
                "entity": entity,
                "top_category": top_category,
                "specific_category": specific_category,
                "wikipedia_url": wikipedia_url,
                "wikipedia_category": wikipedia_category,
                "confidence": confidence,
                "witness": context,
                "page_number": page_number or None,
            }
        )

    return {
        "document_name": document_name,
        "entities": entities,
    }


def parse_wikipedia_enrichment(
    text: str,
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str] | None]]:
    enrichment_by_entity: dict[str, dict[str, str]] = {}
    enrichment_by_normalized_key: dict[str, dict[str, str] | None] = {}
    for line in _iter_text_lines(text):
        parsed = _parse_wikipedia_enrichment_line(line)
        if parsed is None:
            continue
        entity, wikipedia_url, wikipedia_category = parsed
        key = entity
        if not key or key in enrichment_by_entity:
            continue
        enrichment = {
            "wikipedia_url": wikipedia_url,
            "wikipedia_category": wikipedia_category,
        }
        enrichment_by_entity[key] = enrichment
        for normalized_key in _iter_entity_match_keys(key):
            existing = enrichment_by_normalized_key.get(normalized_key)
            if existing is None and normalized_key in enrichment_by_normalized_key:
                continue
            if existing is not None and existing != enrichment:
                enrichment_by_normalized_key[normalized_key] = None
                continue
            enrichment_by_normalized_key[normalized_key] = enrichment
    return enrichment_by_entity, enrichment_by_normalized_key


def build_allowed_entity_keys(entity_list_text: str) -> set[str]:
    allowed_keys: set[str] = set()
    for line in _iter_text_lines(entity_list_text):
        parsed = _parse_entity_inventory_line(line)
        if parsed is None:
            continue
        entity = parsed[0]
        for key in _iter_entity_match_keys(entity):
            allowed_keys.add(key)
    return allowed_keys


def entity_is_allowed(entity: str, allowed_entity_keys: set[str]) -> bool:
    if not _has_non_whitespace(entity):
        return False
    for key in _iter_entity_match_keys(entity):
        if key in allowed_entity_keys:
            return True
    return False


def _append_missing_entity(entity: str, missing: list[str], seen_normalized: set[str]) -> None:
    normalized = _normalize_entity_key(entity)
    if not normalized or normalized in seen_normalized:
        return
    seen_normalized.add(normalized)
    missing.append(entity)


def build_label_statistics(*, entities_text: str, relationship_text: str, document_text: str) -> dict[str, object]:
    node_label_counts: dict[str, int] = {}
    parsed_node_rows = 0
    skipped_node_rows = 0
    entity_occurrence_counts: dict[str, dict[str, object]] = {}
    total_entity_mentions = 0
    entity_rows: list[tuple[str, str]] = []

    for line in _iter_text_lines(entities_text):
        parsed = _parse_entity_inventory_line(line)
        if parsed is None:
            if _has_non_whitespace(line):
                skipped_node_rows += 1
            continue
        entity, category, _, _, _ = parsed
        if not category:
            skipped_node_rows += 1
            continue
        if not entity:
            skipped_node_rows += 1
            continue
        node_label_counts[category] = node_label_counts.get(category, 0) + 1
        parsed_node_rows += 1
        entity_rows.append((entity, category))

    normalized_document_text = _normalize_search_text(document_text) if entity_rows else ""
    if normalized_document_text:
        variant_normalizer = _cached_search_text_normalizer()
        variant_counts = _build_variant_occurrence_counts(
            normalized_document_text=normalized_document_text,
            variants=(
                variant
                for entity, _ in entity_rows
                for variant in _iter_entity_variants(entity)
            ),
            normalize_variant=variant_normalizer,
        )
    else:
        variant_normalizer = _normalize_search_text
        variant_counts = {}
    for entity, category in entity_rows:
        occurrence_info = _build_entity_occurrence_info(
            entity=entity,
            category=category,
            counts_by_normalized_variant=variant_counts,
            normalize_variant=variant_normalizer,
        )
        entity_occurrence_counts[entity] = occurrence_info
        total_entity_mentions += int(occurrence_info["count"])

    edge_label_counts: dict[str, int] = {}
    edge_category_counts: dict[str, int] = {}
    edge_evidence_type_counts: dict[str, int] = {}
    parsed_edge_rows = 0
    skipped_edge_rows = 0

    for line in _iter_text_lines(relationship_text):
        parsed = _parse_relationship_record(line)
        if parsed is None:
            if _has_non_whitespace(line):
                skipped_edge_rows += 1
            continue
        _, relation, _, relation_category, _, _, evidence_type = parsed
        if not relation:
            skipped_edge_rows += 1
            continue
        edge_label_counts[relation] = edge_label_counts.get(relation, 0) + 1
        if relation_category:
            edge_category_counts[relation_category] = edge_category_counts.get(relation_category, 0) + 1
        if evidence_type:
            edge_evidence_type_counts[evidence_type] = edge_evidence_type_counts.get(evidence_type, 0) + 1
        parsed_edge_rows += 1

    return {
        "nodes": {
            "total": parsed_node_rows,
            "total_mentions_in_document": total_entity_mentions,
            "skipped_rows": skipped_node_rows,
            "label_counts": dict(node_label_counts),
            "entity_occurrence_counts": entity_occurrence_counts,
        },
        "edges": {
            "total": parsed_edge_rows,
            "skipped_rows": skipped_edge_rows,
            "label_counts": dict(edge_label_counts),
            "relation_category_counts": dict(edge_category_counts),
            "evidence_type_counts": dict(edge_evidence_type_counts),
        },
    }


def normalize_output_text(text: str) -> str:
    source = text or ""
    start, end = _non_whitespace_bounds(source)
    if start == end:
        return ""
    if start == 0 and end == len(source) - 1 and source.endswith("\n"):
        return source
    return source[start:end] + "\n"


def _non_whitespace_bounds(text: str) -> tuple[int, int]:
    start = 0
    end = len(text)
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


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


def _has_non_whitespace(text: str) -> bool:
    for char in text:
        if not char.isspace():
            return True
    return False


def _iter_text_lines(text: str):
    source = text or ""
    start = 0
    index = 0
    length = len(source)
    while index < length:
        char = source[index]
        if char == "\n" or char == "\r":
            yield source[start:index]
            if char == "\r" and index + 1 < length and source[index + 1] == "\n":
                index += 1
            index += 1
            start = index
            continue
        index += 1
    if start < length:
        yield source[start:]


class _DedupedLineWriter:
    def __init__(self) -> None:
        self._first_line = ""
        self._seen: set[str] | None = None
        self._buffer: io.StringIO | None = None
        self._has_lines = False

    def add(self, line: str) -> None:
        if not self._has_lines:
            self._first_line = line
            self._has_lines = True
            return
        if line == self._first_line:
            return
        if self._seen is None:
            self._seen = {self._first_line}
        if line in self._seen:
            return
        self._seen.add(line)
        if self._buffer is None:
            self._buffer = io.StringIO()
            self._buffer.write(self._first_line)
            self._buffer.write("\n")
        self._buffer.write(line)
        self._buffer.write("\n")

    def text(self) -> str:
        if not self._has_lines:
            return ""
        if self._buffer is None:
            return f"{self._first_line}\n"
        return self._buffer.getvalue()


def _parse_entity_inventory_line(line: str) -> tuple[str, str, str, str, str] | None:
    """Entity | Category | Confidence | Context | PageNumber.

    Both Entity and Context may contain ' | ', so positional splitting from the
    right breaks on witnesses like '675 McDonnell Blvd. | Hazelwood, MO 63042'.
    Instead, anchor on Category (contains '>') followed by Confidence.
    """
    stripped = _trimmed_text(line)
    if not stripped:
        return None
    if _is_entity_inventory_metadata_line(stripped):
        return None
    spans = _find_entity_inventory_category_spans(stripped)
    if spans is None:
        return None
    category_span, confidence_span = spans

    entity_end = category_span[0] - len(_FIELD_SEPARATOR)
    entity = _trimmed_slice(stripped, 0, entity_end)
    category = _trimmed_slice(stripped, category_span[0], category_span[1])
    confidence = _trimmed_slice(stripped, confidence_span[0], confidence_span[1])
    remaining_start = confidence_span[1] + len(_FIELD_SEPARATOR)
    if remaining_start > len(stripped):
        return None

    if not entity or not category:
        return None

    last_separator = stripped.rfind(_FIELD_SEPARATOR, remaining_start)
    if last_separator >= 0 and _PAGE_NUMBER_RE.match(
        _trimmed_slice(stripped, last_separator + len(_FIELD_SEPARATOR), len(stripped))
    ):
        page_number = _trimmed_slice(stripped, last_separator + len(_FIELD_SEPARATOR), len(stripped))
        context = _trimmed_slice(stripped, remaining_start, last_separator)
    else:
        page_number = ""
        context = _trimmed_slice(stripped, remaining_start, len(stripped))

    return entity, category, confidence, context, page_number


def _parse_wikipedia_enrichment_line(line: str) -> tuple[str, str, str] | None:
    stripped = _trimmed_text(line)
    if not stripped:
        return None
    if _is_wikipedia_enrichment_metadata_line(stripped):
        return None
    last_separator = stripped.rfind(_FIELD_SEPARATOR)
    if last_separator < 0:
        return None
    previous_separator = stripped.rfind(_FIELD_SEPARATOR, 0, last_separator)
    if previous_separator < 0:
        return None
    entity = _trimmed_slice(stripped, 0, previous_separator)
    wikipedia_url = _trimmed_slice(
        stripped,
        previous_separator + len(_FIELD_SEPARATOR),
        last_separator,
    )
    wikipedia_category = _trimmed_slice(
        stripped,
        last_separator + len(_FIELD_SEPARATOR),
        len(stripped),
    )
    if not entity:
        return None
    return entity, wikipedia_url, wikipedia_category


def _find_entity_inventory_category_spans(text: str) -> tuple[tuple[int, int], tuple[int, int]] | None:
    previous_span: tuple[int, int] | None = None
    for span in _iter_delimited_spans(text, _FIELD_SEPARATOR):
        if previous_span is not None:
            if text.find(">", previous_span[0], previous_span[1]) < 0:
                previous_span = span
                continue
            if _span_is_confidence_value(text, span[0], span[1]):
                return previous_span, span
        previous_span = span
    return None


def _span_is_confidence_value(text: str, start: int, end: int) -> bool:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1

    length = end - start
    if length == 3:
        return _span_equals_ascii_lower(text, start, "low")
    if length == 4:
        return _span_equals_ascii_lower(text, start, "high")
    if length == 6:
        return _span_equals_ascii_lower(text, start, "medium")
    return False


def _span_equals_ascii_lower(text: str, start: int, expected: str) -> bool:
    for offset, expected_char in enumerate(expected):
        char_code = ord(text[start + offset])
        if 65 <= char_code <= 90:
            char_code += 32
        if char_code != ord(expected_char):
            return False
    return True


def _iter_delimited_spans(text: str, delimiter: str):
    start = 0
    delimiter_length = len(delimiter)
    while True:
        index = text.find(delimiter, start)
        if index < 0:
            yield start, len(text)
            return
        yield start, index
        start = index + delimiter_length


def _parse_relationship_line(line: str) -> dict[str, str] | None:
    parsed = _parse_relationship_record(line)
    if parsed is None:
        return None
    entity_1, relation, entity_2, relation_category, confidence, evidence, evidence_type = parsed
    return {
        "entity_1": entity_1,
        "relation": relation,
        "entity_2": entity_2,
        "relation_category": relation_category,
        "confidence": confidence,
        "evidence": evidence,
        "evidence_type": evidence_type,
    }


def _parse_relationship_record(line: str) -> tuple[str, str, str, str, str, str, str] | None:
    source = line or ""
    start = _first_non_whitespace_index(source)
    if start < 0:
        return None
    if _is_relationship_metadata_line(source, start):
        return None
    body = source
    if source[start] == "|":
        end = _last_non_whitespace_end(source)
        if end > start and source[end - 1] == "|":
            body = source[start + 1 : end - 1]
    fields = _parse_relationship_fields(body)
    if fields is None:
        return None
    entity_1, relation, entity_2, relation_category, confidence, evidence = fields
    evidence_type = ""
    if not entity_1 or not entity_2 or not relation:
        return None
    return entity_1, relation, entity_2, relation_category, confidence, evidence, evidence_type


def _parse_relationship_fields(line: str) -> tuple[str, str, str, str, str, str] | None:
    first = line.find("|")
    if first < 0:
        return None
    second = line.find("|", first + 1)
    if second < 0:
        return None
    third = line.find("|", second + 1)
    if third < 0:
        return None
    fourth = line.find("|", third + 1)
    if fourth < 0:
        return None
    confidence_start = fourth + 1
    fifth = line.find("|", confidence_start)
    confidence_end = len(line) if fifth < 0 else fifth
    evidence = "" if fifth < 0 else _trimmed_slice(line, fifth + 1, len(line))
    return (
        _trimmed_slice(line, 0, first),
        _trimmed_slice(line, first + 1, second),
        _trimmed_slice(line, second + 1, third),
        _trimmed_slice(line, third + 1, fourth),
        _trimmed_slice(line, confidence_start, confidence_end),
        evidence,
    )


def _last_non_whitespace_end(text: str) -> int:
    end = len(text)
    while end > 0 and text[end - 1].isspace():
        end -= 1
    return end


def _find_wikipedia_enrichment_for_entity(
    *,
    entity: str,
    enrichment_by_entity: dict[str, dict[str, str]],
    enrichment_by_normalized_key: dict[str, dict[str, str] | None],
) -> dict[str, str] | None:
    exact = enrichment_by_entity.get(entity)
    if exact is not None:
        return exact

    for match_key in _iter_entity_match_keys(entity):
        matched = enrichment_by_normalized_key.get(match_key)
        if matched is None:
            continue
        return matched

    return None


def _entity_match_keys(entity: str) -> list[str]:
    return list(_iter_entity_match_keys(entity))


def _iter_entity_match_keys(entity: str):
    seen: set[str] = set()

    def normalize(value: str) -> str | None:
        normalized = _normalize_entity_key(value)
        if not normalized or normalized in seen:
            return None
        seen.add(normalized)
        return normalized

    stripped = _trimmed_text(entity)
    normalized = normalize(stripped)
    if normalized is not None:
        yield normalized
    if "||" not in stripped and "|" not in stripped:
        return
    for variant in _iter_entity_variants(stripped):
        normalized = normalize(variant)
        if normalized is not None:
            yield normalized


def _normalize_entity_key(text: str) -> str:
    normalized = _normalize_search_text(text)
    if "||" not in normalized:
        return normalized
    normalized = _ALIAS_SEPARATOR_RE.sub(" || ", normalized)
    normalized = _trimmed_text(_WHITESPACE_RE.sub(" ", normalized))
    return normalized


def _wiki_category_is_present(wiki_category: str) -> bool:
    stripped = _trimmed_text(wiki_category or "")
    return bool(stripped) and not _is_null_literal(stripped)


def _is_null_literal(text: str) -> bool:
    return len(text) == 4 and _span_equals_ascii_lower(text, 0, "null")


def _split_category(category: str) -> tuple[str, str]:
    source = category or ""
    first_separator = source.find(">")
    if first_separator < 0:
        stripped = _trimmed_text(source)
        return stripped, stripped
    last_separator = source.rfind(">")
    return _trimmed_slice(source, 0, first_separator), _trimmed_slice(source, last_separator + 1, len(source))


def _build_entity_occurrence_info(
    *,
    entity: str,
    category: str,
    counts_by_normalized_variant: dict[str, int],
    normalize_variant: Callable[[str], str] | None = None,
) -> dict[str, object]:
    variant_counts: dict[str, int] = {}
    total = 0
    if not counts_by_normalized_variant:
        for variant in _iter_entity_variants(entity):
            variant_counts[variant] = 0
        return {
            "category": category,
            "count": 0,
            "variants": variant_counts,
        }
    normalizer = normalize_variant or _normalize_search_text
    for variant in _iter_entity_variants(entity):
        count = counts_by_normalized_variant.get(normalizer(variant), 0)
        variant_counts[variant] = count
        total += count
    return {
        "category": category,
        "count": total,
        "variants": variant_counts,
    }


def _expand_entity_variants(entity: str) -> list[str]:
    """Alias strings for search / Wikipedia matching: Full||Abbr, Full | Abbr, or plain."""
    return list(_iter_entity_variants(entity))


def _iter_entity_variants(entity: str):
    stripped = _trimmed_text(entity)
    if not stripped:
        return
    if "||" not in stripped and "|" not in stripped:
        yield stripped
        return

    seen: set[str] = set()
    emitted = False

    def remember(value: str) -> str | None:
        if not value:
            return None
        k = value.casefold()
        if k in seen:
            return None
        seen.add(k)
        return value

    if "||" in stripped:
        spans = _iter_delimited_alias_part_spans(stripped, "||")
        for start, end in spans:
            variant = remember(_trimmed_slice(stripped, start, end))
            if variant is None:
                continue
            emitted = True
            yield variant
    else:
        spans = _iter_delimited_alias_part_spans(stripped, "|")
        for start, end in spans:
            variant = remember(_trimmed_slice(stripped, start, end))
            if variant is None:
                continue
            emitted = True
            yield variant

    if not emitted:
        yield stripped


def _iter_delimited_alias_part_spans(text: str, delimiter: str):
    start = 0
    delimiter_length = len(delimiter)
    while True:
        index = text.find(delimiter, start)
        if index < 0:
            yield start, len(text)
            return
        yield start, index
        start = index + delimiter_length


def _normalize_search_text(text: str) -> str:
    normalized = (text or "").casefold()
    if len(normalized) > _NORMALIZE_SEARCH_FAST_PATH_LIMIT:
        return _trimmed_text(_WHITESPACE_RE.sub(" ", normalized))
    start, end = _non_whitespace_bounds(normalized)
    if start == end:
        return ""
    if not _has_collapsible_whitespace(normalized, start, end):
        return normalized if start == 0 and end == len(normalized) else normalized[start:end]
    return _WHITESPACE_RE.sub(" ", normalized[start:end])


def _has_collapsible_whitespace(text: str, start: int, end: int) -> bool:
    previous_space = False
    for index in range(start, end):
        char = text[index]
        if not char.isspace():
            previous_space = False
            continue
        if char != " " or previous_space:
            return True
        previous_space = True
    return False


def _cached_search_text_normalizer() -> Callable[[str], str]:
    cache: dict[str, str] = {}
    missing = object()

    def normalize(text: str) -> str:
        cached = cache.get(text, missing)
        if cached is not missing:
            return cached
        normalized = _normalize_search_text(text)
        cache[text] = normalized
        return normalized

    return normalize


def _build_variant_occurrence_counts(
    *,
    normalized_document_text: str,
    variants: Iterable[str],
    normalize_variant: Callable[[str], str] | None = None,
) -> dict[str, int]:
    normalizer = normalize_variant or _normalize_search_text
    unique_variants: dict[str, None] = {}
    for variant in variants:
        normalized_variant = normalizer(variant)
        if not normalized_variant:
            continue
        unique_variants.setdefault(normalized_variant, None)
    if not unique_variants:
        return {}
    if not normalized_document_text:
        return {normalized_variant: 0 for normalized_variant in unique_variants}
    if len(unique_variants) < _BATCHED_OCCURRENCE_THRESHOLD:
        return {
            normalized_variant: _count_normalized_occurrences(normalized_document_text, normalized_variant)
            for normalized_variant in unique_variants
        }
    return _scan_variant_occurrences_once(
        normalized_document_text=normalized_document_text,
        normalized_variants=unique_variants.keys(),
    )


def _scan_variant_occurrences_once(
    *,
    normalized_document_text: str,
    normalized_variants: Iterable[str],
) -> dict[str, int]:
    counts = {variant: 0 for variant in normalized_variants}
    if not normalized_document_text:
        return counts

    variant_trie = _build_variant_trie(counts.keys())
    next_allowed_start: dict[str, int] = {}

    text_length = len(normalized_document_text)
    for index in range(text_length):
        node = variant_trie.get(normalized_document_text[index])
        if node is None:
            continue
        cursor = index
        while isinstance(node, dict):
            terminals = node.get(_TRIE_TERMINAL_KEY)
            if isinstance(terminals, list):
                for variant in terminals:
                    if index < next_allowed_start.get(variant, 0):
                        continue
                    if (
                        variant[0].isalnum()
                        and index > 0
                        and _is_regex_word_char(normalized_document_text[index - 1])
                    ):
                        continue
                    end = index + len(variant)
                    if (
                        variant[-1].isalnum()
                        and end < text_length
                        and _is_regex_word_char(normalized_document_text[end])
                    ):
                        continue
                    counts[variant] += 1
                    next_allowed_start[variant] = end

            cursor += 1
            if cursor >= text_length:
                break
            node = node.get(normalized_document_text[cursor])
    return counts


def _build_variant_trie(normalized_variants: Iterable[str]) -> dict[str, object]:
    root: dict[str, object] = {}
    for variant in normalized_variants:
        if not variant:
            continue
        node = root
        for char in variant:
            child = node.get(char)
            if not isinstance(child, dict):
                child = {}
                node[char] = child
            node = child
        terminals = node.setdefault(_TRIE_TERMINAL_KEY, [])
        if isinstance(terminals, list):
            terminals.append(variant)
    return root


def _is_regex_word_char(char: str) -> bool:
    return char == "_" or char.isalnum()


def _count_occurrences(normalized_document_text: str, entity_variant: str) -> int:
    normalized_variant = _normalize_search_text(entity_variant)
    return _count_normalized_occurrences(normalized_document_text, normalized_variant)


def _count_normalized_occurrences(normalized_document_text: str, normalized_variant: str) -> int:
    if not normalized_variant:
        return 0
    count = 0
    start = 0
    text_length = len(normalized_document_text)
    variant_length = len(normalized_variant)
    first_char_is_word = normalized_variant[0].isalnum()
    last_char_is_word = normalized_variant[-1].isalnum()
    while True:
        index = normalized_document_text.find(normalized_variant, start)
        if index < 0:
            return count
        end = index + variant_length
        if first_char_is_word and index > 0 and _is_regex_word_char(normalized_document_text[index - 1]):
            start = index + 1
            continue
        if last_char_is_word and end < text_length and _is_regex_word_char(normalized_document_text[end]):
            start = index + 1
            continue
        count += 1
        start = end


def _normalize_metadata_line(text: str) -> str:
    trimmed = _trimmed_text(text or "")
    if not trimmed:
        return ""
    return _WHITESPACE_RE.sub(" ", trimmed).casefold()


def _metadata_prefix(text: str) -> str:
    source = text or ""
    start = 0
    length = len(source)
    while start < length and source[start].isspace():
        start += 1
    return source[start : min(length, start + 14)].casefold()


def _is_entity_inventory_metadata_line(text: str) -> bool:
    source = text or ""
    start = _first_non_whitespace_index(source)
    if start < 0:
        return False
    if _is_document_genre_metadata_candidate(source, start):
        return True
    if not _is_entity_table_metadata_candidate(source, start):
        return False
    normalized = _normalize_metadata_line(source)
    if not normalized:
        return False
    return normalized in _ENTITY_INVENTORY_METADATA_LINES


def _is_wikipedia_enrichment_metadata_line(text: str) -> bool:
    source = text or ""
    start = _first_non_whitespace_index(source)
    if start < 0 or not _is_entity_table_metadata_candidate(source, start):
        return False
    normalized = _normalize_metadata_line(source)
    if not normalized:
        return False
    return normalized in _WIKIPEDIA_ENRICHMENT_METADATA_LINES


def _is_relationship_metadata_line(text: str, start: int | None = None) -> bool:
    source = text or ""
    if start is None:
        start = _first_non_whitespace_index(source)
    if start < 0 or not _is_relationship_table_metadata_candidate(source, start):
        return False
    normalized = _normalize_metadata_line(source)
    if not normalized:
        return False
    return normalized in _RELATIONSHIP_METADATA_LINES


def _first_non_whitespace_index(text: str) -> int:
    for index, char in enumerate(text):
        if not char.isspace():
            return index
    return -1


def _is_document_genre_metadata_candidate(text: str, start: int) -> bool:
    length = len(text)
    if start + 8 >= length:
        return False
    if not _span_equals_ascii_lower(text, start, "document"):
        return False
    index = start + 8
    if not text[index].isspace():
        return False
    while index < length and text[index].isspace():
        index += 1
    return index + 6 <= length and _span_equals_ascii_lower(text, index, "genre:")


def _is_relationship_table_metadata_candidate(text: str, start: int) -> bool:
    length = len(text)
    if start + 8 > length:
        return False
    if not _span_equals_ascii_lower(text, start, "entity_1"):
        return False
    index = start + 8
    while index < length and text[index].isspace():
        index += 1
    return index < length and text[index] == "|"


def _is_entity_table_metadata_candidate(text: str, start: int) -> bool:
    length = len(text)
    if start + 6 > length:
        return False
    if not _span_equals_ascii_lower(text, start, "entity"):
        return False
    index = start + 6
    while index < length and text[index].isspace():
        index += 1
    return index < length and text[index] == "|"
