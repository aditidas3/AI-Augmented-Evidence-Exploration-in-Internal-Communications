"""
trace/config.py — Configuration, constants, and lookup tables for TRACE.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Dict


# Namespace UUID for deterministic ID generation (Fix 4.5)
TRACE_NS = uuid.UUID("b7e15163-2fe8-4c0e-8a3b-dfc11c7eacf0")


@dataclass
class TraceConfig:
    """
    All tuneable knobs for a TRACE execution.

    Attributes whose names start with ``tau_`` are threshold gates;
    everything else is a structural or policy parameter.
    """

    # ── Schema constants ────────────────────────────────────
    schema_version: str = "1.0.0"
    graph_version: str = "1.0.0"

    # ── Agent identity ──────────────────────────────────────
    trace_agent_uid: str = "agent::trace::mapper"
    trace_agent_name: str = "TRACE AlignBundle Mapper"

    # ── Q3 policy ───────────────────────────────────────────
    #    "skip"               → no Claim for AMBIGUOUS / 0.0 slots
    #    "create_unsupported" → Claim with status = 'undetermined'
    ambiguous_slot_policy: str = "skip"

    # ── Decision 2 toggle ───────────────────────────────────
    create_identical_to_edges: bool = True

    # ── Fix 4.8: Q4 entity co-reference toggle ─────────────
    create_coref_edges: bool = True

    # ── Structure-preservation toggle ───────────────────────
    #    When True, TRACE keeps the historical graph shape and
    #    does not emit additional claim-to-claim or claim-to-
    #    frame/provenance support edges introduced by later
    #    enrichment passes. Claim content/metadata can still
    #    become richer without changing the graph skeleton.
    preserve_graph_structure: bool = True

    # ── Thresholds ──────────────────────────────────────────
    tau_mention_confidence: float = 0.0   # min mention confidence
    tau_witness_score: float = 0.0        # min raw witness score

    # ── Fix 4.7: safety cap per collection ──────────────────
    max_identical_to_per_collection: int = 500

    # ── MAP-TRANSFORM classifier backend ───────────────────
    # The live TRACE runner passes this config into bundle_builder.
    # Unit tests that call build_trace_bundle() without a TraceConfig
    # continue to use the deterministic heuristic backend.
    map_transform_enabled: bool = True 
    map_transform_classifier_backend: str = "deepseek"  # deepseek | heuristic
    map_transform_deepseek_model: str = "deepseek-v4-pro"
    map_transform_deepseek_base_url: str = "https://api.deepseek.com"
    map_transform_deepseek_api_key_env: str = "DEEPSEEK_API_KEY"
    map_transform_deepseek_reasoning_effort: str = "max"
    map_transform_deepseek_thinking_enabled: bool = True
    map_transform_deepseek_max_tokens: int | None = None
    map_transform_llm_batch_size: int = 8
    map_transform_llm_concurrency: int = 4
    map_transform_llm_max_retries: int = 2
    map_transform_llm_retry_base_delay_seconds: float = 1.0


# ── Fix 4.6 + Fix 5.3: expanded label → action keyword table ─
# Schema-legal action values (base + S1 amendment):
#   created  collected  transferred  verified  modified
#   reviewed  redacted  archived  restored
#   linked  retracted  superseded
#
# Fix 5.3: "prescribed" and "administered" remapped away from
# "created" — prescribing is ordering (transferred), administering
# is delivering (transferred).  This also prevents violations of
# the created_event_is_first constraint.
ACTION_KEYWORDS: Dict[str, str] = {
    # collected
    "identified": "collected",  "documented": "collected",
    "surfaced":   "collected",  "extracted":  "collected",
    "detected":   "collected",  "found":      "collected",
    "discovered": "collected",  "observed":   "collected",
    "reported":   "collected",  "noted":      "collected",
    "recorded":   "collected",  "gathered":   "collected",
    # linked
    "bound":      "linked",     "linked":     "linked",
    "associated": "linked",     "connected":  "linked",
    "mapped":     "linked",     "matched":    "linked",
    "resolved":   "linked",     "correlated": "linked",
    # verified
    "verified":     "verified", "confirmed":    "verified",
    "validated":    "verified", "corroborated": "verified",
    # reviewed
    "reviewed":  "reviewed",    "examined":  "reviewed",
    "assessed":  "reviewed",    "evaluated": "reviewed",
    "analyzed":  "reviewed",    "analysed":  "reviewed",
    "inspected": "reviewed",
    # created
    "created":      "created",  "generated":    "created",
    "produced":     "created",
    "initiated":    "created",  "authored":     "created",
    # Fix 5.3: moved out of "created"
    "prescribed":   "transferred",
    "administered": "transferred",
    # transferred
    "transferred": "transferred", "moved":     "transferred",
    "sent":        "transferred", "submitted": "transferred",
    "delivered":   "transferred", "forwarded": "transferred",
    # modified
    "modified": "modified", "updated": "modified",
    "changed":  "modified", "amended": "modified",
    "revised":  "modified", "edited":  "modified",
    # archived
    "filed":      "archived", "archived":   "archived",
    "stored":     "archived", "catalogued": "archived",
    # redacted
    "redacted": "redacted", "removed":  "redacted",
    "censored": "redacted", "obscured": "redacted",
    # restored
    "restored":   "restored", "recovered":   "restored",
    "reinstated": "restored",
    # retracted
    "retracted": "retracted", "withdrawn": "retracted",
    "revoked":   "retracted",
    # superseded
    "superseded":  "superseded", "replaced":   "superseded",
    "overridden":  "superseded", "deprecated": "superseded",
}
