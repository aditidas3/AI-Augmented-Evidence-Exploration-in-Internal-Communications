"""
operator_loggers.py — Per-operator typed logging helpers.

Thin wrappers over OrchestrationLogger.log_outcome() so operator code
stays readable. Instead of writing:

    logger.log_outcome(run_id, inv_id, "candidate_set", "retrieval_pool_size",
                       metric_value_num=127, metric_unit="count")

operators write:

    align = AlignLogger(logger, run_id, invocation_id)
    align.log_candidate_set(pool_size=127)

All methods return the outcome_id so it can be referenced later in
triggering_outcome_ids when logging decisions.

Methods are derived from the real operator output JSONs — every metric
logged here corresponds to an actual field in the operator's output.
"""

from .service import OrchestrationLogger


class _BaseOperatorLogger:
    def __init__(self, logger: OrchestrationLogger, run_id: str, invocation_id: str) -> None:
        self._logger        = logger
        self._run_id        = run_id
        self._invocation_id = invocation_id

    def _log(self, outcome_kind, outcome_name, severity="info", **kwargs) -> str:
        return self._logger.log_outcome(
            run_id        = self._run_id,
            invocation_id = self._invocation_id,
            outcome_kind  = outcome_kind,
            outcome_name  = outcome_name,
            severity      = severity,
            **kwargs,
        )


# ---------------------------------------------------------------------------
# ALIGN
# Source fields: corpus_stats.{artifact_count, node_count, edge_count,
#   families, collections}, result.{artifact_set, all_anchors, all_mentions,
#   entity_hypotheses, link_hypotheses, subgraphs, diagnostics.{
#   candidates_retrieved, artifacts_selected, total_anchors, total_mentions,
#   subgraphs_discovered, valid_subgraphs, witnesses_generated, total_time_s,
#   retrieval_mode}}
# ---------------------------------------------------------------------------

class AlignLogger(_BaseOperatorLogger):

    def log_candidate_set(self, pool_size: int) -> str:
        """
        Candidates retrieved before selection.
        Source: diagnostics.candidates_retrieved
        """
        return self._log("candidate_set", "retrieval_pool_size",
                         metric_name="pool_size", metric_value_num=pool_size,
                         metric_unit="count")

    def log_artifact_selection(self, selected_count: int, payload: dict = None) -> str:
        """
        Artifacts kept after selection from the candidate pool.
        Source: diagnostics.artifacts_selected / len(result.artifact_set)
        """
        return self._log("artifact_selection", "selected_artifact_count",
                         metric_name="selected_count", metric_value_num=selected_count,
                         metric_unit="count", payload=payload)

    def log_anchor_set(self, anchor_count: int) -> str:
        """
        Anchors found across selected artifacts.
        Source: diagnostics.total_anchors / len(result.all_anchors)
        """
        return self._log("anchor_set", "anchor_count",
                         metric_name="anchor_count", metric_value_num=anchor_count,
                         metric_unit="count")

    def log_mention_count(self, mention_count: int, suppressed: int = 0) -> str:
        """
        Total mentions extracted and how many were suppressed.
        Source: diagnostics.total_mentions / diagnostics.suppressed_mentions
        """
        return self._log("mention_set", "mention_count",
                         metric_name="mention_count", metric_value_num=mention_count,
                         metric_unit="count",
                         payload={"suppressed": suppressed})

    def log_entity_hypotheses(self, count: int) -> str:
        """
        Entity hypotheses generated (nodes in corpus graph).
        Source: diagnostics.entity_hypotheses / corpus_stats.node_count
        """
        return self._log("entity_hypothesis_set", "entity_hypothesis_count",
                         metric_name="entity_hypothesis_count", metric_value_num=count,
                         metric_unit="count")

    def log_link_hypotheses(self, count: int) -> str:
        """
        Link hypotheses generated (edges in corpus graph).
        Source: diagnostics.link_hypotheses / corpus_stats.edge_count
        """
        return self._log("link_hypothesis_set", "link_hypothesis_count",
                         metric_name="link_hypothesis_count", metric_value_num=count,
                         metric_unit="count")

    def log_subgraph_discovery(self, discovered: int, valid: int,
                               witnesses_generated: int) -> str:
        """
        Subgraph search results.
        Source: diagnostics.{subgraphs_discovered, valid_subgraphs, witnesses_generated}
        """
        return self._log("subgraph_discovery", "subgraph_counts",
                         metric_name="valid_subgraphs", metric_value_num=valid,
                         metric_unit="count",
                         payload={
                             "discovered":          discovered,
                             "valid":               valid,
                             "witnesses_generated": witnesses_generated,
                         })

    def log_best_subgraph(self, score: float, hard_coverage: float,
                          soft_coverage: float, coherence_score: float) -> str:
        """
        Quality metrics of the top-ranked subgraph.
        Source: result.subgraphs[0].{score, hard_coverage, soft_coverage, coherence_score}
        """
        return self._log("quality_estimate", "best_subgraph_score",
                         metric_name="subgraph_score", metric_value_num=score,
                         payload={
                             "hard_coverage":   hard_coverage,
                             "soft_coverage":   soft_coverage,
                             "coherence_score": coherence_score,
                         })

    def log_retrieval_mode(self, mode: str) -> str:
        """
        Retrieval mode used (e.g. 'neo4j+lexical').
        Source: diagnostics.retrieval_mode
        """
        return self._log("scope_diagnostic", "retrieval_mode",
                         metric_value_text=mode)

    def log_scope_diagnostic(self, scope_size: int, truncated: bool = False,
                              payload: dict = None) -> str:
        """
        Scope size (number of families) and whether it was truncated.
        Source: len(corpus_stats.families)
        """
        severity = "warning" if truncated else "info"
        return self._log("scope_diagnostic", "scope_size",
                         severity=severity,
                         metric_name="scope_size", metric_value_num=scope_size,
                         payload={**(payload or {}), "truncated": truncated})

    def log_missing_family(self, family_name: str) -> str:
        """
        A required artifact family was not found in the corpus.
        Severity: warning
        """
        return self._log("scope_diagnostic", "missing_required_family",
                         severity="warning",
                         metric_value_text=family_name,
                         payload={"missing_family": family_name})

    def log_retrieval_latency(self, latency_ms: int) -> str:
        """
        Total ALIGN wall-clock time.
        Source: diagnostics.total_time_s (converted to ms by pipeline)
        """
        return self._log("latency_measurement", "retrieval_latency",
                         metric_name="latency_ms", metric_value_num=latency_ms,
                         metric_unit="ms")


