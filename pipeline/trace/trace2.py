"""
trace.py — TRACE: AlignBundle → Evidence Graph / Reasoning Graph Mapper

Consumes an AlignBundle (output of the ALIGN operator) and populates
the Evidence Graph (EG) and Reasoning Graph (RG) in Memgraph according
to the EG-RG schema v1.0.0.

Architecture:
    ALIGN ──produces──▶ AlignBundle (JSON) ──consumed by──▶ TRACE ──writes──▶ EG + RG

TRACE is the sole component that holds both the AlignBundle schema
and the EG-RG schema simultaneously.  Every created node and edge
carries a `domainMetadata.mapping_reason` property documenting WHY
it was created (the MAPS_TO contract).

Phase Summary
─────────────
  Phase 0   Preconditions: create EG root, TRACE agent
  Phase 1   Artifact  → EvidenceNode[Artifact]           (Q6: flat, no parent doc)
  Phase 2   Anchor    → EvidenceNode[Document]            (text passages)
  Phase 3   Mention   → EvidenceNode[Observation]         (Q1: mention granularity)
  Phase 3b  Q4 Entity Co-reference                        (CORROBORATES chains)
  Phase 4   Witness   → EvidenceNode[Testimony]           (dedup by content_hash)
  Phase 5   Snapshot  → EvidenceNode[Record, tertiary]    (Q7: KG0 entity reference)
  Phase 6   FrameWit  → EvidenceNode[Derived]             (Q2: chain steps → ProvenanceEvents)
  Phase 7   Slot      → Claim                             (Q3: quality → status)
  Phase 8   Post-proc → OTHER_EVIDENCE_REL {subtype:"IDENTICAL_TO"}
                                                           (Decision 2: same-collection segments)

Decisions Implemented
─────────────────────
  1  Artifact IDs stay as strings — no UUID translation
  2  OTHER_EVIDENCE_REL edges with subtype "IDENTICAL_TO" between
     same-collection artifact segments.  The EG schema defines
     OTHER_EVIDENCE_REL as the catch-all evidence-relation type with
     a `subtype` String property — not a first-class IDENTICAL_TO
     edge type.  See pipeline/trace/EG-RG schemas.md §1.2.2.
  3  NER misclassification handled upstream — not our concern
  4  All mapping_reason annotations written here (MAPS_TO contract)

Fixes Applied (v2)
───────────────────
  4.1  Cross-graph bridge writer for GROUNDED_BY edges
  4.2  Snapshot nodes linked to FrameWitnesses via DERIVES_FROM
  4.3  FrameWitness DERIVES_FROM constituent mentions
  4.4  Witness collection falls back to slot_bindings[*].witnesses[*]
  4.5  Deterministic UIDs (uuid5) for full re-run idempotency
  4.6  Expanded label→action mapping; original_label preserved
  4.7  Chain topology for IDENTICAL-TO and CORROBORATES (O(n))
  4.8  Q4 entity co-reference via CORROBORATES edges

Fixes Applied (v3 — schema-compliance pass)
────────────────────────────────────────────
  5.1  domainMetadata / tags passed as native dict/list (Map / String[])
  5.2  Shadow Claim type "reference" → "other" (claim_type_enum)
  5.3  "prescribed"/"administered" remapped away from "created" action
  5.4  Guard: 'created' action forced to sequenceIndex 0 only
  5.5  Provenance chain timestamps enforced monotonic
  5.6  AUTHORED_BY edge added for RG GraphRoot
  5.7  reliabilityScore / confidenceScore / edge confidence clamped [0,1]
  5.8  evidenceGraphId set on GROUNDED_BY edges
  5.9  Dangling GROUNDED_BY targets guarded
  5.10 MemgraphWriter._flat preserves lists (Memgraph supports String[])
"""

from __future__ import annotations

import uuid
import json
import logging
import re
import hashlib
from datetime import datetime, timezone
from typing import (
    Any, Dict, List, Optional, Set,
)
from collections import defaultdict

try:
    from .config import TraceConfig, TRACE_NS, ACTION_KEYWORDS as _ACTION_KEYWORDS
    from .bundle_builder import build_trace_bundle, validate_trace_bundle
    from .writers import GraphWriter, InMemoryGraphWriter, MemgraphWriter
    from .result import TraceResult
except ImportError:
    from config import TraceConfig, TRACE_NS, ACTION_KEYWORDS as _ACTION_KEYWORDS  # type: ignore[no-redef]
    from bundle_builder import build_trace_bundle, validate_trace_bundle  # type: ignore[no-redef]
    from writers import GraphWriter, InMemoryGraphWriter, MemgraphWriter  # type: ignore[no-redef]
    from result import TraceResult  # type: ignore[no-redef]

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
#  The TRACE algorithm
# ═══════════════════════════════════════════════════════════════


