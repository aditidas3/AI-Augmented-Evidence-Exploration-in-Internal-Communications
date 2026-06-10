from __future__ import annotations

import os
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(ENV_PATH, override=False)


@dataclass(frozen=True, slots=True)
class SolrConnectionConfig:
    url: str
    port: str
    core_name: str
    timeout: int = 30

    @classmethod
    def from_env(cls) -> "SolrConnectionConfig":
        url = os.getenv("SOLR_URL", "http://localhost").strip()
        port = os.getenv("SOLR_PORT", "8983").strip()
        core_name = os.getenv("SOLR_CORE_NAME", "").strip()
        timeout = int(os.getenv("SOLR_TIMEOUT", "30"))
        if not core_name:
            raise RuntimeError("Missing SOLR_CORE_NAME in root .env.")
        return cls(url=url, port=port, core_name=core_name, timeout=timeout)

    @property
    def core_url(self) -> str:
        return build_solr_core_url(self.url, self.port, self.core_name)


def build_solr_core_url(raw_url: str, port: str, core_name: str) -> str:
    if not raw_url:
        raw_url = "http://localhost"
    if "://" not in raw_url:
        raw_url = f"http://{raw_url}"

    parsed = urlsplit(raw_url)
    netloc = parsed.netloc
    host_port = netloc.rsplit("@", 1)[-1]
    if port and ":" not in host_port:
        netloc = f"{netloc}:{port}"

    path_body = _compact_url_path(parsed.path)
    if not path_body:
        path_body = f"solr/{core_name}"
    else:
        last_segment, last_start = _last_path_segment(path_body)
        previous_segment = ""
        if last_start > 0:
            previous_segment, _ = _last_path_segment(path_body, last_start - 1)

        if last_segment == "solr":
            if last_segment != core_name or previous_segment != "solr":
                path_body = f"{path_body}/{core_name}"
        elif last_segment != core_name:
            path_body = f"{path_body}/solr/{core_name}"

    path = "/" + path_body
    return urlunsplit((parsed.scheme, netloc, path.rstrip("/"), "", ""))


def _compact_url_path(path: str) -> str:
    length = len(path)
    start = 0
    while start < length and path[start] == "/":
        start += 1
    if start >= length:
        return ""

    end = length
    while end > start and path[end - 1] == "/":
        end -= 1

    if path.find("//", start, end) < 0:
        return path[start:end]

    buffer = StringIO()
    wrote_segment = False
    index = start
    while index < end:
        while index < end and path[index] == "/":
            index += 1
        segment_start = index
        while index < end and path[index] != "/":
            index += 1
        if segment_start == index:
            continue
        if wrote_segment:
            buffer.write("/")
        buffer.write(path[segment_start:index])
        wrote_segment = True
    return buffer.getvalue()


def _last_path_segment(path: str, end: int | None = None) -> tuple[str, int]:
    if end is None:
        end = len(path)
    slash_index = path.rfind("/", 0, end)
    start = slash_index + 1
    return path[start:end], start


class SolrClient:
    def __init__(self, config: SolrConnectionConfig | None = None, solr: Any | None = None) -> None:
        self.config = config or SolrConnectionConfig.from_env()
        self._solr = solr or _create_pysolr_client(self.config)

    @classmethod
    def from_env(cls) -> "SolrClient":
        return cls(SolrConnectionConfig.from_env())

    def replace_document(self, doc: dict[str, Any], *, commit: bool = True) -> dict[str, Any]:
        if not doc.get("id"):
            raise ValueError("Solr document requires an id field.")
        return self.add_documents([doc], commit=commit)

    def add_documents(self, docs: list[dict[str, Any]], *, commit: bool = True) -> dict[str, Any]:
        if not docs:
            return {"responseHeader": {"status": 0}, "adds": 0}
        response = self._solr.add(docs, commit=commit)
        return response or {}

    def commit(self) -> dict[str, Any]:
        response = self._solr.commit()
        return response or {}


def _create_pysolr_client(config: SolrConnectionConfig) -> Any:
    try:
        import pysolr
    except ImportError as exc:
        raise RuntimeError("Missing dependency 'pysolr'. Install requirements.txt before Solr ingest.") from exc
    return pysolr.Solr(config.core_url, timeout=config.timeout, always_commit=False)
