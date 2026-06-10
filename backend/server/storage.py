from __future__ import annotations

import json
import math
import os
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
SERVER_DIR = ROOT / 'server'
FRONTEND_DIR = REPO_ROOT / 'frontend'
DATA_DIR = SERVER_DIR / 'data'
CONFIG_DIR = SERVER_DIR / 'config'
RUNS_DIR = SERVER_DIR / 'runs'
SCOPES_DIR = SERVER_DIR / 'scopes'
COLLECTIONS_PATH = DATA_DIR / 'collections.json'
SCOPES_PATH = DATA_DIR / 'scopes.json'
RUNS_PATH = DATA_DIR / 'runs.json'
WORKSPACES_PATH = DATA_DIR / 'workspaces.json'
QUESTIONS_PATH = DATA_DIR / 'questions.json'
COMPARISONS_PATH = DATA_DIR / 'comparisons.json'
COMPOSITES_PATH = DATA_DIR / 'composites.json'
CONFLICT_REVIEWS_PATH = DATA_DIR / 'conflict_reviews.json'
ANALYST_REVIEWS_PATH = DATA_DIR / 'analyst_reviews.json'
COMMON_FILTERS_PATH = CONFIG_DIR / 'common-filters.json'
APPLICATIONS_DIR = CONFIG_DIR / 'applications'
COLLECTION_CONFIG_DIR = CONFIG_DIR / 'collections'
DEFAULT_WORKSPACE_ID = 'workspace-default'
_JSON_LOCKS: dict[str, threading.RLock] = {}
_JSON_LOCKS_GUARD = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _JSON_LOCKS_GUARD:
        lock = _JSON_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _JSON_LOCKS[key] = lock
        return lock


def load_json(path: Path):
    if not path.exists():
        return {}
    with _json_lock(path):
        return json.loads(path.read_text(encoding='utf-8-sig'))


def save_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    with _json_lock(path):
        try:
            body = json.dumps(payload, indent=2)
            with tempfile.NamedTemporaryFile(
                'w',
                delete=False,
                dir=path.parent,
                prefix=f'.{path.name}.',
                suffix='.tmp',
                encoding='utf-8',
            ) as handle:
                tmp_path = Path(handle.name)
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
            tmp_path = None
        finally:
            if tmp_path and tmp_path.exists():
                tmp_path.unlink()


def resolve_storage_path(ref: str | Path) -> Path:
    """Resolve API/file refs used by run indexes into server-owned storage."""
    if isinstance(ref, Path):
        return ref

    normalized = str(ref).replace('\\', '/').lstrip('/')
    if normalized.startswith('runs/'):
        return RUNS_DIR / normalized[len('runs/'):]
    if normalized.startswith('server/runs/'):
        return ROOT / normalized
    return ROOT / normalized


def remove_child_dir(base_dir: Path, child_name: str) -> bool:
    base = base_dir.resolve()
    candidate = (base / child_name).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        raise ValueError(f'Path is outside storage directory: {child_name}')
    if candidate == base:
        raise ValueError('Refusing to delete storage root')
    if not candidate.exists():
        return False
    if not candidate.is_dir():
        return False
    shutil.rmtree(candidate)
    return True


def sanitize_for_strict_json(value):
    """Return a browser-JSON-safe copy of a payload."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: sanitize_for_strict_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_for_strict_json(item) for item in value]
    return value