class Trace:
    """
    Stateful, single-use mapper.  Instantiate → ``execute()`` → discard.

    Parameters
    ----------
    eg : GraphWriter
        Writer for the Evidence Graph.
    rg : GraphWriter
        Writer for the Reasoning Graph.
    bridge : GraphWriter, optional
        Writer for cross-graph edges (Fix 4.1).  Defaults to *eg*.
        For single-database deployments (the common case) leave as
        ``None`` — ``eg`` and ``rg`` point at the same Memgraph and
        ``bridge`` inherits ``eg``.
        For split-database deployments, provide a writer that can
        resolve node UIDs from both the EG and the RG.
    cfg : TraceConfig, optional
    """

    # ─────────────────────────────────────────────────────────
    #  Construction
    # ─────────────────────────────────────────────────────────

    def __init__(
        self,
        eg: GraphWriter,
        rg: GraphWriter,
        bridge: GraphWriter | None = None,
        cfg: TraceConfig | None = None,
    ) -> None:
        self.eg = eg
        self.rg = rg
        self.bridge = bridge or eg                     # Fix 4.1
        self.cfg = cfg or TraceConfig()

        # ── Dedup registries ────────────────────────────────
        self._seen_artifacts: Set[str] = set()
        self._seen_anchors: Set[str] = set()
        self._seen_mentions: Set[str] = set()
        self._seen_witness_hashes: Dict[str, str] = {}   # hash → witness_id
        self._seen_snapshots: Set[str] = set()
        self._seen_frame_witnesses: Set[str] = set()

        # ── Fix 4.8: Q4 co-reference registry ──────────────
        #    key = kg0_entity_id or normalized form
        #    value = [mention_uid, ...]
        self._entity_mentions: Dict[str, List[str]] = defaultdict(list)
        self._mention_hypotheses: Dict[str, Dict[str, Any]] = {}
        self._entity_hyp_snapshot_meta: Dict[str, Dict[str, Any]] = {}
        self._suppressed_mentions_by_anchor: Dict[str, List[Dict[str, Any]]] = {}
        self._suppressed_mention_total: int = 0
        self._evidence_nodes: Dict[str, Dict[str, Any]] = {}
        self._reliability_factor_uids: List[str] = []

        # ── Fix 4.2: subgraph → [snapshot node_ids] ────────
        self._subgraph_snapshots: Dict[str, List[str]] = defaultdict(list)

        # ── Collection → [artifact_id] for Decision 2 ──────
        self._coll_arts: Dict[str, List[str]] = defaultdict(list)

        # ── Fix 5.8: EG root UID for GROUNDED_BY edges ─────
        self._eg_root_uid: str = ""

        # ── Accumulators ────────────────────────────────────
        self._ev_uids: List[str] = []
        self._claim_uids: List[str] = []
        self._inference_uids: List[str] = []
        self._defeater_uids: List[str] = []
        self._maps_log: List[Dict[str, Any]] = []
        self._diags: List[Dict[str, Any]] = []
        self._slot_claim_records: List[Dict[str, Any]] = []
        self._claim_relation_count: int = 0
        self._frame_contexts: Dict[str, Dict[str, Any]] = {}
        self._claim_context_link_count: int = 0

    # ─────────────────────────────────────────────────────────
    #  Public entry point
    # ─────────────────────────────────────────────────────────

    def execute(self, bundle: Dict[str, Any]) -> TraceResult:
        """
        Run all TRACE phases on *bundle* (a parsed AlignBundle).

        Parameters
        ----------
        bundle : dict
            Top-level dict with ``corpus_stats`` and ``result`` keys.

        Returns
        -------
        TraceResult
        """
        result = bundle.get("result", {})
        corpus = bundle.get("corpus_stats", {})

        if not result:
            self._diag("ERROR", "TRACE_EMPTY_RESULT",
                       "AlignBundle has no 'result' section.")
            return self._finalise(None, None)

        self._prepare_alignment_indices(result)
        eg_root = self._phase0_preconditions(result, corpus)
        self._phase1_artifacts(result, eg_root)
        self._phase2_anchors(result, eg_root)
        self._phase3_mentions(result, eg_root)
        self._phase3b_entity_coref(eg_root)            # Fix 4.8
        self._phase4_witnesses(result, eg_root)
        self._phase5_snapshots(result, eg_root)
        self._phase6_frame_witnesses(result, eg_root)
        rg_root = self._phase7_claims(result)
        if self.cfg.create_identical_to_edges:
            self._phase8_identical_to()
        self._phase9_schema_completion(eg_root, rg_root)

        return self._finalise(eg_root, rg_root)

    # ═════════════════════════════════════════════════════════
    #  Phase 0 — Preconditions
    # ═════════════════════════════════════════════════════════

    def _phase0_preconditions(
        self, result: Dict, corpus: Dict
    ) -> str:
        """Create EG GraphRoot and the TRACE system-agent node."""

        now = self._now()
        intent_id = result.get("intent_id", "unknown")
        question = result.get("question_text", "")

        # ── Agent ───────────────────────────────────────────
        self.eg.create_node(["Agent"], {
            "uid":  self.cfg.trace_agent_uid,
            "type": "system",
            "name": self.cfg.trace_agent_name,
            "role": "align_bundle_mapper",
        })

        # ── EG root — Fix 4.5: deterministic UID ───────────
        eg_root = self._deterministic_uid(f"eg_root::{intent_id}")
        self.eg.create_node(["GraphRoot"], {
            "uid":           eg_root,
            "graphType":     "EvidenceGraph",
            "version":       self.cfg.graph_version,
            "schemaVersion": self.cfg.schema_version,
            "created":       now,
            "title":         f"EG for {intent_id}",
            "purpose":       question,
            "tags":          ["trace", intent_id],           # Fix 5.1
            "domainMetadata": {
                "artifact_set_size": len(result.get("artifact_set", [])),
                "entity_hypothesis_count": len(result.get("entity_hypotheses", [])),
                "suppressed_mentions": self._suppressed_mention_total,
                "replay_plan": result.get("replay_plan", {}),
                "mapping_reason": (
                    "TRACE graph root seeded from ALIGN result metadata, "
                    "including replay plan, artifact set, and mention/entity "
                    "auditing statistics."
                ),
            },
        })
        self.eg.create_edge(
            eg_root, self.cfg.trace_agent_uid,
            "AUTHORED_BY",
            {"uid": self._edge_uid(
                eg_root, self.cfg.trace_agent_uid, "AUTHORED_BY")},
        )

        # Fix 5.8: store for Phase 7 GROUNDED_BY edges
        self._eg_root_uid = eg_root

        self._diag("INFO", "TRACE_PHASE0",
                   f"EG root {eg_root} created for intent {intent_id}")
        return eg_root

    def _prepare_alignment_indices(self, result: Dict[str, Any]) -> None:
        """
        Pre-index newer ALIGN signals so later phases can consume them
        without re-scanning the whole result repeatedly.
        """
        self._suppressed_mentions_by_anchor = (
            result.get("suppressed_mentions", {}) or {}
        )
        self._suppressed_mention_total = sum(
            len(v) for v in self._suppressed_mentions_by_anchor.values()
        )

        for hyp in result.get("entity_hypotheses", []) or []:
            hyp_id = hyp.get("hypothesis_id", "")
            canonical = hyp.get("canonical_name", "")
            category = hyp.get("category", "")
            kg0_ids = list(hyp.get("kg0_entity_ids", []) or [])
            candidates = hyp.get("kg0_link_candidates", []) or []
            selected_candidate = next(
                (c for c in candidates if c.get("selected")),
                candidates[0] if candidates else None,
            )
            hyp_meta = {
                "hypothesis_id": hyp_id,
                "canonical_name": canonical,
                "category": category,
                "confidence": hyp.get("confidence", 0.0),
                "kg0_entity_ids": kg0_ids,
                "selected_candidate": selected_candidate,
            }

            for mention in hyp.get("mentions", []) or []:
                mention_id = mention.get("mention_id", "")
                if mention_id:
                    self._mention_hypotheses[mention_id] = hyp_meta

            for kg0_id in kg0_ids:
                current = self._entity_hyp_snapshot_meta.get(kg0_id)
                candidate_score = 0.0
                if selected_candidate:
                    candidate_score = float(
                        selected_candidate.get(
                            "normalized_score",
                            selected_candidate.get("aggregate_score", 0.0),
                        ) or 0.0
                    )
                meta = {
                    "kg0_node_id": kg0_id,
                    "display_name": (
                        (selected_candidate or {}).get("name")
                        or canonical
                        or kg0_id
                    ),
                    "label": (selected_candidate or {}).get("label", ""),
                    "collection": "",
                    "graph_variable": "",
                    "entity_hypothesis_id": hyp_id,
                    "canonical_name": canonical,
                    "category": category,
                    "hypothesis_confidence": hyp.get("confidence", 0.0),
                    "candidate_score": candidate_score,
                    "selected_candidate": selected_candidate or {},
                }
                if (
                    current is None
                    or meta["candidate_score"] > current.get("candidate_score", 0.0)
                ):
                    self._entity_hyp_snapshot_meta[kg0_id] = meta

        self._diag(
            "INFO",
            "TRACE_ALIGN_INDEX",
            (
                f"Indexed {len(self._mention_hypotheses)} mention→hypothesis links, "
                f"{len(self._entity_hyp_snapshot_meta)} hypothesis-grounded KG nodes, "
                f"{self._suppressed_mention_total} suppressed mentions"
            ),
        )

    def _emit_evidence_node(
        self,
        eg_root: str,
        properties: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Centralized EvidenceNode creation so schema enrichment stays
        consistent across all TRACE phases.
        """
        enriched = self._enrich_evidence_node_properties(properties)
        uid = enriched.get("uid", "")
        if uid:
            self._evidence_nodes[uid] = enriched
        self.eg.create_node(["EvidenceNode"], enriched)
        self.eg.create_edge(
            eg_root, uid, "CONTAINS_NODE",
            {"uid": self._edge_uid(eg_root, uid, "CONTAINS_NODE")},
        )
        return enriched

    def _enrich_evidence_node_properties(
        self,
        properties: Dict[str, Any],
    ) -> Dict[str, Any]:
        props = dict(properties)
        uid = str(props.get("uid", ""))
        created_at = props.get("createdAt") or self._now()
        props["createdAt"] = created_at

        domain_type = str(props.get("domainType", ""))
        evidence_type = str(props.get("type", "Other"))
        source_category = str(props.get("sourceCategory", "") or self._infer_source_category(domain_type))
        props["sourceCategory"] = source_category
        method = str(props.get("method", "") or self._infer_evidence_method(evidence_type, domain_type))
        props["method"] = method
        props["methodDetail"] = props.get("methodDetail") or domain_type or evidence_type
        props["sourceOrigin"] = props.get("sourceOrigin") or self._infer_source_origin(
            source_category,
            domain_type,
            props.get("domainMetadata", {}),
        )

        content_excerpt = str(
            props.get("contentExcerpt")
            or props.get("description")
            or props.get("label")
            or uid
        )
        props["contentExcerpt"] = content_excerpt
        props["sourceReference"] = props.get("sourceReference") or uid
        props["hashAlgorithm"] = props.get("hashAlgorithm") or "SHA-256"
        props["hashValue"] = props.get("hashValue") or self._payload_hash({
            "uid": uid,
            "type": evidence_type,
            "domainType": domain_type,
            "label": props.get("label", ""),
            "description": props.get("description", ""),
            "contentExcerpt": content_excerpt,
            "sourceReference": props.get("sourceReference", ""),
            "sourceCategory": source_category,
        })
        props["integrityStatus"] = props.get("integrityStatus") or "verified"
        props["integrityVerifiedAt"] = props.get("integrityVerifiedAt") or created_at

        reliability_score = props.get("reliabilityScore")
        if reliability_score is None:
            reliability_score = self._default_reliability_score(
                evidence_type,
                source_category,
                method,
            )
        props["reliabilityScore"] = self._clamp01(float(reliability_score))
        props["reliabilityMethod"] = props.get("reliabilityMethod") or "heuristic"
        props["reliabilityAssessedAt"] = props.get("reliabilityAssessedAt") or created_at

        access_level = str(
            props.get("accessLevel")
            or self._infer_access_level(
                " ".join(
                    str(part) for part in [
                        props.get("label", ""),
                        props.get("description", ""),
                        content_excerpt,
                    ] if part
                )
            )
        )
        props["accessLevel"] = access_level
        props["handlingInstructions"] = (
            props.get("handlingInstructions")
            or self._handling_instructions(access_level)
        )
        props["lifecycleStatus"] = props.get("lifecycleStatus") or "active"

        content_media_type = str(
            props.get("contentMediaType")
            or self._infer_content_media_type(evidence_type, domain_type)
        )
        props["contentMediaType"] = content_media_type
        props["contentUri"] = props.get("contentUri") or self._content_uri(uid, props.get("sourceReference", ""))
        props["contentSize"] = props.get("contentSize") or len(content_excerpt.encode("utf-8"))
        props["contentLanguage"] = props.get("contentLanguage") or "en"

        temporal_precision = props.get("temporalPrecision")
        if temporal_precision is None:
            temporal_precision = self._infer_temporal_precision(
                props.get("temporalStart") or props.get("temporalEnd") or created_at
            )
        props["temporalPrecision"] = temporal_precision

        tags = list(props.get("tags", []) or [])
        for tag in ["trace", evidence_type.lower()]:
            if tag not in tags:
                tags.append(tag)
        domain_hint = domain_type.split("::")[-1].lower() if domain_type else ""
        if domain_hint and domain_hint not in tags:
            tags.append(domain_hint)
        props["tags"] = tags

        domain_metadata = dict(props.get("domainMetadata", {}) or {})
        domain_metadata.setdefault("schema_enriched", True)
        domain_metadata.setdefault("schema_enrichment_version", "1")
        domain_metadata.setdefault("hash_scope", "trace_evidence_payload")
        domain_metadata.setdefault("source_origin", props["sourceOrigin"])
        props["domainMetadata"] = domain_metadata
        return props

    @staticmethod
    def _infer_source_category(domain_type: str) -> str:
        lower = (domain_type or "").lower()
        if "snapshot" in lower:
            return "tertiary"
        if "framewitness" in lower or "witness" in lower:
            return "secondary"
        return "primary"

    @staticmethod
    def _infer_evidence_method(evidence_type: str, domain_type: str) -> str:
        type_lower = (evidence_type or "").lower()
        domain_lower = (domain_type or "").lower()
        if type_lower == "artifact":
            return "direct-capture"
        if "framewitness" in domain_lower or type_lower == "derived":
            return "aggregation"
        if "snapshot" in domain_lower or type_lower == "record":
            return "transformation"
        if type_lower in {"document", "observation", "testimony", "correspondence"}:
            return "extraction"
        return "unknown"

    @staticmethod
    def _infer_source_origin(
        source_category: str,
        domain_type: str,
        domain_metadata: Dict[str, Any],
    ) -> str:
        if source_category == "tertiary":
            return "KG0"
        collection = domain_metadata.get("collection")
        if collection:
            return f"ALIGN::{collection}"
        if "framewitness" in (domain_type or "").lower():
            return "TRACE::FrameAggregation"
        return "ALIGN::LocalCorpus"

    @staticmethod
    def _infer_access_level(text: str) -> str:
        lower = (text or "").lower()
        if any(token in lower for token in ["attorney-client", "privileged", "confidential"]):
            return "confidential"
        if any(token in lower for token in ["restricted", "do not distribute", "sensitive"]):
            return "restricted"
        if any(token in lower for token in ["public", "press release", "published"]):
            return "public"
        return "internal"

    @staticmethod
    def _handling_instructions(access_level: str) -> str:
        mapping = {
            "public": "Standard handling permitted.",
            "internal": "Internal use only.",
            "restricted": "Restricted distribution; share on a need-to-know basis.",
            "confidential": "Confidential handling required; do not redistribute without approval.",
        }
        return mapping.get(access_level or "internal", "Internal use only.")

    @staticmethod
    def _infer_content_media_type(evidence_type: str, domain_type: str) -> str:
        type_lower = (evidence_type or "").lower()
        domain_lower = (domain_type or "").lower()
        if type_lower == "record" or "snapshot" in domain_lower:
            return "application/json"
        if type_lower in {"artifact", "media"}:
            return "application/octet-stream"
        return "text/plain"

    @staticmethod
    def _content_uri(uid: str, source_reference: Any) -> str:
        ref = str(source_reference or "").strip()
        if ref and ("://" in ref or ref.startswith("/") or ref.startswith(".") or "::" in ref):
            return ref
        return f"trace://evidence/{uid}"

    @staticmethod
    def _infer_temporal_precision(value: Any) -> str:
        if isinstance(value, datetime):
            return "exact"
        text = str(value or "").strip()
        if not text:
            return "unknown"
        if re.match(r"^\d{4}$", text):
            return "year"
        if re.match(r"^\d{4}-\d{2}$", text):
            return "month"
        if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
            return "day"
        if "T" in text:
            return "exact"
        if any(token in text.lower() for token in ["circa", "approx", "around"]):
            return "approximate"
        return "unknown"

    @staticmethod
    def _payload_hash(payload: Dict[str, Any]) -> str:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _default_reliability_score(
        evidence_type: str,
        source_category: str,
        method: str,
    ) -> float:
        score = {
            "primary": 0.88,
            "secondary": 0.76,
            "tertiary": 0.68,
        }.get((source_category or "secondary").lower(), 0.7)
        if method == "aggregation":
            score -= 0.05
        elif method == "direct-capture":
            score += 0.03
        if (evidence_type or "").lower() in {"artifact", "document"}:
            score += 0.02
        if (evidence_type or "").lower() == "derived":
            score -= 0.03
        return max(0.0, min(1.0, score))

    def _reliability_factor_payload(
        self,
        evidence_uid: str,
        evidence_props: Dict[str, Any],
    ) -> Dict[str, Any]:
        score = float(evidence_props.get("reliabilityScore", 0.0) or 0.0)
        if score >= 0.8:
            impact = "positive"
        elif score >= 0.55:
            impact = "neutral"
        else:
            impact = "negative"
        factor_uid = self._deterministic_uid(f"reliability_factor::{evidence_uid}")
        return {
            "uid": factor_uid,
            "factor": "trace_reliability_basis",
            "impact": impact,
            "notes": (
                "Derived from source category="
                f"{evidence_props.get('sourceCategory', '')}, "
                f"method={evidence_props.get('method', '')}, "
                f"integrity={evidence_props.get('integrityStatus', '')}, "
                f"score={score:.2f}."
            ),
        }

    # ═════════════════════════════════════════════════════════
    #  Phase 1 — Artifacts
    # ═════════════════════════════════════════════════════════

    def _phase1_artifacts(self, result: Dict, eg_root: str) -> None:
        """
        Artifact → EvidenceNode[Artifact]

        Q6 decision: flat mapping, no parent-document node.
        Decision 1: artifact_id used as-is (string UID).
        """

        # Collect artifact metadata from all_anchors (richest source)
        art_meta: Dict[str, Dict[str, str]] = {}

        for art_id, anchors in result.get("all_anchors", {}).items():
            if art_id in art_meta:
                continue
            meta = anchors[0].get("metadata", {}) if anchors else {}
            art_meta[art_id] = {
                "collection": self._collection_of(art_id),
                "family":     meta.get("artifact_family", ""),
                "date":       meta.get("artifact_date", ""),
            }

        # Also pick up artifact_ids referenced only in subgraphs
        for sg in result.get("subgraphs", []):
            fw = sg.get("frame_witness", {})
            for art_id in fw.get("artifact_ids", []):
                if art_id not in art_meta:
                    art_meta[art_id] = {
                        "collection": self._collection_of(art_id),
                        "family": "", "date": "",
                    }

        n = 0
        for art_id, m in art_meta.items():
            if art_id in self._seen_artifacts:
                continue
            self._seen_artifacts.add(art_id)

            seg_label = art_id.split("::")[-1] if "::" in art_id else art_id

            self._emit_evidence_node(eg_root, {
                "uid":             art_id,
                "type":            "Artifact",
                "domainType":      "AlignBundle::Artifact",
                "label":           seg_label,
                "description":     f"Segment from collection {m['collection']}",
                "createdAt":       self._now(),
                "lifecycleStatus": "active",
                "sourceCategory":  "primary",
                "sourceReference": art_id,
                "temporalStart":   m["date"] or None,
                "domainMetadata":  {                         # Fix 5.1
                    "collection":    m["collection"],
                    "family":        m["family"],
                    "artifact_date": m["date"],
                    "mapping_reason": (
                        "AlignBundle::Artifact → EvidenceNode[Artifact]. "
                        "Q6: flat mapping, collection in domainMetadata, "
                        "no parent-document consolidation. "
                        "Decision 1: string artifact_id as UID."
                    ),
                },
            })

            # Index for Phase 8 (Decision 2)
            if m["collection"]:
                self._coll_arts[m["collection"]].append(art_id)

            self._log_map("Artifact", art_id,
                          "EvidenceNode[Artifact]", art_id,
                          "Q6 flat mapping; string ID (Decision 1)")
            self._ev_uids.append(art_id)
            n += 1

        self._diag("INFO", "TRACE_PHASE1",
                   f"{n} Artifact EvidenceNodes created")

    # ═════════════════════════════════════════════════════════
    #  Phase 2 — Anchors
    # ═════════════════════════════════════════════════════════

    def _phase2_anchors(self, result: Dict, eg_root: str) -> None:
        """
        Anchor → EvidenceNode[Document]

        Each anchor is a located text span within an artifact.
        Edge: Anchor -[DERIVES_FROM]→ Artifact
        """

        n = 0
        for art_id, anchors in result.get("all_anchors", {}).items():
            for anch in anchors:
                aid = anch.get("anchor_id", "")
                if not aid or aid in self._seen_anchors:
                    continue
                self._seen_anchors.add(aid)

                raw   = anch.get("raw_text", "")
                rel   = anch.get("relevance_score", 0.0)
                path  = anch.get("path", [])
                meta  = anch.get("metadata", {})
                date  = meta.get("artifact_date", "")
                fam   = meta.get("artifact_family", "")
                title = meta.get("is_title", False) or \
                        meta.get("is_slide_title", False)

                path_str = self._path_str(path)

                self._emit_evidence_node(eg_root, {
                    "uid":             aid,
                    "type":            "Document",
                    "domainType":      "AlignBundle::Anchor",
                    "label":           f"Anchor@{art_id}"
                                       + (" [title]" if title else ""),
                    "description":     (raw[:200] + "…") if len(raw) > 200
                                       else raw,
                    "contentExcerpt":  raw,
                    "createdAt":       self._now(),
                    "lifecycleStatus": "active",
                    "reliabilityScore": self._clamp01(rel),  # Fix 5.7
                    "sourceCategory":  "primary",
                    "sourceReference": f"{art_id}::{path_str}",
                    "temporalStart":   date or None,
                    "domainMetadata":  {                      # Fix 5.1
                        "path":       path,
                        "family":     fam,
                        "is_title":   title,
                        "artifact_id": art_id,
                        "mapping_reason": (
                            "AlignBundle::Anchor → EvidenceNode[Document]. "
                            "raw_text→contentExcerpt, "
                            "relevance_score→reliabilityScore."
                        ),
                    },
                })

                # Anchor DERIVES_FROM Artifact
                self.eg.create_edge(aid, art_id, "DERIVES_FROM", {
                    "uid":           self._edge_uid(
                                         aid, art_id, "DERIVES_FROM"),
                    "confidence":    self._clamp01(rel),     # Fix 5.7
                    "justification": "Text passage extracted from artifact",
                    "assertedByUid": self.cfg.trace_agent_uid,
                    "assertedAt":    self._now(),
                    "domainMetadata": {                       # Fix 5.1
                        "role": "extracted_from",
                        "mapping_reason": (
                            "Anchor DERIVES_FROM Artifact: the anchor "
                            "text was extracted at the specified path."
                        ),
                    },
                })

                self._log_map("Anchor", aid,
                              "EvidenceNode[Document]", aid,
                              "Anchor text passage → Document node")
                self._ev_uids.append(aid)
                n += 1

        self._diag("INFO", "TRACE_PHASE2",
                   f"{n} Anchor EvidenceNodes created")

    # ═════════════════════════════════════════════════════════
    #  Phase 3 — Mentions  (Q1)
    # ═════════════════════════════════════════════════════════

    def _phase3_mentions(self, result: Dict, eg_root: str) -> None:
        """
        Mention → EvidenceNode[Observation]   (Q1: mention granularity)

        Each unique mention_id becomes a first-class node with full
        span metadata.  Edge: Mention -[DERIVES_FROM]→ Anchor.

        Also populates the Q4 co-reference registry (Fix 4.8).
        """

        n = 0
        skipped = 0

        for anchor_id, mentions in result.get("all_mentions", {}).items():
            for m in mentions:
                mid = m.get("mention_id", "")
                if not mid or mid in self._seen_mentions:
                    continue

                conf = m.get("confidence", 0.0)
                if conf < self.cfg.tau_mention_confidence:
                    skipped += 1
                    continue
                self._seen_mentions.add(mid)

                surface    = m.get("surface", "")
                cat        = m.get("category", "")
                norm       = m.get("normalized", "")
                span_s     = m.get("span_start")
                span_e     = m.get("span_end")
                kg0_eid    = m.get("kg0_entity_id")
                qualifiers = m.get("qualifiers", {})
                type_resolution = qualifiers.get("type_resolution", {})
                hyp_meta = self._mention_hypotheses.get(mid, {})
                resolved_cat = type_resolution.get("resolved_category", cat)

                self._emit_evidence_node(eg_root, {
                    "uid":             mid,
                    "type":            "Observation",
                    "domainType":      "AlignBundle::Mention",
                    "label":           f"'{surface}' [{resolved_cat or cat}]",
                    "description":     (
                        f"Entity mention '{surface}' "
                        f"(category={cat}, resolved={resolved_cat or cat}) "
                        f"at span [{span_s}:{span_e}]"
                    ),
                    "contentExcerpt":  surface,
                    "createdAt":       self._now(),
                    "lifecycleStatus": "active",
                    "reliabilityScore": self._clamp01(conf), # Fix 5.7
                    "sourceCategory":  "primary",
                    "sourceReference": f"{anchor_id}::[{span_s}:{span_e}]",
                    "domainMetadata":  {                      # Fix 5.1
                        "surface":       surface,
                        "category":      cat,
                        "resolved_category": resolved_cat,
                        "category_scores": m.get("category_scores", {}),
                        "normalized":    norm,
                        "span_start":    span_s,
                        "span_end":      span_e,
                        "kg0_entity_id": kg0_eid,
                        "qualifiers":    qualifiers,
                        "type_resolution": type_resolution,
                        "kg_link": qualifiers.get("kg_link", {}),
                        "entity_hypothesis_id": hyp_meta.get("hypothesis_id", ""),
                        "entity_hypothesis_name": hyp_meta.get("canonical_name", ""),
                        "entity_hypothesis_category": hyp_meta.get("category", ""),
                        "entity_hypothesis_kg0_ids": hyp_meta.get("kg0_entity_ids", []),
                        "anchor_id":     anchor_id,
                        "mapping_reason": (
                            "Q1: AlignBundle::Mention → "
                            "EvidenceNode[Observation]. "
                            "surface→label/contentExcerpt, "
                            "confidence→reliabilityScore, "
                            "span→domainMetadata.span."
                        ),
                    },
                })

                # Mention DERIVES_FROM Anchor  (Q5 edge pattern)
                if anchor_id in self._seen_anchors:
                    self.eg.create_edge(mid, anchor_id, "DERIVES_FROM", {
                        "uid":           self._edge_uid(
                                             mid, anchor_id, "DERIVES_FROM"),
                        "confidence":    self._clamp01(conf),  # Fix 5.7
                        "justification": (
                            f"Mention '{surface}' detected within "
                            f"anchor text at [{span_s}:{span_e}]"
                        ),
                        "assertedByUid": self.cfg.trace_agent_uid,
                        "assertedAt":    self._now(),
                        "domainMetadata": {                    # Fix 5.1
                            "role": "detected_within",
                            "mapping_reason": (
                                "Q5: Mention DERIVES_FROM Anchor — "
                                "entity was detected in the anchor's text."
                            ),
                        },
                    })

                # ── Fix 4.8: register for Q4 co-reference ──
                hyp_kg_ids = hyp_meta.get("kg0_entity_ids", [])
                coref_key = (
                    hyp_meta.get("hypothesis_id")
                    or (hyp_kg_ids[0] if hyp_kg_ids else "")
                    or kg0_eid
                    or norm
                )
                if coref_key:
                    self._entity_mentions[coref_key].append(mid)

                self._log_map("Mention", mid,
                              "EvidenceNode[Observation]", mid,
                              "Q1 mention granularity mapping")
                self._ev_uids.append(mid)
                n += 1

        self._diag("INFO", "TRACE_PHASE3",
                   f"{n} Mention nodes created, {skipped} below threshold")

    # ═════════════════════════════════════════════════════════
    #  Phase 3b — Q4: Entity Co-reference  (Fix 4.8)
    # ═════════════════════════════════════════════════════════

    def _phase3b_entity_coref(self, eg_root: str) -> None:
        """
        Q4 — Entity Co-reference Linking.

        ASSUMPTION: Q4 addresses cross-document entity resolution.
        Mentions sharing the same kg0_entity_id (or, failing that,
        the same normalized form) refer to the same real-world
        entity.  We link them via CORROBORATES edges.

        Fix 4.7 principle applied: chain topology A→B→C instead of
        full mesh, giving O(n) edges per entity group.

        Toggle: cfg.create_coref_edges

        If Q4 was defined differently in the original discussion,
        replace this phase body while keeping the phase slot.
        """

        if not self.cfg.create_coref_edges:
            self._diag("INFO", "TRACE_PHASE3B",
                       "Q4 co-reference disabled by config")
            return

        n = 0
        groups = 0
        for coref_key, mention_ids in self._entity_mentions.items():
            if len(mention_ids) < 2:
                continue
            groups += 1

            # Chain topology: m[0]↔m[1]↔m[2]↔…
            for i in range(len(mention_ids) - 1):
                a, b = mention_ids[i], mention_ids[i + 1]
                self.eg.create_edge(a, b, "CORROBORATES", {
                    "uid":           self._edge_uid(
                                         a, b, "CORROBORATES"),
                    "confidence":    1.0,
                    "justification": (
                        f"Mentions share entity reference '{coref_key}'"
                    ),
                    "assertedByUid": self.cfg.trace_agent_uid,
                    "assertedAt":    self._now(),
                    "domainMetadata": {                        # Fix 5.1
                        "coref_key": coref_key,
                        "mapping_reason": (
                            "Q4: Entity co-reference. Mentions sharing "
                            "kg0_entity_id or normalized form linked via "
                            "CORROBORATES (chain topology, Fix 4.7)."
                        ),
                    },
                })
                n += 1

        self._diag("INFO", "TRACE_PHASE3B",
                   f"{n} CORROBORATES edges across "
                   f"{groups} co-reference groups (Q4)")

    # ═════════════════════════════════════════════════════════
    #  Phase 4 — Witnesses  (content_hash dedup)
    # ═════════════════════════════════════════════════════════

    def _phase4_witnesses(self, result: Dict, eg_root: str) -> None:
        """
        Witness → EvidenceNode[Testimony]

        CRITICAL DEDUP: The sample data shows 3× duplication of
        witnesses sharing the same content_hash within a slot.
        We keep exactly one node per unique content_hash.

        Fix 4.4: if ``all_witnesses`` is absent from the bundle, we
        fall back to collecting witnesses from
        ``slot_bindings[*].witnesses[*]``.

        Edges (Q5):
            Witness -[DERIVES_FROM]→ Anchor
            Witness -[DERIVES_FROM]→ Mention
        """

        # Fix 4.4: robust witness collection
        all_wits = self._collect_all_witnesses(result)
        anchor_to_artifact = {
            str(anchor.get("anchor_id", "")): str(art_id)
            for art_id, anchors in result.get("all_anchors", {}).items()
            for anchor in anchors or []
            if anchor.get("anchor_id")
        }

        n = 0
        dup = 0

        # Per-slot, per-original-var counter used to give each EVIDENCE
        # witness a unique var name in its EvidenceNode metadata. ALIGN
        # binds multiple co-supporting documents to a single skeleton
        # variable (e.g., var=D → three different doc surfaces); CONFLICT
        # groups EvidenceNodes by var_name and pairwise-compares
        # surfaces, so co-supporting docs under the same var were being
        # flagged as contradictions. Unique var names (D1, D2, D3, …)
        # prevent that grouping. Scope is EVIDENCE only — other slots
        # keep their original vars. The slot-level values_by_var used by
        # Claim rendering is unchanged, so evidence-claim statements
        # still aggregate correctly under the original var.
        evidence_var_counter: Dict[str, int] = defaultdict(int)

        for wit in all_wits:
            wid   = wit.get("witness_id", "")
            chash = wit.get("content_hash", "")
            score = wit.get("score", 0.0)

            # ── Gate: minimum score ─────────────────────────
            if score < self.cfg.tau_witness_score:
                continue

            # ── Gate: content-hash dedup ─────────────────────
            if chash in self._seen_witness_hashes:
                dup += 1
                continue
            self._seen_witness_hashes[chash] = wid

            # ── Parse witness fields ────────────────────────
            quality  = wit.get("quality", "AMBIGUOUS")
            justif   = wit.get("justification", "")
            ts       = wit.get("timestamp", "")
            phase    = wit.get("phase", "")

            ie       = wit.get("intent_element", {})
            eid      = ie.get("element_id", "")
            detail   = ie.get("element_detail", {})
            var      = detail.get("var", "")
            stype    = detail.get("slot_type", "")

            # EVIDENCE-only uniqueness: suffix a 1-based counter so every
            # emitted witness carries a distinct var for CONFLICT.
            if stype == "EVIDENCE" and var:
                evidence_var_counter[var] += 1
                effective_var = f"{var}{evidence_var_counter[var]}"
            else:
                effective_var = var

            anch_d   = wit.get("anchor", {})
            anch_id  = anch_d.get("anchor_id", "")
            artifact_id = str(anch_d.get("artifact_id") or anchor_to_artifact.get(anch_id, ""))
            ment_d   = wit.get("mention", {})
            ment_id  = ment_d.get("mention_id", "")
            surface  = ment_d.get("surface")
            if not surface:
                surface = wit.get("surface")
            if not surface:
                surface = "?"
            kg0_eid = ment_d.get("kg0_entity_id")
            if kg0_eid is None and "kg0_entity_id" in wit:
                kg0_eid = wit.get("kg0_entity_id")

            # Normalise score into [0,1] — raw scores in the
            # sample range up to ~25.5
            norm_score = self._clamp01(                      # Fix 5.7
                score / 100.0 if score > 1.0 else score
            )

            self._emit_evidence_node(eg_root, {
                "uid":             wid,
                "type":            "Testimony",
                "domainType":      "AlignBundle::Witness",
                "label":           f"Witness: {stype}/{effective_var}→'{surface}'",
                "description":     justif,
                "contentExcerpt":  justif,
                "createdAt":       ts or self._now(),
                "lifecycleStatus": "active",
                "reliabilityScore": norm_score,
                "sourceCategory":  "secondary",
                "method":          "extraction",
                "domainMetadata":  {                          # Fix 5.1
                    "quality":      quality,
                    "content_hash": chash,
                    "phase":        phase,
                    "score_raw":    score,
                    "element_id":   eid,
                    "var_name":     effective_var,
                    "slot_type":    stype,
                    "anchor_id":    anch_id,
                    "artifact_id":   artifact_id,
                    "mention_id":   ment_id,
                    "surface":      surface,
                    "kg0_entity_id": kg0_eid,
                    "mapping_reason": (
                        "AlignBundle::Witness → EvidenceNode[Testimony]. "
                        "Dedup by content_hash: one node per unique hash. "
                        "justification→contentExcerpt, score→reliabilityScore."
                        ),
                    },
                })

            # ── Q5 edges ────────────────────────────────────
            if anch_id and anch_id in self._seen_anchors:
                self.eg.create_edge(wid, anch_id, "DERIVES_FROM", {
                    "uid":           self._edge_uid(
                                         wid, anch_id, "DERIVES_FROM"),
                    "confidence":    norm_score,
                    "justification": (
                        f"Witness attests slot {stype} binding "
                        f"from anchor {anch_id}"
                    ),
                    "assertedByUid": self.cfg.trace_agent_uid,
                    "assertedAt":    self._now(),
                    "domainMetadata": {                        # Fix 5.1
                        "role": "attests_from",
                        "mapping_reason":
                            "Q5: Witness DERIVES_FROM Anchor.",
                    },
                })

            if ment_id and ment_id in self._seen_mentions:
                self.eg.create_edge(wid, ment_id, "DERIVES_FROM", {
                    "uid":           self._edge_uid(
                                         wid, ment_id, "DERIVES_FROM"),
                    "confidence":    self._clamp01(           # Fix 5.7
                        ment_d.get("confidence", 0.0)),
                    "justification": (
                        f"Witness binds mention '{surface}' "
                        f"to variable {effective_var}"
                    ),
                    "assertedByUid": self.cfg.trace_agent_uid,
                    "assertedAt":    self._now(),
                    "domainMetadata": {                        # Fix 5.1
                        "role": "identifies_mention",
                        "mapping_reason":
                            "Q5: Witness DERIVES_FROM Mention.",
                    },
                })

            self._log_map(
                "Witness", wid,
                "EvidenceNode[Testimony]", wid,
                f"content_hash={chash}; {dup} dups so far",
            )
            self._ev_uids.append(wid)
            n += 1

        self._diag("INFO", "TRACE_PHASE4",
                   f"{n} Witness nodes, {dup} duplicates suppressed")

    # ═════════════════════════════════════════════════════════
    #  Phase 5 — Snapshot Nodes  (Q7)
    # ═════════════════════════════════════════════════════════

    def _phase5_snapshots(self, result: Dict, eg_root: str) -> None:
        """
        SnapshotNode → EvidenceNode[Record, sourceCategory='tertiary']

        These are pre-existing KG0 entities (e.g. FENTORA Drug)
        referenced by INFERRED variable bindings in subgraphs.

        Fix 4.2: builds ``_subgraph_snapshots`` mapping so Phase 6
        can create DERIVES_FROM edges from FrameWitness to each
        snapshot it references.
        """

        n = 0
        n_hyp = 0
        for sg in result.get("subgraphs", []):
            sgid = sg.get("subgraph_id", "")
            for sn in sg.get("snapshot", {}).get("nodes", []):
                nid = sn.get("node_id", "")
                if not nid:
                    continue

                # Fix 4.2: always register for the subgraph map
                self._subgraph_snapshots[sgid].append(nid)

                dname  = sn.get("display_name", "")
                labels = sn.get("labels", [])
                props  = sn.get("properties", {})
                coll   = props.get("collection", "")
                var    = sn.get("assigned_to", "")

                created = self._create_snapshot_node(
                    eg_root=eg_root,
                    node_id=nid,
                    display_name=dname,
                    labels=labels,
                    collection=coll,
                    graph_var=var,
                    props=props,
                    domain_type="AlignBundle::SnapshotNode",
                    description=(
                        f"KG0 entity '{dname}' from {coll}, "
                        f"assigned to variable {var}"
                    ),
                    mapping_reason=(
                        "Q7: SnapshotNode → EvidenceNode[Record, "
                        "sourceCategory='tertiary']. Pre-existing "
                        f"KG0 entity providing evidence terminus "
                        f"for variable {var}."
                    ),
                    extra_metadata={
                        "inferred": sn.get("inferred", True),
                    },
                    map_source="SnapshotNode",
                    map_message=f"Q7: KG0 entity '{dname}', var={var}",
                )
                if created:
                    n += 1

        for nid, meta in self._entity_hyp_snapshot_meta.items():
            created = self._create_snapshot_node(
                eg_root=eg_root,
                node_id=nid,
                display_name=meta.get("display_name", nid),
                labels=[meta.get("label")] if meta.get("label") else [],
                collection=meta.get("collection", ""),
                graph_var=meta.get("graph_variable", ""),
                props={},
                domain_type="AlignBundle::EntityHypothesisSnapshot",
                description=(
                    "KG0 entity grounded from entity hypothesis "
                    f"'{meta.get('canonical_name', nid)}'"
                ),
                mapping_reason=(
                    "Hypothesis-level KG grounding promoted from "
                    "AlignBundle::EntityHypothesis into "
                    "EvidenceNode[Record] so TRACE can retain the "
                    "resolved KG terminus even when it was not only "
                    "present in a subgraph snapshot."
                ),
                extra_metadata={
                    "entity_hypothesis_id": meta.get("entity_hypothesis_id", ""),
                    "canonical_name": meta.get("canonical_name", ""),
                    "hypothesis_category": meta.get("category", ""),
                    "hypothesis_confidence": meta.get("hypothesis_confidence", 0.0),
                    "selected_candidate": meta.get("selected_candidate", {}),
                    "candidate_score": meta.get("candidate_score", 0.0),
                },
                map_source="EntityHypothesis",
                map_message=(
                    "Hypothesis-grounded KG entity "
                    f"'{meta.get('display_name', nid)}'"
                ),
            )
            if created:
                n_hyp += 1

        self._diag("INFO", "TRACE_PHASE5",
                   f"{n} subgraph snapshots + {n_hyp} hypothesis snapshots created")

    def _create_snapshot_node(
        self,
        *,
        eg_root: str,
        node_id: str,
        display_name: str,
        labels: List[str],
        collection: str,
        graph_var: str,
        props: Dict[str, Any],
        domain_type: str,
        description: str,
        mapping_reason: str,
        extra_metadata: Optional[Dict[str, Any]] = None,
        map_source: str = "SnapshotNode",
        map_message: str = "",
    ) -> bool:
        if not node_id or node_id in self._seen_snapshots:
            return False
        self._seen_snapshots.add(node_id)

        self._emit_evidence_node(eg_root, {
            "uid":             node_id,
            "type":            "Record",
            "domainType":      domain_type,
            "label":           display_name or node_id,
            "description":     description,
            "contentExcerpt":  display_name or node_id,
            "createdAt":       self._now(),
            "lifecycleStatus": "active",
            "sourceCategory":  "tertiary",
            "sourceReference": node_id,
            "domainMetadata":  {
                "kg0_node_id":   node_id,
                "labels":        labels,
                "collection":    collection,
                "graphVariable": graph_var,
                "kg0_properties": props,
                **(extra_metadata or {}),
                "mapping_reason": mapping_reason,
            },
        })
        self._log_map(
            map_source, node_id,
            "EvidenceNode[Record]", node_id,
            map_message or f"KG0 entity '{display_name or node_id}'",
        )
        self._ev_uids.append(node_id)
        return True

    # ═════════════════════════════════════════════════════════
    #  Phase 6 — Frame Witnesses + Evidence-Chain ProvenanceEvents
    # ═════════════════════════════════════════════════════════

    def _phase6_frame_witnesses(
        self, result: Dict, eg_root: str
    ) -> None:
        """
        FrameWitness → EvidenceNode[Derived]
        EvidenceChainStep → ProvenanceEvent   (Q2)

        A FrameWitness aggregates evidence across a subgraph.
        Its ``evidence_chain_summary`` steps become ProvenanceEvents
        chained via HAS_PROVENANCE_EVENT with sequenceIndex.

        Fix 4.2: DERIVES_FROM edges to snapshot nodes in the same
        subgraph, connecting the derived evidence to its KG0 terminus.

        Fix 4.3: DERIVES_FROM edges to constituent mentions
        (``mention_ids``), providing mention-level lineage.

        Fix 4.6: expanded label→action mapping; original_label
        preserved in ProvenanceEvent.domainMetadata.

        Fix 5.4: 'created' action guarded to sequenceIndex 0 only.
        Fix 5.5: timestamps enforced monotonically non-decreasing.
        """

        n_fw = 0
        n_pe = 0

        for sg in result.get("subgraphs", []):
            fw   = sg.get("frame_witness", {})
            fwid = fw.get("witness_id", "")
            if not fwid or fwid in self._seen_frame_witnesses:
                continue
            self._seen_frame_witnesses.add(fwid)

            sgid  = sg.get("subgraph_id", "")
            desc  = fw.get("description", "")
            coh   = fw.get("coherence_score", 0.0)
            tspan = fw.get("temporal_span", {})

            self._emit_evidence_node(eg_root, {
                "uid":             fwid,
                "type":            "Derived",
                "domainType":      "AlignBundle::FrameWitness",
                "label":           f"Frame for {sgid}",
                "description":     desc,
                "contentExcerpt":  desc,
                "createdAt":       self._now(),
                "lifecycleStatus": "active",
                "reliabilityScore": self._clamp01(coh),      # Fix 5.7
                "method":          "aggregation",
                "sourceCategory":  "secondary",
                "temporalStart":   tspan.get("earliest"),
                "temporalEnd":     tspan.get("latest"),
                "domainMetadata":  {                          # Fix 5.1
                    "subgraph_id":          sgid,
                    "temporal_consistency": fw.get(
                        "temporal_consistency", True),
                    "artifact_ids":         fw.get("artifact_ids", []),
                    "anchor_count":         len(fw.get("anchor_ids", [])),
                    "mention_count":        len(fw.get("mention_ids", [])),
                    "skeleton_bindings":    fw.get(
                        "skeleton_bindings", {}),
                    "edge_satisfactions":   sg.get(
                        "edge_satisfactions", {}),
                    "hard_coverage": sg.get("hard_coverage", 0.0),
                    "soft_coverage": sg.get("soft_coverage", 0.0),
                    "score":         sg.get("score", 0.0),
                    "diversity":     sg.get("diversity_score", 0.0),
                    "mapping_reason": (
                        "AlignBundle::FrameWitness → "
                        "EvidenceNode[Derived]. "
                        "EvidenceChainSteps → ProvenanceEvents (Q2). "
                        "Fix 4.2: linked to snapshot nodes. "
                        "Fix 4.3: linked to constituent mentions."
                    ),
                },
            })

            # ── Link to constituent anchors via DERIVES_FROM ─
            for aid in fw.get("anchor_ids", []):
                if aid in self._seen_anchors:
                    self.eg.create_edge(fwid, aid, "DERIVES_FROM", {
                        "uid":           self._edge_uid(
                                             fwid, aid, "DERIVES_FROM"),
                        "justification": "Frame draws from anchor",
                        "assertedByUid": self.cfg.trace_agent_uid,
                        "assertedAt":    self._now(),
                        "domainMetadata": {                    # Fix 5.1
                            "role": "draws_from_anchor",
                            "mapping_reason":
                                "FrameWitness DERIVES_FROM Anchor.",
                        },
                    })

            # ── Fix 4.3: Link to constituent mentions ───────
            for mid in fw.get("mention_ids", []):
                if mid in self._seen_mentions:
                    self.eg.create_edge(fwid, mid, "DERIVES_FROM", {
                        "uid":           self._edge_uid(
                                             fwid, mid, "DERIVES_FROM"),
                        "justification": "Frame draws from mention",
                        "assertedByUid": self.cfg.trace_agent_uid,
                        "assertedAt":    self._now(),
                        "domainMetadata": {                    # Fix 5.1
                            "role": "draws_from_mention",
                            "mapping_reason": (
                                "Fix 4.3: FrameWitness DERIVES_FROM "
                                "Mention — provides mention-level "
                                "lineage in the evidence frame."
                            ),
                        },
                    })

            # ── Fix 4.2: Link to snapshot nodes ─────────────
            for snid in self._subgraph_snapshots.get(sgid, []):
                if snid in self._seen_snapshots:
                    self.eg.create_edge(fwid, snid, "DERIVES_FROM", {
                        "uid":           self._edge_uid(
                                             fwid, snid, "DERIVES_FROM"),
                        "justification": (
                            f"Frame references KG0 entity {snid} "
                            f"in subgraph {sgid}"
                        ),
                        "assertedByUid": self.cfg.trace_agent_uid,
                        "assertedAt":    self._now(),
                        "domainMetadata": {                    # Fix 5.1
                            "role": "references_kg0_entity",
                            "mapping_reason": (
                                "Fix 4.2: FrameWitness DERIVES_FROM "
                                "SnapshotNode — connects derived "
                                "evidence to its KG0 entity terminus."
                            ),
                        },
                    })

            # ── Q2: EvidenceChainStep → ProvenanceEvent ─────
            # Fix 5.5: sort by step number, enforce monotonic timestamps
            chain_steps = sorted(
                fw.get("evidence_chain_summary", []),
                key=lambda s: s.get("step", 0),
            )
            event_records: List[Dict[str, Any]] = []
            prev_ts = ""
            for step in chain_steps:
                snum   = step.get("step", 0)
                label  = step.get("label", "")
                date   = step.get("date", "")
                summ   = step.get("summary", "")

                action = self._label_to_action(label)

                # Fix 5.4: created_event_is_first constraint
                seq_idx = snum - 1   # 0-based
                if action == "created" and seq_idx != 0:
                    action = "collected"

                # Fix 5.5: enforce monotonic timestamps for
                # provenance_chain_ordering constraint
                if not date or (prev_ts and date < prev_ts):
                    date = prev_ts or self._now()
                prev_ts = date

                # Fix 4.5: deterministic PE UID
                pe_uid = self._deterministic_uid(
                    f"provenance_event::{fwid}::step::{snum}")

                self.eg.create_node(["ProvenanceEvent"], {
                    "uid":       pe_uid,
                    "action":    action,
                    "timestamp": date,
                    "notes":     f"[{label}] {summ}",
                    # Fix 4.6 + Fix 5.1: preserve original label
                    "domainMetadata": {
                        "original_label": label,
                        "mapping_reason": (
                            f"Q2: EvidenceChainStep → ProvenanceEvent. "
                            f"label='{label}' mapped to action='{action}'. "
                            f"Fix 4.6: original label preserved."
                        ),
                    },
                })
                self.eg.create_edge(
                    fwid, pe_uid, "HAS_PROVENANCE_EVENT", {
                        "uid":           self._edge_uid(
                                             fwid, pe_uid,
                                             "HAS_PROVENANCE_EVENT"),
                        "sequenceIndex": seq_idx,
                    },
                )
                self.eg.create_edge(
                    pe_uid, self.cfg.trace_agent_uid,
                    "PERFORMED_BY", {
                        "uid": self._edge_uid(
                                   pe_uid, self.cfg.trace_agent_uid,
                                   "PERFORMED_BY"),
                    },
                )

                self._log_map(
                    "EvidenceChainStep", f"{fwid}:step:{snum}",
                    "ProvenanceEvent", pe_uid,
                    f"Q2: step={snum}, label='{label}', "
                    f"action='{action}'",
                )
                event_records.append({
                    "uid": pe_uid,
                    "label": label,
                    "notes": f"[{label}] {summ}",
                    "artifact_id": step.get("artifact_id", ""),
                    "anchor_id": step.get("anchor_id", ""),
                    "mention_id": step.get("mention_id", ""),
                })
                n_pe += 1

            self._log_map(
                "FrameWitness", fwid,
                "EvidenceNode[Derived]", fwid,
                f"Subgraph {sgid}, "
                f"{len(fw.get('evidence_chain_summary',[]))} steps",
            )
            self._frame_contexts[fwid] = {
                "frame_uid": fwid,
                "subgraph_id": sgid,
                "anchor_ids": set(fw.get("anchor_ids", []) or []),
                "mention_ids": set(fw.get("mention_ids", []) or []),
                "artifact_ids": set(fw.get("artifact_ids", []) or []),
                "coherence_score": coh,
                "edge_satisfactions": dict(sg.get("edge_satisfactions", {}) or {}),
                "available_vars": {
                    k for k, v in (sg.get("bindings", {}) or {}).items()
                    if isinstance(v, dict) and v.get("bound")
                },
                "events": event_records,
            }
            self._ev_uids.append(fwid)
            n_fw += 1

        self._diag("INFO", "TRACE_PHASE6",
                   f"{n_fw} FrameWitnesses, {n_pe} ProvenanceEvents")

    # ═════════════════════════════════════════════════════════
    #  Phase 7 — Claims from Slot Bindings  (Q3)
    # ═════════════════════════════════════════════════════════

    def _phase7_claims(self, result: Dict) -> Optional[str]:
        """
        SlotBinding → Claim in the RG.

        Q3 mapping:
            GROUNDED  + conf > 0  →  status = 'supported'
            INFERRED  + conf > 0  →  status = 'weakly-supported'
            AMBIGUOUS + conf = 0  →  skip  OR  'undetermined'
                (controlled by cfg.ambiguous_slot_policy)

        Fix 4.1: bridge writer for GROUNDED_BY edges.
                  When ``self.bridge is not self.rg``, a lightweight
                  shadow Claim node is created in the bridge database
                  so that the edge's MATCH clause finds the source.

        Bridge: Claim -[GROUNDED_BY {role:'grounds'}]→ Witness EvidenceNode
        """

        slots = result.get("slot_bindings", [])
        if not slots:
            self._diag("WARNING", "TRACE_NO_SLOTS",
                       "No slot_bindings in result")
            return None

        # ── RG root — Fix 4.5: deterministic UID ───────────
        intent_id = result.get("intent_id", "")
        rg_root = self._deterministic_uid(f"rg_root::{intent_id}")
        self.rg.create_node(["GraphRoot"], {
            "uid":           rg_root,
            "graphType":     "ReasoningGraph",
            "version":       self.cfg.graph_version,
            "schemaVersion": self.cfg.schema_version,
            "created":       self._now(),
            "question":      result.get("question_text", ""),
            "title":         f"RG for {intent_id}",
            "tags":          ["trace", intent_id],           # Fix 5.1
        })
        self.rg.create_node(["Agent"], {
            "uid":  self.cfg.trace_agent_uid,
            "type": "system",
            "name": self.cfg.trace_agent_name,
            "role": "align_bundle_mapper",
        })

        # Fix 5.6: AUTHORED_BY edge for RG root
        self.rg.create_edge(
            rg_root, self.cfg.trace_agent_uid,
            "AUTHORED_BY",
            {"uid": self._edge_uid(
                rg_root, self.cfg.trace_agent_uid, "AUTHORED_BY")},
        )

        n_created = 0
        n_skipped = 0

        ordered_slots = self._ordered_slots_for_claims(slots)

        for rank, slot in enumerate(ordered_slots, start=1):
            sid   = slot.get("slot_id", "")
            stype = slot.get("slot_type", "")
            desc  = slot.get("description", "")
            qual  = slot.get("quality", "AMBIGUOUS")
            conf  = slot.get("confidence", 0.0)
            wits  = slot.get("witnesses", [])
            rendered = self._render_slot_claim(slot)
            support_meta = self._build_claim_support_metadata(slot, rendered)
            claim_now = self._now()

            # ── Q3: quality → status ────────────────────────
            if qual == "AMBIGUOUS" and conf == 0.0:
                if self.cfg.ambiguous_slot_policy == "skip":
                    self._diag("INFO", "TRACE_SLOT_SKIP",
                               f"Slot {sid} skipped (AMBIGUOUS/0.0)")
                    n_skipped += 1
                    continue
                status = "undetermined"
            elif qual == "GROUNDED":
                status = "supported"
            elif qual == "INFERRED":
                status = "weakly-supported"
            else:
                status = "proposed"

            # Fix 4.5: deterministic claim UID
            claim_uid = self._deterministic_uid(f"claim::{sid}")

            self.rg.create_node(["Claim"], {
                "uid":                claim_uid,
                "type":               rendered.get("claim_type", "finding"),
                "domainType":         "AlignBundle::SlotClaim",
                "statement":          rendered["statement"],
                "status":             status,
                "confidenceScore":    self._clamp01(conf),   # Fix 5.7
                "confidenceMethod":   "algorithmic",
                "confidenceRationale": self._build_claim_confidence_rationale(
                    slot=slot,
                    status=status,
                    rendered=rendered,
                    support_meta=support_meta,
                ),
                "confidenceAssessedAt": claim_now,
                "assertedAt":         claim_now,
                "tags":               ["trace", stype, sid], # Fix 5.1
                "domainMetadata":     {                       # Fix 5.1
                    "slot_id":      sid,
                    "slot_type":    stype,
                    "claim_rank":   rank,
                    "slot_priority": self._slot_type_priority(stype),
                    "quality":      qual,
                    "status":       status,
                    "slot_description": desc,
                    "slot_value_count": len(slot.get("value", []) or []),
                    "slot_var_count": len(rendered.get("values_by_var", {})),
                    "anchor_group_count": len(rendered.get("anchor_groups", [])),
                    "primary_artifact_id": support_meta.get("primary_artifact_id", ""),
                    "artifact_scope": support_meta.get("artifact_scope", []),
                    "primary_anchor_address": support_meta.get("primary_anchor_address", ""),
                    "witness_count": len(wits),
                    "slot_values_by_var": rendered.get("values_by_var", {}),
                    "slot_anchor_groups": rendered.get("anchor_groups", []),
                    "slot_summary": rendered.get("summary", ""),
                    "statement_style": rendered.get("statement_style", "structured_narrative"),
                    "statement_quality": rendered.get("statement_quality", {}),
                    "key_entities": rendered.get("key_entities", []),
                    "support_context": support_meta.get("support_context", {}),
                    "support_summary": support_meta.get("support_summary", ""),
                    "provenance_support_steps": support_meta.get("provenance_support_steps", []),
                    **rendered.get("structured_metadata", {}),
                    "mapping_reason": (
                        f"Q3: Slot quality='{qual}' → "
                        f"Claim status='{status}'. "
                        f"confidence={conf}→confidenceScore."
                    ),
                },
            })
            self.rg.create_edge(
                rg_root, claim_uid, "CONTAINS_CLAIM",
                {"uid": self._edge_uid(
                     rg_root, claim_uid, "CONTAINS_CLAIM")},
            )
            self.rg.create_edge(
                claim_uid, self.cfg.trace_agent_uid, "ASSERTED_BY",
                {"uid": self._edge_uid(
                     claim_uid, self.cfg.trace_agent_uid, "ASSERTED_BY")},
            )
            self.rg.create_edge(
                claim_uid, self.cfg.trace_agent_uid, "CONFIDENCE_ASSESSED_BY",
                {"uid": self._edge_uid(
                    claim_uid, self.cfg.trace_agent_uid, "CONFIDENCE_ASSESSED_BY")},
            )

            # ── Fix 4.1: shadow claim in bridge database ────
            # If the bridge writer is not the RG writer, the
            # Claim node does not exist in the bridge's database.
            # Create a lightweight shadow so the MATCH succeeds.
            if self.bridge is not self.rg:
                self.bridge.create_node(["Claim"], {
                    "uid":       claim_uid,
                    "type":      "other",                    # Fix 5.2
                    "statement": (
                        f"Shadow ref → see RG for full claim "
                        f"{claim_uid}"
                    ),
                    "status":    status,
                })

            # ── Bridge: GROUNDED_BY ─────────────────────────
            # Dedup witnesses by content_hash within this slot
            seen_h: Set[str] = set()
            for w in wits:
                ch = w.get("content_hash", "")
                if ch in seen_h:
                    continue
                seen_h.add(ch)

                # Resolve to the canonical witness_id we kept
                canon = self._seen_witness_hashes.get(
                    ch, w.get("witness_id", ""))

                # Fix 5.9: guard against dangling targets.  The edge
                # is emitted via self.bridge, so both endpoints must
                # be resolvable in the bridge's database.  For the
                # default single-DB case self.bridge is self.eg so
                # this is equivalent to the EG check.  For split-DB
                # deployments the witness must exist in the bridge
                # specifically — checking self.eg would miss dangling
                # targets on the bridge side.
                if not self.bridge.node_exists(canon):
                    self._diag(
                        "WARNING", "TRACE_DANGLING_WITNESS",
                        f"Witness {canon} not resolvable in bridge; "
                        f"skipping GROUNDED_BY for claim {claim_uid}",
                    )
                    continue

                raw_sc = w.get("score", 0.0)
                norm   = self._clamp01(                      # Fix 5.7
                    raw_sc / 100.0 if raw_sc > 1.0 else raw_sc
                )

                # Fix 4.1: use self.bridge, not self.eg
                self.bridge.create_edge(
                    claim_uid, canon, "GROUNDED_BY", {
                        "uid":       self._edge_uid(
                                         claim_uid, canon, "GROUNDED_BY"),
                        "role":      "grounds",
                        "relevance": norm,
                        "excerpt":   w.get("justification", ""),
                        "justification": (
                            f"Witness {canon} grounds {stype} claim "
                            f"via {w.get('quality','?')} binding"
                        ),
                        "evidenceGraphId": self._eg_root_uid,  # Fix 5.8
                    },
                )

            self._log_map(
                "SlotBinding", sid,
                f"Claim[status='{status}']", claim_uid,
                f"Q3: {stype}, quality={qual}, "
                f"{len(seen_h)} unique witnesses",
            )
            self._slot_claim_records.append({
                "slot_id": sid,
                "slot_type": stype,
                "claim_uid": claim_uid,
                "status": status,
                "quality": qual,
                "confidence": conf,
                "rendered": rendered,
                "slot": slot,
                "grounding_evidence_uids": sorted(
                    {
                        self._seen_witness_hashes.get(
                            w.get("content_hash", ""),
                            w.get("witness_id", ""),
                        )
                        for w in wits
                        if (
                            self._seen_witness_hashes.get(
                                w.get("content_hash", ""),
                                w.get("witness_id", ""),
                            )
                            and self.eg.node_exists(
                                self._seen_witness_hashes.get(
                                    w.get("content_hash", ""),
                                    w.get("witness_id", ""),
                                )
                            )
                        )
                    }
                ),
            })
            self._claim_uids.append(claim_uid)
            n_created += 1

        self._phase7a_inferences(rg_root)

        if self.cfg.preserve_graph_structure:
            self._diag(
                "INFO",
                "TRACE_PHASE7B",
                "Skipped claim-to-claim reasoning edges to preserve existing graph structure",
            )
            self._diag(
                "INFO",
                "TRACE_PHASE7C",
                "Skipped claim-to-frame/provenance support edges to preserve existing graph structure",
            )
        else:
            self._phase7b_claim_relations()
            self._phase7c_claim_context_support()
        self._diag("INFO", "TRACE_PHASE7",
                   f"{n_created} Claims created, "
                   f"{n_skipped} slots skipped")
        return rg_root

    def _claim_relation_specs(self) -> List[Dict[str, Any]]:
        """
        Derive a stable set of slot-to-slot reasoning links from the
        currently emitted claim set. These specs drive both lightweight
        claim relations (when enabled) and explicit RG Inference nodes.
        """
        if not self._slot_claim_records:
            return []

        by_type: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for rec in self._slot_claim_records:
            by_type[str(rec.get("slot_type", "")).upper()].append(rec)

        specs: List[Dict[str, Any]] = []
        focal = by_type.get("WHAT", [])
        evidence = by_type.get("EVIDENCE", [])
        how = by_type.get("HOW", [])
        who = by_type.get("WHO", [])
        outcome = by_type.get("OUTCOME", [])
        when = by_type.get("WHEN", [])
        where = by_type.get("WHERE", [])

        if focal:
            what_rec = focal[0]
            for rec in evidence:
                specs.append({
                    "from_rec": rec,
                    "to_rec": what_rec,
                    "rel": "SUPPORTS",
                    "role": "evidence_for_focus",
                    "justification": "Evidence claim supports the focal WHAT claim.",
                })
            for rec in who:
                specs.append({
                    "from_rec": rec,
                    "to_rec": what_rec,
                    "rel": "ABOUT",
                    "role": "actors_for_focus",
                    "justification": "WHO claim identifies the actors attached to the focal WHAT claim.",
                })
            for rec in how:
                specs.append({
                    "from_rec": rec,
                    "to_rec": what_rec,
                    "rel": "ABOUT",
                    "role": "process_for_focus",
                    "justification": "HOW claim describes the responsibility chain around the focal WHAT claim.",
                })
            for rec in when:
                specs.append({
                    "from_rec": rec,
                    "to_rec": what_rec,
                    "rel": "QUALIFIES",
                    "role": "temporal_context_for_focus",
                    "justification": "WHEN claim qualifies the focal WHAT claim with its timeframe.",
                })
            for rec in where:
                specs.append({
                    "from_rec": rec,
                    "to_rec": what_rec,
                    "rel": "QUALIFIES",
                    "role": "spatial_context_for_focus",
                    "justification": "WHERE claim qualifies the focal WHAT claim with its setting.",
                })
            for rec in outcome:
                specs.append({
                    "from_rec": what_rec,
                    "to_rec": rec,
                    "rel": "RESULTED_IN",
                    "role": "focus_to_outcome",
                    "justification": "Focal WHAT claim leads into the OUTCOME claim.",
                })

        if how and outcome:
            for how_rec in how:
                for out_rec in outcome:
                    specs.append({
                        "from_rec": how_rec,
                        "to_rec": out_rec,
                        "rel": "RESULTED_IN",
                        "role": "process_to_outcome",
                        "justification": "HOW responsibility chain leads into the OUTCOME claim.",
                    })

        if evidence and how:
            for ev_rec in evidence:
                for how_rec in how:
                    specs.append({
                        "from_rec": ev_rec,
                        "to_rec": how_rec,
                        "rel": "SUPPORTS",
                        "role": "evidence_for_process",
                        "justification": "Evidence claim supports the HOW responsibility chain.",
                    })

        deduped: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        for spec in specs:
            key = (
                f"{spec['from_rec'].get('claim_uid', '')}|"
                f"{spec['to_rec'].get('claim_uid', '')}|"
                f"{spec['rel']}"
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(spec)
        return deduped

    def _phase7a_inferences(self, rg_root: str) -> None:
        """
        Materialize explicit RG Inference and Defeater nodes from the
        slot-derived claim structure. This keeps the current claim
        statements intact while filling in the core RG node families
        expected by the EG-RG schema and the TRACE contract.
        """
        if not rg_root:
            return

        relation_specs = self._claim_relation_specs()
        if not relation_specs:
            self._diag(
                "INFO",
                "TRACE_PHASE7A",
                "No slot-derived relation specs available for inference generation",
            )
            return

        inference_count = 0
        defeater_count = 0

        for spec in relation_specs:
            premise = spec["from_rec"]
            conclusion = spec["to_rec"]
            premise_uid = premise.get("claim_uid", "")
            conclusion_uid = conclusion.get("claim_uid", "")
            rel = spec.get("rel", "SUPPORTS")
            role = spec.get("role", "")
            if not premise_uid or not conclusion_uid:
                continue

            inference_uid = self._deterministic_uid(
                f"inference::{premise_uid}::{rel}::{conclusion_uid}"
            )
            inference_type = self._inference_type_for_relation(rel, role)
            inference_conf = self._clamp01(
                (
                    float(premise.get("confidence", 0.0) or 0.0)
                    + float(conclusion.get("confidence", 0.0) or 0.0)
                ) / 2.0
            )
            inference_now = self._now()
            inference_label = self._inference_label_for_spec(spec)
            inference_rule_name = self._inference_rule_name_for_spec(spec)

            self.rg.create_node(["Inference"], {
                "uid": inference_uid,
                "type": inference_type,
                "label": inference_label,
                "ruleName": inference_rule_name,
                "ruleFormalExpr": (
                    f"{premise.get('slot_type', 'PREMISE')} "
                    f"{rel} "
                    f"{conclusion.get('slot_type', 'CONCLUSION')}"
                ),
                "ruleSource": "TRACE::Phase7a",
                "confidenceScore": inference_conf,
                "confidenceMethod": "heuristic",
                "confidenceRationale": (
                    f"This inference summarizes the slot-derived relation "
                    f"{premise.get('slot_type', '')} {rel} {conclusion.get('slot_type', '')} "
                    f"with aggregate confidence {inference_conf:.2f}."
                ),
                "confidenceAssessedAt": inference_now,
                "justification": spec.get("justification", ""),
                "performedAt": inference_now,
                "domainMetadata": {
                    "role": role,
                    "premise_slot_id": premise.get("slot_id", ""),
                    "premise_slot_type": premise.get("slot_type", ""),
                    "conclusion_slot_id": conclusion.get("slot_id", ""),
                    "conclusion_slot_type": conclusion.get("slot_type", ""),
                    "mapping_reason": (
                        "Phase 7a: explicit RG inference derived from the "
                        "slot-level reasoning template used for TRACE claims."
                    ),
                },
            })
            self.rg.create_edge(
                rg_root, inference_uid, "CONTAINS_INFERENCE",
                {"uid": self._edge_uid(rg_root, inference_uid, "CONTAINS_INFERENCE")},
            )
            self.rg.create_edge(
                inference_uid, premise_uid, "HAS_PREMISE",
                {
                    "uid": self._edge_uid(inference_uid, premise_uid, "HAS_PREMISE"),
                    "role": str(premise.get("slot_type", "")).lower() or "premise",
                },
            )
            self.rg.create_edge(
                inference_uid, conclusion_uid, "HAS_CONCLUSION",
                {"uid": self._edge_uid(inference_uid, conclusion_uid, "HAS_CONCLUSION")},
            )
            self.rg.create_edge(
                inference_uid, self.cfg.trace_agent_uid, "PERFORMED_BY",
                {"uid": self._edge_uid(inference_uid, self.cfg.trace_agent_uid, "PERFORMED_BY")},
            )
            self.rg.create_edge(
                inference_uid, self.cfg.trace_agent_uid, "CONFIDENCE_ASSESSED_BY",
                {
                    "uid": self._edge_uid(
                        inference_uid,
                        self.cfg.trace_agent_uid,
                        "CONFIDENCE_ASSESSED_BY",
                    ),
                },
            )
            if self.bridge is not self.rg:
                self.bridge.create_node(["Inference"], {
                    "uid": inference_uid,
                    "type": inference_type,
                    "label": inference_label,
                })

            evidence_uids = sorted({
                *list(premise.get("grounding_evidence_uids", []) or []),
                *list(conclusion.get("grounding_evidence_uids", []) or []),
            })
            for evidence_uid in evidence_uids[:4]:
                if not self.eg.node_exists(evidence_uid):
                    continue
                self.bridge.create_edge(
                    inference_uid, evidence_uid, "GROUNDED_BY",
                    {
                        "uid": self._edge_uid(
                            inference_uid,
                            evidence_uid,
                            f"GROUNDED_BY::{evidence_uid}",
                        ),
                        "role": "grounds",
                        "relevance": inference_conf,
                        "justification": (
                            f"Inference {inference_uid} is grounded by evidence "
                            f"already supporting its premise/conclusion claims."
                        ),
                        "evidenceGraphId": self._eg_root_uid,
                    },
                )
            evidence_premise_uid = next(
                (candidate for candidate in evidence_uids if self.eg.node_exists(candidate)),
                "",
            )
            if evidence_premise_uid:
                self.bridge.create_edge(
                    inference_uid,
                    evidence_premise_uid,
                    "HAS_PREMISE",
                    {
                        "uid": self._edge_uid(
                            inference_uid,
                            evidence_premise_uid,
                            "HAS_PREMISE",
                        ),
                        "role": "evidence-premise",
                    },
                )

            self._inference_uids.append(inference_uid)
            inference_count += 1

            defeater_payload = self._defeater_for_inference(spec, inference_conf)
            if not defeater_payload:
                continue
            defeater_uid = self._deterministic_uid(
                f"defeater::{inference_uid}::{defeater_payload['type']}"
            )
            self.rg.create_node(["Defeater"], {
                "uid": defeater_uid,
                "type": defeater_payload["type"],
                "description": defeater_payload["description"],
            })
            self.rg.create_edge(
                rg_root, defeater_uid, "CONTAINS_DEFEATER",
                {
                    "uid": self._edge_uid(
                        rg_root,
                        defeater_uid,
                        "CONTAINS_DEFEATER",
                    ),
                },
            )
            self.rg.create_edge(
                inference_uid, defeater_uid, "HAS_DEFEATER",
                {
                    "uid": self._edge_uid(
                        inference_uid,
                        defeater_uid,
                        "HAS_DEFEATER",
                    ),
                },
            )
            ref_claim_uid = defeater_payload.get("references_claim_uid", "")
            if ref_claim_uid:
                self.rg.create_edge(
                    defeater_uid, ref_claim_uid, "REFERENCES_CLAIM",
                    {
                        "uid": self._edge_uid(
                            defeater_uid,
                            ref_claim_uid,
                            "REFERENCES_CLAIM",
                        ),
                    },
                )
            if self.bridge is not self.rg:
                self.bridge.create_node(["Defeater"], {
                    "uid": defeater_uid,
                    "type": defeater_payload["type"],
                    "description": defeater_payload["description"],
                })
            if evidence_premise_uid:
                self.bridge.create_edge(
                    defeater_uid,
                    evidence_premise_uid,
                    "REFERENCES_EVIDENCE",
                    {
                        "uid": self._edge_uid(
                            defeater_uid,
                            evidence_premise_uid,
                            "REFERENCES_EVIDENCE",
                        ),
                    },
                )
            self._defeater_uids.append(defeater_uid)
            defeater_count += 1

        self._diag(
            "INFO",
            "TRACE_PHASE7A",
            f"{inference_count} Inferences, {defeater_count} Defeaters created",
        )

    @staticmethod
    def _inference_type_for_relation(rel: str, role: str) -> str:
        rel = (rel or "").upper()
        role = (role or "").lower()
        if rel == "RESULTED_IN":
            return "causal"
        if rel == "SUPPORTS":
            return "inductive"
        if rel == "QUALIFIES":
            return "deductive"
        if rel == "ABOUT":
            return "abductive" if "actor" in role else "other"
        return "other"

    @staticmethod
    def _inference_label_for_spec(spec: Dict[str, Any]) -> str:
        premise = spec["from_rec"]
        conclusion = spec["to_rec"]
        return (
            f"{premise.get('slot_type', '')} "
            f"{spec.get('rel', '')} "
            f"{conclusion.get('slot_type', '')}"
        ).strip()

    @staticmethod
    def _inference_rule_name_for_spec(spec: Dict[str, Any]) -> str:
        return (
            f"slot_{str(spec['from_rec'].get('slot_type', '')).lower()}_"
            f"{str(spec.get('rel', '')).lower()}_"
            f"{str(spec['to_rec'].get('slot_type', '')).lower()}"
        )

    def _defeater_for_inference(
        self,
        spec: Dict[str, Any],
        inference_conf: float,
    ) -> Optional[Dict[str, str]]:
        """
        Create a lightweight, generic defeater when the derived
        inference remains only moderately confident. This is a
        non-destructive caution marker, not a hard contradiction.
        """
        rel = str(spec.get("rel", "")).upper()
        if rel not in {"SUPPORTS", "RESULTED_IN"}:
            return None
        if inference_conf >= 0.69:
            return None

        premise = spec["from_rec"]
        conclusion = spec["to_rec"]
        weaker = premise
        if float(conclusion.get("confidence", 0.0) or 0.0) < float(
            premise.get("confidence", 0.0) or 0.0
        ):
            weaker = conclusion

        return {
            "type": "undercutting",
            "description": (
                f"The inference from {premise.get('slot_type', '')} to "
                f"{conclusion.get('slot_type', '')} remains tentative because "
                f"the available support is only moderate "
                f"(aggregate confidence {inference_conf:.2f})."
            ),
            "references_claim_uid": weaker.get("claim_uid", ""),
        }

    def _phase7b_claim_relations(self) -> None:
        """
        Add a lightweight reasoning layer across slot-derived claims.

        WHAT acts as the focal claim when present; evidence supports it,
        WHO/HOW contextualize it, and HOW can lead into OUTCOME.
        """
        if self.cfg.preserve_graph_structure:
            return
        edge_specs = self._claim_relation_specs()
        seen_edges: Set[str] = set()
        created = 0
        for spec in edge_specs:
            edge_key = (
                f"{spec['from_rec'].get('claim_uid', '')}|"
                f"{spec['to_rec'].get('claim_uid', '')}|"
                f"{spec['rel']}"
            )
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)

            self.rg.create_edge(
                spec["from_rec"]["claim_uid"],
                spec["to_rec"]["claim_uid"],
                spec["rel"],
                {
                    "uid": self._edge_uid(
                        spec["from_rec"]["claim_uid"],
                        spec["to_rec"]["claim_uid"],
                        spec["rel"],
                    ),
                    "confidence": 1.0,
                    "justification": spec["justification"],
                    "assertedByUid": self.cfg.trace_agent_uid,
                    "assertedAt": self._now(),
                    "domainMetadata": {
                        "role": spec["role"],
                        "mapping_reason": (
                            "Phase 7b: slot-derived claim relation linking "
                            "WHO/HOW/EVIDENCE/OUTCOME claims around the focal WHAT claim."
                        ),
                    },
                },
            )
            created += 1

        self._claim_relation_count += created
        self._diag(
            "INFO",
            "TRACE_PHASE7B",
            f"{created} claim-to-claim reasoning edges created",
        )

    def _phase7c_claim_context_support(self) -> None:
        """
        Bridge slot-derived claims back to the most relevant FrameWitness
        and its strongest provenance steps.
        """
        if self.cfg.preserve_graph_structure:
            return
        if not self._slot_claim_records or not self._frame_contexts:
            return

        created = 0
        for rec in self._slot_claim_records:
            slot = rec.get("slot", {})
            claim_uid = rec.get("claim_uid", "")
            rendered = rec.get("rendered", {})
            frame = self._best_frame_for_slot(slot)
            if not claim_uid or not frame:
                continue

            frame_uid = frame["frame_uid"]
            if self.eg.node_exists(frame_uid):
                self.bridge.create_edge(
                    claim_uid, frame_uid, "GROUNDED_BY",
                    {
                        "uid": self._edge_uid(claim_uid, frame_uid, "GROUNDED_BY"),
                        "role": "frame_support",
                        "relevance": self._clamp01(
                            float(frame.get("coherence_score", 0.0))
                        ),
                        "excerpt": rendered.get("summary", ""),
                        "justification": (
                            f"FrameWitness {frame_uid} provides the strongest "
                            f"frame-level support for slot {rec.get('slot_id', '')}."
                        ),
                        "evidenceGraphId": self._eg_root_uid,
                        "domainMetadata": {
                            "subgraph_id": frame.get("subgraph_id", ""),
                            "preference_hits": frame.get("_frame_preference_hits", []),
                            "preference_bonus": frame.get("_frame_preference_bonus", 0.0),
                            "slot_type": rec.get("slot_type", ""),
                            "mapping_reason": (
                                "Phase 7c: Claim bridged to its best-matching "
                                "FrameWitness based on anchor/mention/artifact overlap."
                            ),
                        },
                    },
                )
                created += 1

            for event in self._best_frame_events_for_slot(frame, slot, rendered):
                event_uid = event.get("uid", "")
                if not event_uid or not self.eg.node_exists(event_uid):
                    continue
                self.bridge.create_edge(
                    claim_uid, event_uid, "GROUNDED_BY",
                    {
                        "uid": self._edge_uid(
                            claim_uid,
                            event_uid,
                            f"GROUNDED_BY::{event_uid}"
                        ),
                        "role": "provenance_step",
                        "relevance": self._clamp01(float(event.get("_score", 0.0))),
                        "excerpt": event.get("notes", ""),
                        "justification": (
                            f"Provenance step {event_uid} is a high-overlap support "
                            f"event for slot {rec.get('slot_id', '')}."
                        ),
                        "evidenceGraphId": self._eg_root_uid,
                        "domainMetadata": {
                            "frame_uid": frame_uid,
                            "original_label": event.get("label", ""),
                            "preference_hits": event.get("_preference_hits", []),
                            "preference_bonus": event.get("_preference_bonus", 0.0),
                            "slot_type": rec.get("slot_type", ""),
                            "mapping_reason": (
                                "Phase 7c: Claim bridged to a supporting "
                                "ProvenanceEvent selected from the best-matching frame."
                            ),
                        },
                    },
                )
                created += 1

        self._claim_context_link_count += created
        self._diag(
            "INFO",
            "TRACE_PHASE7C",
            f"{created} claim-to-frame/provenance support edges created",
        )

    # ═════════════════════════════════════════════════════════
    #  Phase 8 — IDENTICAL-TO  (Decision 2)
    # ═════════════════════════════════════════════════════════

    def _phase8_identical_to(self) -> None:
        """
        Create OTHER_EVIDENCE_REL edges (with subtype ``IDENTICAL_TO``)
        between artifact segments sharing the same collection prefix.

        The EG schema does not define a first-class ``IDENTICAL_TO``
        edge type — instead, the catch-all ``OTHER_EVIDENCE_REL``
        carries a ``subtype: "IDENTICAL_TO"`` property discriminator.
        See ``pipeline/trace/EG-RG schemas.md`` §1.2.2.

        Fix 4.7: uses chain topology (A→B→C) instead of full mesh
        (A↔B, A↔C, B↔C), reducing from O(n²) to O(n) edges while
        keeping the entire group reachable via traversal.  A safety
        cap ``cfg.max_identical_to_per_collection`` prevents runaway
        edge creation for pathological inputs.
        """

        n = 0
        for coll, ids in self._coll_arts.items():
            if len(ids) < 2:
                continue

            # Safety cap
            if len(ids) > self.cfg.max_identical_to_per_collection:
                self._diag(
                    "WARNING", "TRACE_IDENTICAL_CAP",
                    f"Collection '{coll}' has {len(ids)} segments, "
                    f"capping at {self.cfg.max_identical_to_per_collection}",
                )
                ids = ids[:self.cfg.max_identical_to_per_collection]

            # Chain topology: ids[0]↔ids[1]↔ids[2]↔…
            for i in range(len(ids) - 1):
                a, b = ids[i], ids[i + 1]
                self.eg.create_edge(
                    a, b, "OTHER_EVIDENCE_REL", {
                        "uid":     self._edge_uid(
                                       a, b, "OTHER_EVIDENCE_REL"),
                        "subtype": "IDENTICAL_TO",
                        "confidence": 1.0,
                        "justification": (
                            f"Segments from collection '{coll}': "
                            f"different segment types from the "
                            f"same source document."
                        ),
                        "assertedByUid": self.cfg.trace_agent_uid,
                        "assertedAt":    self._now(),
                        "domainMetadata": {                    # Fix 5.1
                            "collection": coll,
                            "mapping_reason": (
                                "Decision 2: IDENTICAL-TO between "
                                "same-collection segments "
                                "(chain topology, Fix 4.7)."
                            ),
                        },
                    },
                )
                n += 1

        self._diag("INFO", "TRACE_PHASE8",
                   f"{n} IDENTICAL-TO edges created (chain topology)")

    # ═════════════════════════════════════════════════════════
    #  Phase 9 — Schema enrichment
    # ═════════════════════════════════════════════════════════

    def _phase9_schema_completion(
        self,
        eg_root: Optional[str],
        rg_root: Optional[str],
    ) -> None:
        """
        Complete schema-facing optional EG/RG relationships that are
        safe to derive generically from TRACE's emitted nodes.
        """
        if not eg_root:
            return

        reliability_nodes = 0
        created_edges = 0
        integrity_edges = 0
        reliability_edges = 0

        for evidence_uid, evidence_props in self._evidence_nodes.items():
            self.eg.create_edge(
                evidence_uid,
                self.cfg.trace_agent_uid,
                "CREATED_BY",
                {"uid": self._edge_uid(evidence_uid, self.cfg.trace_agent_uid, "CREATED_BY")},
            )
            created_edges += 1

            if evidence_props.get("integrityStatus"):
                self.eg.create_edge(
                    evidence_uid,
                    self.cfg.trace_agent_uid,
                    "INTEGRITY_VERIFIED_BY",
                    {
                        "uid": self._edge_uid(
                            evidence_uid,
                            self.cfg.trace_agent_uid,
                            "INTEGRITY_VERIFIED_BY",
                        ),
                    },
                )
                integrity_edges += 1

            if evidence_props.get("reliabilityScore") is not None:
                self.eg.create_edge(
                    evidence_uid,
                    self.cfg.trace_agent_uid,
                    "RELIABILITY_ASSESSED_BY",
                    {
                        "uid": self._edge_uid(
                            evidence_uid,
                            self.cfg.trace_agent_uid,
                            "RELIABILITY_ASSESSED_BY",
                        ),
                    },
                )
                reliability_edges += 1

            factor_props = self._reliability_factor_payload(evidence_uid, evidence_props)
            factor_uid = factor_props["uid"]
            self.eg.create_node(["ReliabilityFactor"], factor_props)
            self.eg.create_edge(
                evidence_uid,
                factor_uid,
                "HAS_RELIABILITY_FACTOR",
                {"uid": self._edge_uid(evidence_uid, factor_uid, "HAS_RELIABILITY_FACTOR")},
            )
            self._reliability_factor_uids.append(factor_uid)
            reliability_nodes += 1

        # Note: Claim -[CONFIDENCE_ASSESSED_BY]-> Agent edges are
        # emitted by Phase 7 (L1669) at Claim-creation time.  An
        # earlier revision of Phase 9 re-emitted the same edges
        # here; the deterministic _edge_uid made it idempotent but
        # the loop was dead code, so it has been removed.
        # Inference -[CONFIDENCE_ASSESSED_BY]-> Agent edges are
        # still emitted by Phase 7a, which is correct.

        self._diag(
            "INFO",
            "TRACE_PHASE9",
            (
                f"{reliability_nodes} ReliabilityFactor nodes; "
                f"{created_edges} CREATED_BY; "
                f"{integrity_edges} INTEGRITY_VERIFIED_BY; "
                f"{reliability_edges} RELIABILITY_ASSESSED_BY"
            ),
        )

    # ═════════════════════════════════════════════════════════
    #  Utility helpers
    # ═════════════════════════════════════════════════════════

    @staticmethod
    def _collection_of(artifact_id: str) -> str:
        """``artifact::zmyh0257::segment_1_document`` → ``zmyh0257``"""
        parts = artifact_id.split("::")
        return parts[1] if len(parts) >= 2 else ""

    @staticmethod
    def _path_str(path: List[Dict]) -> str:
        """``[{p,0},{para,0}]`` → ``p0.para0``"""
        return ".".join(
            f"{c.get('component_type','?')}{c.get('index',0)}"
            for c in path
        )

    @staticmethod
    def _label_to_action(label: str) -> str:
        """
        Map EvidenceChainStep label → ProvenanceEvent action enum.

        Fix 4.6: uses the expanded ``_ACTION_KEYWORDS`` table
        with keyword-in-string scanning.  Falls back to 'collected'
        as a safe default if no keyword matches.

        Schema-legal values (base + S1 amendment):
            created  collected  transferred  verified  modified
            reviewed  redacted  archived  restored
            linked  retracted  superseded
        """
        lo = label.lower()
        for keyword, action in _ACTION_KEYWORDS.items():
            if keyword in lo:
                return action
        return "collected"

    @staticmethod
    def _clamp01(val: float) -> float:
        """Fix 5.7: clamp a value to the schema-required [0.0, 1.0] range."""
        if val < 0.0:
            return 0.0
        if val > 1.0:
            return 1.0
        return val

    @staticmethod
    def _deterministic_uid(seed: str) -> str:
        """
        Fix 4.5: deterministic UUID5 from a seed string.
        Guarantees the same UID across re-runs for the same input.
        """
        return str(uuid.uuid5(TRACE_NS, seed))

    @staticmethod
    def _edge_uid(from_uid: str, to_uid: str, rel_type: str) -> str:
        """
        Fix 4.5: deterministic edge UID from the (from, to, type)
        triple.  Ensures re-runs produce identical edges that can
        be MERGEd without duplication.
        """
        return str(uuid.uuid5(
            TRACE_NS, f"{from_uid}|{to_uid}|{rel_type}"))

    def _render_slot_claim(self, slot: Dict[str, Any]) -> Dict[str, Any]:
        """
        Turn a slot binding into a more structured, slot-aware claim
        representation.  This keeps the old fallback behavior for
        unhandled slot types, while letting WHO/HOW/EVIDENCE read more
        like responsibility/evidence chains.
        """
        values = list(slot.get("value", []) or [])
        desc = slot.get("description", "")
        stype = slot.get("slot_type", "")
        values_by_var = self._slot_values_by_var(values)
        anchor_groups = self._slot_anchor_groups(values)
        summary = self._generic_slot_summary(values)

        rendered = {
            "statement": self._compose_generic_claim_statement(
                stype, desc, summary
            ),
            "summary": summary,
            "claim_type": "finding",
            "statement_style": "structured_narrative",
            "values_by_var": values_by_var,
            "anchor_groups": anchor_groups,
            "key_entities": self._render_key_entities(values_by_var),
            "statement_quality": {},
            "structured_metadata": {
                "slot_description": desc,
            },
        }

        if stype == "WHO":
            rendered.update(self._render_who_claim(desc, values_by_var, anchor_groups))
        elif stype == "HOW":
            rendered.update(self._render_how_claim(desc, values_by_var, anchor_groups))
        elif stype == "EVIDENCE":
            rendered.update(self._render_evidence_claim(desc, values_by_var, anchor_groups))
        elif stype == "OUTCOME":
            rendered.update(self._render_outcome_claim(desc, values_by_var, anchor_groups))
        elif stype == "WHAT":
            rendered.update(self._render_what_claim(desc, values_by_var, anchor_groups))
        elif stype == "WHEN":
            rendered.update(self._render_when_claim(desc, values_by_var, anchor_groups))
        elif stype == "WHERE":
            rendered.update(self._render_where_claim(desc, values_by_var, anchor_groups))

        rendered["statement"] = self._ensure_terminal_punctuation(
            rendered.get("statement", "")
        )
        rendered["summary"] = self._clean_excerpt(
            rendered.get("summary", ""),
            180,
        )
        rendered["statement_quality"] = self._measure_statement_quality(rendered)
        return rendered

    def _ordered_slots_for_claims(
        self,
        slots: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        indexed = list(enumerate(slots))
        indexed.sort(key=lambda item: self._slot_sort_key(item[1], item[0]))
        return [slot for _, slot in indexed]

    def _slot_sort_key(
        self,
        slot: Dict[str, Any],
        original_index: int,
    ) -> tuple:
        stype = str(slot.get("slot_type", "")).upper()
        qual = str(slot.get("quality", "")).upper()
        conf = float(slot.get("confidence", 0.0) or 0.0)
        sid = slot.get("slot_id", "")
        witness_count = len(slot.get("witnesses", []) or [])
        quality_rank = {
            "GROUNDED": 0,
            "INFERRED": 1,
            "PROPOSED": 2,
            "AMBIGUOUS": 3,
        }.get(qual, 4)
        return (
            self._slot_type_priority(stype),
            quality_rank,
            -self._clamp01(conf),
            -witness_count,
            sid,
            original_index,
        )

    @staticmethod
    def _slot_type_priority(slot_type: str) -> int:
        order = {
            "WHAT": 0,
            "WHO": 1,
            "HOW": 2,
            "EVIDENCE": 3,
            "OUTCOME": 4,
            "WHEN": 5,
            "WHERE": 6,
        }
        return order.get((slot_type or "").upper(), 50)

    def _build_claim_confidence_rationale(
        self,
        *,
        slot: Dict[str, Any],
        status: str,
        rendered: Dict[str, Any],
        support_meta: Dict[str, Any],
    ) -> str:
        qual = str(slot.get("quality", ""))
        conf = float(slot.get("confidence", 0.0) or 0.0)
        witnesses = len(slot.get("witnesses", []) or [])
        quality_phrase = {
            "GROUNDED": "grounded",
            "INFERRED": "inferred",
            "AMBIGUOUS": "ambiguous",
            "PROPOSED": "proposed",
        }.get(qual.upper(), qual.lower() or "unspecified")
        parts = [
            (
                f"This claim is {status} because ALIGN marked the slot as "
                f"{quality_phrase} with confidence {conf:.2f}"
            )
        ]
        if witnesses:
            parts.append(
                f"It is supported by {witnesses} grounding witness"
                f"{'' if witnesses == 1 else 'es'}"
            )
        support_context = support_meta.get("support_context", {})
        if support_context.get("frame_uid"):
            parts.append(
                "The best supporting frame is "
                f"{support_context.get('frame_uid')} "
                f"with coherence {float(support_context.get('coherence_score', 0.0)):.2f}"
            )
        prov_steps = support_meta.get("provenance_support_steps", [])
        if prov_steps:
            labels = [step.get("label", "") for step in prov_steps if step.get("label")]
            if labels:
                parts.append(
                    "Key provenance steps include "
                    f"{self._join_readable(labels, limit=3)}"
                )
        if rendered.get("summary"):
            parts.append(
                f"The retained slot summary is {self._clean_excerpt(rendered['summary'], 90)!r}"
            )
        return ". ".join(part.rstrip(".") for part in parts if part) + "."

    def _build_claim_support_metadata(
        self,
        slot: Dict[str, Any],
        rendered: Dict[str, Any],
    ) -> Dict[str, Any]:
        anchor_groups = rendered.get("anchor_groups", []) or []
        artifact_scope = [
            aid for aid in dict.fromkeys(
                group.get("artifact_id", "") for group in anchor_groups
            ) if aid
        ]
        frame = self._best_frame_for_slot(slot)
        support_steps: List[Dict[str, Any]] = []
        support_context: Dict[str, Any] = {}
        support_summary = ""

        if frame:
            support_context = {
                "frame_uid": frame.get("frame_uid", ""),
                "subgraph_id": frame.get("subgraph_id", ""),
                "coherence_score": frame.get("coherence_score", 0.0),
                "preference_hits": frame.get("_frame_preference_hits", []),
                "preference_bonus": frame.get("_frame_preference_bonus", 0.0),
            }
            for event in self._best_frame_events_for_slot(frame, slot, rendered):
                support_steps.append({
                    "uid": event.get("uid", ""),
                    "label": event.get("label", ""),
                    "relevance": self._clamp01(float(event.get("_score", 0.0))),
                    "preference_hits": event.get("_preference_hits", []),
                    "notes_excerpt": self._clean_excerpt(event.get("notes", ""), 140),
                })
            labels = [step["label"] for step in support_steps if step.get("label")]
            artifact_hint = artifact_scope[0] if artifact_scope else ""
            support_summary = (
                "Primary support comes from frame "
                f"{support_context.get('frame_uid', '')}"
            )
            if artifact_hint:
                support_summary += f" in {artifact_hint}"
            if labels:
                support_summary += (
                    ", especially the provenance steps "
                    f"{self._join_readable(labels, limit=4)}"
                )
            support_summary += "."

        return {
            "primary_artifact_id": artifact_scope[0] if artifact_scope else "",
            "artifact_scope": artifact_scope,
            "primary_anchor_address": (
                anchor_groups[0].get("anchor_address", "")
                if anchor_groups else ""
            ),
            "support_context": support_context,
            "support_summary": support_summary,
            "provenance_support_steps": support_steps,
        }

    @staticmethod
    def _compose_generic_claim_statement(
        slot_type: str,
        description: str,
        summary: str,
    ) -> str:
        label = (slot_type or "CLAIM").upper()
        desc = (description or "").strip()
        if desc:
            desc = desc[0].upper() + desc[1:]
        if summary:
            return f"{label}: {desc}. Key values: {summary}"
        if desc:
            return f"{label}: {desc}"
        return f"{label}: No structured values were retained"

    @staticmethod
    def _ensure_terminal_punctuation(text: str) -> str:
        text = (text or "").strip()
        if not text:
            return text
        if text[-1] in ".!?":
            return text
        return text + "."

    @staticmethod
    def _render_key_entities(values_by_var: Dict[str, List[str]]) -> List[str]:
        ordered: List[str] = []
        seen: Set[str] = set()
        for vals in values_by_var.values():
            for val in vals:
                norm = val.lower()
                if norm in seen:
                    continue
                seen.add(norm)
                ordered.append(val)
        return ordered[:8]

    def _measure_statement_quality(
        self,
        rendered: Dict[str, Any],
    ) -> Dict[str, Any]:
        statement = rendered.get("statement", "") or ""
        summary = rendered.get("summary", "") or ""
        key_entities = rendered.get("key_entities", []) or []
        anchor_groups = rendered.get("anchor_groups", []) or []
        return {
            "statement_length": len(statement),
            "summary_length": len(summary),
            "key_entity_count": len(key_entities),
            "anchor_group_count": len(anchor_groups),
            "has_summary": bool(summary),
            "has_chain_language": "chain" in statement.lower(),
            "style": rendered.get("statement_style", "structured_narrative"),
        }

    @staticmethod
    def _slot_values_by_var(values: List[Dict[str, Any]]) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = defaultdict(list)
        seen: Dict[str, Set[str]] = defaultdict(set)
        for val in values:
            var = val.get("var", "?")
            surface = str(val.get("surface", "")).strip()
            if not surface:
                continue
            key = surface.lower()
            if key in seen[var]:
                continue
            seen[var].add(key)
            out[var].append(surface)
        return dict(out)

    def _slot_anchor_groups(self, values: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        groups: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []
        for val in values:
            anchor = val.get("anchor_address", "") or ""
            if not anchor:
                anchor = f"unknown::{len(order)}"
            if anchor not in groups:
                groups[anchor] = {
                    "anchor_address": anchor,
                    "artifact_id": self._artifact_from_anchor_address(anchor),
                    "vars": defaultdict(list),
                    "raw_text": val.get("raw_text", ""),
                }
                order.append(anchor)
            var = val.get("var", "?")
            surface = str(val.get("surface", "")).strip()
            if surface and surface not in groups[anchor]["vars"][var]:
                groups[anchor]["vars"][var].append(surface)
        final: List[Dict[str, Any]] = []
        for anchor in order:
            item = groups[anchor]
            item["vars"] = dict(item["vars"])
            final.append(item)
        return final

    @staticmethod
    def _artifact_from_anchor_address(anchor_address: str) -> str:
        if ".body." in anchor_address:
            return anchor_address.split(".body.", 1)[0]
        if ".header" in anchor_address:
            return anchor_address.split(".header", 1)[0]
        return anchor_address

    @staticmethod
    def _first_value(values_by_var: Dict[str, List[str]], var: str) -> str:
        vals = values_by_var.get(var, [])
        return vals[0] if vals else ""

    @staticmethod
    def _first_value_any(values_by_var: Dict[str, List[str]], *vars: str) -> str:
        for var in vars:
            vals = values_by_var.get(var, [])
            if vals:
                return vals[0]
        return ""

    @staticmethod
    def _values_for_any(values_by_var: Dict[str, List[str]], *vars: str) -> List[str]:
        ordered: List[str] = []
        seen: Set[str] = set()
        for var in vars:
            for value in values_by_var.get(var, []):
                norm = value.lower()
                if norm in seen:
                    continue
                seen.add(norm)
                ordered.append(value)
        return ordered

    @staticmethod
    def _join_readable(values: List[str], limit: int = 3) -> str:
        vals = [v for v in values if v]
        if not vals:
            return ""
        vals = vals[:limit]
        if len(vals) == 1:
            return vals[0]
        if len(vals) == 2:
            return f"{vals[0]} and {vals[1]}"
        return ", ".join(vals[:-1]) + f", and {vals[-1]}"

    @staticmethod
    def _clean_excerpt(text: str, limit: int = 220) -> str:
        text = " ".join((text or "").split())
        if len(text) <= limit:
            return text
        return text[: limit - 3].rstrip() + "..."

    @staticmethod
    def _artifact_label_from_id(artifact_id: str) -> str:
        raw = str(artifact_id or "")
        if raw.startswith("artifact::"):
            raw = raw[len("artifact::") :]
        for marker in (".p", ".body", ".header", ".slide", ".msg"):
            if marker in raw:
                raw = raw.split(marker, 1)[0]
                break
        return raw.replace("_", " ").strip()

    def _extract_date_from_anchor_groups(
        self,
        anchor_groups: List[Dict[str, Any]],
    ) -> str:
        for group in anchor_groups:
            raw = str(group.get("raw_text", ""))
            iso_match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", raw)
            if iso_match:
                return iso_match.group(1)
            month_match = re.search(
                r"\b("
                r"January|February|March|April|May|June|July|August|September|October|November|December"
                r")\s+\d{1,2},\s+\d{4}\b",
                raw,
                re.IGNORECASE,
            )
            if month_match:
                return month_match.group(0)
        return ""

    def _trace_condition_values(
        self,
        values: List[str],
        *,
        exclude_terms: Optional[Set[str]] = None,
    ) -> List[str]:
        generic_terms = {
            "associated condition",
            "associated conditions",
            "complication",
            "complications",
            "condition",
            "conditions",
            "disease",
            "diseases",
            "disorder",
            "disorders",
            "medical finding",
            "medical findings",
            "symptom",
            "symptoms",
        }
        noise_pattern = re.compile(
            r"\b(?:audit(?:-c)?|questionnaire|screen(?:er)?|index|itq|pcl-?5|phq-?8|phq-?9|gad-?7)\b",
            re.IGNORECASE,
        )
        condition_pattern = re.compile(
            r"\b(?:"
            r"anxiety(?:\s+disorder)?|"
            r"cardiovascular\s+disease|"
            r"complex\s+post-?traumatic\s+stress\s+disorder|"
            r"concussion|"
            r"cptsd|"
            r"depression|"
            r"diabetes|"
            r"dsm-?iv(?:\s+\w+){0,2}\s+disorders?|"
            r"generalized\s+anxiety\s+disorder|"
            r"insomnia|"
            r"major\s+depression|"
            r"major\s+depressive\s+disorder|"
            r"mild\s+traumatic\s+brain\s+injury|"
            r"obesity|"
            r"post-?concussive\s+symptoms?|"
            r"post-?traumatic\s+stress\s+disorder|"
            r"posttraumatic\s+stress\s+disorder|"
            r"ptsd|"
            r"short\s+sleep|"
            r"sleep\s+disturbances?|"
            r"traumatic\s+brain\s+injury"
            r")\b",
            re.IGNORECASE,
        )

        cleaned: List[str] = []
        seen: Set[str] = set()
        excluded = {term.lower() for term in (exclude_terms or set()) if term}
        for value in values:
            surface = " ".join(str(value or "").split())
            if not surface:
                continue
            normalized = re.sub(r"[^a-z0-9\s-]", "", surface.lower()).replace("-", " ")
            normalized = " ".join(normalized.split())
            if not normalized or normalized in excluded:
                continue
            if normalized in generic_terms:
                continue
            if noise_pattern.search(surface):
                continue
            if not condition_pattern.search(surface):
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            cleaned.append(surface)
        return cleaned

    def _generic_slot_summary(self, values: List[Dict[str, Any]]) -> str:
        pairs: List[str] = []
        seen: Set[str] = set()
        for val in values[:6]:
            item = f"{val.get('var', '?')}={val.get('surface', '?')}"
            if item in seen:
                continue
            seen.add(item)
            pairs.append(item)
        return "; ".join(pairs)

    def _render_who_claim(
        self,
        desc: str,
        values_by_var: Dict[str, List[str]],
        anchor_groups: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        people = self._values_for_any(values_by_var, "P")
        person = people[0] if people else ""
        role = self._first_value(values_by_var, "R")
        org = self._first_value(values_by_var, "O")

        statement = f"[WHO] {desc}"
        if len(people) > 1 and role and org:
            statement = (
                "WHO: The retrieved evidence is associated with "
                f"{self._join_readable(people[:3], limit=3)}, "
                f"serving as {role} at {org}"
            )
        elif len(people) > 1 and role:
            statement = (
                "WHO: The retrieved evidence is associated with "
                f"{self._join_readable(people[:3], limit=3)}, "
                f"serving as {role}"
            )
        elif len(people) > 1:
            statement = (
                "WHO: The retrieved evidence was authored by "
                f"{self._join_readable(people[:3], limit=3)}"
            )
        elif person and role and org:
            statement = (
                f"WHO: {person} was identified as an associated author or researcher, "
                f"serving as {role} at {org}"
            )
        elif person and role:
            statement = (
                f"WHO: {person} was identified as an associated author or researcher, "
                f"serving as {role}"
            )
        elif person:
            statement = f"WHO: {person} was identified as an associated author or researcher"
        elif role:
            statement = f"WHO: The associated role was {role}"

        return {
            "statement": statement,
            "summary": self._join_readable(
                [v for v in [*people[:3], role, org] if v]
            ) or desc,
            "structured_metadata": {
                "responsibility_chain": {
                    "person": person,
                    "people": people[:3],
                    "role": role,
                    "organization": org,
                    "anchor_group": anchor_groups[0] if anchor_groups else {},
                },
            },
        }

    def _render_how_claim(
        self,
        desc: str,
        values_by_var: Dict[str, List[str]],
        anchor_groups: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        person = self._first_value(values_by_var, "P")
        role = self._first_value(values_by_var, "R")
        org = self._first_value(values_by_var, "O")
        response = self._first_value(values_by_var, "RESP")
        claim = self._first_value(values_by_var, "CL")

        chain = [v for v in [person, role, org, response] if v]
        statement = self._compose_generic_claim_statement("HOW", desc, "")
        if chain:
            statement = "HOW: Responsibility chain: " + " -> ".join(chain)
            if claim:
                statement += (
                    f"; concern: \"{self._clean_excerpt(claim, 160)}\""
                )

        return {
            "statement": statement,
            "summary": " -> ".join(chain) if chain else desc,
            "structured_metadata": {
                "responsibility_chain": {
                    "person": person,
                    "role": role,
                    "organization": org,
                    "response": response,
                    "claim": claim,
                    "anchor_group": anchor_groups[0] if anchor_groups else {},
                },
            },
        }

    def _render_evidence_claim(
        self,
        desc: str,
        values_by_var: Dict[str, List[str]],
        anchor_groups: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        documents = self._values_for_any(values_by_var, "A", "D")
        if not documents:
            documents = [
                self._artifact_label_from_id(group.get("artifact_id", ""))
                for group in anchor_groups
                if group.get("artifact_id")
            ]
            documents = [doc for doc in documents if doc]
        claim = self._first_value(values_by_var, "CL")
        policy = self._first_value(values_by_var, "POL")
        text_span = self._first_value(values_by_var, "TS")
        risk = self._first_value_any(values_by_var, "RF", "C")
        event = self._first_value_any(values_by_var, "EV", "E")

        support_items = [
            self._clean_excerpt(v, 120)
            for v in [claim, policy, risk, event]
            if v
        ]
        statement = self._compose_generic_claim_statement("EVIDENCE", desc, "")
        if documents and support_items:
            statement = (
                "EVIDENCE: Supporting evidence comes from "
                f"{self._join_readable([self._clean_excerpt(doc, 90) for doc in documents], limit=2)}, "
                f"including {self._join_readable(support_items, limit=3)}"
            )
        elif documents:
            statement = (
                "EVIDENCE: Supporting evidence comes from "
                f"{self._join_readable([self._clean_excerpt(doc, 90) for doc in documents], limit=2)}"
            )
        elif support_items:
            statement = (
                "EVIDENCE: The strongest supporting material includes "
                f"{self._join_readable(support_items, limit=3)}"
            )
        if text_span and text_span not in {claim, risk}:
            statement += f"; quoted span: \"{self._clean_excerpt(text_span, 140)}\""

        return {
            "statement": statement,
            "summary": (
                self._join_readable(
                    [self._clean_excerpt(v, 70) for v in documents + support_items],
                    limit=4,
                )
                if (documents or support_items)
                else desc
            ),
            "structured_metadata": {
                "evidence_chain": {
                    "document": documents[0] if documents else "",
                    "claim": claim,
                    "policy": policy,
                    "quoted_span": text_span,
                    "risk_finding": risk,
                    "event": event,
                    "anchor_groups": anchor_groups[:3],
                },
            },
        }

    def _render_outcome_claim(
        self,
        desc: str,
        values_by_var: Dict[str, List[str]],
        anchor_groups: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        response = self._first_value(values_by_var, "RESP")
        claim = self._first_value(values_by_var, "CL")
        policy = self._first_value(values_by_var, "POL")
        strategy = self._first_value(values_by_var, "S")
        statement = self._compose_generic_claim_statement("OUTCOME", desc, "")
        if response:
            statement = f"OUTCOME: Response '{response}'"
            if strategy:
                statement += f" for {strategy}"
            if policy:
                statement += f" under {policy}"
            if claim:
                statement += f"; unresolved concern: \"{self._clean_excerpt(claim, 150)}\""
            statement += "."
        return {
            "statement": statement,
            "summary": self._join_readable(
                [v for v in [response, strategy, policy] if v]
            ) or desc,
            "structured_metadata": {
                "outcome_chain": {
                    "response": response,
                    "claim": claim,
                    "policy": policy,
                    "strategy": strategy,
                    "anchor_groups": anchor_groups[:2],
                },
            },
        }

    def _render_what_claim(
        self,
        desc: str,
        values_by_var: Dict[str, List[str]],
        anchor_groups: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        strategy = self._first_value_any(values_by_var, "S")
        document = self._first_value_any(values_by_var, "A", "D")
        primary_condition = self._first_value_any(values_by_var, "T")
        focus_conditions = self._trace_condition_values(
            self._values_for_any(values_by_var, "C", "RF", "CL"),
            exclude_terms={primary_condition} if primary_condition else None,
        )
        statement = self._compose_generic_claim_statement("WHAT", desc, "")
        if focus_conditions:
            prefix = "WHAT: The retrieved evidence links"
            if primary_condition:
                prefix += f" {primary_condition}"
            prefix += " to "
            statement = prefix + self._join_readable(focus_conditions, limit=6)
        elif strategy:
            statement = f"WHAT: The focal strategy/document was {strategy}"
        elif document:
            statement = f"WHAT: The focal document was {document}"
        return {
            "statement": statement,
            "summary": (
                self._join_readable(focus_conditions, limit=6)
                if focus_conditions
                else (strategy or document or desc)
            ),
            "structured_metadata": {
                "focus_object": {
                    "strategy": strategy,
                    "document": document,
                    "primary_condition": primary_condition,
                    "conditions": focus_conditions,
                    "anchor_group": anchor_groups[0] if anchor_groups else {},
                },
            },
        }

    def _render_when_claim(
        self,
        desc: str,
        values_by_var: Dict[str, List[str]],
        anchor_groups: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        event = self._first_value_any(values_by_var, "EV", "E")
        date_value = self._first_value_any(values_by_var, "D", "DATE")
        if not date_value:
            date_value = self._extract_date_from_anchor_groups(anchor_groups)
        timeframe = self._join_readable(
            [v for v in [event, date_value] if v],
            limit=2,
        )
        statement = self._compose_generic_claim_statement("WHEN", desc, "")
        if (
            event
            and self._trace_condition_values([event])
            and date_value
        ):
            statement = (
                f"WHEN: The relevant timeframe centers on {event}, dated {date_value}"
            )
        elif event and date_value:
            statement = f"WHEN: The supporting document is dated {date_value} and notes {event}"
        elif event:
            statement = f"WHEN: The relevant timeframe centers on {event}"
        elif date_value:
            statement = f"WHEN: The relevant date or period is {date_value}"
        return {
            "statement": statement,
            "summary": timeframe or desc,
            "structured_metadata": {
                "time_context": {
                    "event": event,
                    "date": date_value,
                    "anchor_group": anchor_groups[0] if anchor_groups else {},
                },
            },
        }

    def _render_where_claim(
        self,
        desc: str,
        values_by_var: Dict[str, List[str]],
        anchor_groups: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        location = self._first_value(values_by_var, "L")
        gpe = self._first_value(values_by_var, "GPE")
        org = self._first_value(values_by_var, "O")
        place = self._join_readable(
            [v for v in [location, gpe, org] if v],
            limit=3,
        )
        statement = self._compose_generic_claim_statement("WHERE", desc, "")
        if location and org:
            statement = (
                f"WHERE: The activity is situated at {location} within {org}"
            )
        elif place:
            statement = f"WHERE: The relevant setting is {place}"
        return {
            "statement": statement,
            "summary": place or desc,
            "structured_metadata": {
                "location_context": {
                    "location": location,
                    "gpe": gpe,
                    "organization": org,
                    "anchor_group": anchor_groups[0] if anchor_groups else {},
                },
            },
        }

    def _best_frame_for_slot(self, slot: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        signal = self._slot_signal_ids(slot)
        slot_type = str(slot.get("slot_type", "")).upper()
        prefs = self._slot_frame_preferences(slot_type)
        rendered = self._render_slot_claim(slot)
        slot_text = " ".join(filter(None, [
            slot.get("description", ""),
            rendered.get("summary", ""),
            rendered.get("statement", ""),
        ]))
        best: Optional[Dict[str, Any]] = None
        best_score = 0.0
        for frame in self._frame_contexts.values():
            mention_overlap = len(signal["mention_ids"] & frame["mention_ids"])
            anchor_overlap = len(signal["anchor_ids"] & frame["anchor_ids"])
            artifact_overlap = len(signal["artifact_ids"] & frame["artifact_ids"])
            pref_bonus = 0.0
            pref_hits: List[str] = []

            frame_desc = " ".join(filter(None, [
                frame.get("subgraph_id", ""),
                " ".join(
                    str(e.get("notes", "")) for e in frame.get("events", [])[:4]
                ),
            ]))
            pref_bonus += 1.5 * self._token_jaccard_text(slot_text, frame_desc)

            for edge_name, weight in prefs.get("edges", {}).items():
                if frame.get("edge_satisfactions", {}).get(edge_name):
                    pref_bonus += weight
                    pref_hits.append(edge_name)

            for var_name, weight in prefs.get("vars", {}).items():
                if var_name in frame.get("available_vars", set()):
                    pref_bonus += weight
                    pref_hits.append(f"var:{var_name}")

            score = (
                3.0 * mention_overlap
                + 2.0 * anchor_overlap
                + 1.0 * artifact_overlap
                + 0.25 * float(frame.get("coherence_score", 0.0))
                + pref_bonus
            )
            if score > best_score:
                best_score = score
                best = {
                    **frame,
                    "_frame_preference_hits": pref_hits,
                    "_frame_preference_bonus": pref_bonus,
                }
        return best if best_score > 0.0 else None

    def _best_frame_events_for_slot(
        self,
        frame: Dict[str, Any],
        slot: Dict[str, Any],
        rendered: Dict[str, Any],
        limit: int = 2,
    ) -> List[Dict[str, Any]]:
        signal = self._slot_signal_ids(slot)
        slot_type = str(slot.get("slot_type", "")).upper()
        preferred = self._slot_event_preferences(slot_type)
        slot_text = " ".join(filter(None, [
            slot.get("description", ""),
            rendered.get("summary", ""),
            rendered.get("statement", ""),
        ]))
        ranked: List[Dict[str, Any]] = []
        for event in frame.get("events", []):
            score = 0.0
            if event.get("mention_id") in signal["mention_ids"]:
                score += 3.0
            if event.get("anchor_id") in signal["anchor_ids"]:
                score += 2.0
            if event.get("artifact_id") in signal["artifact_ids"]:
                score += 0.5
            score += 2.0 * self._token_jaccard_text(
                slot_text,
                event.get("notes", ""),
            )
            label_lower = str(event.get("label", "")).lower()
            pref_bonus = 0.0
            pref_hits: List[str] = []
            for keyword, weight in preferred.items():
                if keyword in label_lower:
                    pref_bonus += weight
                    pref_hits.append(keyword)
            score += pref_bonus
            if score <= 0.0:
                continue
            ranked.append({
                **event,
                "_score": self._clamp01(score / 5.0),
                "_preference_hits": pref_hits,
                "_preference_bonus": pref_bonus,
            })

        ranked.sort(
            key=lambda e: (
                -float(e.get("_score", 0.0)),
                -float(e.get("_preference_bonus", 0.0)),
                e.get("uid", ""),
            )
        )
        dynamic_limit = 3 if slot_type in {"EVIDENCE", "HOW"} else limit
        return ranked[:dynamic_limit]

    @staticmethod
    def _slot_event_preferences(slot_type: str) -> Dict[str, float]:
        slot_type = (slot_type or "").upper()
        if slot_type == "WHO":
            return {
                "actor identified": 1.8,
                "variable 'r' bound": 1.5,
                "organization identified": 0.8,
            }
        if slot_type == "HOW":
            return {
                "variable 'resp' bound": 1.8,
                "organization identified": 1.4,
                "variable 'r' bound": 1.2,
                "claim documented": 0.9,
            }
        if slot_type == "EVIDENCE":
            return {
                "source document established": 1.7,
                "claim documented": 1.9,
                "policy identified": 1.6,
                "risk finding surfaced": 1.5,
                "variable 'ts' bound": 1.3,
                "event identified": 0.8,
            }
        if slot_type == "OUTCOME":
            return {
                "variable 'resp' bound": 1.8,
                "claim documented": 1.2,
                "policy identified": 0.8,
                "strategy documented": 0.8,
            }
        if slot_type == "WHAT":
            return {
                "strategy documented": 1.9,
                "claim documented": 1.6,
                "source document established": 1.5,
                "topic identified": 1.4,
            }
        if slot_type == "WHEN":
            return {
                "event identified": 1.8,
                "source document established": 1.0,
                "claim documented": 0.6,
            }
        return {}

    @staticmethod
    def _slot_frame_preferences(slot_type: str) -> Dict[str, Dict[str, float]]:
        slot_type = (slot_type or "").upper()
        if slot_type == "WHO":
            return {
                "edges": {
                    "D-[AUTHORED_BY]->P": 1.8,
                    "A-[AUTHORED_BY]->P": 1.8,
                    "P-[AFFILIATED_WITH]->R": 1.8,
                },
                "vars": {
                    "D": 1.0,
                    "P": 1.2,
                    "R": 1.2,
                },
            }
        if slot_type == "WHAT":
            return {
                "edges": {
                    "CL-[ABOUT]->C": 2.2,
                    "CL-[ABOUT]->T": 2.0,
                    "CL-[EVIDENCED_BY]->D": 1.4,
                    "CL-[ABOUT]->S": 2.0,
                    "A-[SUPPORTS]->CL": 1.0,
                },
                "vars": {
                    "C": 1.7,
                    "T": 1.2,
                    "CL": 1.0,
                    "D": 0.8,
                    "S": 1.5,
                    "A": 1.0,
                },
            }
        if slot_type == "HOW":
            return {
                "edges": {
                    "RESP-[RESULTED_IN]->CL": 1.8,
                    "P-[AFFILIATED_WITH]->R": 0.9,
                },
                "vars": {
                    "RESP": 1.5,
                    "R": 1.0,
                    "O": 1.0,
                    "CL": 0.8,
                },
            }
        if slot_type == "EVIDENCE":
            return {
                "edges": {
                    "CL-[EVIDENCED_BY]->D": 2.0,
                    "CL-[ABOUT]->C": 1.4,
                    "CL-[ABOUT]->T": 1.2,
                    "D-[HAS_TIME]->E": 0.9,
                    "A-[SUPPORTS]->CL": 1.9,
                    "CL-[CONTRADICTS]->POL": 1.7,
                    "CL-[QUOTES_SPAN]->TS": 1.6,
                    "TS-[EVIDENCED_BY]->A": 1.4,
                    "CL-[ABOUT]->RF": 1.6,
                    "RF-[HAS_TIME]->EV": 1.1,
                },
                "vars": {
                    "D": 1.0,
                    "C": 0.8,
                    "T": 0.8,
                    "E": 0.6,
                    "A": 1.0,
                    "CL": 1.2,
                    "POL": 1.1,
                    "TS": 1.0,
                    "RF": 1.0,
                    "EV": 0.7,
                },
            }
        if slot_type == "WHEN":
            return {
                "edges": {
                    "D-[HAS_TIME]->E": 2.0,
                    "CL-[EVIDENCED_BY]->D": 0.9,
                },
                "vars": {
                    "E": 1.6,
                    "D": 1.2,
                },
            }
        if slot_type == "OUTCOME":
            return {
                "edges": {
                    "RESP-[RESULTED_IN]->CL": 1.9,
                    "CL-[ABOUT]->S": 0.9,
                },
                "vars": {
                    "RESP": 1.4,
                    "CL": 1.0,
                    "POL": 0.8,
                    "S": 0.8,
                },
            }
        return {"edges": {}, "vars": {}}

    def _slot_signal_ids(self, slot: Dict[str, Any]) -> Dict[str, Set[str]]:
        mention_ids: Set[str] = set()
        anchor_ids: Set[str] = set()
        artifact_ids: Set[str] = set()

        for wit in slot.get("witnesses", []) or []:
            anchor = wit.get("anchor", {}) or {}
            mention = wit.get("mention", {}) or {}
            anchor_id = anchor.get("anchor_id", "")
            mention_id = mention.get("mention_id", "")
            artifact_id = anchor.get("artifact_id", "")
            if anchor_id:
                anchor_ids.add(anchor_id)
            if mention_id:
                mention_ids.add(mention_id)
            if artifact_id:
                artifact_ids.add(artifact_id)

        for val in slot.get("value", []) or []:
            anchor_address = val.get("anchor_address", "")
            artifact_id = self._artifact_from_anchor_address(anchor_address)
            if artifact_id:
                artifact_ids.add(artifact_id)

        return {
            "mention_ids": mention_ids,
            "anchor_ids": anchor_ids,
            "artifact_ids": artifact_ids,
        }

    @staticmethod
    def _token_jaccard_text(a: str, b: str) -> float:
        ta = set(re.findall(r"\b\w+\b", (a or "").lower()))
        tb = set(re.findall(r"\b\w+\b", (b or "").lower()))
        if not ta and not tb:
            return 1.0
        if not ta or not tb:
            return 0.0
        return len(ta & tb) / len(ta | tb)

    def _collect_all_witnesses(self, result: Dict) -> List[Dict]:
        """
        Fix 4.4: robust witness collection.

        Tries ``result["all_witnesses"]`` first (preferred — flat
        list pre-assembled by the ALIGN operator).  Falls back to
        iterating ``slot_bindings[*].witnesses[*]``, deduplicating
        by ``witness_id`` to avoid double-processing.
        """
        if "all_witnesses" in result and result["all_witnesses"]:
            return result["all_witnesses"]

        self._diag("INFO", "TRACE_WITNESS_FALLBACK",
                   "all_witnesses absent; collecting from slot_bindings")

        seen: Set[str] = set()
        collected: List[Dict] = []
        for slot in result.get("slot_bindings", []):
            for w in slot.get("witnesses", []):
                wid = w.get("witness_id", "")
                if wid and wid not in seen:
                    seen.add(wid)
                    collected.append(w)
        return collected

    def _log_map(
        self, src_type: str, src_id: str,
        tgt_type: str, tgt_id: str, reason: str,
    ) -> None:
        """Append an entry to the MAPS_TO audit log."""
        self._maps_log.append({
            "source_type":   src_type,
            "source_id":     src_id,
            "target_type":   tgt_type,
            "target_id":     tgt_id,
            "mapping_reason": reason,
            "timestamp":     self._now(),
        })

    def _diag(
        self, severity: str, code: str, message: str,
        context: Dict | None = None,
    ) -> None:
        entry = {
            "severity": severity, "code": code,
            "message": message, "context": context or {},
            "timestamp": self._now(),
        }
        self._diags.append(entry)
        getattr(logger, severity.lower(), logger.info)(
            f"[{code}] {message}"
        )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _finalise(
        self, eg_root: Optional[str], rg_root: Optional[str],
    ) -> TraceResult:
        return TraceResult(
            eg_root_uid=eg_root or "",
            rg_root_uid=rg_root,
            evidence_node_uids=list(self._ev_uids),
            claim_uids=list(self._claim_uids),
            maps_to_log=list(self._maps_log),
            diagnostics=list(self._diags),
            stats={
                "artifacts":       len(self._seen_artifacts),
                "anchors":         len(self._seen_anchors),
                "mentions":        len(self._seen_mentions),
                "suppressed_mentions": self._suppressed_mention_total,
                "mention_hypotheses": len(self._mention_hypotheses),
                "witnesses_kept":  len(self._seen_witness_hashes),
                "snapshot_nodes":  len(self._seen_snapshots),
                "frame_witnesses": len(self._seen_frame_witnesses),
                "reliability_factors": len(self._reliability_factor_uids),
                "claims":          len(self._claim_uids),
                "inferences":      len(self._inference_uids),
                "defeaters":       len(self._defeater_uids),
                "claim_relations": self._claim_relation_count,
                "claim_context_links": self._claim_context_link_count,
                "coref_groups":    sum(
                    1 for v in self._entity_mentions.values()
                    if len(v) >= 2
                ),
                "maps_to_entries": len(self._maps_log),
                "evidence_total":  len(self._ev_uids),
            },
        )


# ═══════════════════════════════════════════════════════════════
#  Convenience entry-point and CLI
# ═══════════════════════════════════════════════════════════════

def _drop_shadow_claim_nodes_for_bundle(
    eg_writer: GraphWriter,
    rg_writer: GraphWriter,
) -> int:
    """Remove split-DB bridge Claim shadows from in-memory EG bundle output."""
    eg_nodes = getattr(eg_writer, "nodes", None)
    rg_nodes = getattr(rg_writer, "nodes", None)
    if not isinstance(eg_nodes, list) or not isinstance(rg_nodes, list):
        return 0

    real_claim_uids = {
        node.get("properties", {}).get("uid", "")
        for node in rg_nodes
        if "Claim" in node.get("labels", [])
    }
    if not real_claim_uids:
        return 0

    def is_shadow_claim(node: Dict[str, Any]) -> bool:
        return (
            "Claim" in node.get("labels", [])
            and node.get("properties", {}).get("uid", "") in real_claim_uids
        )

    filtered = [node for node in eg_nodes if not is_shadow_claim(node)]
    dropped = len(eg_nodes) - len(filtered)
    if dropped:
        eg_writer.nodes = filtered
    return dropped


def generate_trace_bundle(
    align_bundle: Dict[str, Any] | str,
    cfg: TraceConfig | None = None,
    eg_writer: GraphWriter | None = None,
    rg_writer: GraphWriter | None = None,
    bridge_writer: GraphWriter | None = None,
    *,
    validate: bool = True,
) -> Dict[str, Any]:
    """Run TRACE and return the canonical chain-first TraceBundle."""
    bundle = json.loads(align_bundle) if isinstance(align_bundle, str) else align_bundle
    eg = eg_writer or InMemoryGraphWriter()
    rg = rg_writer or InMemoryGraphWriter()
    bridge = bridge_writer or eg
    trace_config = cfg or TraceConfig()

    trace = Trace(eg=eg, rg=rg, bridge=bridge, cfg=trace_config)
    result = trace.execute(bundle)
    _drop_shadow_claim_nodes_for_bundle(eg, rg)
    trace_bundle = build_trace_bundle(
        align_bundle=bundle,
        trace_result=result,
        eg_writer=eg,
        rg_writer=rg,
        trace_config=trace_config,
    )
    if validate:
        errors = validate_trace_bundle(trace_bundle)
        if errors:
            preview = "; ".join(errors[:5])
            raise ValueError(f"TraceBundle validation failed: {preview}")
    return trace_bundle


def run_trace(
    bundle_json: str,
    cfg: TraceConfig | None = None,
    use_memgraph: bool = False,
    mg_host: str = "127.0.0.1",
    mg_port: int = 7687,
    mg_user: str = "",
    mg_password: str = "",
) -> TraceResult:
    """
    One-liner to execute TRACE on a raw JSON string.

    Parameters
    ----------
    bundle_json : str
        The AlignBundle JSON.
    cfg : TraceConfig, optional
    use_memgraph : bool
        If True, write to a live Memgraph instance.
    mg_host, mg_port : str, int
        Memgraph connection coordinates (only when use_memgraph=True).

    Returns
    -------
    TraceResult
    """
    bundle = json.loads(bundle_json)

    if use_memgraph:
        writer = MemgraphWriter(
            host=mg_host,
            port=mg_port,
            username=mg_user,
            password=mg_password,
        )
        # Single-database: all three point at the same instance
        eg = rg = bridge = writer
    else:
        eg = InMemoryGraphWriter()
        rg = InMemoryGraphWriter()
        bridge = eg   # Fix 4.1: bridge defaults to eg

    trace = Trace(eg=eg, rg=rg, bridge=bridge, cfg=cfg)
    return trace.execute(bundle)


# ── CLI ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
    )

    if len(sys.argv) < 2:
        print("Usage:  python trace.py <align_bundle.json> "
              "[--memgraph] [--dump-maps]")
        sys.exit(1)

    path = sys.argv[1]
    use_mg = "--memgraph" in sys.argv

    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()

    res = run_trace(raw, use_memgraph=use_mg)

    print(f"\n{'═'*60}")
    print(f"  TRACE Execution Complete")
    print(f"{'═'*60}")
    print(f"  EG Root UID        {res.eg_root_uid}")
    print(f"  RG Root UID        {res.rg_root_uid or '(none)'}")
    print(f"  Evidence Nodes     {len(res.evidence_node_uids)}")
    print(f"  Claims             {len(res.claim_uids)}")
    print(f"  MAPS_TO log        {len(res.maps_to_log)}")
    print(f"  Diagnostics        {len(res.diagnostics)}")
    print()
    for k, v in res.stats.items():
        print(f"    {k:<20s}  {v}")
    print(f"{'═'*60}")

    # Optionally dump the full maps_to log
    if "--dump-maps" in sys.argv:
        print("\n── MAPS_TO Audit Log ──")
        for entry in res.maps_to_log:
            print(
                f"  {entry['source_type']:30s}  "
                f"{entry['source_id'][:30]:30s}  →  "
                f"{entry['target_type']:30s}  "
                f"{entry['target_id'][:30]}"
            )
            print(f"    reason: {entry['mapping_reason']}")
