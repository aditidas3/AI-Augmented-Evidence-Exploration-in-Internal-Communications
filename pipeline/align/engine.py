"""
align/engine.py
Main ALIGN orchestrator: runs all six phases in sequence.

FIX #1: Passes link_hyps to Phase 6.
FIX #2: Adds post-pipeline soundness assertions (scope + GraphSpec).
FIX #7: Handles Phase 5 returning (subgraphs, stats) tuple.
"""

from __future__ import annotations
import logging
import time
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

from .core_types import (
    IntentObject,
    AlignResult,
    CompiledScopePredicate,
    CompiledGraphSkeleton,
    CandidateArtifact,
    Subgraph,
    parse_intent_object,
)
from ..operators.configs import AlignConfig
from .infrastructure.index_facade import IndexFacade
from .infrastructure.adapters import AdapterRegistry
from .infrastructure.contract_check import (
    AlignContractError,
    ContractReport,
    check_intent_satisfiability,
    run_contract_check,
)
from .phases import (
    Phase0_IntentValidation,
    Phase1_ScopedRetrieval,
    Phase2_ArtifactSelection,
    Phase3_AnchorMentionExtraction,
    Phase4_EntityLinkHypothesis,
    Phase5_SubgraphDiscovery,
    Phase6_SlotBinding,
)

logger = logging.getLogger(__name__)


