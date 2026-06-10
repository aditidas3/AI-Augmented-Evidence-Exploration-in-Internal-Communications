"""
explain.py
==========
EXPLAIN operator — Natural Language Answer Generator

Implements Algorithm 5 from the paper:
    Phase 1 — ExtractDerivationSubgraph + IdentifyDecisionPoints
    Phase 2 — Provenance Narratives per finding (GenProvenanceNarrative)
    Phase 3 — Conflict and Limitation Explanations
    Phase 4 — Decision Explanations + Analytical Sensitivity Estimation
    Phase 5 — UncertaintyMap + TetherMap + TetherComplete assertion

Pipeline position:
    ALIGN → TRACE → CONFLICT → CONSTRUCT → EXPLAIN

Run:
    python explain.py                     (in-memory, no Neo4j)
    python run_explain_neo4j.py           (with Neo4j write-back)

Input:
    results/construct/construct_bundle.json

Output:
    results/explain/explain_bundle.json full structured ExplBundle for dashboard
    explain_output.txt       plain text answer for investigator
"""

from __future__ import annotations

import json
import uuid
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
INPUT_PATH   = REPO_ROOT / "results" / "construct" / "construct_bundle.json"
OUTPUT_PATH  = REPO_ROOT / "results" / "explain" / "explain_bundle.json"
TEXT_PATH    = REPO_ROOT / "results" / "explain" / "explain_output.txt"

EXPLAIN_NS   = uuid.UUID("e0000000-0000-4000-a000-000000000001")

CONFIDENCE_LABELS = [
    (0.85, "HIGH"),
    (0.60, "MODERATE"),
    (0.35, "LOW"),
    (0.00, "VERY LOW"),
]
SLOT_ORDER = ["WHAT", "WHO", "WHY", "HOW", "WHEN", "OUTCOME", "EVIDENCE"]

# Sensitivity margin threshold — if confidence margin to acceptance
# floor is above this the decision is considered stable
SENSITIVITY_STABLE_MARGIN = 0.20
ACCEPTANCE_FLOOR           = 0.50   # minimum confidence to accept a Claim


# ── Helpers ───────────────────────────────────────────────────────────────────
def _uid(seed: str) -> str:
    return str(uuid.uuid5(EXPLAIN_NS, seed))

def _confidence_label(score: float) -> str:
    for threshold, label in CONFIDENCE_LABELS:
        if score >= threshold:
            return label
    return "VERY LOW"

def _clean_statement(statement: str) -> str:
    slots = ["WHAT:", "WHO:", "WHY:", "HOW:", "WHEN:", "EVIDENCE:", "OUTCOME:"]
    for _ in range(3):
        for slot in slots:
            s = statement.strip()
            if s.startswith(slot):
                statement = s[len(slot):].strip()
    boilerplate = [
        "The focal document was ", "The retrieved evidence was authored by ",
        "Responsibility chain: ", "The relevant date or period is ",
        "Supporting evidence comes from ",
    ]
    for bp in boilerplate:
        if statement.startswith(bp):
            statement = statement[len(bp):]
    return statement.strip()

def _extract_doc_ids(text: str) -> List[str]:
    return list(set(re.findall(r'\b[a-z]{2,4}\d{3,6}\b', text)))


# ── Data structures ───────────────────────────────────────────────────────────
@dataclass
class SlotAnswer:
    slot:            str
    statement:       str
    status:          str
    confidence:      float
    construct_score: float
    witness_uids:    List[str] = field(default_factory=list)
    doc_ids:         List[str] = field(default_factory=list)

@dataclass
class DecisionPoint:
    """One Inference node where CONSTRUCT accepted or reduced a Claim."""
    inference_uid:   str
    rule_name:       str
    slot:            str
    original_conf:   float
    construct_score: float
    defeaters:       int
    sensitivity:     str   # STABLE | MARGINAL | SENSITIVE | NOT_COMPUTED
    sensitivity_margin: float
    rationale:       str

@dataclass
class ProvenanceNarrative:
    """Per-finding derivation path and narrative sentence."""
    finding:         str   # slot name
    statement:       str   # cleaned slot answer
    derivation_path: List[str]   # UIDs from synthesis to witness
    decisions:       List[str]   # relevant decision point UIDs
    narrative:       str         # generated sentence
    rg_tether:       str         # UID of supporting RG node

@dataclass
class ConflictExplanation:
    """Explanation of a conflict-derived limitation."""
    limitation:      str
    is_conflict:     bool
    conflict_rule:   str
    witness_a:       str
    witness_b:       str
    slot:            str
    context:         str   # gathered from EG
    explanation:     str   # generated text
    rg_tether:       str

@dataclass
class ExplainResult:
    answer_text:      str
    confidence_label: str
    confidence_score: float
    contested_slots:  List[str]
    missing_slots:    List[str]
    citations:        List[str]
    evidence_chain:   List[str]
    warnings:         List[str]
    stats:            Dict
    explain_node_uid: str
    # Algorithm 5 structured components
    provenance_narratives:  List[ProvenanceNarrative]  = field(default_factory=list)
    conflict_explanations:  List[ConflictExplanation]  = field(default_factory=list)
    decision_points:        List[DecisionPoint]        = field(default_factory=list)
    uncertainty_map:        Dict                       = field(default_factory=dict)
    tether_map:             Dict                       = field(default_factory=dict)
    tether_complete:        bool                       = True
    derivation_subgraph:    Dict                       = field(default_factory=dict)
    explain_bundle:         Dict                       = field(default_factory=dict)


