from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .ids import sha_id
from .solr_client import SolrClient

_GENERATED_LABEL_ROW_PREFIX = '{"page_index":'
_GENERATED_LABEL_FIELD_PREFIX = ',"label":"'
_YYYY_MM_DD_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True, slots=True)
class SolrRawTextIngestResult:
    document_id: str
    solr_id: str
    indexed_documents: int
    body_chars: int
    core_url: str


@dataclass(frozen=True, slots=True)
class _PageLabelStats:
    family: str
    labels: list[str]
    page_count: int


def build_raw_text_solr_document(
    *,
    doc_dir: Path,
    collection_name: str = "",
    document_name: str = "",
    document_date: str = "",
) -> dict[str, Any]:
    doc_id = doc_dir.name
    text_path = doc_dir / f"{doc_id}.txt"
    try:
        with text_path.open("r", encoding="utf-8") as fh:
            body = fh.read()
    except OSError as exc:
        raise FileNotFoundError(f"Raw OCR text file not found: {text_path}") from exc
    label_stats = _load_page_label_stats(doc_dir / "labels.jsonl")

    doc: dict[str, Any] = {
        "id": sha_id("doc_strong", doc_id),
        "document_id": doc_id,
        "title": document_name or doc_id,
        "family": label_stats.family.upper(),
        "page_count": label_stats.page_count,
        "indexed_at": datetime.now(timezone.utc).isoformat(),
    }
    if body:
        doc["body"] = body
    if collection_name:
        doc["collection"] = collection_name
    solr_date = _normalize_solr_date(document_date)
    if solr_date:
        doc["date"] = solr_date
    if label_stats.labels:
        doc["labels"] = label_stats.labels
    return doc


def ingest_raw_text_to_solr(
    *,
    doc_dir: Path,
    collection_name: str = "",
    document_name: str = "",
    document_date: str = "",
    client: SolrClient | None = None,
) -> SolrRawTextIngestResult:
    solr = client or SolrClient.from_env()
    doc = build_raw_text_solr_document(
        doc_dir=doc_dir,
        collection_name=collection_name,
        document_name=document_name,
        document_date=document_date,
    )
    solr.replace_document(doc)
    return SolrRawTextIngestResult(
        document_id=str(doc["document_id"]),
        solr_id=str(doc["id"]),
        indexed_documents=1,
        body_chars=len(str(doc.get("body") or "")),
        core_url=solr.config.core_url,
    )


def _load_page_label_stats(labels_path: Path) -> _PageLabelStats:
    try:
        fh = labels_path.open("r", encoding="utf-8")
    except OSError:
        return _PageLabelStats(family="document", labels=[], page_count=0)

    counts: dict[str, int] = {}
    first_seen: dict[str, int] = {}
    unique_labels: set[str] = set()
    page_count = 0

    with fh:
        for line in fh:
            if not _jsonl_line_has_payload(line):
                continue
            label = _parse_generated_label_row(line)
            if label is None:
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                raw_label = payload.get("label") or ""
                label = _trimmed_text(raw_label if isinstance(raw_label, str) else str(raw_label))
            if not label:
                continue

            page_count += 1
            normalized = label.lower()
            if normalized not in first_seen:
                first_seen[normalized] = len(first_seen)
                unique_labels.add(label.upper())
            counts[normalized] = counts.get(normalized, 0) + 1

    if not counts:
        family = "document"
    else:
        family = max(counts, key=lambda item: (counts[item], -first_seen[item]))
    return _PageLabelStats(
        family=family,
        labels=sorted(unique_labels),
        page_count=page_count,
    )


def _load_page_labels(labels_path: Path) -> list[str]:
    try:
        fh = labels_path.open("r", encoding="utf-8")
    except OSError:
        return []
    labels: list[str] = []
    with fh:
        for line in fh:
            if not _jsonl_line_has_payload(line):
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            raw_label = payload.get("label") or ""
            label = _trimmed_text(raw_label if isinstance(raw_label, str) else str(raw_label))
            if label:
                labels.append(label)
    return labels


def _normalize_solr_date(value: str) -> str:
    text = _trimmed_text(value if isinstance(value, str) else str(value or ""))
    if not text:
        return ""
    if _YYYY_MM_DD_RE.match(text):
        return f"{text}T00:00:00Z"
    if text.endswith("Z") and "T" in text:
        return text
    return ""


def _dominant_label(labels: list[str]) -> str:
    if not labels:
        return "document"
    counts: dict[str, int] = {}
    first_seen: dict[str, int] = {}
    for label in labels:
        raw_label = label or ""
        normalized = _trimmed_text(raw_label if isinstance(raw_label, str) else str(raw_label)).lower()
        if not normalized:
            continue
        if normalized not in first_seen:
            first_seen[normalized] = len(first_seen)
        counts[normalized] = counts.get(normalized, 0) + 1
    if not counts:
        return "document"
    return max(counts, key=lambda item: (counts[item], -first_seen[item]))


def _has_non_whitespace(text: str) -> bool:
    for char in text:
        if not char.isspace():
            return True
    return False


def _jsonl_line_has_payload(line: str) -> bool:
    return bool(line) and (line[0] == "{" or _has_non_whitespace(line))


def _parse_generated_label_row(line: str) -> str | None:
    if not line.startswith(_GENERATED_LABEL_ROW_PREFIX):
        return None
    page_index = len(_GENERATED_LABEL_ROW_PREFIX)
    page_index = _skip_json_int_value(line, page_index)
    if page_index < 0 or not line.startswith(_GENERATED_LABEL_FIELD_PREFIX, page_index):
        return None
    label_start = page_index + len(_GENERATED_LABEL_FIELD_PREFIX)
    label_end = line.find('"', label_start)
    if label_end < 0:
        return None
    label = line[label_start:label_end]
    if "\\" in label:
        return None
    return _trimmed_text(label)


def _skip_json_int_value(line: str, index: int) -> int:
    if index < len(line) and line[index] == "-":
        index += 1
    digit_count = 0
    while index < len(line):
        char = line[index]
        if not ("0" <= char <= "9"):
            break
        index += 1
        digit_count += 1
    return index if digit_count else -1


def _trimmed_text(text: str) -> str:
    start = 0
    end = len(text)
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if start >= end:
        return ""
    if start == 0 and end == len(text):
        return text
    return text[start:end]