# ---------------------------------------------------------------------------
# TRACE
# Source fields: trace_result.stats.{artifacts, anchors, mentions,
#   witnesses_kept, claims, inferences, frame_witnesses, coref_groups},
#   len(eg.nodes), len(eg.edges), len(rg.nodes), len(rg.edges),
#   diagnostics[*].message
# ---------------------------------------------------------------------------

class TraceLogger(_BaseOperatorLogger):

    def log_witnesses_kept(self, count: int) -> str:
        """
        Witness nodes written to the Evidence Graph.
        Source: trace_result.stats.witnesses_kept
        """
        return self._log("witness_check", "witnesses_kept",
                         metric_name="witness_count", metric_value_num=count,
                         metric_unit="count")

    def log_claims_written(self, count: int) -> str:
        """
        Claim nodes written to the Reasoning Graph (one per slot).
        Source: trace_result.stats.claims
        """
        return self._log("chain_candidate_set", "claims_written",
                         metric_name="claim_count", metric_value_num=count,
                         metric_unit="count")

    def log_inferences_written(self, count: int) -> str:
        """
        Inference nodes written to the Reasoning Graph.
        Source: trace_result.stats.inferences
        """
        return self._log("chain_selected", "inferences_written",
                         metric_name="inference_count", metric_value_num=count,
                         metric_unit="count")

    def log_frame_witnesses(self, count: int) -> str:
        """
        FrameWitness nodes written (best subgraph representatives).
        Source: trace_result.stats.frame_witnesses
        """
        return self._log("witness_check", "frame_witnesses",
                         metric_name="frame_witness_count", metric_value_num=count,
                         metric_unit="count")

    def log_coref_groups(self, count: int, corroborates_edges: int) -> str:
        """
        Co-reference groups found and CORROBORATES edges written.
        Source: trace_result.stats.coref_groups, diagnostics TRACE_PHASE3B
        """
        return self._log("validation_transition", "coref_resolution",
                         metric_name="coref_groups", metric_value_num=count,
                         payload={"corroborates_edges": corroborates_edges})

    def log_eg_size(self, node_count: int, edge_count: int) -> str:
        """
        Final size of the Evidence Graph after TRACE.
        Source: len(eg.nodes), len(eg.edges)
        """
        return self._log("quality_estimate", "eg_size",
                         metric_name="eg_node_count", metric_value_num=node_count,
                         payload={"edge_count": edge_count})

    def log_slot_candidates(self, slot_name: str, candidate_count: int) -> str:
        """
        Candidate count for a specific slot.
        Source: per-slot witness counts
        """
        return self._log("slot_candidate_set", "slot_candidate_count",
                         entity_type="slot", entity_id=slot_name,
                         metric_name="candidate_count",
                         metric_value_num=candidate_count, metric_unit="count")

    def log_slot_binding(self, slot_name: str, bound_value: str,
                         confidence: float) -> str:
        """
        Final binding for a slot.
        Source: result.slot_bindings[*]
        """
        return self._log("slot_binding", "slot_bound",
                         entity_type="slot", entity_id=slot_name,
                         metric_name="confidence", metric_value_num=confidence,
                         metric_value_text=bound_value)

    def log_coverage_check(self, filled_slots: int, total_slots: int) -> str:
        """
        How many of the required slots were filled.
        Source: trace_result.stats.claims vs expected slot count
        """
        ratio    = filled_slots / total_slots if total_slots else 0.0
        severity = "warning" if filled_slots < total_slots else "info"
        return self._log("coverage_check", "slot_coverage",
                         severity=severity,
                         metric_name="coverage_ratio", metric_value_num=ratio,
                         payload={"filled": filled_slots, "total": total_slots})

    def log_retrieval_latency(self, latency_ms: int) -> str:
        """Total TRACE wall-clock time."""
        return self._log("latency_measurement", "trace_latency",
                         metric_name="latency_ms", metric_value_num=latency_ms,
                         metric_unit="ms")

    def log_witness_check(self, passed: bool, completeness_score: float,
                          payload: dict = None) -> str:
        """Witness completeness check result."""
        severity = "info" if passed else "warning"
        return self._log("witness_check", "witness_completeness",
                         severity=severity,
                         metric_name="completeness_score",
                         metric_value_num=completeness_score,
                         metric_value_text="passed" if passed else "failed",
                         payload=payload or {})

    def log_temporal_check(self, status: str, constraint_count: int) -> str:
        """Temporal constraint check. status: 'satisfied' | 'open' | 'violated'"""
        severity = "warning" if status == "violated" else "info"
        return self._log("temporal_check", "temporal_constraint_status",
                         severity=severity,
                         metric_value_text=status,
                         metric_name="constraint_count",
                         metric_value_num=constraint_count)

    def log_commit_proposal(self, eg_commit_id: str,
                            proposal_payload: dict = None) -> str:
        """EG commit proposal — links to EGCommitLogger.log_commit_result()."""
        return self._log("commit_proposal", "eg_commit_proposal",
                         eg_commit_id=eg_commit_id,
                         payload=proposal_payload or {})

    def log_validation_transition(self, from_state: str, to_state: str) -> str:
        """Validation state transition."""
        return self._log("validation_transition", "validation_state_change",
                         metric_value_text=f"{from_state} -> {to_state}",
                         payload={"from": from_state, "to": to_state})


