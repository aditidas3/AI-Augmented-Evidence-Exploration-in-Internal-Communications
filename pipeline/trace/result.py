"""
trace/result.py — Immutable output container for a TRACE execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class TraceResult:
    """Immutable output of a TRACE execution."""

    eg_root_uid: str
    rg_root_uid: Optional[str]
    evidence_node_uids: List[str]
    claim_uids: List[str]
    maps_to_log: List[Dict[str, Any]]     # audit trail of every mapping
    diagnostics: List[Dict[str, Any]]
    stats: Dict[str, int]
