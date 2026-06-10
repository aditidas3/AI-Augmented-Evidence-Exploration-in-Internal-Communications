from __future__ import annotations

from pathlib import Path

TEXT_EXTENSIONS = {
    "",
    ".csv",
    ".htm",
    ".html",
    ".json",
    ".jsonl",
    ".log",
    ".md",
    ".markdown",
    ".ndjson",
    ".rtf",
    ".text",
    ".tsv",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}

BINARY_EXTENSIONS = {
    ".7z",
    ".avif",
    ".bin",
    ".bmp",
    ".db",
    ".dll",
    ".doc",
    ".docx",
    ".exe",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".mp3",
    ".mp4",
    ".parquet",
    ".png",
    ".ppt",
    ".pptx",
    ".rar",
    ".sqlite",
    ".tar",
    ".tif",
    ".tiff",
    ".webp",
    ".xls",
    ".xlsx",
}


def is_pdf_source_path(path: Path) -> bool:
    return _suffix(path) == ".pdf"


def is_zip_source_path(path: Path) -> bool:
    return _suffix(path) == ".zip"


def is_supported_input_filename(name: str) -> bool:
    suffix = _suffix(Path(name))
    if suffix in {".pdf", ".zip"}:
        return True
    return is_supported_text_filename(name)


def is_supported_zip_member_filename(name: str) -> bool:
    suffix = _suffix(Path(name))
    if suffix == ".zip":
        return False
    if suffix == ".pdf":
        return True
    return is_supported_text_filename(name)


def is_supported_text_filename(name: str) -> bool:
    suffix = _suffix(Path(name))
    if suffix in TEXT_EXTENSIONS:
        return True
    return suffix not in BINARY_EXTENSIONS and suffix not in {".pdf", ".zip"}


def read_text_source(path: Path) -> str:
    data = path.read_bytes()
    text = _decode_text_bytes(data, path=path)
    return _normalize_newlines(text)


def _suffix(path: Path) -> str:
    return path.suffix.casefold()


def _decode_text_bytes(data: bytes, *, path: Path) -> str:
    if not data:
        return ""
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16")
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig")
    if b"\x00" in data[:4096]:
        raise ValueError(f"Unsupported binary input file: {path}")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252")


def _normalize_newlines(text: str) -> str:
    if "\r" not in text:
        return text
    return text.replace("\r\n", "\n").replace("\r", "\n")