# ---------------------------------------------------------------------------
# MAP_TRANSFORM
# ---------------------------------------------------------------------------

class MapTransformLogger(_BaseOperatorLogger):

    def log_mapping_candidates(self, pair_count: int) -> str:
        """Number of source-target pairs being considered."""
        return self._log("mapping_candidate_set", "mapping_pair_count",
                         metric_name="pair_count", metric_value_num=pair_count,
                         metric_unit="count")

    def log_qualifier_drop(self, qualifier_type: str,
                           payload: dict = None) -> str:
        """A qualifier was dropped during mapping. Severity: warning."""
        return self._log("qualifier_drop", "qualifier_drop_detected",
                         severity="warning",
                         metric_value_text=qualifier_type,
                         payload=payload or {})

    def log_absence_witness(self, passed: bool, payload: dict = None) -> str:
        """Absence witness validation result."""
        severity = "info" if passed else "warning"
        return self._log("witness_check", "absence_witness_check",
                         severity=severity,
                         metric_value_text="passed" if passed else "failed",
                         payload=payload or {})

    def log_mapping_edges(self, edge_count: int, payload: dict = None) -> str:
        """Number of mapping edges committed."""
        return self._log("mapping_edge_set", "mapping_edge_count",
                         metric_name="edge_count", metric_value_num=edge_count,
                         metric_unit="count", payload=payload or {})


