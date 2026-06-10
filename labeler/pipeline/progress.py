from __future__ import annotations

import csv
import stat
from pathlib import Path
from typing import Iterable

from labeler.pipeline.stage2_outputs import STAGE2_ENTITIES_FILENAME, STAGE2_RELATIONSHIP_FILENAME

PROGRESS_FILENAME = "progress.csv"
FIELDNAMES = [
    "document_name",
    "is_segmented",
    "is_ocred",
    "is_entity_extracted",
    "is_relationship_extracted",
]


def progress_path(output_root: Path) -> Path:
    return output_root / PROGRESS_FILENAME


def _file_complete(path: Path) -> bool:
    try:
        file_stat = path.stat()
        return stat.S_ISREG(file_stat.st_mode) and file_stat.st_size > 0
    except OSError:
        return False


def compute_status(output_root: Path, doc_id: str) -> dict[str, int]:
    doc_dir = output_root / doc_id
    return {
        "is_segmented": int(_file_complete(doc_dir / "labels.json")),
        "is_ocred": int(_file_complete(doc_dir / f"{doc_id}.txt")),
        "is_entity_extracted": int(_file_complete(doc_dir / STAGE2_ENTITIES_FILENAME)),
        "is_relationship_extracted": int(_file_complete(doc_dir / STAGE2_RELATIONSHIP_FILENAME)),
    }


def initialize_progress_csv(output_root: Path, doc_ids: Iterable[str]) -> Path:
    """Create the progress CSV, refreshing existing rows from disk and adding new ones.

    Preserves any existing rows for documents not in doc_ids.
    """
    output_root.mkdir(parents=True, exist_ok=True)
    path = progress_path(output_root)

    rows = _read_progress_rows(path)

    for doc_id in doc_ids:
        status = compute_status(output_root, doc_id)
        rows[doc_id] = {"document_name": doc_id, **status}

    _write_rows(path, rows.values())
    return path


def update_progress_row(output_root: Path, doc_id: str) -> Path:
    """Recompute and write the status row for a single document."""
    return update_progress_rows(output_root, [doc_id])


def update_progress_rows(output_root: Path, doc_ids: Iterable[str]) -> Path:
    """Recompute and write status rows for multiple documents with one CSV read/write."""
    path = progress_path(output_root)
    rows = _read_progress_rows(path)

    for doc_id in doc_ids:
        status = compute_status(output_root, doc_id)
        rows[doc_id] = {"document_name": doc_id, **status}

    _write_rows(path, rows.values())
    return path


def _read_progress_rows(path: Path) -> dict[str, dict[str, object]]:
    rows: dict[str, dict[str, object]] = {}
    try:
        fh = path.open("r", encoding="utf-8", newline="")
    except FileNotFoundError:
        return rows
    with fh:
        reader = csv.DictReader(fh)
        for row in reader:
            name = _trimmed_text(row.get("document_name") or "")
            if name:
                row["document_name"] = name
                rows[name] = row
    return rows


def _write_rows(path: Path, rows: Iterable[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(FIELDNAMES)
        for row in rows:
            writer.writerow(row.get(field, 0) for field in FIELDNAMES)


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
