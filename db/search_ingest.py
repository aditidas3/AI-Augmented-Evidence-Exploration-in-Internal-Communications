from __future__ import annotations

import heapq
import io
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol, TextIO

from .ids import sha_id
from .raw_tables import CATALOG_TABLE
from .solr_client import SolrClient
from .solr_rawtext_ingest import build_raw_text_solr_document


LOGGER = logging.getLogger(__name__)
PAGE_MARKER_RE = re.compile(
    r"^\s*=+\s*Page\s+(\d+)\s*=+\s*$",
    re.IGNORECASE | re.MULTILINE,
)
QDRANT_PAGE_NAMESPACE = uuid.UUID("13dd9314-415c-4c9d-85b9-6f77af67e433")
_JSON_DECODER = json.JSONDecoder()
_SUMMARY_READ_CHUNK_SIZE = 64 * 1024
_GENERATED_LABEL_ROW_PREFIX = '{"page_index":'
_GENERATED_LABEL_FIELD_PREFIX = ',"label":"'
_DD_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
_DD_RE = re.compile(r"(\d{4})\s+(\w+)\s+(\d{1,2})")


class TextEmbedder(Protocol):
    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        ...


@dataclass(frozen=True, slots=True)
class PageText:
    page_index: int
    text: str


@dataclass(frozen=True, slots=True)
class SearchIngestStats:
    documents_seen: int = 0
    solr_documents: int = 0
    qdrant_points: int = 0
    skipped_documents: int = 0


@dataclass(frozen=True, slots=True)
class QdrantIngestConfig:
    host: str = "localhost"
    port: int = 6333
    grpc_port: int = 6334
    collection_name: str = "align_embeddings"
    vector_size: int = 768
    batch_size: int = 64
    use_grpc: bool = False


class SentenceTransformerEmbedder:
    def __init__(
        self,
        model_name: str = "sentence-transformers/all-mpnet-base-v2",
        *,
        batch_size: int = 32,
    ) -> None:
        self.model_name = model_name
        self.batch_size = int(batch_size)
        self._model = None

    @property
    def model(self) -> Any:
        if self._model is None:
            try:
                from pipeline.database_env import load_pipeline_dotenv
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise RuntimeError(
                    "Missing dependency 'sentence-transformers'. "
                    "Install requirements.txt before Qdrant ingest."
                ) from exc
            load_pipeline_dotenv()
            self._model = SentenceTransformer(self.model_name)
        return self._model

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = self.model.encode(
            texts,
            batch_size=self.batch_size,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return vectors.tolist()


class FixedVectorEmbedder:
    """Deterministic embedder for tests and dry-run shape checks."""

    def __init__(self, dimension: int = 768) -> None:
        self.dimension = int(dimension)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * self.dimension for _ in texts]


def iter_doc_dirs(docs_root: Path, limit: int | None = None) -> list[Path]:
    if limit is None:
        return sorted(_iter_ingestable_doc_dirs(docs_root))

    max_count = max(0, int(limit))
    if max_count == 0:
        return []

    return heapq.nsmallest(max_count, _iter_ingestable_doc_dirs(docs_root))


def _iter_doc_dirs_for_ingest(docs_root: Path, limit: int | None = None) -> Iterable[Path]:
    if limit is None:
        return _iter_ingestable_doc_dirs(docs_root)
    return iter(iter_doc_dirs(docs_root, limit))


def _iter_ingestable_doc_dirs(docs_root: Path) -> Iterable[Path]:
    with os.scandir(docs_root) as entries:
        for entry in entries:
            if _is_ingestable_doc_entry(entry):
                yield Path(entry.path)


def _is_ingestable_doc_entry(entry) -> bool:
    try:
        return entry.is_dir() and os.path.isfile(os.path.join(entry.path, f"{entry.name}.txt"))
    except OSError:
        return False