# ---------------------------------------------------------------------------
# CONFLICT
# Source fields: conflict_result.stats.{witnesses_indexed, slot_groups,
#   conflicts_found, rule1_surface_mismatch, rule2_temporal_clash,
#   rule3_negation, rule4_cross_artifact, rule5_reliability,
#   defeaters_created, claims_contested, negation_backend},
#   len(conflict_result.conflicts), conflict_result.contradicts_edges
# ---------------------------------------------------------------------------

class ConflictLogger(_BaseOperatorLogger):

    def log_witnesses_indexed(self, count: int, slot_groups: int) -> str:
        """
        Witnesses loaded and indexed for pairwise comparison.
        Source: conflict_result.stats.{witnesses_indexed, slot_groups}
        """
        return self._log("conflict_candidate_set", "witnesses_indexed",
                         metric_name="witness_count", metric_value_num=count,
                         metric_unit="count",
                         payload={"slot_groups": slot_groups})

    def log_conflicts_found(self, total: int, by_rule: dict) -> str:
        """
        Total conflicts detected, broken down by rule.
        Source: conflict_result.stats.{conflicts_found,
                rule1_surface_mismatch, rule2_temporal_clash,
                rule3_negation, rule4_cross_artifact, rule5_reliability}

        by_rule example:
            {
                "rule1_surface_mismatch": 3,
                "rule2_temporal_clash":   0,
                "rule3_negation":         0,
                "rule4_cross_artifact":   0,
                "rule5_reliability":      0,
            }
        """
        return self._log("conflict_candidate_set", "conflicts_found",
                         metric_name="conflict_count", metric_value_num=total,
                         metric_unit="count",
                         payload=by_rule)

    def log_defeaters_created(self, rebutting: int, undercutting: int) -> str:
        """
        Defeater nodes written to the Reasoning Graph, by type.
        Source: derived from conflict_result.conflicts[*].defeater_type counts.
        Rebutting → Claim.status set to 'contested'.
        Undercutting → Inference confidence reduced by CONSTRUCT.
        """
        total    = rebutting + undercutting
        severity = "warning" if total > 0 else "info"
        return self._log("uncertainty_signal", "defeaters_created",
                         severity=severity,
                         metric_name="defeater_count", metric_value_num=total,
                         metric_unit="count",
                         payload={
                             "rebutting":   rebutting,
                             "undercutting": undercutting,
                         })

    def log_claims_contested(self, count: int, slot_names: list = None) -> str:
        """
        Claims whose status was set to 'contested' (rebutting defeaters only).
        Source: conflict_result.stats.claims_contested,
                conflict_result.claims_contested (list of UIDs)
        """
        severity = "warning" if count > 0 else "info"
        return self._log("uncertainty_signal", "claims_contested",
                         severity=severity,
                         metric_name="contested_count", metric_value_num=count,
                         metric_unit="count",
                         payload={"slot_names": slot_names or []})

    def log_contradicts_edges(self, edge_count: int) -> str:
        """
        CONTRADICTS edges written to the Evidence Graph (forward + symmetric).
        Source: conflict_result.contradicts_edges
        """
        return self._log("conflict_edge_set", "contradicts_edges_written",
                         metric_name="edge_count", metric_value_num=edge_count,
                         metric_unit="count")

    def log_negation_backend(self, backend: str) -> str:
        """
        Negation detection backend used for Rule 3.
        Source: conflict_result.stats.negation_backend
        Values: 'spacy+negspacy' | 'spacy' | 'regex'
        """
        return self._log("scope_diagnostic", "negation_backend",
                         metric_value_text=backend)

    # ── kept for future use ────────────────────────────────────────────────

    def log_stance_label(self, stance: str, entity_pair: tuple,
                         payload: dict = None) -> str:
        """
        Stance assigned between two entities.
        Values: 'supports' | 'refutes' | 'ambiguous' | 'supersedes'
        """
        severity = "warning" if stance == "ambiguous" else "info"
        return self._log("stance_label", "stance_assigned",
                         severity=severity,
                         metric_value_text=stance,
                         payload={**(payload or {}),
                                   "entity_pair": list(entity_pair)})

    def log_unresolved_conflict(self, conflict_id: str,
                                cluster_size: int) -> str:
        """Unresolved conflict cluster. Severity: warning."""
        return self._log("uncertainty_signal", "unresolved_conflict",
                         severity="warning",
                         entity_type="conflict", entity_id=conflict_id,
                         metric_name="cluster_size",
                         metric_value_num=cluster_size)


