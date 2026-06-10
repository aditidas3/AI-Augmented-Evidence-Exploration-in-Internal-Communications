"""
service.py — OrchestrationLogger

The only object operators and the orchestrator interact with.
All methods return instantly — heavy DB work happens in the worker thread.

Usage:
    from orchestration_logging.service import OrchestrationLogger
    import orchestration_logging.db as db

    db.init_pool()
    logger = OrchestrationLogger()
    logger.start()

    run_id        = logger.create_run(intent_object, corpus_snapshot_id, config_hash)
    logger.start_run(run_id)

    invocation_id = logger.start_invocation(run_id, "ALIGN", stage_order=1)
    logger.log_outcome(run_id, invocation_id, "candidate_set", "retrieval_pool_size", metric_value_num=127)
    logger.finish_invocation(invocation_id, "succeeded", latency_ms=340)

    state_id      = logger.snapshot_state(run_id, invocation_id, current_stage="ALIGN", unfilled_slots=2, ...)
    logger.log_decision(run_id, invocation_id, state_id, "continue_pipeline", "proceed to TRACE", ...)

    logger.finish_run(run_id, "completed")
    logger.stop()
    db.close_pool()
"""

import hashlib
import json
import logging
import queue
import uuid
from datetime import datetime, timezone
from typing import Any

import psycopg2.extras