def load_docs_summary_metadata(docs_root: Path) -> dict[str, dict[str, Any]]:
    summary_path = docs_root / "summary.json"
    try:
        fh = summary_path.open("r", encoding="utf-8")
    except OSError:
        metadata: dict[str, dict[str, Any]] = {}
    else:
        try:
            with fh:
                documents = _iter_summary_documents(fh)
                metadata = {}
                for item in documents:
                    if not isinstance(item, dict):
                        continue
                    doc_id = _trimmed_text(str(item.get("doc_id") or ""))
                    if doc_id:
                        metadata[doc_id] = item
        except json.JSONDecodeError:
            metadata = {}

    if not metadata:
        metadata = {doc_dir.name: {} for doc_dir in _iter_ingestable_doc_dirs(docs_root)}
    _merge_catalog_metadata(metadata)
    return metadata


def _merge_catalog_metadata(metadata: dict[str, dict[str, Any]]) -> None:
    doc_ids = sorted(metadata)
    if not doc_ids:
        return
    try:
        from .db_connect import get_postgres_client

        client = get_postgres_client()
    except Exception as exc:
        LOGGER.warning("Could not initialize PostgreSQL client for catalog lookup: %s", exc)
        return

    try:
        rows = client.fetch_all(
            f"""
            SELECT id, collection_name, dd
            FROM {CATALOG_TABLE}
            WHERE id = ANY(%s)
            """,
            (doc_ids,),
        )
    except Exception as exc:
        LOGGER.warning("Could not load catalog dates from %s: %s", CATALOG_TABLE, exc)
        return
    finally:
        client.close()

    for row in rows:
        doc_id = _trimmed_text(str(row.get("id") or ""))
        if not doc_id or doc_id not in metadata:
            continue
        item = metadata[doc_id]
        collection_name = _trimmed_text(str(row.get("collection_name") or ""))
        if collection_name and not item.get("collection_name"):
            item["collection_name"] = collection_name
        document_date = _pick_newest_dd(row.get("dd"))
        if document_date:
            item["date"] = document_date
            item["document_date"] = document_date


def _parse_dd_date(value: Any) -> str:
    text = _trimmed_text(value if isinstance(value, str) else str(value or ""))
    match = _DD_RE.match(text)
    if not match:
        return ""
    year, month_text, day = match.group(1), match.group(2).lower(), match.group(3)
    month = _DD_MONTHS.get(month_text)
    if not month:
        return ""
    return f"{year}-{month:02d}-{int(day):02d}"


