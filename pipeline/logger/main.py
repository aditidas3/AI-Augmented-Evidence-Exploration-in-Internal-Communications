"""
main.py — Project Pipeline Runner with Integrated Logging

Runs all five operators (ALIGN → TRACE → CONFLICT → CONSTRUCT → EXPLAIN)
in sequence and logs everything to PostgreSQL automatically.

To run this file:
    python main.py

Requirements:
    - Neo4j Aura instance accessible (credentials in .env)
    - PostgreSQL running with aex_test database set up
    - State logging SQL scripts already applied
    - .env file in the same folder as this script
    - Intent file path set in constants.py
"""

import json
import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

# ── load .env before anything else ───────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env")

# ── all paths and filenames come from constants.py ────────────────────────────
from constants import (
    PIPELINE_TEST,
    PIPELINE,
    INTENT_PATH,
    ALIGN_SCRIPT,
    ALIGN_BUNDLE,
    ALIGN_BUNDLE_PROJECTION,
    TRACE_SCRIPT,
    TRACE_VERIFY_SCRIPT,
    TRACE_BUNDLE,
    CONFLICT_SCRIPT,
    CONFLICT_OUTPUT,
    CONSTRUCT_SCRIPT,
    CONSTRUCT_OUTPUT,
    EXPLAIN_SCRIPT,
    EXPLAIN_OUTPUT_JSON,
    EXPLAIN_OUTPUT_TXT,
    STATE_LOGGING_ROOT,
)

# ── logging package ───────────────────────────────────────────────────────────
import state_logging.scripts.orchestration_logging.db as db
from state_logging.scripts.orchestration_logging.service import OrchestrationLogger
from state_logging.scripts.orchestration_logging.operator_loggers import (
    AlignLogger,
    TraceLogger,
    ConflictLogger,
    ConstructLogger,
    ExplainLogger,
)

# ── Neo4j connection (from .env) ──────────────────────────────────────────────
NEO4J_URI  = os.getenv("NEO4J_URI")
NEO4J_DB   = os.getenv("NEO4J_DB")
NEO4J_USER = os.getenv("NEO4J_USER")
NEO4J_PASS = os.getenv("NEO4J_PASS")

# ── PostgreSQL connection (from .env) ─────────────────────────────────────────
PG_HOST = os.getenv("PG_HOST")
PG_DB   = os.getenv("PG_DB")
PG_USER = os.getenv("PG_USER")
PG_PASS = os.getenv("PG_PASSWORD")


# =============================================================================
# Helpers
# =============================================================================