from . import db
from .worker import LoggerWorker, SIGNAL_FLUSH

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(obj: Any) -> str:
    raw = json.dumps(obj, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _run_sql(sql: str, params: tuple) -> None:
    """Execute a single SQL call synchronously (for infrequent writes)."""
    conn = db.get_conn()
    try:
        with conn.cursor() as cur:
            print("SQL:", sql)
            print("PARAMS:", params)
            print("SQL count:", sql.count('%s'))
            print("PARAMS count:", len(params))
            cur.execute(sql, params)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        db.put_conn(conn)


class OrchestrationLogger:
    """
    Public API for all logging in the AEX system.

    Internally owns:
        - a queue.Queue (the in-memory buffer)
        - a LoggerWorker (the background flush thread)

    Outcome events are queued (non-blocking).
    Run/invocation/snapshot/decision writes are synchronous (infrequent).
    """

    def __init__(self) -> None:
        self._queue  = queue.Queue()
        self._worker = LoggerWorker(self._queue)

    def start(self) -> None:
        """Start the background worker thread. Call once before logging."""
        self._worker.start()

    def stop(self) -> None:
        """Drain the queue, flush remaining events, stop the worker."""
        self._worker.stop()

    # ------------------------------------------------------------------
    # Run lifecycle
    # ------------------------------------------------------------------

    def create_run(
        self,
        intent_object:       dict,
        corpus_snapshot_id:  str,
        config_hash:         str,
        user_id:             str  = None,
        session_id:          str  = None,
        eg_snapshot_id:      str  = None,
        kg0_snapshot_id:     str  = None,
        policy_hash:         str  = None,
    ) -> str:
        """
        Input:
            intent_object       — full INTAKE output (question, entities, scope,
                                  temporal constraints, etc.) as a dict
            corpus_snapshot_id  — which corpus version is being used
            config_hash         — which config version is being used
            user_id             — optional, who asked the question
            session_id          — optional, session this belongs to
            eg_snapshot_id      — optional, EG snapshot at run start
            kg0_snapshot_id     — optional, KG0 snapshot at run start
            policy_hash         — optional, which orchestration policy is active

        Output:
            run_id (str UUID) — use this for all subsequent calls
        """
        run_id        = str(uuid.uuid4())
        question_hash = _hash(intent_object)

        _run_sql(
            "SELECT create_exploration_run(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                run_id,
                json.dumps(intent_object),
                question_hash,
                user_id,
                session_id,
                corpus_snapshot_id,
                eg_snapshot_id,
                kg0_snapshot_id,
                config_hash,
                policy_hash,
            )
        )
        log.info("Run created: %s", run_id)
        return run_id

    def start_run(self, run_id: str) -> None:
        """
        Input:  run_id from create_run()
        Output: nothing — marks run as 'running', sets started_at
        """
        _run_sql("SELECT update_run_status(%s,%s)", (run_id, "running"))

    def finish_run(
        self,
        run_id:          str,
        status:          str,   # 'completed' | 'failed' | 'aborted'
        final_answer_id: str = None,
        notes:           str = None,
    ) -> None:
        """
        Input:
            run_id          — the run to close
            status          — 'completed', 'failed', or 'aborted'
            final_answer_id — optional ID of the answer produced
            notes           — optional free-text notes

        Output: nothing — flushes remaining events then closes the run record
        """
        # Force flush before closing so no events are lost
        self._queue.put(SIGNAL_FLUSH)
        self._queue.join()

        _run_sql(
            "SELECT update_run_status(%s,%s,%s,%s)",
            (run_id, status, final_answer_id, notes)
        )
        log.info("Run finished: %s status=%s", run_id, status)

    # ------------------------------------------------------------------
    # Operator invocation lifecycle
    # ------------------------------------------------------------------

    def start_invocation(
        self,
        run_id:               str,
        operator_name:        str,   # plain string — 'ALIGN', 'TRACE', etc.
        stage_order:          int,
        config_hash:          str  = "default",
        parent_invocation_id: str  = None,
        attempt_no:           int  = 1,
        input_hash:           str  = None,
        module_registry_hash: str  = None,
        metadata:             dict = None,
    ) -> str:
        """
        Input:
            run_id               — which run this belongs to
            operator_name        — name of the operator being called (plain string)
            stage_order          — position in the pipeline (1, 2, 3 ...)
            config_hash          — operator config version
            parent_invocation_id — optional, if this is a nested call
            attempt_no           — retry count (starts at 1)
            input_hash           — optional hash of the operator's input
            module_registry_hash — optional, which module set is loaded
            metadata             — optional extra info as dict

        Output:
            invocation_id (str UUID) — pass this to log_outcome() and finish_invocation()
        """
        invocation_id = str(uuid.uuid4())
        _run_sql(
            "SELECT start_operator_invocation(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                invocation_id,
                run_id,
                operator_name,
                parent_invocation_id,
                attempt_no,
                stage_order,
                input_hash,
                config_hash,
                module_registry_hash,
                json.dumps(metadata or {}),
            )
        )
        log.debug("Invocation started: %s operator=%s", invocation_id, operator_name)
        return invocation_id

    def finish_invocation(
        self,
        invocation_id: str,
        status:        str,       # 'succeeded' | 'failed' | 'partial' | 'skipped'
        latency_ms:    int  = None,
        output_hash:   str  = None,
        error_code:    str  = None,
        error_message: str  = None,
        metadata:      dict = None,
    ) -> None:
        """
        Input:
            invocation_id — the invocation to close
            status        — outcome status
            latency_ms    — how long the operator took in milliseconds
            output_hash   — optional hash of the operator's output
            error_code    — optional short error code if failed
            error_message — optional full error message if failed
            metadata      — optional extra fields to merge into metadata

        Output: nothing — closes the invocation record and signals a flush
        """
        _run_sql(
            "SELECT finish_operator_invocation(%s,%s,%s,%s,%s,%s,%s)",
            (
                invocation_id,
                status,
                output_hash,
                latency_ms,
                error_code,
                error_message,
                json.dumps(metadata or {}),
            )
        )
        # Operator finished = natural idle moment = flush outstanding events
        self._queue.put(SIGNAL_FLUSH)
        log.debug("Invocation finished: %s status=%s latency_ms=%s", invocation_id, status, latency_ms)

    # ------------------------------------------------------------------
    # Outcome logging — called frequently, never blocks
    # ------------------------------------------------------------------

    def log_outcome(
        self,
        run_id:               str,
        invocation_id:        str,
        outcome_kind:         str,
        outcome_name:         str,
        severity:             str  = "info",
        entity_type:          str  = None,
        entity_id:            str  = None,
        metric_name:          str  = None,
        metric_value_num:     float = None,
        metric_value_text:    str  = None,
        metric_unit:          str  = None,
        payload:              dict = None,
        witness_refs:         list = None,
        eg_refs:              list = None,
        rg_refs:              list = None,
        eg_commit_id:         str  = None,
        caused_by_outcome_id: str  = None,
        tags:                 list = None,
    ) -> str:
        """
        Input:
            run_id               — which run
            invocation_id        — which operator call produced this outcome
            outcome_kind         — type of outcome ('candidate_set', 'coverage_check', etc.)
            outcome_name         — human readable label ('retrieval_pool_size', 'slot_coverage')
            severity             — 'info' (default) | 'warning' | 'critical'
            entity_type          — optional, what kind of entity this is about
            entity_id            — optional, which specific entity
            metric_name          — optional, name of the numeric metric
            metric_value_num     — optional, the numeric value (127, 0.87, 3)
            metric_value_text    — optional, a text value ('satisfied', 'open', 'violated')
            metric_unit          — optional, unit for the numeric value
            payload              — optional dict for anything that doesn't fit above
            witness_refs         — optional list of witness IDs involved
            eg_refs              — optional list of EG object IDs involved
            rg_refs              — optional list of RG node IDs involved
            eg_commit_id         — links a commit_proposal to its commit_result
            caused_by_outcome_id — optional, which prior outcome caused this one
            tags                 — optional list of string tags

        Output:
            outcome_id (str UUID) — returned so callers can reference it in
                                    triggering_outcome_ids when logging decisions
        """
        outcome_id = str(uuid.uuid4())
        event = {
            "outcome_id":           outcome_id,
            "run_id":               run_id,
            "invocation_id":        invocation_id,
            "outcome_kind":         outcome_kind,
            "outcome_name":         outcome_name,
            "severity":             severity,
            "event_time":           _now_iso(),
            "entity_type":          entity_type,
            "entity_id":            entity_id,
            "metric_name":          metric_name,
            "metric_value_num":     metric_value_num,
            "metric_value_text":    metric_value_text,
            "metric_unit":          metric_unit,
            "payload":              payload or {},
            "witness_refs":         witness_refs or [],
            "eg_refs":              eg_refs or [],
            "rg_refs":              rg_refs or [],
            "eg_commit_id":         eg_commit_id,
            "caused_by_outcome_id": caused_by_outcome_id,
            "tags":                 tags or [],
        }
        # Drop into queue — returns in microseconds
        self._queue.put(event)
        return outcome_id

    # ------------------------------------------------------------------
    # State snapshot — called by orchestrator after each operator
    # ------------------------------------------------------------------

    def snapshot_state(
        self,
        run_id:                      str,
        invocation_id:               str,
        snapshot_seq:                int,
        current_stage:               str   = None,
        active_hypothesis_count:     int   = None,
        candidate_artifact_count:    int   = None,
        candidate_chain_count:       int   = None,
        selected_chain_count:        int   = None,
        open_temporal_constraints:   int   = None,
        unresolved_conflicts:        int   = None,
        unfilled_slots:              int   = None,
        avg_witness_strength:        float = None,
        avg_confidence:              float = None,
        latency_budget_remaining_ms: int   = None,
        scope_size_estimate:         int   = None,
        review_needed:               bool  = False,
        state_payload:               dict  = None,
    ) -> str:
        """
        Input:
            run_id                      — which run
            invocation_id               — which operator just finished
            snapshot_seq                — monotonically increasing counter per run (1, 2, 3...)
            current_stage               — which operator just completed
            active_hypothesis_count     — how many hypotheses are active
            candidate_artifact_count    — how many artifacts are in consideration
            candidate_chain_count       — how many chains are being evaluated
            selected_chain_count        — how many chains have been selected
            open_temporal_constraints   — how many temporal constraints are unresolved
            unresolved_conflicts        — how many conflicts are unresolved
            unfilled_slots              — how many slots still need filling
            avg_witness_strength        — average witness strength across active chains
            avg_confidence              — average confidence across active hypotheses
            latency_budget_remaining_ms — how much time budget is left
            scope_size_estimate         — estimated scope size
            review_needed               — whether human review is required
            state_payload               — anything extra as a dict

        Output:
            state_id (str UUID) — pass this to log_decision() so the decision
                                  is linked to the state it was based on
        """
        state_id = str(uuid.uuid4())
        _run_sql(
            "SELECT create_state_snapshot(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                state_id,                           # 1  p_state_id
                run_id,                             # 2  p_run_id
                invocation_id,                      # 3  p_invocation_id
                snapshot_seq,                       # 4  p_snapshot_seq
                current_stage,                      # 5  p_current_stage
                active_hypothesis_count,            # 6  p_active_hypothesis_count
                candidate_artifact_count,           # 7  p_candidate_artifact_count
                candidate_chain_count,              # 8  p_candidate_chain_count
                selected_chain_count,               # 9  p_selected_chain_count
                open_temporal_constraints,          # 10 p_open_temporal_constraints
                unresolved_conflicts,               # 11 p_unresolved_conflicts
                unfilled_slots,                     # 12 p_unfilled_slots
                avg_witness_strength,               # 13 p_avg_witness_strength
                avg_confidence,                     # 14 p_avg_confidence
                latency_budget_remaining_ms,        # 15 p_latency_budget_remaining_ms
                scope_size_estimate,                # 16 p_scope_size_estimate
                review_needed,                      # 17 p_review_needed
                json.dumps(state_payload or {}),    # 18 p_state_payload
            )
        )
        log.debug("Snapshot created: %s seq=%d stage=%s", state_id, snapshot_seq, current_stage)
        return state_id

    # ------------------------------------------------------------------
    # Decision logging — called by orchestrator after each decision
    # ------------------------------------------------------------------

    def log_decision(
        self,
        run_id:                 str,
        invocation_id:          str,
        state_id:               str,
        decision_kind:          str,
        selected_action:        str,
        rationale:              str  = None,
        rationale_payload:      dict = None,
        triggering_outcome_ids: list = None,
        expected_effect:        dict = None,
        executed_by:            str  = "orchestrator_policy_v1",
        success:                bool = None,
        followup_invocation_id: str  = None,
    ) -> str:
        """
        Input:
            run_id                  — which run
            invocation_id           — which orchestrator invocation made this decision
            state_id                — the snapshot this decision was based on
            decision_kind           — type of decision ('continue_pipeline', 'expand_scope', etc.)
            selected_action         — plain English description of what was decided
            rationale               — plain English why
            rationale_payload       — structured version of the rationale as a dict
            triggering_outcome_ids  — list of outcome_ids that caused this decision
            expected_effect         — what the orchestrator expects to happen next
            executed_by             — which policy/version made this decision
            success                 — optional, filled in retrospectively
            followup_invocation_id  — optional, the invocation this decision spawned

        Output:
            decision_id (str UUID)
        """
        decision_id = str(uuid.uuid4())
        _run_sql(
            "SELECT log_orchestration_decision(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                decision_id, run_id, invocation_id, state_id,
                decision_kind, selected_action, rationale,
                json.dumps(rationale_payload or {}),
                json.dumps(triggering_outcome_ids or []),
                json.dumps(expected_effect or {}),
                executed_by, success, followup_invocation_id,
            )
        )
        log.debug("Decision logged: %s kind=%s action=%s", decision_id, decision_kind, selected_action)
        return decision_id
