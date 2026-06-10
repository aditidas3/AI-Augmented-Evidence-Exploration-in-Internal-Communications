-- =============================================================================
-- AEX Adaptive Orchestration Logging Layer
-- 01_schema.sql — Enums, Tables, Indexes
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. Enum types
-- ---------------------------------------------------------------------------

CREATE TYPE run_status AS ENUM (
    'created',
    'running',
    'paused',
    'completed',
    'failed',
    'aborted'
);

-- operator_name is NOT an enum — it is plain TEXT on the table.
-- This means new operators (RESOLVE, RANK, VERIFY, etc.) can be added
-- at any time without a schema migration.

CREATE TYPE invocation_status AS ENUM (
    'started',
    'succeeded',
    'failed',
    'partial',
    'skipped'
);

CREATE TYPE outcome_kind AS ENUM (
    -- scope and retrieval (ALIGN)
    'candidate_set',
    'artifact_selection',
    'anchor_set',
    'scope_diagnostic',

    -- entity and link reasoning (ALIGN)
    'entity_hypothesis_set',
    'link_hypothesis_set',

    -- slot and witness (TRACE)
    'slot_candidate_set',
    'slot_binding',
    'witness_check',
    'coverage_check',

    -- chain construction (TRACE)
    'chain_candidate_set',
    'chain_selected',

    -- temporal reasoning (TRACE)
    'temporal_check',

    -- EG commit tracking (TRACE → EG commit manager)
    'commit_proposal',
    'commit_result',

    -- transformation analysis (MAP_TRANSFORM)
    'mapping_candidate_set',
    'mapping_edge_set',
    'qualifier_drop',

    -- conflict reasoning (CONFLICT)
    'conflict_candidate_set',
    'conflict_edge_set',
    'stance_label',
    'uncertainty_signal',

    -- answer construction (CONSTRUCT)
    'unsupported_rewrite',
    'tether_failure',
    'validation_transition',
    'quality_estimate',

    -- explanation (EXPLAIN)
    'sensitivity_probe',

    -- system-wide
    'budget_consumption',
    'latency_measurement',
    'failure_event',
    'custom'
);

CREATE TYPE decision_kind AS ENUM (
    'continue_pipeline',
    'retry_operator',
    'expand_scope',
    'narrow_scope',
    'switch_module',
    'lower_threshold',
    'raise_threshold',
    'branch_hypothesis',
    'request_review',
    'defer_commit',
    'commit_to_eg',
    'skip_step',
    'abort_run'
);

CREATE TYPE outcome_severity AS ENUM (
    'info',
    'warning',
    'critical'
);


-- ---------------------------------------------------------------------------
-- 2. exploration_run — one row per question/session
--
-- intent_object stores the full structured intent produced by INTAKE:
-- question text, entities, scope, temporal constraints, etc.
-- Stored as JSONB so the schema does not need to mirror the intent
-- structure, which may evolve independently.
-- ---------------------------------------------------------------------------

CREATE TABLE exploration_run (
    run_id                UUID            PRIMARY KEY,
    intent_object         JSONB           NOT NULL,
    question_hash         TEXT            NOT NULL,
    user_id               TEXT,
    session_id            TEXT,
    corpus_snapshot_id    TEXT            NOT NULL,
    eg_snapshot_id        TEXT,
    kg0_snapshot_id       TEXT,
    config_hash           TEXT            NOT NULL,
    policy_hash           TEXT,
    created_at            TIMESTAMPTZ     NOT NULL DEFAULT now(),
    started_at            TIMESTAMPTZ,
    ended_at              TIMESTAMPTZ,
    status                run_status      NOT NULL DEFAULT 'created',
    final_answer_id       UUID,
    notes                 TEXT
);

CREATE INDEX idx_run_status        ON exploration_run (status);
CREATE INDEX idx_run_created_at    ON exploration_run (created_at);
CREATE INDEX idx_run_question_hash ON exploration_run (question_hash);
CREATE INDEX idx_run_intent_gin    ON exploration_run USING GIN (intent_object);


-- ---------------------------------------------------------------------------
-- 3. operator_invocation — one row per operator call
--
-- operator_name is TEXT not enum so new operators can be added without
-- a schema migration.
-- ---------------------------------------------------------------------------

