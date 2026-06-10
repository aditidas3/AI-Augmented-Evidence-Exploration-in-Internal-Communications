from __future__ import annotations

from .shared import *  # noqa: F401,F403
from .shared import (
    _NEO4J_QUERY_ERRORS,
    _normalize_time_filter_bound,
    _node_id_expression,
    _node_identity_value,
    _node_match_condition,
    _node_text_expression,
    _resolved_edge_relationship_types,
)
from ..selection.artifact_selection import (
    artifact_bridge_tokens as _artifact_bridge_tokens,
    artifact_selection_score as _artifact_selection_score,
    has_cross_family_bridge as _has_cross_family_bridge,
)
from ..relevance.intent_relevance import score_candidate_relevance

class Phase2_ArtifactSelection:
    """
    Select bounded artifact set from candidates.

    Algorithm: Weighted set cover.
    Score each artifact by retrieval score, greedily select to
    maximize coverage of required families and graph spec variable needs.
    """

    def __init__(self, config: AlignConfig):
        self.config = config
        self.last_selection_diagnostics: Dict[str, Any] = {}

    def execute(
        self,
        candidates: List[CandidateArtifact],
        intent: IntentObject,
    ) -> List[CandidateArtifact]:
        """Execute Phase 2: select artifact set."""
        logger.info(f"Phase 2: Selecting from {len(candidates)} candidates")

        requested_required_families = {
            self.config.canonical_family(x) for x in intent.required_families
        }
        candidate_families = {
            self.config.canonical_family(candidate.family)
            for candidate in candidates
            if candidate.family
        }
        skipped_required_families = requested_required_families - candidate_families
        required_families = requested_required_families & candidate_families
        k = self.config.k_artifacts

        # Greedy weighted set cover
        selected: List[CandidateArtifact] = []
        covered_families: Set[str] = set()
        remaining = list(candidates)
        relevance_by_id: Dict[str, Any] = {}
        audit_by_id: Dict[str, Dict[str, Any]] = {}

        for rank, candidate in enumerate(candidates, start=1):
            relevance = score_candidate_relevance(
                candidate,
                intent,
                required_families_override=required_families,
            )
            reject_reasons = self._candidate_rejection_reasons(relevance)
            selectable = not reject_reasons
            relevance_by_id[candidate.artifact_id] = relevance

            audit_row = self._candidate_audit_row(
                candidate=candidate,
                relevance=relevance,
                retrieval_rank=rank,
                selectable=selectable,
                reject_reasons=reject_reasons,
            )
            audit_by_id[candidate.artifact_id] = audit_row
            candidate.metadata["intent_relevance"] = audit_row["intent_relevance"]
            candidate.metadata["phase2_selection"] = {
                "selectable": selectable,
                "decision": audit_row["decision"],
                "reject_reasons": reject_reasons,
                "penalty_reasons": audit_row["penalty_reasons"],
            }

        while remaining and len(selected) < k:
            # Score each remaining candidate
            best = None
            best_score = float("-inf")

            for candidate in remaining:
                score = _artifact_selection_score(
                    candidate,
                    selected,
                    required_families=required_families,
                    covered_families=covered_families,
                    family_coverage_weight=self.config.family_coverage_weight,
                    diversity_weight=self.config.diversity_weight,
                )
                score += self._document_group_boost(candidate, selected)
                relevance = relevance_by_id.get(candidate.artifact_id)
                if relevance is None:
                    relevance = score_candidate_relevance(
                        candidate,
                        intent,
                        required_families_override=required_families,
                    )
                    relevance_by_id[candidate.artifact_id] = relevance
                if not self._candidate_is_selectable(relevance):
                    continue
                score += self.config.intent_relevance_weight * relevance.selection_bonus
                audit_row = audit_by_id.get(candidate.artifact_id)
                if audit_row is not None:
                    previous_score = audit_row.get("best_selection_score")
                    previous_score = (
                        float(previous_score)
                        if previous_score is not None
                        else float("-inf")
                    )
                    audit_row["best_selection_score"] = round(
                        max(previous_score, score),
                        4,
                    )

                if score > best_score:
                    best_score = score
                    best = candidate

            if best is None:
                break

            selected.append(best)
            covered_families.add(self.config.canonical_family(best.family))
            remaining.remove(best)

        # Size bound (do not rely on assert; affects runtime correctness under -O)
        if len(selected) > self.config.k_artifacts:
            raise ValueError(
                f"Artifact set size {len(selected)} exceeds "
                f"K_artifacts={self.config.k_artifacts}"
            )

        # Constructibility check + repair (paper: test + RepairConstructibility)
        if self.config.enable_constructibility_repair:
            selected = self._repair_constructibility(
                selected=selected,
                remaining=remaining,
                required_families=required_families,
                k=k,
                intent=intent,
            )
            covered_families = {
                self.config.canonical_family(c.family)
                for c in selected
            }

        selected_ids = {candidate.artifact_id for candidate in selected}
        for selection_rank, candidate in enumerate(selected, start=1):
            audit_row = audit_by_id.get(candidate.artifact_id)
            if audit_row is None:
                continue
            audit_row["decision"] = "selected"
            audit_row["selection_rank"] = selection_rank
            candidate.metadata["phase2_selection"] = {
                "selectable": True,
                "decision": "selected",
                "reject_reasons": [],
                "penalty_reasons": audit_row["penalty_reasons"],
                "selection_rank": selection_rank,
            }
        for audit_row in audit_by_id.values():
            if audit_row["artifact_id"] in selected_ids:
                continue
            if audit_row["selectable"]:
                audit_row["decision"] = "not_selected"

        self.last_selection_diagnostics = self._phase2_selection_diagnostics(
            audit_by_id,
            selected,
        )
        self.last_selection_diagnostics.update({
            "requested_required_families": sorted(requested_required_families),
            "candidate_families": sorted(candidate_families),
            "enforceable_required_families": sorted(required_families),
            "skipped_required_families": sorted(skipped_required_families),
            "covered_required_families": sorted(covered_families & required_families),
        })

        uncovered = required_families - covered_families
        if uncovered and self.config.enforce_required_families:
            raise ValueError(
                f"Required families not covered: {uncovered}. "
                f"Covered: {covered_families}. "
                f"Candidates had families: "
                f"{set(c.family for c in candidates)}"
            )

        logger.info(
            f"  Selected {len(selected)} artifacts, "
            f"covering families: {covered_families}"
        )
        return selected

    # ----------------------------------------------------------
    # Constructibility heuristics (metadata-only)
    # ----------------------------------------------------------

    def _candidate_audit_row(
        self,
        *,
        candidate: CandidateArtifact,
        relevance: Any,
        retrieval_rank: int,
        selectable: bool,
        reject_reasons: List[str],
    ) -> Dict[str, Any]:
        penalty_reasons: List[str] = []
        if relevance.missing_hard_entity_penalty > 0.0:
            penalty_reasons.append("missing_hard_entity_penalty_applied")
        if relevance.scope_drift_penalty > 0.0:
            penalty_reasons.append("scope_drift_penalty_applied")

        intent_relevance = {
            "total": round(relevance.total, 4),
            "hard_entity_coverage": round(
                relevance.hard_entity_coverage, 4
            ),
            "required_hard_entity_coverage": round(
                relevance.required_hard_entity_coverage, 4
            ),
            "topic_coverage": round(relevance.topic_coverage, 4),
            "focus_coverage": round(relevance.focus_coverage, 4),
            "time_score": round(relevance.time_score, 4),
            "family_score": round(relevance.family_score, 4),
            "missing_hard_entity_penalty": round(
                relevance.missing_hard_entity_penalty, 4
            ),
            "scope_drift_penalty": round(
                relevance.scope_drift_penalty, 4
            ),
            "has_text": bool(relevance.has_text),
        }
        return {
            "artifact_id": candidate.artifact_id,
            "artifact_name": candidate.artifact_name,
            "family": candidate.family,
            "retrieval_rank": retrieval_rank,
            "lex_rank": candidate.lex_rank,
            "sem_rank": candidate.sem_rank,
            "lex_score": round(float(candidate.lex_score or 0.0), 4),
            "sem_score": round(float(candidate.sem_score or 0.0), 4),
            "fused_score": round(float(candidate.fused_score or 0.0), 4),
            "intent_relevance": intent_relevance,
            "selectable": selectable,
            "decision": "selectable" if selectable else "rejected",
            "reject_reasons": list(reject_reasons),
            "penalty_reasons": penalty_reasons,
            "best_selection_score": None,
            "selection_rank": None,
        }

    def _phase2_selection_diagnostics(
        self,
        audit_by_id: Dict[str, Dict[str, Any]],
        selected: List[CandidateArtifact],
    ) -> Dict[str, Any]:
        rejection_counts: Dict[str, int] = {}
        for row in audit_by_id.values():
            for reason in row.get("reject_reasons", []):
                rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
        candidates = sorted(
            audit_by_id.values(),
            key=lambda row: int(row.get("retrieval_rank", 0) or 0),
        )
        return {
            "candidate_count": len(candidates),
            "selected_count": len(selected),
            "rejected_count": sum(
                1 for row in candidates if row.get("decision") == "rejected"
            ),
            "not_selected_count": sum(
                1 for row in candidates if row.get("decision") == "not_selected"
            ),
            "selected_artifact_ids": [c.artifact_id for c in selected],
            "rejection_counts": rejection_counts,
            "candidates": candidates,
        }

    def _candidate_rejection_reasons(self, relevance: Any) -> List[str]:
        reasons: List[str] = []
        required_floor = float(
            getattr(self.config, "min_required_hard_entity_coverage", 0.0)
            or 0.0
        )
        if (
            relevance.has_text
            and relevance.missing_hard_entity_penalty >= 2.5
            and relevance.required_hard_entity_coverage < required_floor
        ):
            reasons.append("missing_required_hard_entity")
        if relevance.time_score < 0.0:
            reasons.append("outside_time_filter")
        if relevance.scope_drift_penalty >= 1.0:
            reasons.append("high_scope_drift")
        floor = float(
            getattr(self.config, "min_intent_relevance_score", 0.0) or 0.0
        )
        if relevance.total < floor:
            reasons.append("below_min_intent_relevance_score")
        return reasons

    def _candidate_is_selectable(self, relevance) -> bool:
        return not self._candidate_rejection_reasons(relevance)

    def _document_group_boost(
        self,
        candidate: CandidateArtifact,
        selected: List[CandidateArtifact],
    ) -> float:
        if not selected:
            return 0.0
        weight = float(
            getattr(self.config, "document_group_boost_weight", 0.0)
            or 0.0
        )
        if weight <= 0.0:
            return 0.0
        candidate_keys = self._document_group_keys(candidate)
        if not candidate_keys:
            return 0.0
        for artifact in selected:
            if candidate_keys & self._document_group_keys(artifact):
                return weight
        return 0.0

    @staticmethod
    def _document_group_keys(candidate: CandidateArtifact) -> Set[str]:
        metadata = candidate.metadata or {}
        keys: Set[str] = set()
        for field in (
            "source_document_id",
            "document_id",
            "thread_id",
            "conversation_id",
            "email_thread_id",
            "message_thread_id",
            "source_uri",
        ):
            raw_value = metadata.get(field)
            values = raw_value if isinstance(raw_value, (list, tuple, set)) else [raw_value]
            for value in values:
                normalized = str(value or "").strip().lower()
                if not normalized or normalized in {"unknown", "none", "null"}:
                    continue
                keys.add(f"{field}:{normalized}")
        return keys

    def _artifact_bridge_tokens(self, c: CandidateArtifact) -> Set[str]:
        """
        Extract simple tokens for bridge scoring. This is a lightweight,
        metadata-only proxy for "can these artifacts form a connected chain?"
        """
        return _artifact_bridge_tokens(c)

    def _has_cross_family_bridge(self, selected: List[CandidateArtifact]) -> bool:
        """
        True if there exists at least one pair of artifacts from different
        families sharing a non-trivial token intersection.
        """
        return _has_cross_family_bridge(selected)

    def _repair_constructibility(
        self,
        selected: List[CandidateArtifact],
        remaining: List[CandidateArtifact],
        required_families: Set[str],
        k: int,
        intent: IntentObject,
    ) -> List[CandidateArtifact]:
        """
        Attempt to ensure the selected set has at least one cross-family
        bridge signal. Adds artifacts greedily from remaining candidates.
        """
        # Only meaningful when we have >=2 required families.
        if len(required_families) <= 1:
            return selected

        if self._has_cross_family_bridge(selected):
            return selected

        selected_ids = {c.artifact_id for c in selected}
        selected_tokens = {
            c.artifact_id: self._artifact_bridge_tokens(c) for c in selected
        }

        while (
            remaining
            and len(selected) < k
            and not self._has_cross_family_bridge(selected)
        ):
            best = None
            best_score = -1

            for cand in remaining:
                if cand.artifact_id in selected_ids:
                    continue
                relevance = score_candidate_relevance(
                    cand,
                    intent,
                    required_families_override=required_families,
                )
                if not self._candidate_is_selectable(relevance):
                    continue
                cand_tokens = self._artifact_bridge_tokens(cand)
                # Score = max overlap with any already-selected artifact from a different family
                score = 0
                for s in selected:
                    if s.family == cand.family:
                        continue
                    score = max(
                        score,
                        len(
                            cand_tokens
                            & selected_tokens.get(s.artifact_id, set())
                        ),
                    )
                # Small bonus for being in a required family
                if cand.family in required_families:
                    score += 1
                if score > best_score:
                    best_score = score
                    best = cand

            if best is None:
                break
            selected.append(best)
            selected_ids.add(best.artifact_id)
            selected_tokens[best.artifact_id] = self._artifact_bridge_tokens(best)
            remaining.remove(best)

        if self.config.enforce_constructibility and not self._has_cross_family_bridge(selected):
            raise ValueError(
                "Phase 2 constructibility repair failed: selected artifacts do not contain a "
                "cross-family bridge signal (metadata-only heuristic)."
            )
        return selected


# ============================================================
# Phase 3: Anchor Extraction and Mention Extraction
# ============================================================