class AlignEngine:
    """
    ALIGN: Anchored Litigation-Intelligence Graph Navigator

    Orchestrates the six-phase pipeline:
        Phase 0: Intent validation and compilation
        Phase 1: Scope-filtered retrieval (lexical + semantic)
        Phase 2: Artifact set selection
        Phase 3: Anchor extraction and mention extraction
        Phase 4: Entity and link hypothesis generation
        Phase 5: Subgraph discovery (beam search)
        Phase 6: Slot binding and witness construction

    All phases produce witnesses for TRACE consumption.
    All outputs are deterministic given fixed config and KG0.
    """

    def __init__(
        self,
        config: Optional[AlignConfig] = None,
        index: Optional[IndexFacade] = None,
    ):
        self.config = config or AlignConfig()
        self.index = index or IndexFacade(self.config)
        # Pass the facade so AdapterRegistry can wire a KG0NativeAdapter
        # against the live Neo4jStore when Solr is commented out.
        self.adapters = AdapterRegistry(self.config, self.index)

        # Initialize phases
        self.phase0 = Phase0_IntentValidation(self.config, self.index)
        self.phase1 = Phase1_ScopedRetrieval(self.config, self.index)
        self.phase2 = Phase2_ArtifactSelection(self.config)
        self.phase3 = Phase3_AnchorMentionExtraction(
            self.config, self.index, self.adapters
        )
        self.phase4 = Phase4_EntityLinkHypothesis(self.config, self.index)
        self.phase5 = Phase5_SubgraphDiscovery(self.config, self.index)
        self.phase6 = Phase6_SlotBinding(self.config, self.index)

        # Run the KG0 contract check once per engine so schema drift
        # surfaces here (with an actionable diagnostic) instead of
        # silently degrading Phase 3/4 output. Skipped when the caller
        # sets ``config.skip_contract_check`` — unit tests and
        # first-time bootstraps against an empty graph should opt out.
        self._contract_checked = False
        self._contract_report: Optional[ContractReport] = None

    def execute(
        self,
        intent: IntentObject,
    ) -> AlignResult:
        """Execute the complete ALIGN pipeline.

        align_result
        ├── intent_id
        ├── question
        ├── intent (from second output - the parsed intent structure)
        ├── skeleton (from second output - compiled skeleton)
        ├── anchors[] (from second output - first-class anchor objects)
        ├── mentions[] (from second output - first-class mention objects)
        ├── frames[] (from first output - but restructured)
        │   └── each frame contains:
        │       ├── slot_bindings (from first output)
        │       │   └── evidence_chains reference anchor_ids, mention_ids
        │       ├── subgraph_snapshot (from first output)
        │       └── witness metadata (coherence_score, temporal_consistency, etc.)
        ├── cross_frame_links[] (was cross_witness_links in second output)
        ├── answer_synthesis (from second output)
        ├── provenance_registry (from first output)
        └── run_manifest (from first output)
        """
        t_start = time.time()
        logger.info(
            f"ALIGN pipeline starting for intent "
            f"{intent.header.intent_id}"
        )

        # ---- Contract check (first execute only) ----
        if (
            not self._contract_checked
            and not getattr(self.config, "skip_contract_check", False)
        ):
            t_contract = time.time()
            self._contract_report = run_contract_check(self.index)
            logger.info(
                f"  Contract check: {time.time() - t_contract:.2f}s"
            )
            self._contract_checked = True

        # Per-query satisfiability probe. Cheap (O(1) set lookups
        # against the cached label snapshot) so we run it on every
        # execute. Reports as warnings, not errors, because operators
        # sometimes legitimately explore an empty corpus or stress
        # the pipeline against intents whose categories aren't yet
        # represented in KG0.
        if self._contract_report is not None:
            for warning in check_intent_satisfiability(
                intent, self._contract_report, self.config
            ):
                logger.warning("ALIGN intent satisfiability: %s", warning)

        # ---- Phase 0: Validate and compile ----
        t0 = time.time()
        phase0_output = self.phase0.execute(intent)
        scope = phase0_output["scope"]
        skeleton = phase0_output["skeleton"]
        retrieval_query = phase0_output["retrieval_query"]
        hashes = phase0_output["hashes"]
        logger.info(f"  Phase 0: {time.time() - t0:.2f}s")

        # ---- Phase 1: Scoped retrieval ----
        t1 = time.time()
        candidates = self.phase1.execute(retrieval_query, scope)
        logger.info(f"  Phase 1: {time.time() - t1:.2f}s")

        # ---- Phase 2: Artifact selection ----
        t2 = time.time()
        selected = self.phase2.execute(candidates, intent)
        logger.info(f"  Phase 2: {time.time() - t2:.2f}s")

        # ---- Phase 3: Anchor/mention extraction ----
        t3 = time.time()
        all_anchors, all_mentions, suppressed_mentions = self.phase3.execute(selected, intent)
        logger.info(f"  Phase 3: {time.time() - t3:.2f}s")

        # ---- Phase 4: Entity/link hypotheses ----
        t4 = time.time()
        entity_hyps, link_hyps = self.phase4.execute(
            all_anchors, all_mentions, intent
        )
        logger.info(f"  Phase 4: {time.time() - t4:.2f}s")

        # ---- Phase 5: Subgraph discovery ----
        t5 = time.time()
        subgraphs, phase5_stats = self.phase5.execute(
            all_anchors,
            all_mentions,
            entity_hyps,
            link_hyps,
            skeleton,
            intent,
        )
        logger.info(f"  Phase 5: {time.time() - t5:.2f}s")

        # ---- Phase 6: Slot binding ----
        # FIX #1: pass link_hyps to Phase 6
        t6 = time.time()
        slot_bindings, all_witnesses = self.phase6.execute(
            subgraphs,
            all_anchors,
            all_mentions,
            entity_hyps,
            link_hyps,
            intent,
        )
        logger.info(f"  Phase 6: {time.time() - t6:.2f}s")

        # Witness yield warning. When Phase 6 produces far fewer
        # witnesses than subgraphs, downstream TRACE is starved and
        # the question is unlikely to be answerable from this
        # ALIGN run. Common causes: an under-specified intent slot
        # spec, hint-grounded vars that beam search collapsed, or
        # Phase 6 dedup that's too aggressive. Logged as a warning
        # rather than an error so legitimate sparse intents (single
        # slot, single var) still complete.
        threshold = float(
            getattr(self.config, "min_witnesses_per_subgraph", 0.0) or 0.0
        )
        if subgraphs and threshold > 0.0:
            ratio = len(all_witnesses) / float(len(subgraphs))
            if ratio < threshold:
                logger.warning(
                    "ALIGN witness yield low: %d witnesses / %d "
                    "subgraphs = %.2f (threshold %.2f). TRACE may "
                    "be starved; check intent.SlotSpec for "
                    "under-specified slot/var coverage.",
                    len(all_witnesses),
                    len(subgraphs),
                    ratio,
                    threshold,
                )

        # Materialize KG structure only after the search/binding stages
        # have completed so ALIGN can collect first and fetch structure later.
        t7 = time.time()
        self.phase5.materialize_kg_structure(
            subgraphs,
            entity_hyps,
            link_hyps,
            all_anchors,
            all_mentions,
            intent,
        )
        logger.info(f"  Post-Phase 5 KG structure: {time.time() - t7:.2f}s")

        # ---- FIX #2: Soundness and replay checks ----
        self._assert_scope_soundness(selected, scope)
        self._assert_graph_spec_soundness(subgraphs, intent)

        total_time = time.time() - t_start
        total_anchors = sum(len(a) for a in all_anchors.values())
        total_mentions = sum(len(m) for m in all_mentions.values())
        suppressed_mention_count = sum(
            len(m) for m in suppressed_mentions.values()
        )
        empty_evidence_reasons: List[str] = []
        if not selected:
            empty_evidence_reasons.append("no_selected_artifacts")
        if not total_anchors:
            empty_evidence_reasons.append("no_anchors")
        if not total_mentions:
            empty_evidence_reasons.append("no_mentions")
        if not all_witnesses:
            empty_evidence_reasons.append("no_witnesses")

        # Assemble result
        result = AlignResult(
            intent_id=intent.header.intent_id,
            question_text=intent.header.question_text,
            artifact_set=selected,
            slot_bindings=slot_bindings,
            subgraphs=subgraphs,
            all_witnesses=all_witnesses,
            all_anchors=all_anchors,
            all_mentions=all_mentions,
            suppressed_mentions=suppressed_mentions,
            entity_hypotheses=entity_hyps,
            link_hypotheses=link_hyps,
            diagnostics={
                "total_time_s": total_time,
                "phase_times": {
                    "phase_0": t1 - t0,
                    "phase_1": t2 - t1,
                    "phase_2": t3 - t2,
                    "phase_3": t4 - t3,
                    "phase_4": t5 - t4,
                    "phase_5": t6 - t5,
                    "phase_6": t7 - t6,
                    "post_phase_5_kg_structure": time.time() - t7,
                },
                "retrieval_mode": (
                    "solr_lexical"
                    if retrieval_query.qdrant_vector is None
                    else "full"
                ),
                "retrieval_note": (
                    "Semantic retrieval (Qdrant) disabled; "
                    "sem_score=0 on all artifacts is expected."
                    if retrieval_query.qdrant_vector is None
                    else ""
                ),
                "candidates_retrieved": len(candidates),
                "artifacts_selected": len(selected),
                "total_anchors": total_anchors,
                "total_mentions": total_mentions,
                "suppressed_mentions": suppressed_mention_count,
                "entity_hypotheses": len(entity_hyps),
                "link_hypotheses": len(link_hyps),
                "subgraphs_discovered": len(subgraphs),
                "valid_subgraphs": sum(
                    1 for s in subgraphs if s.is_valid
                ),
                "witnesses_generated": len(all_witnesses),
                "phase_5_subgraph_discovery": phase5_stats,
                "phase_2_selection": getattr(
                    self.phase2,
                    "last_selection_diagnostics",
                    {},
                ),
                "evidence_status": (
                    "EMPTY_EVIDENCE"
                    if empty_evidence_reasons
                    else "EVIDENCE_AVAILABLE"
                ),
                "empty_evidence_reasons": empty_evidence_reasons,
            },
            replay_plan=hashes,
        )

        logger.info(
            f"ALIGN pipeline complete: "
            f"{total_time:.2f}s total, "
            f"{len(all_witnesses)} witnesses for TRACE"
        )
        return result

    # ----------------------------------------------------------------
    # Soundness assertions (pseudocode lines 25–26)
    # ----------------------------------------------------------------

    def _assert_scope_soundness(
        self,
        selected: List[CandidateArtifact],
        scope: CompiledScopePredicate,
    ):
        """
        Pseudocode line 25: assert forall a in Aset, P_scope(a).

        Verifies every selected artifact satisfies the scope predicate
        in-memory, independent of store-level filter enforcement.
        """
        for candidate in selected:
            assert scope.evaluate(candidate.metadata), (
                f"Scope soundness violation: artifact "
                f"'{candidate.artifact_id}' with metadata "
                f"{candidate.metadata} does not satisfy scope predicate "
                f"(mode={scope.mode.value})"
            )

    def _assert_graph_spec_soundness(
        self,
        subgraphs: List[Subgraph],
        intent: IntentObject,
    ):
        """
        Pseudocode line 26: assert GraphSpecSound(H_sub, G).

        Each returned subgraph must be explainably consistent with
        GraphSpec constraints, up to soft edges.  Hard variables must
        be bound, and hard edges (where both endpoints are bound)
        must be satisfied.
        """
        if not self.config.enforce_graph_spec_soundness:
            return

        gs = intent.graph_spec
        if gs is None:
            return

        for sg in subgraphs:
            # Hard variables must be bound
            for var in gs.hard_vars:
                binding = sg.bindings.get(var.var)
                if not (binding and binding.bound):
                    raise ValueError(
                        f"GraphSpec soundness: hard variable '{var.var}' "
                        f"is unbound in subgraph {sg.subgraph_id}"
                    )

            # Hard edges must be satisfied when both endpoints are bound
            for edge in gs.hard_edges:
                edge_key = f"{edge.src}-[{edge.rel}]->{edge.dst}"
                src_b = sg.bindings.get(edge.src)
                dst_b = sg.bindings.get(edge.dst)
                if (
                    src_b
                    and src_b.bound
                    and dst_b
                    and dst_b.bound
                ):
                    if not sg.edge_satisfactions.get(edge_key, False):
                        raise ValueError(
                            f"GraphSpec soundness: hard edge {edge_key} "
                            f"unsatisfied in subgraph {sg.subgraph_id}"
                        )

    # ----------------------------------------------------------------

    def execute_from_raw(
        self, raw_intent: Dict[str, Any]
    ) -> AlignResult:
        """Execute from a raw JSON intent bundle."""
        intent = parse_intent_object(raw_intent)
        return self.execute(intent)

    def execute_batch(
        self,
        intents: List[IntentObject],
    ) -> List[AlignResult]:
        """Execute multiple intents sequentially."""
        results = []
        for i, intent in enumerate(intents):
            logger.info(f"Processing intent {i+1}/{len(intents)}")
            result = self.execute(intent)
            results.append(result)
        return results

    def close(self):
        """Clean up resources."""
        self.index.close()