# ══════════════════════════════════════════════════════════════════════════════
#  EXPLAIN operator
# ══════════════════════════════════════════════════════════════════════════════
class Explain:
    """Single-use stateful operator. Instantiate → execute() once → discard."""

    def __init__(self, data: dict):
        self._data    = data
        self._bundle_first = "construct_bundle" in data or "ans_bundle" in data
        if self._bundle_first:
            self._trace_bundle = data.get("trace_bundle", {})
            self._conflict_bundle = data.get("conflict_bundle", {})
            self._construct_bundle = data.get("construct_bundle", data)
            self._ans_bundle = self._construct_bundle.get("ans_bundle", self._construct_bundle)
            self._warnings = []
            return

        self._rg_nodes = data["rg"]["nodes"]
        self._rg_edges = data["rg"]["edges"]
        self._eg_nodes = data["eg"]["nodes"]
        self._eg_edges = data["eg"]["edges"]
        self._cr       = data.get("conflict_result", {})
        self._constr   = data.get("construct_result", {})
        self._tr       = data.get("trace_result", {})

        self._slots:      Dict[str, SlotAnswer] = {}
        self._defeaters:  List[Dict]            = []
        self._syn_claim:  Optional[Dict]        = None
        self._syn_inf:    Optional[Dict]        = None
        self._warnings:   List[str]             = []

        # Algorithm 5 state
        self._deriv_subgraph: Dict  = {"nodes": [], "edges": []}
        self._decision_points: List[DecisionPoint] = []

    # ── Public entry point ────────────────────────────────────────────────────
    def execute(self) -> ExplainResult:
        if self._bundle_first:
            return self._execute_bundle_first()

        self._load_synthesis()
        self._load_slots()
        self._load_defeaters()
        self._load_witnesses_per_slot()

        # ── Algorithm 5 Phase 1 ────────────────────────────────────────────
        self._extract_derivation_subgraph()
        self._identify_decision_points()

        # ── Algorithm 5 Phase 2 ────────────────────────────────────────────
        pn = self._build_provenance_narratives()

        # ── Algorithm 5 Phase 3 ────────────────────────────────────────────
        ce = self._build_conflict_explanations()

        # ── Algorithm 5 Phase 4 ────────────────────────────────────────────
        de = self._build_decision_explanations()

        # ── Answer text (from provenance narratives) ───────────────────────
        answer_text   = self._generate_answer(pn, ce)
        evidence_chain = self._build_evidence_chain(pn, ce, de)
        citations     = self._collect_citations()

        # ── Algorithm 5 Phase 5 ────────────────────────────────────────────
        uncertainty_map  = self._build_uncertainty_map(pn, ce, de)
        tether_map       = self._build_tether_map(pn, ce, de)
        tether_complete  = self._assert_tether_complete(tether_map)

        contested   = [s for s, a in self._slots.items() if a.status == "contested"]
        missing     = self._constr.get("limitations", [])
        conf_score  = float(
            self._syn_claim["properties"].get("constructScore",
            self._syn_claim["properties"].get("confidenceScore", 0.5))
        ) if self._syn_claim else 0.0
        conf_label  = _confidence_label(conf_score)
        explain_uid = _uid("explain::node::" + self._tr.get("eg_root_uid", ""))

        stats = {
            "slots_answered":           len(self._slots),
            "slots_contested":          len(contested),
            "citations":                len(citations),
            "defeaters":                len(self._defeaters),
            "confidence":               round(conf_score, 4),
            "confidence_label":         conf_label,
            "provenance_narratives":    len(pn),
            "conflict_explanations":    len(ce),
            "decision_points":          len(de),
            "tether_complete":          tether_complete,
            "derivation_nodes":         len(self._deriv_subgraph["nodes"]),
        }

        return ExplainResult(
            answer_text             = answer_text,
            confidence_label        = conf_label,
            confidence_score        = conf_score,
            contested_slots         = contested,
            missing_slots           = [l for l in missing if "No " in l],
            citations               = citations,
            evidence_chain          = evidence_chain,
            warnings                = self._warnings,
            stats                   = stats,
            explain_node_uid        = explain_uid,
            provenance_narratives   = pn,
            conflict_explanations   = ce,
            decision_points         = de,
            uncertainty_map         = uncertainty_map,
            tether_map              = tether_map,
            tether_complete         = tether_complete,
            derivation_subgraph     = self._deriv_subgraph,
        )

    def _execute_bundle_first(self) -> ExplainResult:
        ans = self._ans_bundle
        trace = self._trace_bundle
        conflict = self._conflict_bundle.get("conflict_structure", {})
        construct_status = (
            self._construct_bundle.get("status")
            or ans.get("status")
            or (self._construct_bundle.get("construct_result", {}) or {}).get("status")
        )
        construct_answer_text = ans.get("answer_text") or ans.get("narrative") or ""
        answer_text = construct_answer_text
        citation_map = list(ans.get("citation_map", []) or [])
        limitations = list(ans.get("limitations", []) or [])
        selected_chain_id = (
            ans.get("selected_chain_id")
            or self._construct_bundle.get("selected_chain_id", "")
        )
        if construct_status == "NO_ANSWER_CONSTRUCTED":
            if not answer_text:
                answer_text = (
                    "NO_ANSWER_CONSTRUCTED: No answer could be constructed "
                    "because ALIGN/TRACE produced empty evidence."
                )
            warnings = [
                lim.get("description", "")
                for lim in limitations
                if isinstance(lim, dict) and lim.get("description")
            ]
            if not warnings:
                warnings = ["No ranked evidence chain was selected."]
            evidence_chain = self._bundle_evidence_chain(
                trace=trace,
                conflict=conflict,
                selected_chain_id="",
                citation_count=0,
            )
            explain_bundle = {
                "bundle_id": _uid(
                    f"explain_bundle::NO_ANSWER::{self._construct_bundle.get('construct_bundle_id', '')}"
                ),
                "summary": answer_text,
                "status": "NO_ANSWER_CONSTRUCTED",
                "provenance_narratives": [],
                "conflict_explanations": [],
                "decision_explanations": [
                    {
                        "decision_node_id": _uid("decision::NO_ANSWER_CONSTRUCTED"),
                        "rationale": (
                            "CONSTRUCT did not select a ranked evidence chain "
                            "because upstream evidence was empty."
                        ),
                        "selected_chain_id": "",
                    }
                ],
                "uncertainty_map": [],
                "tether_map": [],
                "tether_failures": [],
                "selected_chain_id": "",
            }
            stats = {
                "status": "NO_ANSWER_CONSTRUCTED",
                "citations": 0,
                "tethers": 0,
                "tether_failures": 0,
                "uncertainties": 0,
                "conflicts": 0,
                "confidence": 0.0,
                "confidence_label": "NO_ANSWER",
            }
            return ExplainResult(
                answer_text=answer_text,
                confidence_label="NO_ANSWER",
                confidence_score=0.0,
                contested_slots=[],
                missing_slots=["NO_ANSWER_CONSTRUCTED"],
                citations=[],
                evidence_chain=evidence_chain,
                warnings=warnings,
                stats=stats,
                explain_node_uid=_uid("explain::node::NO_ANSWER_CONSTRUCTED"),
                explain_bundle=explain_bundle,
            )

        construct_answer_text, construct_citation_map = self._normalize_slot_answer_citations(
            ans=ans,
            answer_text=construct_answer_text,
            citation_map=citation_map,
        )
        answer_text, citation_map, answer_text_mode = self._compose_bundle_answer(
            ans=ans,
            construct_answer_text=construct_answer_text,
            construct_citation_map=construct_citation_map,
        )
        guard_reasons = self._answerability_guard(
            ans=ans,
            trace=trace,
            selected_chain_id=selected_chain_id,
            answer_text=answer_text,
            citation_map=citation_map,
        )
        if guard_reasons:
            guarded_answer = (
                "NO_ANSWER_CONSTRUCTED: No answer could be constructed because "
                + ", ".join(guard_reasons)
                + "."
            )
            guard_limitation = {
                "limitation_id": _uid(
                    f"limitation::EXPLAIN_GUARD::{selected_chain_id}::{','.join(guard_reasons)}"
                ),
                "kind": "NO_ANSWER_CONSTRUCTED",
                "description": guarded_answer,
                "reasons": list(guard_reasons),
            }
            return self._no_answer_bundle_result(
                answer_text=guarded_answer,
                trace=trace,
                conflict=conflict,
                limitations=[*limitations, guard_limitation],
            )

        confidence = float(ans.get("confidence", ans.get("constructScore", 0.0)) or 0.0)
        confidence_label = _confidence_label(confidence)
        citations = self._citations_from_citation_map(citation_map)
        tether_map, tether_failures = self._tether_map_from_citations(citation_map)
        evidence_chain = self._bundle_evidence_chain(
            trace=trace,
            conflict=conflict,
            selected_chain_id=selected_chain_id,
            citation_count=len(citation_map),
        )
        uncertainty_map = self._uncertainty_from_limitations_and_mappings(
            limitations=limitations,
            trace=trace,
            selected_chain_id=selected_chain_id,
        )
        conflict_explanations = self._conflict_explanations(conflict)
        provenance_narratives = self._provenance_narratives(trace, selected_chain_id, ans)
        decision_explanations = [
            {
                "decision_node_id": _uid(f"decision::{selected_chain_id}"),
                "rationale": "CONSTRUCT selected the highest effective ranked chain after conflict penalties.",
                "selected_chain_id": selected_chain_id,
            }
        ] if selected_chain_id else []
        warnings = [lim.get("description", "") for lim in limitations if lim.get("description")]
        explain_bundle = {
            "bundle_id": _uid(f"explain_bundle::{self._construct_bundle.get('construct_bundle_id', selected_chain_id)}"),
            "answer_text": answer_text,
            "summary": answer_text,
            "citation_map": citation_map,
            "construct_answer_text": construct_answer_text,
            "construct_citation_map": construct_citation_map,
            "answer_text_mode": answer_text_mode,
            "provenance_narratives": provenance_narratives,
            "conflict_explanations": conflict_explanations,
            "decision_explanations": decision_explanations,
            "uncertainty_map": uncertainty_map,
            "tether_map": tether_map,
            "tether_failures": tether_failures,
            "selected_chain_id": selected_chain_id,
        }
        stats = {
            "citations": len(citations),
            "tethers": len(tether_map),
            "tether_failures": len(tether_failures),
            "uncertainties": len(uncertainty_map),
            "conflicts": len(conflict_explanations),
            "confidence": round(confidence, 4),
            "confidence_label": confidence_label,
            "answer_text_mode": answer_text_mode,
        }
        return ExplainResult(
            answer_text=answer_text,
            confidence_label=confidence_label,
            confidence_score=confidence,
            contested_slots=[],
            missing_slots=[],
            citations=citations,
            evidence_chain=evidence_chain,
            warnings=warnings,
            stats=stats,
            explain_node_uid=_uid(f"explain::node::{selected_chain_id}"),
            explain_bundle=explain_bundle,
        )

    def _no_answer_bundle_result(
        self,
        *,
        answer_text: str,
        trace: dict,
        conflict: dict,
        limitations: List[dict],
    ) -> ExplainResult:
        warnings = [
            lim.get("description", "")
            for lim in limitations
            if isinstance(lim, dict) and lim.get("description")
        ]
        if not warnings:
            warnings = ["No ranked evidence chain was selected."]
        evidence_chain = self._bundle_evidence_chain(
            trace=trace,
            conflict=conflict,
            selected_chain_id="",
            citation_count=0,
        )
        explain_bundle = {
            "bundle_id": _uid(
                f"explain_bundle::NO_ANSWER::{self._construct_bundle.get('construct_bundle_id', '')}"
            ),
            "summary": answer_text,
            "status": "NO_ANSWER_CONSTRUCTED",
            "provenance_narratives": [],
            "conflict_explanations": [],
            "decision_explanations": [
                {
                    "decision_node_id": _uid("decision::NO_ANSWER_CONSTRUCTED"),
                    "rationale": (
                        "CONSTRUCT did not select a ranked evidence chain "
                        "because upstream evidence was empty or incomplete."
                    ),
                    "selected_chain_id": "",
                }
            ],
            "uncertainty_map": [],
            "tether_map": [],
            "tether_failures": [],
            "selected_chain_id": "",
        }
        stats = {
            "status": "NO_ANSWER_CONSTRUCTED",
            "citations": 0,
            "tethers": 0,
            "tether_failures": 0,
            "uncertainties": 0,
            "conflicts": 0,
            "confidence": 0.0,
            "confidence_label": "NO_ANSWER",
        }
        return ExplainResult(
            answer_text=answer_text,
            confidence_label="NO_ANSWER",
            confidence_score=0.0,
            contested_slots=[],
            missing_slots=["NO_ANSWER_CONSTRUCTED"],
            citations=[],
            evidence_chain=evidence_chain,
            warnings=warnings,
            stats=stats,
            explain_node_uid=_uid("explain::node::NO_ANSWER_CONSTRUCTED"),
            explain_bundle=explain_bundle,
        )

    @classmethod
    def _answerability_guard(
        cls,
        *,
        ans: dict,
        trace: dict,
        selected_chain_id: str,
        answer_text: str,
        citation_map: List[dict],
    ) -> List[str]:
        reasons: List[str] = []
        required_slots = cls._required_slot_types(trace)
        if required_slots:
            present = cls._selected_chain_slot_types(trace, selected_chain_id)
            missing = [slot for slot in required_slots if slot not in present]
            for slot in missing:
                reasons.append(f"missing_required_slot:{slot}")

        if answer_text and not citation_map:
            reasons.append("missing_answer_citations")

        return sorted(set(reasons))

    @staticmethod
    def _required_slot_types(trace: dict) -> List[str]:
        trace_spec = trace.get("trace_spec", {}) or {}
        explicit = [
            str(slot or "").upper()
            for slot in (trace_spec.get("required_slot_types", []) or [])
            if str(slot or "").strip()
        ]
        if explicit:
            return explicit
        question_text = str(trace_spec.get("question_text", "") or "").lower()
        if not question_text:
            return []
        available = {
            str(slot.get("slot_type", "") or "").upper()
            for slot in (trace_spec.get("slot_specs", []) or [])
            if slot.get("slot_type")
        }
        required: List[str] = []
        if "WHAT" in available and (
            "what" in question_text
            or "checklist" in question_text
            or "policy" in question_text
            or "target drug" in question_text
        ):
            required.append("WHAT")
        if "WHEN" in available and (
            "when" in question_text
            or "timeline" in question_text
            or "date" in question_text
            or "first" in question_text
            or "over time" in question_text
            or "evolve" in question_text
        ):
            required.append("WHEN")
        if "HOW" in available and (
            "how" in question_text
            or "process" in question_text
            or "mechanism" in question_text
            or "evolve" in question_text
        ):
            required.append("HOW")
        if "EVIDENCE" in available:
            required.append("EVIDENCE")
        return required

    @staticmethod
    def _selected_chain_slot_types(trace: dict, selected_chain_id: str) -> set[str]:
        candidate_by_id = {
            str(candidate.get("candidate_id")): candidate
            for candidates in (trace.get("slot_candidates", {}) or {}).values()
            for candidate in (candidates or [])
            if candidate.get("candidate_id")
        }
        for chain in trace.get("ranked_chains", []) or []:
            if selected_chain_id and chain.get("chain_id") != selected_chain_id:
                continue
            slots: set[str] = set()
            for candidate_id in chain.get("slot_candidate_ids", []) or []:
                candidate = candidate_by_id.get(str(candidate_id))
                if not candidate:
                    continue
                slot = str(candidate.get("slot_type", "") or "").upper()
                if slot:
                    slots.add(slot)
            if selected_chain_id or slots:
                return slots
        return set()

    @staticmethod
    def _has_slot_labels(answer_text: str) -> bool:
        labels = "|".join(re.escape(slot) for slot in SLOT_ORDER)
        return bool(re.search(rf"\b(?:{labels}):", str(answer_text or ""), re.IGNORECASE))

    @classmethod
    def _normalize_slot_answer_citations(
        cls,
        *,
        ans: dict,
        answer_text: str,
        citation_map: List[dict],
    ) -> Tuple[str, List[dict]]:
        if not cls._has_slot_labels(answer_text):
            return answer_text, citation_map

        segments = cls._slot_labeled_segments(answer_text)
        if not segments:
            return answer_text, citation_map

        findings = list(ans.get("findings") or [])
        repaired_citations: List[dict] = []
        used_segments: set[int] = set()
        seen: set[tuple[str, str]] = set()
        ordered = sorted(
            findings,
            key=lambda finding: (
                cls._slot_sort_index(str(finding.get("slot_type", "") or "")),
                str(finding.get("display_id", "")),
            ),
        )
        for finding in ordered:
            statement = str(finding.get("statement", "") or "").strip()
            if not statement:
                continue
            slot = str(finding.get("slot_type", "") or "").upper()
            key = (slot, statement.casefold())
            if key in seen:
                continue
            seen.add(key)
            sentence = cls._slot_segment_for_finding(
                slot=slot,
                statement=statement,
                segments=segments,
                used_segments=used_segments,
            ) or f"{slot}: {statement}"
            citation = cls._citation_for_finding(
                finding=finding,
                citation_map=citation_map,
                sentence_index=len(repaired_citations),
                sentence_text=sentence,
            )
            if citation is None:
                continue
            repaired_citations.append(citation)

        if not repaired_citations:
            return cls._repair_slot_labeled_answer_text(
                answer_text=answer_text,
                citation_map=citation_map,
            )
        return answer_text, repaired_citations

    @classmethod
    def _repair_slot_labeled_answer_text(
        cls,
        *,
        answer_text: str,
        citation_map: List[dict],
    ) -> Tuple[str, List[dict]]:
        segments = cls._slot_labeled_segments(answer_text)
        if not segments:
            return answer_text, citation_map

        repaired_citations: List[dict] = []
        seen: set[tuple[str, str]] = set()
        for slot, statement, raw_segment in segments:
            key = (slot, statement.casefold())
            if key in seen:
                continue
            seen.add(key)
            citation = cls._citation_for_slot_segment(
                slot=slot,
                statement=statement,
                raw_segment=raw_segment,
                citation_map=citation_map,
                sentence_index=len(repaired_citations),
                sentence_text=raw_segment,
            )
            if citation is None:
                continue
            repaired_citations.append(citation)

        if not repaired_citations:
            return answer_text, citation_map
        return answer_text, repaired_citations

    @classmethod
    def _compose_bundle_answer(
        cls,
        *,
        ans: dict,
        construct_answer_text: str,
        construct_citation_map: List[dict],
    ) -> Tuple[str, List[dict], str]:
        construct_mode = str(ans.get("answer_text_mode", "") or "")
        if construct_mode.startswith("deterministic_") and str(construct_answer_text or "").strip():
            return (
                construct_answer_text,
                construct_citation_map,
                "passthrough_construct_synthesis",
            )

        answer_text, citation_map = cls._natural_answer_from_findings(
            findings=list(ans.get("findings") or []),
            citation_map=construct_citation_map,
        )
        if answer_text:
            return answer_text, citation_map, "natural_from_findings"

        answer_text, citation_map = cls._natural_answer_from_slot_segments(
            answer_text=construct_answer_text,
            citation_map=construct_citation_map,
        )
        if answer_text:
            return answer_text, citation_map, "natural_from_slot_segments"

        return construct_answer_text, construct_citation_map, "passthrough"

    @classmethod
    def _natural_answer_from_findings(
        cls,
        *,
        findings: List[dict],
        citation_map: List[dict],
    ) -> Tuple[str, List[dict]]:
        sentences: List[str] = []
        citations: List[dict] = []
        seen: set[tuple[str, str]] = set()
        ordered = sorted(
            findings,
            key=lambda finding: (
                cls._slot_sort_index(str(finding.get("slot_type", "") or "")),
                str(finding.get("display_id", "")),
            ),
        )
        for finding in ordered:
            slot = str(finding.get("slot_type", "") or "").upper()
            statement = _clean_statement(str(finding.get("statement", "") or "")).strip()
            if not statement:
                continue
            key = (slot, " ".join(statement.casefold().split()))
            if key in seen:
                continue
            seen.add(key)
            sentence = cls._sentence_from_finding(slot, statement)
            citation = cls._citation_for_finding(
                finding=finding,
                citation_map=citation_map,
                sentence_index=len(sentences),
                sentence_text=sentence,
            )
            sentences.append(sentence)
            if citation is not None:
                citations.append(citation)

        return " ".join(sentences), citations

    @classmethod
    def _natural_answer_from_slot_segments(
        cls,
        *,
        answer_text: str,
        citation_map: List[dict],
    ) -> Tuple[str, List[dict]]:
        if not cls._has_slot_labels(answer_text):
            return "", []

        sentences: List[str] = []
        citations: List[dict] = []
        seen: set[tuple[str, str]] = set()
        for slot, statement, raw_segment in cls._slot_labeled_segments(answer_text):
            key = (slot, " ".join(statement.casefold().split()))
            if key in seen:
                continue
            seen.add(key)
            sentence = cls._sentence_from_finding(slot, statement)
            citation = cls._citation_for_slot_segment(
                slot=slot,
                statement=statement,
                raw_segment=raw_segment,
                citation_map=citation_map,
                sentence_index=len(sentences),
                sentence_text=sentence,
            )
            sentences.append(sentence)
            if citation is not None:
                citations.append(citation)

        return " ".join(sentences), citations

    @staticmethod
    def _slot_labeled_segments(answer_text: str) -> List[Tuple[str, str, str]]:
        labels = "|".join(re.escape(slot) for slot in SLOT_ORDER)
        matches = list(re.finditer(rf"\b({labels}):", str(answer_text or ""), re.IGNORECASE))
        segments: List[Tuple[str, str, str]] = []
        for idx, match in enumerate(matches):
            start = match.end()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(answer_text)
            slot = match.group(1).upper()
            raw_statement = str(answer_text[start:end] or "").strip(" ;,\n\t")
            statement = _clean_statement(raw_statement).strip()
            if not statement:
                continue
            raw_segment = str(answer_text[match.start():end] or "").strip()
            segments.append((slot, statement, raw_segment))
        return segments

    @staticmethod
    def _slot_segment_for_finding(
        *,
        slot: str,
        statement: str,
        segments: List[Tuple[str, str, str]],
        used_segments: set[int],
    ) -> Optional[str]:
        normalized_slot = str(slot or "").upper()
        statement_key = " ".join(str(statement or "").casefold().split())
        for index, (segment_slot, segment_statement, raw_segment) in enumerate(segments):
            if index in used_segments or segment_slot != normalized_slot:
                continue
            segment_key = " ".join(str(segment_statement or "").casefold().split())
            if (
                statement_key == segment_key
                or statement_key in segment_key
                or segment_key in statement_key
            ):
                used_segments.add(index)
                return raw_segment

        for index, (segment_slot, _segment_statement, raw_segment) in enumerate(segments):
            if index in used_segments or segment_slot != normalized_slot:
                continue
            used_segments.add(index)
            return raw_segment
        return None

    @staticmethod
    def _slot_sort_index(slot: str) -> int:
        normalized = str(slot or "").upper()
        try:
            return SLOT_ORDER.index(normalized)
        except ValueError:
            return len(SLOT_ORDER)

    @staticmethod
    def _sentence_from_finding(slot: str, statement: str) -> str:
        prefixes = {
            "WHAT": "The selected evidence identifies",
            "WHO": "The selected evidence names",
            "WHY": "The selected evidence gives the reason as",
            "WHEN": "The selected evidence dates this to",
            "HOW": "The selected evidence describes the process as",
            "OUTCOME": "The selected evidence gives the outcome as",
            "EVIDENCE": "Supporting evidence includes",
        }
        prefix = prefixes.get(str(slot or "").upper(), "The selected evidence includes")
        terminal = "" if statement.endswith((".", "?", "!")) else "."
        return f"{prefix} {statement}{terminal}"

    @staticmethod
    def _citation_for_finding(
        *,
        finding: dict,
        citation_map: List[dict],
        sentence_index: int,
        sentence_text: str,
    ) -> Optional[dict]:
        candidate_ids = {
            str(candidate_id)
            for candidate_id in finding.get("supporting_candidate_ids", []) or []
            if candidate_id
        }
        object_ids = {
            str(object_id)
            for object_id in finding.get("supporting_object_ids", []) or []
            if object_id
        }
        selected: Optional[dict] = None
        for entry in citation_map:
            entry_candidates = {
                str(candidate_id)
                for candidate_id in entry.get("candidate_ids", []) or []
                if candidate_id
            }
            entry_objects = {
                str(object_id)
                for object_id in entry.get("eg_object_ids", []) or []
                if object_id
            }
            if (candidate_ids and candidate_ids & entry_candidates) or (
                object_ids and object_ids & entry_objects
            ):
                selected = entry
                break

        if selected is not None:
            repaired = dict(selected)
        else:
            witness = finding.get("witness_bundle", {}) or {}
            witness_path = witness.get("path", []) or []
            if not object_ids and not witness_path:
                return None
            repaired = {
                "eg_object_ids": sorted(object_ids),
                "candidate_ids": sorted(candidate_ids),
                "witness_paths": [witness_path] if witness_path else [],
                "confidence": finding.get("evidence_strength", 0.0),
            }

        repaired["sentence_index"] = sentence_index
        repaired["sentence_text"] = sentence_text
        if not repaired.get("witness_paths"):
            witness = finding.get("witness_bundle", {}) or {}
            witness_path = witness.get("path", []) or []
            if witness_path:
                repaired["witness_paths"] = [witness_path]
        if not repaired.get("eg_object_ids"):
            repaired["eg_object_ids"] = sorted(object_ids)
        if not repaired.get("candidate_ids"):
            repaired["candidate_ids"] = sorted(candidate_ids)
        return repaired

    @staticmethod
    def _citation_for_slot_segment(
        *,
        slot: str,
        statement: str,
        raw_segment: str,
        citation_map: List[dict],
        sentence_index: int,
        sentence_text: str,
    ) -> Optional[dict]:
        selected: Optional[dict] = None
        statement_key = " ".join(statement.casefold().split())
        raw_key = " ".join(raw_segment.casefold().split())
        labeled_key = " ".join(f"{slot}: {statement}".casefold().split())

        for entry in citation_map:
            entry_text = " ".join(str(entry.get("sentence_text", "") or "").casefold().split())
            if not entry_text:
                continue
            if (
                entry_text in {statement_key, raw_key, labeled_key}
                or statement_key in entry_text
                or entry_text in raw_key
            ):
                selected = entry
                break

        if selected is None and sentence_index < len(citation_map):
            selected = citation_map[sentence_index]

        if selected is None:
            return None

        repaired = dict(selected)
        repaired["sentence_index"] = sentence_index
        repaired["sentence_text"] = sentence_text
        return repaired

    @staticmethod
    def _citations_from_citation_map(citation_map: List[dict]) -> List[str]:
        citations = set()
        for entry in citation_map:
            for oid in entry.get("eg_object_ids", []) or []:
                citations.add(str(oid))
            for path in entry.get("witness_paths", []) or []:
                if path:
                    citations.add(str(path[0]))
        return sorted(citations)

    @staticmethod
    def _tether_map_from_citations(citation_map: List[dict]) -> Tuple[List[dict], List[dict]]:
        tether_map: List[dict] = []
        failures: List[dict] = []
        for entry in citation_map:
            tethered_to = list(entry.get("eg_object_ids", []) or [])
            for path in entry.get("witness_paths", []) or []:
                tethered_to.extend(str(part) for part in path if part)
            tethered_to = sorted(set(tethered_to))
            if tethered_to:
                tether_map.append({
                    "sentence_index": entry.get("sentence_index", len(tether_map)),
                    "sentence_text": entry.get("sentence_text", ""),
                    "tethered_to": tethered_to,
                    "tether_type": "BOTH",
                })
            else:
                failures.append({
                    "sentence_index": entry.get("sentence_index", len(failures)),
                    "sentence_text": entry.get("sentence_text", ""),
                    "reason": "No EG object or witness path citation.",
                })
        return tether_map, failures

    @staticmethod
    def _bundle_evidence_chain(
        *,
        trace: dict,
        conflict: dict,
        selected_chain_id: str,
        citation_count: int,
    ) -> List[str]:
        chain: List[str] = []
        for inv in (trace.get("provenance_manifest", {}) or {}).get("operator_invocations", []) or []:
            operator = inv.get("operator", "TRACE")
            phase = inv.get("phase", "")
            chain.append(f"{operator}: {phase}")
        edges = conflict.get("edges", {}) or {}
        edge_count = len(edges) if isinstance(edges, dict) else len(list(edges))
        chain.append(f"CONFLICT: {edge_count} conflict edge(s) considered")
        if selected_chain_id:
            chain.append(f"CONSTRUCT: selected chain {selected_chain_id}")
        else:
            chain.append("CONSTRUCT: no ranked chain selected")
        chain.append(f"EXPLAIN: generated explanation with {citation_count} citation tether(s)")
        return chain

    @staticmethod
    def _uncertainty_from_limitations_and_mappings(
        *,
        limitations: List[dict],
        trace: dict,
        selected_chain_id: str,
    ) -> List[dict]:
        uncertainties: List[dict] = []
        for lim in limitations:
            uncertainties.append({
                "entry_id": _uid(f"uncertainty::{lim.get('limitation_id', lim.get('description', ''))}"),
                "uncertainty_type": lim.get("limitation_type", "EXTRACTION_UNCERTAINTY"),
                "description": lim.get("description", ""),
                "source_type": "CONSTRUCT_LIMITATION",
            })
        selected_mapping_ids = set()
        for chain in trace.get("ranked_chains", []) or []:
            if chain.get("chain_id") == selected_chain_id:
                selected_mapping_ids.update(chain.get("mapping_ids", []) or [])
        lossy = {"OMISSION", "QUALIFIER_DROP", "HEDGE_DROP"}
        for mapping in (trace.get("map_transform", {}) or {}).get("retained", []) or []:
            if selected_mapping_ids and mapping.get("mapping_id") not in selected_mapping_ids:
                continue
            label = mapping.get("label", "")
            if label in lossy:
                uncertainties.append({
                    "entry_id": _uid(f"uncertainty::mapping::{mapping.get('mapping_id', '')}"),
                    "uncertainty_type": "EXTRACTION_UNCERTAINTY",
                    "description": f"MAP-TRANSFORM mapping {mapping.get('mapping_id', '')} is labelled {label}.",
                    "source_type": "MAP_TRANSFORM",
                    "source_mapping_id": mapping.get("mapping_id", ""),
                })
        return uncertainties

    @staticmethod
    def _conflict_explanations(conflict: dict) -> List[dict]:
        edges = conflict.get("edges", {}) or {}
        iterable = edges.values() if isinstance(edges, dict) else edges
        return [
            {
                "conflict_edge_id": edge.get("edge_id", ""),
                "explanation": edge.get("description") or f"Conflict stance: {edge.get('stance', '')}",
                "is_conflict_derived": True,
            }
            for edge in iterable
        ]

    @staticmethod
    def _provenance_narratives(trace: dict, selected_chain_id: str, ans: Optional[dict] = None) -> List[dict]:
        findings = (ans or {}).get("findings") or []
        if findings:
            narratives: List[dict] = []
            for index, finding in enumerate(findings):
                statement = str(finding.get("statement", "") or "").strip()
                slot_type = str(finding.get("slot_type", "") or "").upper()
                strength = finding.get("evidence_strength", finding.get("confidence", ""))
                path = (
                    finding.get("witness_bundle", {}) or {}
                ).get("path") or finding.get("supporting_object_ids", []) or []
                audit_path = " -> ".join(str(part) for part in path if part)
                normalized = ""
                candidate_ids = set(finding.get("supporting_candidate_ids", []) or [])
                for candidates in (trace.get("slot_candidates", {}) or {}).values():
                    for candidate in candidates or []:
                        if str(candidate.get("candidate_id")) in candidate_ids:
                            normalized = str(candidate.get("normalized_surface", "") or "")
                            break
                    if normalized:
                        break
                quoted_statement = f'"{statement}"' if statement else '"the selected span"'
                quoted_normalized = f' "{normalized}"' if normalized and normalized != statement else ""
                narrative = (
                    f"{slot_type or 'EVIDENCE'} finding {finding.get('display_id', index + 1)} "
                    f"{quoted_statement}{quoted_normalized} contributes to the evidence map. "
                    "The analyst can verify the literal span against the cited source. "
                    f"This matters because it supports the selected chain {selected_chain_id or 'unknown'}. "
                    "This is abstract-level evidence, not a full-text appraisal. "
                    f"The evidence strength is marked LOW for audit conservatism (score {strength}). "
                    f"Audit path: {audit_path or 'not available'}."
                )
                narratives.append({
                    "finding_id": finding.get("finding_id", f"finding-{index + 1}"),
                    "display_id": finding.get("display_id", f"F{index + 1}"),
                    "slot_type": slot_type,
                    "narrative": narrative,
                    "derivation_path": path,
                    "decision_points": ["rank_chains", "construct_chain_select"],
                })
            return narratives

        phases = [
            inv.get("phase", "")
            for inv in (trace.get("provenance_manifest", {}) or {}).get("operator_invocations", []) or []
        ]
        return [
            {
                "finding_id": selected_chain_id,
                "derivation_path": phases,
                "decision_points": ["rank_chains", "construct_chain_select"],
                "narrative": "TRACE produced ranked chains; CONSTRUCT selected the answer chain; EXPLAIN tethered the narrative to citations.",
            }
        ]

    # ── Load methods ──────────────────────────────────────────────────────────
    def _load_synthesis(self):
        for node in self._rg_nodes:
            labels = node.get("labels", [])
            props  = node.get("properties", {})
            if "Synthesised" in labels:
                self._syn_claim = node
            if "Inference" in labels and "synthesis" in props.get("ruleName", ""):
                self._syn_inf = node
        if not self._syn_claim:
            self._warnings.append("No Synthesised Claim found.")

    def _load_slots(self):
        construct_scores: Dict[str, float] = {}
        for u in self._constr.get("updated_inferences", []):
            if "Rule3" in u.get("rule", ""):
                construct_scores[u["uid"]] = float(
                    u.get("constructScore", u.get("updated_conf", 0.0)))

        # Read from cite_map if available (from CONSTRUCT Algorithm 4)
        cite_map = self._constr.get("cite_map", {})

        for node in self._rg_nodes:
            if "Claim" not in node.get("labels", []):
                continue
            if "Synthesised" in node.get("labels", []):
                continue
            p  = node["properties"]
            dm = p.get("domainMetadata", {}) or {}
            slot = dm.get("slot_type", "")
            if not slot:
                continue
            raw_conf  = float(p.get("confidenceScore", 0.8))
            con_score = float(p.get("constructScore", raw_conf))
            for inf_uid, cscore in construct_scores.items():
                if slot.lower() in inf_uid.lower():
                    con_score = cscore
                    break
            statement = _clean_statement(p.get("statement", ""))
            # Use witness_uids from cite_map if available
            witness_uids = []
            if slot in cite_map:
                witness_uids = cite_map[slot].get("witness_uids", [])
            self._slots[slot] = SlotAnswer(
                slot=slot, statement=statement,
                status=p.get("status", "supported"),
                confidence=raw_conf, construct_score=con_score,
                doc_ids=_extract_doc_ids(statement),
                witness_uids=witness_uids,
            )

    def _load_defeaters(self):
        self._defeaters = [
            n for n in self._rg_nodes
            if "Defeater" in n.get("labels", [])
        ]

    def _load_witnesses_per_slot(self):
        all_edges = list(self._rg_edges) + list(self._eg_edges)
        for edge in all_edges:
            if edge.get("type") != "GROUNDED_BY":
                continue
            claim_uid   = edge.get("from", "")
            witness_uid = edge.get("to", "")
            for node in self._rg_nodes:
                if "Claim" not in node.get("labels", []):
                    continue
                if "Synthesised" in node.get("labels", []):
                    continue
                p  = node["properties"]
                dm = p.get("domainMetadata", {}) or {}
                if p.get("uid") == claim_uid:
                    slot = dm.get("slot_type", "")
                    if slot in self._slots:
                        if witness_uid not in self._slots[slot].witness_uids:
                            self._slots[slot].witness_uids.append(witness_uid)
                    break

    # ── Algorithm 5 Phase 1 ───────────────────────────────────────────────────
    def _extract_derivation_subgraph(self) -> None:
        """
        ExtractDerivationSubgraph — Algorithm 5 Line 1.

        Builds a focused subgraph of the RG containing only the nodes
        and edges that contributed to the final synthesised answer.
        Includes: synthesis Inference, synthesised Claim, slot Claims,
        supporting Inferences, and Defeater nodes.
        """
        relevant_uids = set()

        # Seed with synthesis nodes
        if self._syn_claim:
            relevant_uids.add(self._syn_claim["properties"].get("uid", ""))
        if self._syn_inf:
            relevant_uids.add(self._syn_inf["properties"].get("uid", ""))

        # Add slot Claims
        for node in self._rg_nodes:
            if "Claim" in node.get("labels", []):
                relevant_uids.add(node["properties"].get("uid", ""))

        # Add supporting Inferences
        for node in self._rg_nodes:
            if "Inference" in node.get("labels", []):
                relevant_uids.add(node["properties"].get("uid", ""))

        # Add Defeaters
        for d in self._defeaters:
            relevant_uids.add(d["properties"].get("uid", ""))

        # Filter nodes and edges to derivation subgraph
        deriv_nodes = [
            n for n in self._rg_nodes
            if n["properties"].get("uid") in relevant_uids
        ]
        deriv_edges = [
            e for e in self._rg_edges
            if e.get("from") in relevant_uids or e.get("to") in relevant_uids
        ]

        self._deriv_subgraph = {"nodes": deriv_nodes, "edges": deriv_edges}

    def _identify_decision_points(self) -> None:
        """
        IdentifyDecisionPoints — Algorithm 5 Line 2.

        Identifies Inference nodes in the derivation subgraph where
        CONSTRUCT made a confidence decision — either confirming a
        Claim (Rule 1/2) or applying a penalty (Rule 3/4).

        For each decision point computes analytical sensitivity:
        how much would confidence need to change to cross the
        acceptance floor? This avoids expensive pipeline re-runs.
        """
        updated_map = {
            u["uid"]: u
            for u in self._constr.get("updated_inferences", [])
        }

        for node in self._deriv_subgraph["nodes"]:
            if "Inference" not in node.get("labels", []):
                continue
            p        = node["properties"]
            inf_uid  = p.get("uid", "")
            rule     = p.get("ruleName", "")
            orig     = float(p.get("confidenceScore", 0.5))
            cscore   = float(p.get("constructScore", orig))

            # Determine which slot this inference relates to
            slot = ""
            for s in SLOT_ORDER:
                if s.lower() in rule.lower():
                    slot = s
                    break

            # Count defeaters applied to this inference
            n_defeaters = sum(
                1 for e in self._deriv_subgraph["edges"]
                if e.get("type") == "HAS_DEFEATER" and e.get("from") == inf_uid
            )

            # ── Analytical sensitivity estimation ─────────────────────────
            # Sensitivity = how far is constructScore from acceptance floor?
            # STABLE   : margin > 0.20 — conclusion robust to evidence variation
            # MARGINAL : margin 0.10-0.20 — conclusion somewhat sensitive
            # SENSITIVE: margin < 0.10 — conclusion could flip with small changes
            margin = cscore - ACCEPTANCE_FLOOR
            if margin > SENSITIVITY_STABLE_MARGIN:
                sensitivity = "STABLE"
            elif margin > 0.10:
                sensitivity = "MARGINAL"
            elif margin > 0:
                sensitivity = "SENSITIVE"
            else:
                sensitivity = "BELOW_FLOOR"

            rationale = (
                f"Rule {rule} — original conf {orig:.3f}"
                + (f" reduced to {cscore:.3f} by {n_defeaters} Defeater(s)" if n_defeaters else "")
                + f" — sensitivity: {sensitivity} (margin={margin:.3f})"
            )

            self._decision_points.append(DecisionPoint(
                inference_uid     = inf_uid,
                rule_name         = rule,
                slot              = slot,
                original_conf     = orig,
                construct_score   = cscore,
                defeaters         = n_defeaters,
                sensitivity       = sensitivity,
                sensitivity_margin= round(margin, 4),
                rationale         = rationale,
            ))

    # ── Algorithm 5 Phase 2 ───────────────────────────────────────────────────
    def _build_provenance_narratives(self) -> List[ProvenanceNarrative]:
        """
        GenProvenanceNarrative — Algorithm 5 Lines 4-11.

        For each slot answer (finding) traces the derivation path
        from the synthesis Inference through the slot Claim down
        to the supporting witnesses. Generates a narrative sentence
        per finding and asserts RG tethering.
        """
        narratives = []
        syn_uid = self._syn_claim["properties"].get("uid", "") if self._syn_claim else ""

        for slot in SLOT_ORDER:
            a = self._slots.get(slot)
            if not a:
                continue

            # Trace derivation path: synthesis -> slot Claim -> witnesses
            path = []
            if syn_uid:
                path.append(syn_uid)

            # Find the Claim node for this slot
            claim_uid = ""
            for node in self._rg_nodes:
                if "Claim" not in node.get("labels", []):
                    continue
                if "Synthesised" in node.get("labels", []):
                    continue
                dm = node["properties"].get("domainMetadata", {}) or {}
                if dm.get("slot_type") == slot:
                    claim_uid = node["properties"].get("uid", "")
                    path.append(claim_uid)
                    break

            # Add witnesses
            path.extend(a.witness_uids[:3])

            # Find relevant decision points for this slot
            relevant_dps = [
                dp.inference_uid for dp in self._decision_points
                if dp.slot == slot
            ]

            # Generate narrative sentence per slot template
            narrative = self._gen_slot_narrative(slot, a)

            # RG tether — the synthesis Claim or slot Claim UID
            tether = claim_uid or syn_uid

            narratives.append(ProvenanceNarrative(
                finding          = slot,
                statement        = a.statement,
                derivation_path  = path,
                decisions        = relevant_dps,
                narrative        = narrative,
                rg_tether        = tether,
            ))

        return narratives

    def _gen_slot_narrative(self, slot: str, a: SlotAnswer) -> str:
        """Generate a natural language sentence for one slot finding."""
        status_note = " (contested)" if a.status == "contested" else ""
        conf_note   = f" [conf={a.construct_score:.2f}]"

        templates = {
            "WHAT":     f"The focal document is {a.statement}{status_note}.{conf_note}",
            "WHO":      f"The responsible parties are {a.statement}.{conf_note}",
            "HOW":      f"This was carried out via {a.statement}.{conf_note}",
            "WHEN":     f"The relevant date is {a.statement}.{conf_note}",
            "EVIDENCE": f"Supporting evidence: {a.statement}.{conf_note}",
        }
        return templates.get(slot, f"{slot}: {a.statement}{status_note}.{conf_note}")

    # ── Algorithm 5 Phase 3 ───────────────────────────────────────────────────
    def _build_conflict_explanations(self) -> List[ConflictExplanation]:
        """
        GenConflictExplanation / GenLimitationExplanation — Algorithm 5 Lines 12-23.

        For each limitation from CONSTRUCT:
          - If conflict-derived: looks up the CONTRADICTS edge in the EG,
            gathers context (witness surfaces, slot, rule), generates explanation
          - Otherwise: generates a limitation explanation from the derivation subgraph
        Asserts RG tethering for each explanation.
        """
        explanations  = []
        limitations   = self._constr.get("limitations", [])
        conflicts_raw = self._cr.get("conflicts", [])

        # Build CONTRADICTS edge lookup by slot
        contradicts_by_slot: Dict[str, List[Dict]] = {}
        for c in conflicts_raw:
            slot = ""
            desc = c.get("description", "")
            m    = re.search(r"Slot '(\w+)'", desc)
            if m:
                slot = m.group(1)
            contradicts_by_slot.setdefault(slot, []).append(c)

        for lim in limitations:
            is_conflict = "contested" in lim.lower()
            slot        = ""
            for s in SLOT_ORDER:
                if s in lim:
                    slot = s
                    break

            if is_conflict and slot in contradicts_by_slot:
                # ── ConflictEdge lookup ────────────────────────────────────
                conflict = contradicts_by_slot[slot][0]
                wa_uid   = conflict.get("witness_a_uid", "")
                wb_uid   = conflict.get("witness_b_uid", "")
                rule     = conflict.get("rule", "SURFACE_MISMATCH")
                desc     = conflict.get("description", "")
                cluster  = self._defeaters[0]["properties"].get("cluster_size", 1) \
                           if self._defeaters else 1

                # ── GatherConflictContext ──────────────────────────────────
                wa_surface = self._get_witness_surface(wa_uid)
                wb_surface = self._get_witness_surface(wb_uid)
                context    = (
                    f"Slot {slot}: witness '{wa_surface}' contradicts "
                    f"'{wb_surface}' via {rule}. "
                    f"Cluster size: {cluster} raw conflict(s)."
                )

                # ── GenConflictExplanation ─────────────────────────────────
                explanation = (
                    f"The {slot} answer is disputed because {cluster} source(s) "
                    f"cite different answers for the same question. "
                    f"Specifically: '{wa_surface}' vs '{wb_surface}'. "
                    f"This is a {rule.lower().replace('_',' ')} — "
                    f"the investigator should verify which instrument applies."
                )

                tether = self._syn_claim["properties"].get("uid", "") \
                         if self._syn_claim else ""

                explanations.append(ConflictExplanation(
                    limitation   = lim,
                    is_conflict  = True,
                    conflict_rule= rule,
                    witness_a    = wa_surface,
                    witness_b    = wb_surface,
                    slot         = slot,
                    context      = context,
                    explanation  = explanation,
                    rg_tether    = tether,
                ))
            else:
                # ── GenLimitationExplanation ───────────────────────────────
                tether = ""
                if slot in self._slots:
                    for node in self._rg_nodes:
                        dm = node.get("properties", {}).get("domainMetadata", {}) or {}
                        if dm.get("slot_type") == slot and "Claim" in node.get("labels", []):
                            tether = node["properties"].get("uid", "")
                            break
                explanation = (
                    f"Limitation: {lim} "
                    f"The investigator should seek additional sources to strengthen this answer."
                )
                explanations.append(ConflictExplanation(
                    limitation   = lim,
                    is_conflict  = False,
                    conflict_rule= "",
                    witness_a    = "",
                    witness_b    = "",
                    slot         = slot,
                    context      = "",
                    explanation  = explanation,
                    rg_tether    = tether,
                ))

        return explanations

    def _get_witness_surface(self, witness_uid: str) -> str:
        """Look up a witness surface text from the EG."""
        for node in self._eg_nodes:
            if node["properties"].get("uid") == witness_uid:
                dm = node["properties"].get("domainMetadata", {}) or {}
                return dm.get("surface", witness_uid[:20])
        return witness_uid[:20]

    # ── Algorithm 5 Phase 4 ───────────────────────────────────────────────────
    def _build_decision_explanations(self) -> List[DecisionPoint]:
        """
        GenDecisionExplanation — Algorithm 5 Lines 24-37.

        Returns the decision points identified in Phase 1 with their
        analytical sensitivity estimates. Uses probe_budget=0 approach
        (no pipeline re-runs) — sensitivity is computed analytically
        as the margin between constructScore and acceptance floor.
        """
        return list(self._decision_points)

    # ── Algorithm 5 Phase 5 ───────────────────────────────────────────────────
    def _build_uncertainty_map(
        self,
        pn: List[ProvenanceNarrative],
        ce: List[ConflictExplanation],
        de: List[DecisionPoint],
    ) -> Dict:
        """
        BuildUncertaintyMap — Algorithm 5 Line 38.

        Aggregates confidence scores, contested slots, sensitivity
        estimates, and limitations into one structured uncertainty map.
        Shows the investigator where the answer is solid and where weak.
        """
        slot_uncertainty = {}
        for slot, a in self._slots.items():
            # Find sensitivity for this slot
            slot_dps = [d for d in de if d.slot == slot]
            sensitivity = slot_dps[0].sensitivity if slot_dps else "NOT_COMPUTED"
            margin      = slot_dps[0].sensitivity_margin if slot_dps else 0.0

            slot_uncertainty[slot] = {
                "confidence":    round(a.confidence, 4),
                "construct_score": round(a.construct_score, 4),
                "status":        a.status,
                "sensitivity":   sensitivity,
                "margin":        margin,
                "witnesses":     len(a.witness_uids),
                "uncertainty_level": (
                    "HIGH"     if a.status == "contested" or sensitivity == "SENSITIVE"
                    else "MEDIUM" if sensitivity == "MARGINAL" or len(a.witness_uids) == 1
                    else "LOW"
                ),
            }

        return {
            "slots":            slot_uncertainty,
            "overall_confidence": round(
                float(self._syn_claim["properties"].get(
                    "constructScore",
                    self._syn_claim["properties"].get("confidenceScore", 0.5)
                )) if self._syn_claim else 0.0, 4
            ),
            "contested_slots":  [s for s, a in self._slots.items() if a.status == "contested"],
            "sensitive_slots":  [d.slot for d in de if d.sensitivity == "SENSITIVE"],
            "marginal_slots":   [d.slot for d in de if d.sensitivity == "MARGINAL"],
            "stable_slots":     [d.slot for d in de if d.sensitivity == "STABLE"],
            "conflict_count":   len([c for c in ce if c.is_conflict]),
            "limitation_count": len(ce),
        }

    def _build_tether_map(
        self,
        pn: List[ProvenanceNarrative],
        ce: List[ConflictExplanation],
        de: List[DecisionPoint],
    ) -> Dict:
        """
        BuildTetherMap — Algorithm 5 Line 40.

        Maps every explanation sentence to its RG node.
        Used by TetherComplete to assert full provenance coverage.
        """
        tether_map = {}
        for p in pn:
            tether_map[f"pn::{p.finding}"] = {
                "text":   p.narrative[:80],
                "tether": p.rg_tether,
                "valid":  bool(p.rg_tether),
            }
        for c in ce:
            tether_map[f"ce::{c.slot}::{c.is_conflict}"] = {
                "text":   c.explanation[:80],
                "tether": c.rg_tether,
                "valid":  bool(c.rg_tether),
            }
        for d in de:
            tether_map[f"de::{d.inference_uid[:16]}"] = {
                "text":   d.rationale[:80],
                "tether": d.inference_uid,
                "valid":  bool(d.inference_uid),
            }
        return tether_map

    def _assert_tether_complete(self, tether_map: Dict) -> bool:
        """
        TetherComplete — Algorithm 5 Line 41.

        Asserts every entry in the tether map has a valid RG node.
        Logs WARNING for any untethered explanation.
        """
        all_ok = True
        for key, entry in tether_map.items():
            if not entry["valid"]:
                all_ok = False
                self._warnings.append(
                    f"TetherComplete FAIL: {key} — "
                    f"'{entry['text']}' has no RG tether"
                )
        return all_ok

    # ── Answer generation ─────────────────────────────────────────────────────
    def _generate_answer(
        self,
        pn: List[ProvenanceNarrative],
        ce: List[ConflictExplanation],
    ) -> str:
        """
        GenDerivationSummary — Algorithm 5 Line 39.

        Assembles the final natural language answer from provenance
        narratives and conflict explanations.
        """
        if not pn:
            return "No slot answers were found."

        parts = []

        # Opening — WHAT + WHO
        what_pn = next((p for p in pn if p.finding == "WHAT"), None)
        who_pn  = next((p for p in pn if p.finding == "WHO"),  None)
        if what_pn and who_pn:
            what_txt = what_pn.statement
            who_txt  = who_pn.statement
            mark     = " ⚠ [contested]" if self._slots.get("WHAT", SlotAnswer("","","supported",0,0)).status == "contested" else ""
            parts.append(f"The focal document is {what_txt}{mark}, involving {who_txt}.")
        elif what_pn:
            mark = " ⚠ [contested]" if self._slots.get("WHAT", SlotAnswer("","","supported",0,0)).status == "contested" else ""
            parts.append(f"The focal document is {what_pn.statement}{mark}.")
        elif who_pn:
            parts.append(f"Involving: {who_pn.statement}.")

        # HOW
        how_pn = next((p for p in pn if p.finding == "HOW"), None)
        if how_pn:
            parts.append(f"The responsible party was {how_pn.statement}.")

        # WHEN
        when_pn = next((p for p in pn if p.finding == "WHEN"), None)
        if when_pn:
            parts.append(f"This occurred on {when_pn.statement}.")

        # EVIDENCE
        ev_pn = next((p for p in pn if p.finding == "EVIDENCE"), None)
        if ev_pn:
            doc_ids  = _extract_doc_ids(ev_pn.statement)
            cite_str = " ".join(f"[Doc: {d}]" for d in doc_ids[:3])
            parts.append(f"Supporting evidence: {ev_pn.statement} {cite_str}.")

        # Conflict explanations
        for c in ce:
            if c.is_conflict:
                parts.append(f"\n⚠ NOTE: {c.explanation}")

        # Confidence footer
        if self._syn_claim:
            conf = float(self._syn_claim["properties"].get(
                "constructScore",
                self._syn_claim["properties"].get("confidenceScore", 0.5)
            ))
            label    = _confidence_label(conf)
            contested = [s for s, a in self._slots.items() if a.status == "contested"]
            footer   = f"\n[Confidence: {label} ({conf:.2f})"
            if contested:
                footer += f" | Contested: {', '.join(contested)}"
            footer += "]"
            parts.append(footer)

        return " ".join(p for p in parts if p)

    def _build_evidence_chain(
        self,
        pn: List[ProvenanceNarrative],
        ce: List[ConflictExplanation],
        de: List[DecisionPoint],
    ) -> List[str]:
        """Build full audit trail including Algorithm 5 components."""
        stats = self._tr.get("stats", {})
        chain = [
            f"ALIGN: Retrieved {stats.get('artifacts','?')} documents, "
            f"{stats.get('anchors','?')} anchors, {stats.get('mentions','?')} mentions",
            f"TRACE: Wrote {stats.get('witnesses_kept','?')} witnesses across "
            f"{len(self._slots)} slots",
            f"CONFLICT: {self._cr.get('stats',{}).get('conflicts_found',0)} conflicts, "
            f"{self._cr.get('stats',{}).get('defeaters_created',0)} Defeaters",
            f"CONSTRUCT: {self._constr.get('stats',{}).get('inferences_weakened',0)} "
            f"confidence reductions, synthesis={self._constr.get('stats',{}).get('synthesis_type','')} "
            f"conf={self._constr.get('stats',{}).get('synthesis_confidence','')}",
            f"EXPLAIN: {len(pn)} provenance narratives, "
            f"{len(ce)} conflict/limitation explanations, "
            f"{len(de)} decision points analysed",
            "",
            "Derivation subgraph:",
            f"  {len(self._deriv_subgraph['nodes'])} nodes, "
            f"{len(self._deriv_subgraph['edges'])} edges",
            "",
            "Slot confidence scores:",
        ]
        for slot in SLOT_ORDER:
            a = self._slots.get(slot)
            if a:
                dp = next((d for d in de if d.slot == slot), None)
                sens = dp.sensitivity if dp else "N/A"
                chain.append(
                    f"  {slot:<12} status={a.status:<12} "
                    f"conf={a.confidence:.4f}  constructScore={a.construct_score:.4f}  "
                    f"sensitivity={sens}"
                )
        if de:
            chain += ["", "Decision points:"]
            for d in de:
                chain.append(f"  {d.rule_name:<48} {d.sensitivity}")
        if ce:
            chain += ["", "Conflict/Limitation explanations:"]
            for c in ce:
                tag = "[CONFLICT]" if c.is_conflict else "[LIMITATION]"
                chain.append(f"  {tag} {c.explanation[:100]}")
        return chain

    def _collect_citations(self) -> List[str]:
        citations = set()
        for a in self._slots.values():
            for doc_id in a.doc_ids:
                citations.add(doc_id)
        for uid_list in [a.witness_uids for a in self._slots.values()]:
            for w_uid in uid_list:
                for node in self._eg_nodes:
                    if node.get("properties", {}).get("uid") == w_uid:
                        dm = node["properties"].get("domainMetadata", {}) or {}
                        anchor = dm.get("anchor_id", "")
                        if "::" in anchor:
                            citations.add(anchor.split("::")[0])
                        kg0 = dm.get("kg0_entity_id", "")
                        if kg0:
                            citations.add(f"kg0:{kg0}")
                        break
        return sorted(citations)