def _pick_newest_dd(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat") and not isinstance(value, str):
        return str(value.isoformat())[:10]
    text = _trimmed_text(value if isinstance(value, str) else str(value))
    if not text:
        return ""
    parts = [part.strip() for part in text.split(";") if part.strip()]
    dates = [_parse_dd_date(part) for part in parts]
    valid_dates = [date for date in dates if date]
    if not valid_dates:
        return ""
    return max(valid_dates)


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


def _iter_summary_documents(handle: TextIO) -> Iterable[Any]:
    reader = _JsonStreamReader(handle)
    reader.skip_whitespace()
    if reader.read_char() != "{":
        return

    while True:
        reader.skip_whitespace()
        char = reader.peek()
        if not char:
            return
        if char == "}":
            reader.read_char()
            return

        key = reader.decode_value()
        reader.skip_whitespace()
        if reader.read_char() != ":":
            raise json.JSONDecodeError("Expected ':' after object key", "", 0)

        if key == "documents":
            yield from _iter_json_array_values(reader)
            return

        _skip_json_value(reader)
        reader.skip_whitespace()
        char = reader.peek()
        if char == ",":
            reader.read_char()
            continue
        if char == "}":
            reader.read_char()
            return
        if not char:
            return
        raise json.JSONDecodeError("Expected ',' or '}' after object value", "", 0)


def _iter_json_array_values(reader: _JsonStreamReader) -> Iterable[Any]:
    reader.skip_whitespace()
    if reader.read_char() != "[":
        return

    while True:
        reader.skip_whitespace()
        char = reader.peek()
        if not char:
            return
        if char == "]":
            reader.read_char()
            return
        yield reader.decode_value()
        reader.skip_whitespace()
        char = reader.peek()
        if char == ",":
            reader.read_char()
            continue
        if char == "]":
            reader.read_char()
            return
        raise json.JSONDecodeError("Expected ',' or ']' after array value", "", 0)


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


def parse_page_texts(raw_text: str) -> list[PageText]:
    return list(iter_page_texts(raw_text))


def iter_page_texts(raw_text: str) -> Iterable[PageText]:
    source_text = raw_text or ""
    previous_match: re.Match[str] | None = None
    for match in PAGE_MARKER_RE.finditer(source_text):
        if previous_match is not None:
            page_index = int(previous_match.group(1))
            start, end = _trimmed_slice_bounds(source_text, previous_match.end(), match.start())
            if start < end:
                yield PageText(page_index=page_index, text=source_text[start:end])
        previous_match = match

    if previous_match is None:
        start, end = _trimmed_slice_bounds(source_text, 0, len(source_text))
        if start < end:
            yield PageText(page_index=1, text=source_text[start:end])
        return

    page_index = int(previous_match.group(1))
    start, end = _trimmed_slice_bounds(source_text, previous_match.end(), len(source_text))
    if start < end:
        yield PageText(page_index=page_index, text=source_text[start:end])


def _trimmed_slice_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def iter_page_texts_from_file(text_path: Path) -> Iterable[PageText]:
    current_page_index: int | None = None
    current_text = io.StringIO()
    current_has_text = False
    pending_whitespace_parts: list[str] = []
    saw_marker = False

    def reset_page_buffer() -> None:
        nonlocal current_has_text
        current_text.seek(0)
        current_text.truncate(0)
        current_has_text = False
        pending_whitespace_parts.clear()

    def append_page_line(line: str) -> None:
        nonlocal current_has_text
        start, end = _trimmed_slice_bounds(line, 0, len(line))
        if start >= end:
            if current_has_text:
                pending_whitespace_parts.append(line)
            return
        if current_has_text:
            if pending_whitespace_parts:
                current_text.write("".join(pending_whitespace_parts))
                pending_whitespace_parts.clear()
            start = 0
        else:
            current_has_text = True
        current_text.write(line[start:end])
        if end < len(line):
            pending_whitespace_parts.append(line[end:])

    with text_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            match = _match_page_marker_line(line)
            if match:
                if current_page_index is not None and current_has_text:
                    yield PageText(page_index=current_page_index, text=current_text.getvalue())
                current_page_index = int(match.group(1))
                reset_page_buffer()
                saw_marker = True
                continue
            if current_page_index is not None or not saw_marker:
                append_page_line(line)

    if current_page_index is not None:
        if current_has_text:
            yield PageText(page_index=current_page_index, text=current_text.getvalue())
        return

    if not saw_marker and current_has_text:
        yield PageText(page_index=1, text=current_text.getvalue())


def _trimmed_text(text: str) -> str:
    start, end = _trimmed_slice_bounds(text, 0, len(text))
    if start >= end:
        return ""
    if start == 0 and end == len(text):
        return text
    return text[start:end]


def _has_non_whitespace(text: str) -> bool:
    for char in text:
        if not char.isspace():
            return True
    return False


def _jsonl_line_has_payload(line: str) -> bool:
    return bool(line) and (line[0] == "{" or _has_non_whitespace(line))


def build_qdrant_page_points(
    *,
    doc_dir: Path,
    metadata: dict[str, Any],
    embedder: TextEmbedder,
    embedding_model: str,
    expected_dimension: int = 768,
    page_batch_size: int = 64,
) -> list[dict[str, Any]]:
    return list(
        iter_qdrant_page_points(
            doc_dir=doc_dir,
            metadata=metadata,
            embedder=embedder,
            embedding_model=embedding_model,
            expected_dimension=expected_dimension,
            page_batch_size=page_batch_size,
        )
    )


def iter_qdrant_page_points(
    *,
    doc_dir: Path,
    metadata: dict[str, Any],
    embedder: TextEmbedder,
    embedding_model: str,
    expected_dimension: int = 768,
    page_batch_size: int = 64,
) -> Iterable[dict[str, Any]]:
    doc_id = doc_dir.name
    text_path = doc_dir / f"{doc_id}.txt"

    page_texts = iter_page_texts_from_file(text_path)
    labels = _load_page_label_map(doc_dir / "labels.jsonl")
    fallback_family: str | None = None
    artifact_id = sha_id("doc_strong", doc_id)
    collection = str(metadata.get("collection_name") or "")
    document_name = str(metadata.get("document_name") or f"{doc_id}.pdf")
    pdf_hash = _load_pdf_hash_if_exists(doc_dir / "stage2_manifest.json")
    page_batch_size = max(1, int(page_batch_size))

    for page_batch in _batched(page_texts, page_batch_size):
        vectors = embedder.embed_batch([page.text for page in page_batch])
        for page, vector in zip(page_batch, vectors):
            if len(vector) != expected_dimension:
                raise ValueError(
                    f"Embedding vector for {doc_id} page {page.page_index} has "
                    f"dimension {len(vector)}; expected {expected_dimension}."
                )
            family = labels.get(page.page_index)
            if not family:
                if fallback_family is None:
                    fallback_family = _dominant_label(labels.values())
                family = fallback_family
            point_id = str(uuid.uuid5(QDRANT_PAGE_NAMESPACE, f"{doc_id}:{page.page_index}"))
            payload: dict[str, Any] = {
                "artifact_id": artifact_id,
                "document_id": doc_id,
                "document_name": document_name,
                "family": family.upper(),
                "page_index": page.page_index,
                "text": page.text,
                "text_chars": len(page.text),
                "embedding_model": embedding_model,
                "granularity": "page",
            }
            if collection:
                payload["collection"] = collection
            if pdf_hash:
                payload["pdf_hash"] = pdf_hash
            yield {
                "id": point_id,
                "vector": vector,
                "payload": payload,
            }


def ingest_docs_to_solr(
    *,
    docs_root: Path,
    metadata_by_doc: dict[str, dict[str, Any]],
    limit: int | None = None,
    dry_run: bool = False,
    client: SolrClient | None = None,
    batch_size: int = 100,
    batch_body_chars: int = 5_000_000,
) -> SearchIngestStats:
    doc_dirs = _iter_doc_dirs_for_ingest(docs_root, limit)
    documents_seen = 0
    if dry_run:
        for _ in doc_dirs:
            documents_seen += 1
        return SearchIngestStats(
            documents_seen=documents_seen,
            solr_documents=documents_seen,
        )

    solr = client or SolrClient.from_env()
    solr_batch_size = max(1, int(batch_size))
    solr_batch_body_chars = max(1, int(batch_body_chars))
    batch: list[dict[str, Any]] = []
    batch_chars = 0
    indexed = 0
    skipped = 0
    for doc_dir in doc_dirs:
        documents_seen += 1
        meta = metadata_by_doc.get(doc_dir.name, {})
        try:
            doc = build_raw_text_solr_document(
                doc_dir=doc_dir,
                collection_name=str(meta.get("collection_name") or ""),
                document_name=str(meta.get("document_name") or ""),
                document_date=str(meta.get("document_date") or meta.get("date") or ""),
            )
        except FileNotFoundError:
            skipped += 1
            continue
        doc_body_chars = _solr_doc_body_chars(doc)
        if batch and batch_chars + doc_body_chars > solr_batch_body_chars:
            solr.add_documents(batch, commit=False)
            batch.clear()
            batch_chars = 0
        batch.append(doc)
        batch_chars += doc_body_chars
        indexed += 1
        if len(batch) >= solr_batch_size or batch_chars >= solr_batch_body_chars:
            solr.add_documents(batch, commit=False)
            batch.clear()
            batch_chars = 0
    if batch:
        solr.add_documents(batch, commit=False)
    if indexed:
        solr.commit()
    return SearchIngestStats(
        documents_seen=documents_seen,
        solr_documents=indexed,
        skipped_documents=skipped,
    )


def _solr_doc_body_chars(doc: dict[str, Any]) -> int:
    body = doc.get("body")
    if body is None:
        return 0
    return len(body) if isinstance(body, str) else len(str(body))


def ingest_docs_to_qdrant(
    *,
    docs_root: Path,
    metadata_by_doc: dict[str, dict[str, Any]],
    embedder: TextEmbedder,
    embedding_model: str,
    config: QdrantIngestConfig = QdrantIngestConfig(),
    limit: int | None = None,
    recreate: bool = False,
    dry_run: bool = False,
) -> SearchIngestStats:
    doc_dirs = _iter_doc_dirs_for_ingest(docs_root, limit)
    client = None if dry_run else create_qdrant_client(config)
    if client is not None:
        ensure_qdrant_collection(client, config, recreate=recreate)

    upsert_batch_size = max(1, int(config.batch_size))
    documents_seen = 0
    total_points = 0
    skipped = 0
    batch: list[dict[str, Any]] = []
    for doc_dir in doc_dirs:
        documents_seen += 1
        try:
            if dry_run:
                total_points += _count_page_texts(doc_dir / f"{doc_dir.name}.txt")
                continue
            point_iter = iter_qdrant_page_points(
                doc_dir=doc_dir,
                metadata=metadata_by_doc.get(doc_dir.name, {}),
                embedder=embedder,
                embedding_model=embedding_model,
                expected_dimension=config.vector_size,
                page_batch_size=upsert_batch_size,
            )
            for point in point_iter:
                total_points += 1
                batch.append(point)
                if len(batch) >= upsert_batch_size:
                    _upsert_qdrant_points(client, config.collection_name, batch)
                    batch.clear()
        except FileNotFoundError:
            skipped += 1
            continue

    if client is not None and batch:
        _upsert_qdrant_points(client, config.collection_name, batch)

    return SearchIngestStats(
        documents_seen=documents_seen,
        qdrant_points=total_points,
        skipped_documents=skipped,
    )


def _count_page_texts(text_path: Path) -> int:
    count = 0
    current_page_has_text = False
    saw_marker = False

    try:
        fh = text_path.open("r", encoding="utf-8")
    except OSError as exc:
        raise FileNotFoundError(f"Raw OCR text file not found: {text_path}") from exc
    with fh:
        for line in fh:
            if _match_page_marker_line(line):
                if saw_marker and current_page_has_text:
                    count += 1
                saw_marker = True
                current_page_has_text = False
                continue
            if not current_page_has_text and _has_non_whitespace(line):
                current_page_has_text = True

    if saw_marker:
        return count + int(current_page_has_text)
    return int(current_page_has_text)


def _match_page_marker_line(line: str) -> re.Match[str] | None:
    if not _starts_with_marker_equals(line) or not _contains_page_word(line):
        return None
    return PAGE_MARKER_RE.match(line)


def _starts_with_marker_equals(text: str) -> bool:
    for char in text:
        if char.isspace():
            continue
        return char == "="
    return False


def _contains_page_word(text: str) -> bool:
    index = 0
    limit = len(text) - 3
    while index < limit:
        char = text[index]
        if char == "P" or char == "p":
            second = text[index + 1]
            third = text[index + 2]
            fourth = text[index + 3]
            if (
                (second == "A" or second == "a")
                and (third == "G" or third == "g")
                and (fourth == "E" or fourth == "e")
            ):
                return True
        index += 1
    return False


def create_qdrant_client(config: QdrantIngestConfig) -> Any:
    try:
        from qdrant_client import QdrantClient
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency 'qdrant-client'. Install requirements.txt before Qdrant ingest."
        ) from exc
    return QdrantClient(
        host=config.host,
        port=config.port,
        grpc_port=config.grpc_port,
        prefer_grpc=config.use_grpc,
    )