# ---------------------------------------------------------------------------
# CONSTRUCT
# Source fields: construct_result.stats.{claims_loaded, inferences_loaded,
#   defeaters_loaded, contested_claims, inferences_weakened,
#   new_nodes_written, new_edges_written, synthesis_type,
#   synthesis_confidence}, construct_result.{synthesis_uid,
#   updated_inferences}
# ---------------------------------------------------------------------------

class ConstructLogger(_BaseOperatorLogger):

    def log_input_loaded(self, claims: int, inferences: int,
                         defeaters: int, contested: int) -> str:
        """
        What CONSTRUCT read from the CONFLICT output at startup.
        Source: construct_result.stats.{claims_loaded, inferences_loaded,
                defeaters_loaded, contested_claims}
        """
        return self._log("candidate_set", "construct_input_loaded",
                         metric_name="claims_loaded", metric_value_num=claims,
                         metric_unit="count",
                         payload={
                             "inferences_loaded": inferences,
                             "defeaters_loaded":  defeaters,
                             "contested_claims":  contested,
                         })

    def log_inferences_weakened(self, count: int,
                                updates: list = None) -> str:
        """
        How many Inferences had constructScore applied by Rule 3.
        Source: construct_result.stats.inferences_weakened,
                construct_result.updated_inferences

        updates example (optional, for full audit):
            [{"uid": "...", "rule": "Rule3", "constructScore": 0.2036}, ...]
        """
        severity = "warning" if count > 0 else "info"
        return self._log("uncertainty_signal", "inferences_weakened",
                         severity=severity,
                         metric_name="weakened_count", metric_value_num=count,
                         metric_unit="count",
                         payload={"updates": updates or []})

    def log_nodes_written(self, new_nodes: int, new_edges: int) -> str:
        """
        Nodes and edges added to the Reasoning Graph by CONSTRUCT.
        Source: construct_result.stats.{new_nodes_written, new_edges_written}
        """
        return self._log("quality_estimate", "construct_graph_writes",
                         metric_name="new_nodes", metric_value_num=new_nodes,
                         metric_unit="count",
                         payload={"new_edges": new_edges})

    def log_synthesis(self, synthesis_type: str, confidence: float,
                      contested_slots: list = None,
                      synthesis_uid: str = None) -> str:
        """
        The top-level synthesis result EXPLAIN will read.
        Source: construct_result.{synthesis_type, synthesis_confidence,
                synthesis_uid}, derived contested slot list.

        synthesis_type: 'composite' | 'partial' | 'null'
        confidence: weighted average of slot Claim scores (WHAT×2, EV×1.5,
                    WHO×1, HOW×1, WHEN×0.8)
        contested_slots: e.g. ['WHAT']
        """
        severity = "warning" if contested_slots else "info"
        return self._log("quality_estimate", "synthesis_result",
                         severity=severity,
                         metric_name="synthesis_confidence",
                         metric_value_num=confidence,
                         metric_value_text=synthesis_type,
                         entity_type="synthesis", entity_id=synthesis_uid or "",
                         payload={
                             "synthesis_type":   synthesis_type,
                             "synthesis_uid":    synthesis_uid,
                             "contested_slots":  contested_slots or [],
                         })

    # ── kept for future use ────────────────────────────────────────────────

    def log_findings_count(self, count: int) -> str:
        """Number of findings produced."""
        return self._log("quality_estimate", "findings_count",
                         metric_name="findings_count", metric_value_num=count,
                         metric_unit="count")

    def log_tether_result(self, passed: bool, failure_count: int = 0) -> str:
        """Tethering check result."""
        severity = "info" if passed else "warning"
        return self._log("tether_failure" if not passed else "validation_transition",
                         "tether_check",
                         severity=severity,
                         metric_name="failure_count",
                         metric_value_num=failure_count,
                         metric_value_text="passed" if passed else "failed")

    def log_limitation_count(self, count: int) -> str:
        """Number of limitations added to the answer."""
        return self._log("quality_estimate", "limitation_count",
                         metric_name="limitation_count", metric_value_num=count,
                         metric_unit="count")


