from __future__ import annotations

import hashlib


def sha_id(namespace: str, *parts: str) -> str:
    """Deterministic 12-char hex ID from a namespace and key parts."""
    raw = namespace + "|" + "|".join(str(part) for part in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:12]