CREATE TABLE operator_invocation (
    invocation_id          UUID               PRIMARY KEY,
    run_id                 UUID               NOT NULL REFERENCES exploration_run (run_id) ON DELETE CASCADE,
    operator_name          TEXT               NOT NULL,
    parent_invocation_id   UUID               REFERENCES operator_invocation (invocation_id),
    attempt_no             INT                NOT NULL DEFAULT 1,
    stage_order            INT                NOT NULL,
    started_at             TIMESTAMPTZ        NOT NULL DEFAULT now(),
    ended_at               TIMESTAMPTZ,
    status                 invocation_status  NOT NULL DEFAULT 'started',
    input_hash             TEXT,
    output_hash            TEXT,
    config_hash            TEXT               NOT NULL,
    module_registry_hash   TEXT,
    latency_ms             BIGINT,
    error_code             TEXT,
    error_message          TEXT,
    metadata               JSONB              NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX idx_invocation_run      ON operator_invocation (run_id);
CREATE INDEX idx_invocation_operator ON operator_invocation (operator_name);
CREATE INDEX idx_invocation_parent   ON operator_invocation (parent_invocation_id);
CREATE INDEX idx_invocation_status   ON operator_invocation (status);


-- ---------------------------------------------------------------------------
-- 4. intermediate_outcome_event — append-only log of everything observed
--
-- eg_commit_id links a commit_proposal to its commit_result so you can
-- trace whether the EG accepted what TRACE proposed.
-- ---------------------------------------------------------------------------

CREATE TABLE intermediate_outcome_event (
    outcome_id             UUID               PRIMARY KEY,
    run_id                 UUID               NOT NULL REFERENCES exploration_run (run_id) ON DELETE CASCADE,
    invocation_id          UUID               REFERENCES operator_invocation (invocation_id) ON DELETE CASCADE,
    outcome_kind           outcome_kind       NOT NULL,
    outcome_name           TEXT               NOT NULL,
    severity               outcome_severity   NOT NULL DEFAULT 'info',
    event_time             TIMESTAMPTZ        NOT NULL DEFAULT now(),

    entity_type            TEXT,
    entity_id              TEXT,

    metric_name            TEXT,
    metric_value_num       DOUBLE PRECISION,
    metric_value_text      TEXT,
    metric_unit            TEXT,

    payload                JSONB              NOT NULL DEFAULT '{}'::jsonb,
    witness_refs           JSONB              NOT NULL DEFAULT '[]'::jsonb,
    eg_refs                JSONB              NOT NULL DEFAULT '[]'::jsonb,
    rg_refs                JSONB              NOT NULL DEFAULT '[]'::jsonb,

    eg_commit_id           TEXT,
    caused_by_outcome_id   UUID               REFERENCES intermediate_outcome_event (outcome_id),
    tags                   JSONB              NOT NULL DEFAULT '[]'::jsonb
);

CREATE INDEX idx_outcome_run         ON intermediate_outcome_event (run_id);
CREATE INDEX idx_outcome_invocation  ON intermediate_outcome_event (invocation_id);
CREATE INDEX idx_outcome_kind        ON intermediate_outcome_event (outcome_kind);
CREATE INDEX idx_outcome_severity    ON intermediate_outcome_event (severity);
CREATE INDEX idx_outcome_event_time  ON intermediate_outcome_event (event_time);
CREATE INDEX idx_outcome_metric      ON intermediate_outcome_event (metric_name);
CREATE INDEX idx_outcome_commit      ON intermediate_outcome_event (eg_commit_id);
CREATE INDEX idx_outcome_payload_gin ON intermediate_outcome_event USING GIN (payload);


-- ---------------------------------------------------------------------------
-- 5. orchestration_state_snapshot — compact state summary at milestones
--
-- current_stage is TEXT not enum, same reason as operator_name.
-- ---------------------------------------------------------------------------

CREATE TABLE orchestration_state_snapshot (
    state_id                     UUID            PRIMARY KEY,
    run_id                       UUID            NOT NULL REFERENCES exploration_run (run_id) ON DELETE CASCADE,
    invocation_id                UUID            REFERENCES operator_invocation (invocation_id) ON DELETE CASCADE,
    snapshot_time                TIMESTAMPTZ     NOT NULL DEFAULT now(),
    snapshot_seq                 BIGINT          NOT NULL,

    current_stage                TEXT,

    active_hypothesis_count      INT,
    candidate_artifact_count     INT,
    candidate_chain_count        INT,
    selected_chain_count         INT,
    open_temporal_constraints    INT,
    unresolved_conflicts         INT,
    unfilled_slots               INT,

    avg_witness_strength         DOUBLE PRECISION,
    avg_confidence               DOUBLE PRECISION,

    latency_budget_remaining_ms  BIGINT,
    scope_size_estimate          BIGINT,

    review_needed                BOOLEAN         NOT NULL DEFAULT FALSE,
    state_payload                JSONB           NOT NULL DEFAULT '{}'::jsonb
);

CREATE UNIQUE INDEX idx_snapshot_run_seq ON orchestration_state_snapshot (run_id, snapshot_seq);
CREATE INDEX idx_snapshot_run_time       ON orchestration_state_snapshot (run_id, snapshot_time);
CREATE INDEX idx_snapshot_stage          ON orchestration_state_snapshot (current_stage);


-- ---------------------------------------------------------------------------
-- 6. orchestration_decision — one row per adaptive control decision
-- ---------------------------------------------------------------------------

CREATE TABLE orchestration_decision (
    decision_id               UUID            PRIMARY KEY,
    run_id                    UUID            NOT NULL REFERENCES exploration_run (run_id) ON DELETE CASCADE,
    invocation_id             UUID            REFERENCES operator_invocation (invocation_id) ON DELETE CASCADE,
    state_id                  UUID            REFERENCES orchestration_state_snapshot (state_id),
    decision_time             TIMESTAMPTZ     NOT NULL DEFAULT now(),
    decision_kind             decision_kind   NOT NULL,
    selected_action           TEXT            NOT NULL,
    rationale                 TEXT,
    rationale_payload         JSONB           NOT NULL DEFAULT '{}'::jsonb,
    triggering_outcome_ids    JSONB           NOT NULL DEFAULT '[]'::jsonb,
    expected_effect           JSONB           NOT NULL DEFAULT '{}'::jsonb,
    executed_by               TEXT            NOT NULL,
    success                   BOOLEAN,
    followup_invocation_id    UUID            REFERENCES operator_invocation (invocation_id)
);

CREATE INDEX idx_decision_run   ON orchestration_decision (run_id);
CREATE INDEX idx_decision_kind  ON orchestration_decision (decision_kind);
CREATE INDEX idx_decision_state ON orchestration_decision (state_id);
