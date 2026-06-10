from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


REPO_ROOT = Path(__file__).resolve().parents[1]
_DOTENV_LOADED = False


def load_pipeline_dotenv() -> None:
    """Load the root .env file without overriding process env."""
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    _DOTENV_LOADED = True

    env_path = REPO_ROOT / ".env"
    try:
        from dotenv import load_dotenv
    except ImportError:
        _load_simple_dotenv(env_path)
        _normalize_huggingface_env()
        return

    load_dotenv(env_path, override=False)
    _normalize_huggingface_env()


def env_first(*names: str) -> str | None:
    load_pipeline_dotenv()
    for name in names:
        value = os.getenv(name)
        if value:
            return value.strip()
    return None


def openai_api_key_from_env(base_url: str | None = None) -> str | None:
    """Return the API key that matches the configured OpenAI-compatible host."""
    load_pipeline_dotenv()
    resolved_base_url = (base_url or os.getenv("OPENAI_BASE_URL", "")).strip()
    host = urlparse(resolved_base_url).netloc.lower()
    if host.endswith("api.deepseek.com"):
        return env_first("DEEPSEEK_API_KEY", "OPENAI_KEY", "OPENAI_API_KEY")
    if host.endswith("openrouter.ai"):
        return env_first("OPENAI_API_KEY", "OPENAI_KEY", "DEEPSEEK_API_KEY")
    return env_first("OPENAI_API_KEY", "OPENAI_KEY", "DEEPSEEK_API_KEY")


def neo4j_config_from_env(config_type: Any, base: Any | None = None) -> Any:
    load_pipeline_dotenv()
    base = base or config_type()
    return config_type(
        uri=env_first("EVIDENCE_EXPLORER_NEO4J_URI", "NEO4J_URI") or base.uri,
        user=env_first("EVIDENCE_EXPLORER_NEO4J_USER", "NEO4J_USER") or base.user,
        password=env_first("EVIDENCE_EXPLORER_NEO4J_PASSWORD", "NEO4J_PASSWORD") or base.password,
        database=env_first("EVIDENCE_EXPLORER_NEO4J_DATABASE", "NEO4J_DATABASE") or base.database,
        max_connection_pool_size=base.max_connection_pool_size,
    )


def _load_simple_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = _strip_env_quotes(value.strip())


def _strip_env_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _normalize_huggingface_env() -> None:
    """Accept common local spellings while exposing Hugging Face's HF_TOKEN."""
    if os.getenv("HF_TOKEN"):
        return
    for name in ("HF_Token", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACE_TOKEN"):
        value = os.getenv(name)
        if value:
            os.environ["HF_TOKEN"] = value.strip()
            return
