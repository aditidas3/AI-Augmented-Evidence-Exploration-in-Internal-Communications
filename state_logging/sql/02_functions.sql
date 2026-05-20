-- =============================================================================
-- AEX Adaptive Orchestration Logging Layer
-- 02_functions.sql — Stored Functions
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Run lifecycle
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION create_exploration_run(
    p_run_id              UUID,
    p_intent_object       JSONB,
    p_question_hash       TEXT,
    p_user_id             TEXT,
    p_session_id          TEXT,
    p_corpus_snapshot_id  TEXT,
    p_eg_snapshot_id      TEXT,
    p_kg0_snapshot_id     TEXT,
    p_config_hash         TEXT,
    p_policy_hash         TEXT
) RETURNS VOID AS $$
BEGIN
    INSERT INTO exploration_run (
        run_id, intent_object, question_hash,
        user_id, session_id,
        corpus_snapshot_id, eg_snapshot_id, kg0_snapshot_id,
        config_hash, policy_hash,
        created_at, status
    ) VALUES (
        p_run_id, p_intent_object, p_question_hash,
        p_user_id, p_session_id,
        p_corpus_snapshot_id, p_eg_snapshot_id, p_kg0_snapshot_id,
        p_config_hash, p_policy_hash,
        now(), 'created'
    );
END;
$$ LANGUAGE plpgsql;


CREATE OR REPLACE FUNCTION update_run_status(
    p_run_id          UUID,
    p_status          run_status,
    p_final_answer_id UUID DEFAULT NULL,
    p_notes           TEXT DEFAULT NULL
) RETURNS VOID AS $$
BEGIN
    UPDATE exploration_run SET
        status          = p_status,
        started_at      = CASE WHEN p_status = 'running'  AND started_at IS NULL THEN now() ELSE started_at END,
        ended_at        = CASE WHEN p_status IN ('completed', 'failed', 'aborted') THEN now() ELSE ended_at END,
        final_answer_id = COALESCE(p_final_answer_id, final_answer_id),
        notes           = COALESCE(p_notes, notes)
    WHERE run_id = p_run_id;
END;
$$ LANGUAGE plpgsql;


-- ---------------------------------------------------------------------------
-- Operator invocation lifecycle
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION start_operator_invocation(
    p_invocation_id        UUID,
    p_run_id               UUID,
    p_operator_name        TEXT,
    p_parent_invocation_id UUID,
    p_attempt_no           INT,
    p_stage_order          INT,
    p_input_hash           TEXT,
    p_config_hash          TEXT,
    p_module_registry_hash TEXT,
    p_metadata             JSONB DEFAULT '{}'::jsonb
) RETURNS VOID AS $$
BEGIN
    INSERT INTO operator_invocation (
        invocation_id, run_id, operator_name,
        parent_invocation_id, attempt_no, stage_order,
        started_at, status,
        input_hash, config_hash, module_registry_hash, metadata
    ) VALUES (
        p_invocation_id, p_run_id, p_operator_name,
        p_parent_invocation_id, p_attempt_no, p_stage_order,
        now(), 'started',
        p_input_hash, p_config_hash, p_module_registry_hash,
        COALESCE(p_metadata, '{}'::jsonb)
    );
END;
$$ LANGUAGE plpgsql;


CREATE OR REPLACE FUNCTION finish_operator_invocation(
    p_invocation_id   UUID,
    p_status          invocation_status,
    p_output_hash     TEXT,
    p_latency_ms      BIGINT,
    p_error_code      TEXT    DEFAULT NULL,
    p_error_message   TEXT    DEFAULT NULL,
    p_metadata_patch  JSONB   DEFAULT '{}'::jsonb
) RETURNS VOID AS $$
BEGIN
    UPDATE operator_invocation SET
        ended_at      = now(),
        status        = p_status,
        output_hash   = p_output_hash,
        latency_ms    = p_latency_ms,
        error_code    = p_error_code,
        error_message = p_error_message,
        metadata      = metadata || COALESCE(p_metadata_patch, '{}'::jsonb)
    WHERE invocation_id = p_invocation_id;
END;
$$ LANGUAGE plpgsql;


-- ---------------------------------------------------------------------------
-- Outcome logging — the main workhorse
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION log_intermediate_outcome(
    p_outcome_id           UUID,
    p_run_id               UUID,
    p_invocation_id        UUID,
    p_outcome_kind         outcome_kind,
    p_outcome_name         TEXT,
    p_severity             outcome_severity,
    p_entity_type          TEXT,
    p_entity_id            TEXT,
    p_metric_name          TEXT,
    p_metric_value_num     DOUBLE PRECISION,
    p_metric_value_text    TEXT,
    p_metric_unit          TEXT,
    p_payload              JSONB,
    p_witness_refs         JSONB,
    p_eg_refs              JSONB,
    p_rg_refs              JSONB,
    p_eg_commit_id         TEXT,
    p_caused_by_outcome_id UUID,
    p_tags                 JSONB
) RETURNS VOID AS $$
BEGIN
    INSERT INTO intermediate_outcome_event (
        outcome_id, run_id, invocation_id,
        outcome_kind, outcome_name, severity, event_time,
        entity_type, entity_id,
        metric_name, metric_value_num, metric_value_text, metric_unit,
        payload, witness_refs, eg_refs, rg_refs,
        eg_commit_id, caused_by_outcome_id, tags
    ) VALUES (
        p_outcome_id, p_run_id, p_invocation_id,
        p_outcome_kind, p_outcome_name, p_severity, now(),
        p_entity_type, p_entity_id,
        p_metric_name, p_metric_value_num, p_metric_value_text, p_metric_unit,
        COALESCE(p_payload,      '{}'::jsonb),
        COALESCE(p_witness_refs, '[]'::jsonb),
        COALESCE(p_eg_refs,      '[]'::jsonb),
        COALESCE(p_rg_refs,      '[]'::jsonb),
        p_eg_commit_id, p_caused_by_outcome_id,
        COALESCE(p_tags, '[]'::jsonb)
    );