def hash_file(path: Path) -> str:
    """Short hash of a file's contents — used for corpus_snapshot_id."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()[:16]


def hash_config(config: dict) -> str:
    """Short hash of a config dict — used for config_hash."""
    raw = json.dumps(config, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def run_script(cmd: list, label: str) -> None:
    """Run a subprocess, print output live, raise on non-zero exit."""
    print(f"\n{'='*60}")
    print(f"Running {label}...")
    print(f"{'='*60}")
    result = subprocess.run(cmd, text=True, capture_output=False)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd)
    print(f"{label} finished successfully.")


def _fail_invocation(logger, run_id, inv_id, t0, kind, error):
    """Shared failure handler — logs outcome + finishes invocation."""
    latency_ms = int((time.monotonic() - t0) * 1000)
    logger.log_outcome(
        run_id           = run_id,
        invocation_id    = inv_id,
        outcome_kind     = "failure_event",
        outcome_name     = f"{kind}_failed",
        severity         = "critical",
        payload          = {"error": str(error)},
    )
    logger.finish_invocation(inv_id, "failed",
                             latency_ms=latency_ms,
                             error_message=str(error))
    return latency_ms


# =============================================================================
# ALIGN  (stage 1)
# =============================================================================

def run_align(logger: OrchestrationLogger, run_id: str, seq: int) -> tuple:
    """
    Runs ALIGN, logs everything, returns (invocation_id, state_id, seq).

    Command: python gen_align_bundle.py corrected_intent.json
    Reads:   INTENT_PATH  (via constants.py)
    Writes:  ALIGN_BUNDLE, ALIGN_BUNDLE_PROJECTION  (via constants.py)
    """
    inv_id = logger.start_invocation(run_id, "ALIGN", stage_order=1)
    t0     = time.monotonic()

    try:
        run_script([
            sys.executable,
            str(ALIGN_SCRIPT),
            str(INTENT_PATH),       
        ], label="ALIGN")

        latency_ms = int((time.monotonic() - t0) * 1000)

        # ── read outputs ──────────────────────────────────────────────────────
        bundle = load_json(ALIGN_BUNDLE)
        result = bundle.get("result", {})
        cs     = bundle.get("corpus_stats", {})
        diag   = result.get("diagnostics", {})
        subs   = result.get("subgraphs", [])
        best   = subs[0] if subs else {}

        candidates_retrieved = diag.get("candidates_retrieved", 0)
        artifacts_selected   = diag.get("artifacts_selected",
                                        len(result.get("artifact_set", [])))
        total_anchors        = diag.get("total_anchors", 0)
        total_mentions       = diag.get("total_mentions", 0)
        suppressed_mentions  = diag.get("suppressed_mentions", 0)
        entity_hypotheses    = diag.get("entity_hypotheses", cs.get("node_count", 0))
        link_hypotheses      = diag.get("link_hypotheses",  cs.get("edge_count", 0))
        subgraphs_discovered = diag.get("subgraphs_discovered", len(subs))
        valid_subgraphs      = diag.get("valid_subgraphs",      len(subs))
        witnesses_generated  = diag.get("witnesses_generated", 0)
        retrieval_mode       = diag.get("retrieval_mode", "unknown")
        families             = cs.get("families",    [])
        collections          = cs.get("collections", [])

        # ── log metrics ───────────────────────────────────────────────────────
        align = AlignLogger(logger, run_id, inv_id)

        align.log_candidate_set(pool_size=candidates_retrieved)
        align.log_artifact_selection(
            selected_count=artifacts_selected,
            payload={"families": families, "collections": collections},
        )
        align.log_anchor_set(anchor_count=total_anchors)
        align.log_mention_count(mention_count=total_mentions,
                                suppressed=suppressed_mentions)
        align.log_entity_hypotheses(count=entity_hypotheses)
        align.log_link_hypotheses(count=link_hypotheses)
        align.log_subgraph_discovery(
            discovered=subgraphs_discovered,
            valid=valid_subgraphs,
            witnesses_generated=witnesses_generated,
        )
        if best:
            align.log_best_subgraph(
                score           = best.get("score", 0.0),
                hard_coverage   = best.get("hard_coverage", 0.0),
                soft_coverage   = best.get("soft_coverage", 0.0),
                coherence_score = best.get("coherence_score", 0.0),
            )
        align.log_retrieval_mode(mode=retrieval_mode)
        align.log_scope_diagnostic(scope_size=len(families))
        align.log_retrieval_latency(latency_ms=latency_ms)

        logger.finish_invocation(inv_id, "succeeded", latency_ms=latency_ms)
        print(f"\nALIGN logged: {artifacts_selected} artifacts, "
              f"{total_anchors} anchors, {total_mentions} mentions, "
              f"{valid_subgraphs} subgraphs, {latency_ms}ms")

    except Exception as e:
        _fail_invocation(logger, run_id, inv_id, t0, "align", e)
        raise

    # ── snapshot + decision ───────────────────────────────────────────────────
    seq     += 1
    state_id = logger.snapshot_state(
        run_id                   = run_id,
        invocation_id            = inv_id,
        snapshot_seq             = seq,
        current_stage            = "ALIGN",
        candidate_artifact_count = artifacts_selected,
        avg_confidence           = best.get("coherence_score") if best else None,
        state_payload            = {
            "candidates_retrieved": candidates_retrieved,
            "artifacts_selected":   artifacts_selected,
            "total_anchors":        total_anchors,
            "total_mentions":       total_mentions,
            "entity_hypotheses":    entity_hypotheses,
            "link_hypotheses":      link_hypotheses,
            "valid_subgraphs":      valid_subgraphs,
            "witnesses_generated":  witnesses_generated,
            "retrieval_mode":       retrieval_mode,
        },
    )
    logger.log_decision(
        run_id          = run_id,
        invocation_id   = inv_id,
        state_id        = state_id,
        decision_kind   = "continue_pipeline",
        selected_action = "Proceed to TRACE",
        rationale       = (
            f"ALIGN completed. {artifacts_selected} artifacts selected from "
            f"{candidates_retrieved} candidates. {valid_subgraphs} valid "
            f"subgraphs discovered, {witnesses_generated} witnesses generated."
        ),
    )
    return inv_id, state_id, seq


# =============================================================================
# TRACE  (stage 2)
# =============================================================================

def run_trace(logger: OrchestrationLogger, run_id: str, seq: int) -> tuple:
    """
    Runs TRACE then TRACE verify, logs everything, returns (invocation_id, state_id, seq).

    Command: python run_trace_to_neo4j.py pipeline_test/align/results/align_bundle.json
    Verify:  python verify_eg_rg.py pipeline_test/trace/results/trace_bundle.json
    Reads:   ALIGN_BUNDLE  (via constants.py)
    Writes:  TRACE_BUNDLE  (via constants.py)
    """
    inv_id = logger.start_invocation(run_id, "TRACE", stage_order=2)
    t0     = time.monotonic()

    try:
        run_script([
            sys.executable,
            str(TRACE_SCRIPT),
            str(ALIGN_BUNDLE),      
        ], label="TRACE")

        # ── STEP 2a:verify step - fail and stop pipeline if verification fails ─────────
        try:
            run_script([
                sys.executable,
                str(TRACE_VERIFY_SCRIPT),
                str(TRACE_BUNDLE),  
            ], label="TRACE verify")
        except Exception as verify_err:
            logger.log_outcome(
                run_id        = run_id,
                invocation_id = inv_id,
                outcome_kind  = "failure_event",
                outcome_name  = "trace_verify_failed",
                severity      = "critical",
                payload       = {"error": str(verify_err)},
            )
            logger.finish_invocation(inv_id, "failed",
                                     latency_ms=int((time.monotonic() - t0) * 1000),
                                     error_message=str(verify_err))
            raise RuntimeError(
                f"TRACE verification failed — pipeline stopped. "
                f"Check trace_bundle.json before proceeding. Error: {verify_err}"
            )

        latency_ms = int((time.monotonic() - t0) * 1000)

        # ── read outputs ──────────────────────────────────────────────────────
        # Schema: trace-bundle.chain.v1
        bundle = load_json(TRACE_BUNDLE)
        ar     = bundle.get("accuracy_report", {})
        pm     = bundle.get("provenance_manifest", {})
        eg     = bundle.get("eg_delta", {})
        rg     = bundle.get("rg_trace", {})
        chains = bundle.get("ranked_chains", [])

        witnesses_kept      = len(pm.get("witness_records", []))
        ranked_chain_count  = ar.get("ranked_chain_count", len(chains))
        witness_complete    = ar.get("witness_complete_chains", 0)
        mapping_count       = ar.get("mapping_count", 0)
        eg_node_count       = len(eg.get("nodes", []))
        eg_edge_count       = len(eg.get("edges", []))
        rg_writes           = len(pm.get("rg_writes", []))

        # slot candidate counts — filled = slots with at least 1 candidate
        slot_candidates = bundle.get("slot_candidates", {})
        filled_slots    = sum(
            1 for v in slot_candidates.values()
            if isinstance(v, list) and len(v) > 0
        )
        total_slots = len(slot_candidates)

        # best chain metrics
        best_chain_score   = chains[0].get("score", 0.0)       if chains else 0.0
        best_chain_conf    = chains[0].get("confidence", 0.0)  if chains else 0.0
        best_slot_coverage = chains[0].get("slot_coverage", 0.0) if chains else 0.0

        # ── log metrics ───────────────────────────────────────────────────────
        trace = TraceLogger(logger, run_id, inv_id)

        trace.log_witnesses_kept(count=witnesses_kept)
        trace.log_claims_written(count=ranked_chain_count)
        trace.log_inferences_written(count=rg_writes)
        trace.log_frame_witnesses(count=witness_complete)
        trace.log_coref_groups(count=mapping_count, corroborates_edges=0)
        trace.log_eg_size(node_count=eg_node_count, edge_count=eg_edge_count)
        trace.log_coverage_check(filled_slots=filled_slots, total_slots=total_slots)
        trace.log_retrieval_latency(latency_ms=latency_ms)

        logger.finish_invocation(inv_id, "succeeded", latency_ms=latency_ms)
        print(f"\nTRACE logged: {witnesses_kept} witnesses, "
              f"{ranked_chain_count} ranked chains ({witness_complete} complete), "
              f"EG {eg_node_count} nodes / {eg_edge_count} edges, {latency_ms}ms")

    except Exception as e:
        _fail_invocation(logger, run_id, inv_id, t0, "trace", e)
        raise

    # ── snapshot + decision ───────────────────────────────────────────────────
    seq     += 1
    state_id = logger.snapshot_state(
        run_id                = run_id,
        invocation_id         = inv_id,
        snapshot_seq          = seq,
        current_stage         = "TRACE",
        candidate_chain_count = witnesses_kept,
        selected_chain_count  = ranked_chain_count,
        unfilled_slots        = total_slots - filled_slots,
        avg_confidence        = best_chain_conf,
        state_payload         = {
            "witnesses_kept":      witnesses_kept,
            "ranked_chain_count":  ranked_chain_count,
            "witness_complete":    witness_complete,
            "filled_slots":        filled_slots,
            "total_slots":         total_slots,
            "best_chain_score":    best_chain_score,
            "best_chain_conf":     best_chain_conf,
            "best_slot_coverage":  best_slot_coverage,
            "eg_node_count":       eg_node_count,
            "eg_edge_count":       eg_edge_count,
            "rg_writes":           rg_writes,
        },
    )
    logger.log_decision(
        run_id          = run_id,
        invocation_id   = inv_id,
        state_id        = state_id,
        decision_kind   = "continue_pipeline",
        selected_action = "Proceed to CONFLICT",
        rationale       = (
            f"TRACE completed. {witnesses_kept} witnesses written across "
            f"{ranked_chain_count} ranked chains ({witness_complete} complete). "
            f"EG has {eg_node_count} nodes / {eg_edge_count} edges."
        ),
    )
    return inv_id, state_id, seq


# =============================================================================
# CONFLICT  (stage 3)
# =============================================================================

def run_conflict(logger: OrchestrationLogger, run_id: str, seq: int) -> tuple:
    """
    Runs CONFLICT, logs everything, returns (invocation_id, state_id, seq).

    Command: python save_conflict_output.py  (no arguments)
    Writes:  CONFLICT_OUTPUT  (via constants.py)
    """
    inv_id = logger.start_invocation(run_id, "CONFLICT", stage_order=3)
    t0     = time.monotonic()

    try:
        run_script([
            sys.executable,
            str(CONFLICT_SCRIPT),   
        ], label="CONFLICT")

        latency_ms = int((time.monotonic() - t0) * 1000)

        # ── read outputs ──────────────────────────────────────────────────────
        bundle    = load_json(CONFLICT_OUTPUT)
        cr        = bundle.get("conflict_result", {})
        stats     = cr.get("stats", {})
        conflicts = cr.get("conflicts", [])

        witnesses_indexed = stats.get("witnesses_indexed", 0)
        slot_groups       = stats.get("slot_groups", 0)
        conflicts_found   = stats.get("conflicts_found", len(conflicts))
        claims_contested  = stats.get("claims_contested", 0)    # int count in stats
        defeaters_created = stats.get("defeaters_created", 0)
        contradicts_edges = cr.get("contradicts_edges", 0)      # top-level int
        negation_backend  = stats.get("negation_backend", "unknown")
        clusters_written  = stats.get("clusters_written", 0)
        cs                = bundle.get("conflict_structure", {})
        pairs_evaluated   = cs.get("pairs_evaluated", 0)
        scope_size        = cs.get("scope_size", 0)

        by_rule = {
            "rule1_surface_mismatch": stats.get("rule1_surface_mismatch", 0),
            "rule2_temporal_clash":   stats.get("rule2_temporal_clash",   0),
            "rule2_supersession":     stats.get("rule2_supersession",     0),
            "rule3_negation":         stats.get("rule3_negation",         0),
            "rule4_cross_artifact":   stats.get("rule4_cross_artifact",   0),
            "rule5_reliability":      stats.get("rule5_reliability",      0),
        }

        rebutting    = sum(1 for c in conflicts
                          if c.get("defeater_type") == "rebutting")
        undercutting = sum(1 for c in conflicts
                          if c.get("defeater_type") == "undercutting")

        # ── log metrics ───────────────────────────────────────────────────────
        conflict = ConflictLogger(logger, run_id, inv_id)

        conflict.log_witnesses_indexed(count=witnesses_indexed,
                                       slot_groups=slot_groups)
        logger.log_outcome(
            run_id           = run_id,
            invocation_id    = inv_id,
            outcome_kind     = "scope_diagnostic",
            outcome_name     = "pairs_evaluated",
            metric_name      = "pairs_evaluated",
            metric_value_num = pairs_evaluated,
            metric_unit      = "count",
        )
        conflict.log_conflicts_found(total=conflicts_found, by_rule=by_rule)
        conflict.log_defeaters_created(rebutting=rebutting,
                                       undercutting=undercutting)
        conflict.log_claims_contested(count=claims_contested)
        conflict.log_contradicts_edges(edge_count=contradicts_edges)
        conflict.log_negation_backend(backend=negation_backend)
        logger.log_outcome(
            run_id           = run_id,
            invocation_id    = inv_id,
            outcome_kind     = "quality_estimate",
            outcome_name     = "conflict_summary",
            metric_name      = "defeaters_created",
            metric_value_num = defeaters_created,
            metric_unit      = "count",
            payload          = {
                "clusters_written": clusters_written,
                "scope_size":       scope_size,
                "pairs_evaluated":  pairs_evaluated,
            },
        )
        logger.log_outcome(
            run_id           = run_id,
            invocation_id    = inv_id,
            outcome_kind     = "latency_measurement",
            outcome_name     = "conflict_latency",
            metric_name      = "latency_ms",
            metric_value_num = latency_ms,
            metric_unit      = "ms",
        )

        logger.finish_invocation(inv_id, "succeeded", latency_ms=latency_ms)
        print(f"\nCONFLICT logged: {witnesses_indexed} witnesses indexed, "
              f"{conflicts_found} conflicts, {rebutting} rebutting + "
              f"{undercutting} undercutting defeaters, {latency_ms}ms")

    except Exception as e:
        _fail_invocation(logger, run_id, inv_id, t0, "conflict", e)
        raise

    # ── snapshot + decision ───────────────────────────────────────────────────
    seq     += 1
    state_id = logger.snapshot_state(
        run_id               = run_id,
        invocation_id        = inv_id,
        snapshot_seq         = seq,
        current_stage        = "CONFLICT",
        unresolved_conflicts = conflicts_found,
        state_payload        = {
            "witnesses_indexed": witnesses_indexed,
            "conflicts_found":   conflicts_found,
            "by_rule":           by_rule,
            "rebutting":         rebutting,
            "undercutting":      undercutting,
            "claims_contested":  claims_contested,
            "contradicts_edges": contradicts_edges,
            "negation_backend":  negation_backend,
            "pairs_evaluated":   pairs_evaluated,
            "scope_size":        scope_size,
            "clusters_written":  clusters_written,
            "defeaters_created": defeaters_created,
        },
    )
    logger.log_decision(
        run_id          = run_id,
        invocation_id   = inv_id,
        state_id        = state_id,
        decision_kind   = "continue_pipeline",
        selected_action = "Proceed to CONSTRUCT",
        rationale       = (
            f"CONFLICT completed. {conflicts_found} conflicts detected "
            f"({rebutting} rebutting, {undercutting} undercutting). "
            f"{claims_contested} Claims contested."
        ),
    )
    return inv_id, state_id, seq


# =============================================================================
# CONSTRUCT  (stage 4)
# =============================================================================

def run_construct(logger: OrchestrationLogger, run_id: str, seq: int) -> tuple:
    """
    Runs CONSTRUCT, logs everything, returns (invocation_id, state_id, seq).

    Command: python construct.py  (no arguments)
    Writes:  CONSTRUCT_OUTPUT  (via constants.py)
    """
    inv_id = logger.start_invocation(run_id, "CONSTRUCT", stage_order=4)
    t0     = time.monotonic()

    try:
        run_script([
            sys.executable,
            str(CONSTRUCT_SCRIPT),  
        ], label="CONSTRUCT")

        latency_ms = int((time.monotonic() - t0) * 1000)

        # ── read outputs ──────────────────────────────────────────────────────
        # Schema: construct-bundle.chain.v1
        bundle      = load_json(CONSTRUCT_OUTPUT)
        constr      = bundle.get("construct_result", {})
        stats       = constr.get("stats", {})
        ans_bundle  = bundle.get("ans_bundle", {})
        rg_delta    = bundle.get("rg_delta", {})
        g_ans       = ans_bundle.get("g_ans", {})

        chains_loaded            = stats.get("chains_loaded",                  0)
        selected_chain_score     = stats.get("selected_chain_score",            0.0)
        selected_chain_effective = stats.get("selected_chain_effective_score",  0.0)
        slot_weighted_confidence = stats.get("slot_weighted_confidence",        0.0)
        findings_count           = stats.get("findings_count",                 0)
        citation_count           = stats.get("citations", len(bundle.get("citation_map", [])))
        limitations_count        = stats.get("limitations", len(bundle.get("limitations", [])))
        synthesis_type           = stats.get("synthesis_type",       "unknown")
        synthesis_confidence     = stats.get("synthesis_confidence",  0.0)
        synthesis_uid            = constr.get("synthesis_uid",        "")
        g_ans_nodes              = len(g_ans.get("nodes", []))
        g_ans_edges              = len(g_ans.get("edges", []))
        rg_nodes                 = len(rg_delta.get("nodes", []))
        rg_edges                 = len(rg_delta.get("edges", []))

        # contested claims from embedded conflict_bundle
        conflict_bundle_inner = bundle.get("conflict_bundle", {})
        contested_claims      = conflict_bundle_inner.get(
                                    "conflict_result", {}
                                ).get("stats", {}).get("claims_contested", 0)
        contested_slots       = []

        # ── log metrics ───────────────────────────────────────────────────────
        construct = ConstructLogger(logger, run_id, inv_id)

        construct.log_input_loaded(
            claims=chains_loaded, inferences=findings_count,
            defeaters=limitations_count, contested=contested_claims,
        )
        construct.log_inferences_weakened(count=limitations_count, updates=[])
        construct.log_nodes_written(new_nodes=g_ans_nodes + rg_nodes,
                                    new_edges=g_ans_edges + rg_edges)
        construct.log_synthesis(
            synthesis_type=synthesis_type,
            confidence=synthesis_confidence,
            contested_slots=contested_slots,
            synthesis_uid=synthesis_uid,
        )
        logger.log_outcome(
            run_id           = run_id,
            invocation_id    = inv_id,
            outcome_kind     = "quality_estimate",
            outcome_name     = "chain_selection",
            metric_name      = "selected_chain_score",
            metric_value_num = selected_chain_score,
            payload          = {
                "chains_loaded":             chains_loaded,
                "effective_score":           selected_chain_effective,
                "slot_weighted_confidence":  slot_weighted_confidence,
                "findings_count":            findings_count,
                "citation_count":            citation_count,
                "limitations_count":         limitations_count,
                "g_ans_nodes":               g_ans_nodes,
                "rg_nodes":                  rg_nodes,
            },
        )
        logger.log_outcome(
            run_id           = run_id,
            invocation_id    = inv_id,
            outcome_kind     = "latency_measurement",
            outcome_name     = "construct_latency",
            metric_name      = "latency_ms",
            metric_value_num = latency_ms,
            metric_unit      = "ms",
        )

        logger.finish_invocation(inv_id, "succeeded", latency_ms=latency_ms)
        print(f"\nCONSTRUCT logged: {chains_loaded} chains, "
              f"selected score={selected_chain_score:.4f}, "
              f"synthesis={synthesis_type} ({synthesis_confidence:.4f}), "
              f"{limitations_count} limitations, {latency_ms}ms")

    except Exception as e:
        _fail_invocation(logger, run_id, inv_id, t0, "construct", e)
        raise

    # ── snapshot + decision ───────────────────────────────────────────────────
    seq     += 1
    state_id = logger.snapshot_state(
        run_id               = run_id,
        invocation_id        = inv_id,
        snapshot_seq         = seq,
        current_stage        = "CONSTRUCT",
        avg_confidence       = synthesis_confidence,
        unresolved_conflicts = contested_claims,
        state_payload        = {
            "chains_loaded":             chains_loaded,
            "selected_chain_score":      selected_chain_score,
            "effective_score":           selected_chain_effective,
            "slot_weighted_confidence":  slot_weighted_confidence,
            "findings_count":            findings_count,
            "citation_count":            citation_count,
            "limitations_count":         limitations_count,
            "synthesis_type":            synthesis_type,
            "synthesis_confidence":      synthesis_confidence,
            "synthesis_uid":             synthesis_uid,
            "contested_claims":          contested_claims,
            "contested_slots":           contested_slots,
            "g_ans_nodes":               g_ans_nodes,
            "rg_nodes":                  rg_nodes,
        },
    )
    logger.log_decision(
        run_id          = run_id,
        invocation_id   = inv_id,
        state_id        = state_id,
        decision_kind   = "continue_pipeline",
        selected_action = "Proceed to EXPLAIN",
        rationale       = (
            f"CONSTRUCT completed. Synthesis type: {synthesis_type}, "
            f"confidence: {synthesis_confidence:.4f}. "
            f"{chains_loaded} chains evaluated, {limitations_count} limitations."
        ),
    )
    return inv_id, state_id, seq


# =============================================================================
# EXPLAIN  (stage 5)
# =============================================================================

def run_explain(logger: OrchestrationLogger, run_id: str, seq: int) -> tuple:
    """
    Runs EXPLAIN, logs everything, returns (invocation_id, state_id, seq).

    Command: python explain.py  (no arguments)
    Writes:  EXPLAIN_OUTPUT_JSON, EXPLAIN_OUTPUT_TXT  (via constants.py)
    """
    inv_id = logger.start_invocation(run_id, "EXPLAIN", stage_order=5)
    t0     = time.monotonic()

    try:
        run_script([
            sys.executable,
            str(EXPLAIN_SCRIPT),    
        ], label="EXPLAIN")

        latency_ms = int((time.monotonic() - t0) * 1000)

        # ── read outputs ──────────────────────────────────────────────────────
        # Schema: explain-bundle.chain.v1
        # No explain_result wrapper — data at top level + explain_bundle sub-object
        bundle = load_json(EXPLAIN_OUTPUT_JSON)
        stats  = bundle.get("stats", {})
        eb     = bundle.get("explain_bundle", {})

        confidence_score      = bundle.get("confidence_score", stats.get("confidence", 0.0))
        confidence_label      = bundle.get("confidence_label", stats.get("confidence_label", ""))
        citation_count        = stats.get("citations",         len(bundle.get("citations", [])))
        tether_count          = stats.get("tethers",           0)
        tether_failures       = stats.get("tether_failures",   0)
        uncertainties         = stats.get("uncertainties",     0)
        conflict_count        = stats.get("conflicts",         0)
        tether_complete       = tether_failures == 0
        defeater_count        = len(bundle.get("warnings", []))
        provenance_narratives = len(eb.get("provenance_narratives",  []))
        conflict_explanations = len(eb.get("conflict_explanations",  []))
        decision_explanations = len(eb.get("decision_explanations",  []))

        # slots — derive from embedded trace_bundle slot_candidates
        inner_trace     = bundle.get("trace_bundle", {})
        slot_candidates = inner_trace.get("slot_candidates", {})
        slots_answered  = sum(
            1 for v in slot_candidates.values()
            if isinstance(v, list) and len(v) > 0
        )
        total_slots     = len(slot_candidates)
        slots_missing   = total_slots - slots_answered
        slots_contested = conflict_count
        contested_slots = []
        missing_slots   = []

        # ── log metrics ───────────────────────────────────────────────────────
        explain = ExplainLogger(logger, run_id, inv_id)

        explain.log_slots_answered(
            answered=slots_answered,
            total=total_slots,
            contested_slots=contested_slots,
            missing_slots=missing_slots,
        )
        explain.log_confidence(score=confidence_score, label=confidence_label)
        explain.log_citations_count(count=citation_count)
        explain.log_defeaters_reported(count=defeater_count)
        logger.log_outcome(
            run_id           = run_id,
            invocation_id    = inv_id,
            outcome_kind     = "quality_estimate",
            outcome_name     = "explain_detail",
            metric_name      = "tether_count",
            metric_value_num = tether_count,
            metric_unit      = "count",
            payload          = {
                "tether_failures":       tether_failures,
                "tether_complete":       tether_complete,
                "uncertainties":         uncertainties,
                "conflict_count":        conflict_count,
                "provenance_narratives": provenance_narratives,
                "conflict_explanations": conflict_explanations,
                "decision_explanations": decision_explanations,
                "defeater_count":        defeater_count,
            },
        )
        logger.log_outcome(
            run_id           = run_id,
            invocation_id    = inv_id,
            outcome_kind     = "latency_measurement",
            outcome_name     = "explain_latency",
            metric_name      = "latency_ms",
            metric_value_num = latency_ms,
            metric_unit      = "ms",
        )

        logger.finish_invocation(inv_id, "succeeded", latency_ms=latency_ms)
        print(f"\nEXPLAIN logged: {slots_answered}/{total_slots} slots answered, "
              f"{citation_count} citations, {tether_count} tethers "
              f"({tether_failures} failures), confidence={confidence_label} "
              f"({confidence_score:.4f}), {latency_ms}ms")

    except Exception as e:
        _fail_invocation(logger, run_id, inv_id, t0, "explain", e)
        raise

    # ── snapshot + decision ───────────────────────────────────────────────────
    seq     += 1
    state_id = logger.snapshot_state(
        run_id               = run_id,
        invocation_id        = inv_id,
        snapshot_seq         = seq,
        current_stage        = "EXPLAIN",
        avg_confidence       = confidence_score,
        unresolved_conflicts = slots_contested,
        review_needed        = confidence_label in ("LOW", "VERY LOW"),
        state_payload        = {
            "slots_answered":          slots_answered,
            "slots_contested":         slots_contested,
            "slots_missing":           slots_missing,
            "confidence_score":        confidence_score,
            "confidence_label":        confidence_label,
            "citation_count":          citation_count,
            "tether_count":            tether_count,
            "tether_failures":         tether_failures,
            "tether_complete":         tether_complete,
            "uncertainties":           uncertainties,
            "conflict_count":          conflict_count,
            "defeater_count":          defeater_count,
            "provenance_narratives":   provenance_narratives,
            "conflict_explanations":   conflict_explanations,
            "decision_explanations":   decision_explanations,
        },
    )
    logger.log_decision(
        run_id          = run_id,
        invocation_id   = inv_id,
        state_id        = state_id,
        decision_kind   = "continue_pipeline",
        selected_action = "Return answer to investigator",
        rationale       = (
            f"EXPLAIN completed. {slots_answered} slots answered, "
            f"confidence {confidence_label} ({confidence_score:.4f}). "
            f"Output written to {EXPLAIN_OUTPUT_TXT.name}."
        ),
    )
    return inv_id, state_id, seq


# =============================================================================
# Main pipeline
# =============================================================================

def main():
    # ── validate intent path ──────────────────────────────────────────────────
    if not INTENT_PATH.exists():
        print(f"ERROR: Intent file not found: {INTENT_PATH}")
        print("Update INTENT_PATH in constants.py.")
        sys.exit(1)

    # ── load intent ───────────────────────────────────────────────────────────
    intent_object = load_json(INTENT_PATH)

    # ── build IDs ─────────────────────────────────────────────────────────────
    corpus_snapshot_id = f"corpus_{hash_file(INTENT_PATH)}"
    config_hash        = hash_config({
        "neo4j_uri":    NEO4J_URI,
        "neo4j_db":     NEO4J_DB,
        "align_script": str(ALIGN_SCRIPT),
        "trace_script": str(TRACE_SCRIPT),
    })

    # ── start logging ─────────────────────────────────────────────────────────
    db.init_pool(
        host     = PG_HOST,
        dbname   = PG_DB,
        user     = PG_USER,
        password = PG_PASS,
    )
    logger = OrchestrationLogger()
    logger.start()

    # ── create run ────────────────────────────────────────────────────────────
    run_id = logger.create_run(
        intent_object      = intent_object,
        corpus_snapshot_id = corpus_snapshot_id,
        config_hash        = config_hash,
        kg0_snapshot_id    = f"neo4j_aura_{NEO4J_DB}",
    )
    logger.start_run(run_id)

    print(f"\nRun started: {run_id}")
    print(f"Intent: {INTENT_PATH}")

    seq = 0

    try:
        _, _, seq = run_align(   logger, run_id, seq)
        _, _, seq = run_trace(   logger, run_id, seq)
        _, _, seq = run_conflict( logger, run_id, seq)
        _, _, seq = run_construct(logger, run_id, seq)
        _, _, seq = run_explain(  logger, run_id, seq)

        logger.finish_run(run_id, "completed",
                          notes="All five operators completed successfully.")
        print(f"\n{'='*60}")
        print(f"Pipeline complete.")
        print(f"Run ID: {run_id}")
        print(f"{'='*60}")
        
    except Exception as e:
        print(f"\nPipeline failed: {e}")
        logger.finish_run(run_id, "failed", notes=str(e))
        raise

    finally:
        time.sleep(1)   # let background worker thread finish flushing
        logger.stop()
        db.close_pool()


if __name__ == "__main__":
    main()