# ============================================================
# Convenience entry point
# ============================================================

def _jsonable(obj: Any) -> Any:
    if isinstance(obj, Enum):
        return obj.value
    if is_dataclass(obj):
        return _jsonable(asdict(obj))
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(x) for x in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def build_align_bundle(
    result: AlignResult,
    *,
    source_uri: str = "",
    corpus_stats: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Package an ``AlignResult`` into the JSON bundle consumed by TRACE."""
    families = sorted({
        str(_jsonable(artifact.family))
        for artifact in result.artifact_set
        if artifact.family
    })
    stats = {
        "output_root": source_uri,
        "artifact_count": len(result.artifact_set),
        "node_count": len(result.entity_hypotheses),
        "edge_count": len(result.link_hypotheses),
        "families": families,
        "collections": [],
    }
    if corpus_stats:
        stats.update(_jsonable(corpus_stats))
    return {
        "result": _jsonable(result),
        "corpus_stats": stats,
    }


def generate_align_bundle(
    intent_json: Dict[str, Any],
    config: Optional[AlignConfig] = None,
    index: Optional[IndexFacade] = None,
    *,
    source_uri: Optional[str] = None,
    corpus_stats: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run ALIGN and return the canonical Trace input bundle."""
    engine = AlignEngine(config=config, index=index)
    owns_index = index is None
    try:
        result = engine.execute_from_raw(intent_json)
        if source_uri is None:
            neo4j_config = getattr(engine.config, "neo4j", None)
            source_uri = str(getattr(neo4j_config, "uri", "") or "")
        return build_align_bundle(
            result,
            source_uri=source_uri,
            corpus_stats=corpus_stats,
        )
    finally:
        if owns_index:
            engine.close()


def run_align(
    intent_json: Dict[str, Any],
    config: Optional[AlignConfig] = None,
) -> AlignResult:
    """
    Run the ALIGN pipeline on a single intent bundle.

    Usage:
        import json
        with open("intent_analysis_results.json") as f:
            intents = json.load(f)

        result = run_align(intents[0]["response"])
    """
    engine = AlignEngine(config)
    try:
        return engine.execute_from_raw(intent_json)
    finally:
        engine.close()