def ensure_qdrant_collection(
    client: Any,
    config: QdrantIngestConfig,
    *,
    recreate: bool = False,
) -> None:
    try:
        from qdrant_client.models import Distance, VectorParams
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency 'qdrant-client'. Install requirements.txt before Qdrant ingest."
        ) from exc

    exists = _qdrant_collection_exists(client, config.collection_name)
    params = VectorParams(size=config.vector_size, distance=Distance.COSINE)
    if recreate and exists:
        client.recreate_collection(
            collection_name=config.collection_name,
            vectors_config=params,
        )
        return
    if not exists:
        client.create_collection(
            collection_name=config.collection_name,
            vectors_config=params,
        )


def _upsert_qdrant_points(
    client: Any,
    collection_name: str,
    points: list[Any],
) -> None:
    from qdrant_client.models import PointStruct

    for index, point in enumerate(points):
        points[index] = PointStruct(
            id=point["id"],
            vector=point["vector"],
            payload=point["payload"],
        )

    client.upsert(
        collection_name=collection_name,
        points=points,
        wait=True,
    )


def _qdrant_collection_exists(client: Any, collection_name: str) -> bool:
    if hasattr(client, "collection_exists"):
        return bool(client.collection_exists(collection_name=collection_name))
    try:
        client.get_collection(collection_name=collection_name)
        return True
    except Exception:
        return False