# ---------------------------------------------------------------------------
# EXPLAIN
# Source fields: explain_result.{confidence_score, confidence_label,
#   contested_slots, missing_slots, citations, stats.{slots_answered,
#   slots_contested, slots_missing, citations, defeaters, confidence}}
# ---------------------------------------------------------------------------

class ExplainLogger(_BaseOperatorLogger):

    def log_slots_answered(self, answered: int, total: int,
                           contested_slots: list = None,
                           missing_slots: list = None) -> str:
        """
        How many slots had answers and which were contested or missing.
        Source: explain_result.stats.{slots_answered, slots_contested,
                slots_missing}, explain_result.{contested_slots, missing_slots}
        """
        severity = "warning" if (contested_slots or missing_slots) else "info"
        return self._log("coverage_check", "slots_answered",
                         severity=severity,
                         metric_name="slots_answered", metric_value_num=answered,
                         metric_unit="count",
                         payload={
                             "total":           total,
                             "contested_slots": contested_slots or [],
                             "missing_slots":   missing_slots  or [],
                         })

    def log_confidence(self, score: float, label: str) -> str:
        """
        Final answer confidence score and label.
        Source: explain_result.{confidence_score, confidence_label}
        label: 'HIGH' | 'MODERATE' | 'LOW' | 'VERY LOW'
        """
        severity = "warning" if label in ("LOW", "VERY LOW") else "info"
        return self._log("quality_estimate", "answer_confidence",
                         severity=severity,
                         metric_name="confidence_score", metric_value_num=score,
                         metric_value_text=label)

    def log_citations_count(self, count: int) -> str:
        """
        Total deduplicated citations collected (doc IDs + kg0 entity IDs).
        Source: explain_result.stats.citations / len(explain_result.citations)
        """
        return self._log("quality_estimate", "citations_collected",
                         metric_name="citation_count", metric_value_num=count,
                         metric_unit="count")

    def log_defeaters_reported(self, count: int) -> str:
        """
        Number of Defeater descriptions included in the answer output.
        Source: explain_result.stats.defeaters
        """
        severity = "warning" if count > 0 else "info"
        return self._log("uncertainty_signal", "defeaters_in_answer",
                         severity=severity,
                         metric_name="defeater_count", metric_value_num=count,
                         metric_unit="count")

    # ── kept for future use ────────────────────────────────────────────────

    def log_decision_points(self, count: int) -> str:
        """Number of decision points found in the derivation."""
        return self._log("quality_estimate", "decision_point_count",
                         metric_name="decision_point_count",
                         metric_value_num=count, metric_unit="count")

    def log_uncertainty_map(self, map_size: int) -> str:
        """Number of entries in the uncertainty map."""
        return self._log("uncertainty_signal", "uncertainty_map_size",
                         metric_name="map_size", metric_value_num=map_size,
                         metric_unit="count")

    def log_sensitivity_probe(self, probe_name: str, result: str,
                              delta: float = None) -> str:
        """
        Sensitivity probe result.
        result: 'stable' | 'sensitive' | 'brittle'
        """
        severity = "warning" if result in ("sensitive", "brittle") else "info"
        return self._log("sensitivity_probe", probe_name,
                         severity=severity,
                         metric_value_text=result,
                         metric_name="delta", metric_value_num=delta,
                         payload={"probe": probe_name, "result": result})

    def log_tether_failure(self, failure_count: int) -> str:
        """Explanation tether check failures."""
        severity = "warning" if failure_count > 0 else "info"
        return self._log("tether_failure", "explanation_tether_failure",
                         severity=severity,
                         metric_name="failure_count",
                         metric_value_num=failure_count)


# ---------------------------------------------------------------------------
# EG Commit Manager
# ---------------------------------------------------------------------------

class EGCommitLogger(_BaseOperatorLogger):
    """
    Logs outcomes from the EG/RG commit manager.
    Links back to the TRACE commit_proposal via eg_commit_id.
    """

    def log_commit_result(self, eg_commit_id: str, accepted: bool,
                          reason: str = None) -> str:
        """
        Input:  eg_commit_id — must match the ID used in
                               TraceLogger.log_commit_proposal()
                accepted     — whether the EG accepted the proposed write
                reason       — optional reason for rejection
        Output: outcome_id
        """
        severity = "info" if accepted else "warning"
        return self._log("commit_result", "eg_commit_result",
                         severity=severity,
                         eg_commit_id=eg_commit_id,
                         metric_value_text="accepted" if accepted else "rejected",
                         payload={"accepted": accepted, "reason": reason})
