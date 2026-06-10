from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any


def stable_json(value: Any) -> str:
    return json.dumps(to_stable_value(value), sort_keys=True)


def short_sha256_text(payload: str, *, length: int = 16) -> str:
    return hashlib.sha256(payload.encode()).hexdigest()[:length]


def to_stable_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return to_stable_value(asdict(value))
    if isinstance(value, dict):
        return {
            str(key): to_stable_value(nested)
            for key, nested in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (set, frozenset)):
        normalized_items = [to_stable_value(item) for item in value]
        return sorted(
            normalized_items,
            key=lambda item: json.dumps(item, sort_keys=True),
        )
    if isinstance(value, list):
        return [to_stable_value(item) for item in value]
    if isinstance(value, tuple):
        return [to_stable_value(item) for item in value]
    return value