def _batched(items: Iterable[PageText], batch_size: int) -> Iterable[list[PageText]]:
    batch: list[PageText] = []
    for item in items:
        batch.append(item)
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def _load_page_label_map(labels_path: Path) -> dict[int, str]:
    labels: dict[int, str] = {}
    try:
        fh = labels_path.open("r", encoding="utf-8")
    except OSError:
        return {}
    with fh:
        for line in fh:
            if not _jsonl_line_has_payload(line):
                continue
            parsed_label_row = _parse_generated_page_label_row(line)
            if parsed_label_row is not None:
                page_index, label = parsed_label_row
                labels[page_index] = label
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                page_index = int(payload.get("page_index"))
            except (TypeError, ValueError):
                continue
            raw_label = payload.get("label") or ""
            label = _trimmed_text(raw_label if isinstance(raw_label, str) else str(raw_label))
            if label:
                labels[page_index] = label
    return labels


def _parse_generated_page_label_row(line: str) -> tuple[int, str] | None:
    if not line.startswith(_GENERATED_LABEL_ROW_PREFIX):
        return None
    page_index_field = _read_json_int_value(line, len(_GENERATED_LABEL_ROW_PREFIX))
    if page_index_field is None:
        return None
    page_index, index = page_index_field
    if not line.startswith(_GENERATED_LABEL_FIELD_PREFIX, index):
        return None
    label_start = index + len(_GENERATED_LABEL_FIELD_PREFIX)
    label_end = line.find('"', label_start)
    if label_end < 0:
        return None
    label = line[label_start:label_end]
    if "\\" in label:
        return None
    label = _trimmed_text(label)
    if not label:
        return None
    return page_index, label


