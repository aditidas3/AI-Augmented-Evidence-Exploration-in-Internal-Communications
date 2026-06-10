from __future__ import annotations

import base64
import hashlib
import io
from dataclasses import dataclass
from pathlib import Path

_BASE64_READ_CHUNK_SIZE = 768 * 1024


@dataclass(frozen=True, slots=True)
class PageInput:
    page_index: int
    image_path: Path
    image_b64: str
    image_hash: str
    mime_type: str


def file_to_base64(path: Path, *, compute_hash: bool = True) -> tuple[str, str]:
    hasher = hashlib.sha256() if compute_hash else None
    encoded = io.StringIO()
    remainder = b""

    with path.open("rb") as fh:
        while True:
            chunk = fh.read(_BASE64_READ_CHUNK_SIZE)
            if not chunk:
                break
            if hasher is not None:
                hasher.update(chunk)
            if remainder:
                chunk = remainder + chunk
                remainder = b""
            encodable_length = len(chunk) - (len(chunk) % 3)
            if encodable_length:
                if encodable_length == len(chunk):
                    encoded.write(base64.b64encode(chunk).decode("ascii"))
                else:
                    encoded.write(base64.b64encode(chunk[:encodable_length]).decode("ascii"))
                    remainder = chunk[encodable_length:]
            else:
                remainder = chunk

    if remainder:
        encoded.write(base64.b64encode(remainder).decode("ascii"))
    digest = hasher.hexdigest() if hasher is not None else ""
    return encoded.getvalue(), digest


def detect_mime_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    return "image/png"