END;
$$ LANGUAGE plpgsql;


-- Bulk insert — accepts a JSONB array of outcome rows.
-- The worker thread calls this once per flush instead of calling
-- log_intermediate_outcome N times.
CREATE OR REPLACE FUNCTION bulk_log_outcomes(
    p_outcomes JSONB   -- array of outcome objects
) RETURNS VOID AS $$
DECLARE
    item JSONB;
BEGIN
    FOR item IN SELECT * FROM jsonb_array_elements(p_outcomes)
    LOOP
        INSERT INTO intermediate_outcome_event (
            outcome_id, run_id, invocation_id,
            outcome_kind, outcome_name, severity, event_time,
            entity_type, entity_id,
            metric_name, metric_value_num, metric_value_text, metric_unit,
            payload, witness_refs, eg_refs, rg_refs,
            eg_commit_id, caused_by_outcome_id, tags
        ) VALUES (
            (item->>'outcome_id')::UUID,
            (item->>'run_id')::UUID,
            (item->>'invocation_id')::UUID,
            (item->>'outcome_kind')::outcome_kind,
            item->>'outcome_name',
            COALESCE((item->>'severity')::outcome_severity, 'info'),
            COALESCE((item->>'event_time')::TIMESTAMPTZ, now()),
            item->>'entity_type',
            item->>'entity_id',
            item->>'metric_name',
            (item->>'metric_value_num')::DOUBLE PRECISION,
            item->>'metric_value_text',
            item->>'metric_unit',
            COALESCE(item->'payload',      '{}'::jsonb),
            COALESCE(item->'witness_refs', '[]'::jsonb),
            COALESCE(item->'eg_refs',      '[]'::jsonb),
            COALESCE(item->'rg_refs',      '[]'::jsonb),
            item->>'eg_commit_id',
            (item->>'caused_by_outcome_id')::UUID,
            COALESCE(item->'tags', '[]'::jsonb)
        );
    END LOOP;
END;
$$ LANGUAGE plpgsql;


-- ---------------------------------------------------------------------------
-- State snapshot
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION create_state_snapshot(
    p_state_id                    UUID,
    p_run_id                      UUID,
    p_invocation_id               UUID,
    p_snapshot_seq                BIGINT,
    p_current_stage               TEXT,
    p_active_hypothesis_count     INT,
    p_candidate_artifact_count    INT,
    p_candidate_chain_count       INT,
    p_selected_chain_count        INT,
    p_open_temporal_constraints   INT,
    p_unresolved_conflicts        INT,
    p_unfilled_slots              INT,
    p_avg_witness_strength        DOUBLE PRECISION,
    p_avg_confidence              DOUBLE PRECISION,
    p_latency_budget_remaining_ms BIGINT,
    p_scope_size_estimate         BIGINT,
    p_review_needed               BOOLEAN,
    p_state_payload               JSONB
) RETURNS VOID AS $$
BEGIN
    INSERT INTO orchestration_state_snapshot (
        state_id, run_id, invocation_id,
        snapshot_time, snapshot_seq,
        current_stage,
        active_hypothesis_count, candidate_artifact_count,
        candidate_chain_count, selected_chain_count,
        open_temporal_constraints, unresolved_conflicts, unfilled_slots,
        avg_witness_strength, avg_confidence,
        latency_budget_remaining_ms, scope_size_estimate,
        review_needed, state_payload
    ) VALUES (
        p_state_id, p_run_id, p_invocation_id,
        now(), p_snapshot_seq,
        p_current_stage,
        p_active_hypothesis_count, p_candidate_artifact_count,
        p_candidate_chain_count, p_selected_chain_count,
        p_open_temporal_constraints, p_unresolved_conflicts, p_unfilled_slots,
        p_avg_witness_strength, p_avg_confidence,
        p_latency_budget_remaining_ms, p_scope_size_estimate,
        COALESCE(p_review_needed, FALSE),
        COALESCE(p_state_payload, '{}'::jsonb)
    );
END;
$$ LANGUAGE plpgsql;


-- ---------------------------------------------------------------------------
-- Decision logging
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION log_orchestration_decision(
    p_decision_id             UUID,
    p_run_id                  UUID,
    p_invocation_id           UUID,
    p_state_id                UUID,
    p_decision_kind           decision_kind,
    p_selected_action         TEXT,
    p_rationale               TEXT,
    p_rationale_payload       JSONB,
    p_triggering_outcome_ids  JSONB,
    p_expected_effect         JSONB,
    p_executed_by             TEXT,
    p_success                 BOOLEAN DEFAULT NULL,
    p_followup_invocation_id  UUID    DEFAULT NULL
) RETURNS VOID AS $$
BEGIN
    INSERT INTO orchestration_decision (
        decision_id, run_id, invocation_id, state_id,
        decision_time, decision_kind,
        selected_action, rationale,
        rationale_payload, triggering_outcome_ids, expected_effect,
        executed_by, success, followup_invocation_id
    ) VALUES (
        p_decision_id, p_run_id, p_invocation_id, p_state_id,
        now(), p_decision_kind,
        p_selected_action, p_rationale,
        COALESCE(p_rationale_payload,      '{}'::jsonb),
        COALESCE(p_triggering_outcome_ids, '[]'::jsonb),
        COALESCE(p_expected_effect,        '{}'::jsonb),
        p_executed_by, p_success, p_followup_invocation_id
    );
END;
$$ LANGUAGE plpgsql;