# ══════════════════════════════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════════════════════════════
def main():
    print("=" * 62)
    print("  EXPLAIN OPERATOR")
    print("=" * 62)
    print(f"\nLoading {INPUT_PATH}...")
    with open(INPUT_PATH) as f:
        data = json.load(f)

    op     = Explain(data)
    result = op.execute()

    if op._bundle_first:
        print("\n── Bundle Input ──────────────────────────────────────────────")
        print(f"  Construct bundle : {data.get('construct_bundle_id', '')}")
        print(f"  Trace bundle     : {data.get('trace_bundle_id', '')}")
        print(f"  Conflict bundle  : {data.get('conflict_bundle_id', '')}")

        print("\n── Answer ───────────────────────────────────────────────────")
        print()
        print(result.answer_text)

        print("\n── Confidence ───────────────────────────────────────────────")
        print(f"  Score : {result.confidence_score:.4f}")
        print(f"  Label : {result.confidence_label}")

        print("\n── Tether Map ───────────────────────────────────────────────")
        print(f"  Entries  : {result.stats.get('tethers', 0)}")
        print(f"  Failures : {result.stats.get('tether_failures', 0)}")

        output = {
            "schema_version": "explain-bundle.chain.v1",
            "explain_bundle_id": result.explain_bundle.get("bundle_id", result.explain_node_uid),
            "construct_bundle_id": data.get("construct_bundle_id", ""),
            "trace_bundle_id": data.get("trace_bundle_id", ""),
            "conflict_bundle_id": data.get("conflict_bundle_id", ""),
            "answer_text": result.answer_text,
            "confidence_score": result.confidence_score,
            "confidence_label": result.confidence_label,
            "citations": result.citations,
            "evidence_chain": result.evidence_chain,
            "warnings": result.warnings,
            "stats": result.stats,
            "explain_bundle": result.explain_bundle,
            "construct_bundle": data,
            "trace_bundle": data.get("trace_bundle", {}),
            "conflict_bundle": data.get("conflict_bundle", {}),
            "plain_text_path": str(TEXT_PATH),
            "provenance_manifest_delta": {
                "operator": "EXPLAIN",
                "input_construct_bundle_id": data.get("construct_bundle_id", ""),
                "output_explain_bundle_id": result.explain_bundle.get("bundle_id", result.explain_node_uid),
            },
        }

        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, default=str)

        with open(TEXT_PATH, "w", encoding="utf-8") as f:
            f.write("INVESTIGATOR ANSWER\n")
            f.write("=" * 60 + "\n\n")
            f.write(result.answer_text + "\n\n")
            f.write("CITATIONS\n")
            f.write("-" * 60 + "\n")
            for c in result.citations:
                f.write(f"  {c}\n")
            f.write("\nEVIDENCE CHAIN (AUDIT TRAIL)\n")
            f.write("-" * 60 + "\n")
            for step in result.evidence_chain:
                f.write(f"  {step}\n")

        print(f"\nSaved to {OUTPUT_PATH}")
        print(f"Plain text saved to {TEXT_PATH}")
        return

    print("\n── Answer ───────────────────────────────────────────────────")
    print()
    print(result.answer_text)

    print("\n── Confidence ───────────────────────────────────────────────")
    print(f"  Score : {result.confidence_score:.4f}")
    print(f"  Label : {result.confidence_label}")
    if result.contested_slots:
        print(f"  ⚠ Contested: {', '.join(result.contested_slots)}")

    print("\n── Derivation Subgraph ──────────────────────────────────────")
    print(f"  Nodes : {len(result.derivation_subgraph.get('nodes',[]))}")
    print(f"  Edges : {len(result.derivation_subgraph.get('edges',[]))}")

    print("\n── Decision Points ──────────────────────────────────────────")
    for dp in result.decision_points:
        print(f"  [{dp.sensitivity:<10}] {dp.rule_name:<45} margin={dp.sensitivity_margin:.3f}")

    print("\n── Conflict Explanations ────────────────────────────────────")
    for ce in result.conflict_explanations:
        tag = "CONFLICT" if ce.is_conflict else "LIMITATION"
        print(f"  [{tag}] {ce.explanation[:90]}")

    print("\n── Uncertainty Map ──────────────────────────────────────────")
    um = result.uncertainty_map
    for slot, entry in um.get("slots", {}).items():
        print(f"  {slot:<12} uncertainty={entry['uncertainty_level']:<8}  "
              f"sensitivity={entry['sensitivity']}")

    print(f"\n── Tether Map ───────────────────────────────────────────────")
    print(f"  Entries  : {len(result.tether_map)}")
    print(f"  Complete : {result.tether_complete}")

    print("\n── Citations ────────────────────────────────────────────────")
    for c in result.citations:
        print(f"  {c}")

    if result.warnings:
        print("\n── Warnings ─────────────────────────────────────────────────")
        for w in result.warnings:
            print(f"  ⚠ {w}")

    print("\n── Stats ────────────────────────────────────────────────────")
    for k, v in result.stats.items():
        print(f"  {k:<30}: {v}")

    # Save JSON
    output = {
        "trace_result":    data["trace_result"],
        "conflict_result": data["conflict_result"],
        "construct_result":data["construct_result"],
        "eg":              data["eg"],
        "rg":              data["rg"],
        "explain_result": {
            "answer_text":           result.answer_text,
            "confidence_score":      result.confidence_score,
            "confidence_label":      result.confidence_label,
            "contested_slots":       result.contested_slots,
            "missing_slots":         result.missing_slots,
            "citations":             result.citations,
            "evidence_chain":        result.evidence_chain,
            "warnings":              result.warnings,
            "stats":                 result.stats,
            "explain_node_uid":      result.explain_node_uid,
            "eg_root_uid":           data["trace_result"].get("eg_root_uid", ""),
            "rg_root_uid":           data["trace_result"].get("rg_root_uid", ""),
            # Algorithm 5 components
            "provenance_narratives": [
                {"finding": p.finding, "statement": p.statement,
                 "narrative": p.narrative, "rg_tether": p.rg_tether,
                 "derivation_path": p.derivation_path,
                 "decisions": p.decisions}
                for p in result.provenance_narratives
            ],
            "conflict_explanations": [
                {"limitation": c.limitation, "is_conflict": c.is_conflict,
                 "slot": c.slot, "explanation": c.explanation,
                 "context": c.context, "rg_tether": c.rg_tether,
                 "witness_a": c.witness_a, "witness_b": c.witness_b}
                for c in result.conflict_explanations
            ],
            "decision_points": [
                {"rule_name": d.rule_name, "slot": d.slot,
                 "original_conf": d.original_conf,
                 "construct_score": d.construct_score,
                 "sensitivity": d.sensitivity,
                 "sensitivity_margin": d.sensitivity_margin,
                 "rationale": d.rationale}
                for d in result.decision_points
            ],
            "uncertainty_map":       result.uncertainty_map,
            "tether_map":            result.tether_map,
            "tether_complete":       result.tether_complete,
            "derivation_subgraph":   {
                "node_count": len(result.derivation_subgraph.get("nodes", [])),
                "edge_count":  len(result.derivation_subgraph.get("edges", [])),
            },
        },
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2, default=str)

    # Save plain text
    with open(TEXT_PATH, "w") as f:
        f.write("INVESTIGATOR ANSWER\n")
        f.write("=" * 60 + "\n\n")
        f.write(result.answer_text + "\n\n")
        f.write("CITATIONS\n" + "-" * 60 + "\n")
        for c in result.citations:
            f.write(f"  {c}\n")
        f.write("\nLIMITATIONS\n" + "-" * 60 + "\n")
        for ce in result.conflict_explanations:
            f.write(f"  {ce.explanation}\n")
        f.write("\nEVIDENCE CHAIN (AUDIT TRAIL)\n" + "-" * 60 + "\n")
        for step in result.evidence_chain:
            f.write(f"  {step}\n")

    print(f"\nSaved to {OUTPUT_PATH}")
    print(f"Plain text saved to {TEXT_PATH}")


if __name__ == "__main__":
    main()