def _read_json_int_value(line: str, index: int) -> tuple[int, int] | None:
    negative = False
    if index < len(line) and line[index] == "-":
        negative = True
        index += 1
    value = 0
    digit_count = 0
    while index < len(line):
        char = line[index]
        if not ("0" <= char <= "9"):
            break
        value = (value * 10) + (ord(char) - 48)
        index += 1
        digit_count += 1
    if not digit_count:
        return None
    return (-value if negative else value), index


def _dominant_label(labels: Iterable[str]) -> str:
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


def _load_json_if_exists(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_pdf_hash_if_exists(path: Path) -> str:
    value = _load_top_level_json_field_if_exists(path, "pdf_hash")
    return str(value or "")


def _load_top_level_json_field_if_exists(path: Path, field: str) -> Any | None:
    try:
        fh = path.open("r", encoding="utf-8")
    except OSError:
        return None
    try:
        with fh:
            return _read_top_level_json_field(fh, field)
    except json.JSONDecodeError:
        return None


def _read_top_level_json_field(handle: TextIO, field: str) -> Any | None:
    reader = _JsonStreamReader(handle)
    reader.skip_whitespace()
    if reader.read_char() != "{":
        return None

    while True:
        reader.skip_whitespace()
        char = reader.peek()
        if not char:
            return None
        if char == "}":
            reader.read_char()
            return None

        key = reader.decode_value()
        reader.skip_whitespace()
        if reader.read_char() != ":":
            raise json.JSONDecodeError("Expected ':' after object key", "", 0)

        if key == field:
            return reader.decode_value()

        _skip_json_value(reader)
        reader.skip_whitespace()
        char = reader.peek()
        if char == ",":
            reader.read_char()
            continue
        if char == "}":
            reader.read_char()
            return None
        if not char:
            return None
        raise json.JSONDecodeError("Expected ',' or '}' after object value", "", 0)
