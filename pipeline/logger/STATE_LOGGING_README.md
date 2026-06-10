# State Logging System

A lightweight infrastructure layer that sits alongside the Project pipeline and records every operator run into PostgreSQL — without adding overhead to operator execution.

---

## What it captures

After every pipeline run, five tables are populated:

| Table | What it holds |
|---|---|
| `exploration_run` | One row per run — intent, config hash, corpus snapshot, status |
| `operator_invocation` | One row per operator — timing, stage order, success/failure |
| `intermediate_outcome_event` | Many rows per operator — individual metrics (candidate counts, confidence scores, defeater counts, latency etc.) |
| `orchestration_state_snapshot` | One row per operator — point-in-time state of the pipeline after each stage |
| `orchestration_decision` | One row per operator — what the pipeline decided to do next and why |

---

## How to run

```bash
# 1. Set intent path in constants.py
# 2. Fill in credentials in .env
# 3. Run
python main.py
```

The main.py runs all five operators in sequence (ALIGN → TRACE → CONFLICT → CONSTRUCT → EXPLAIN) and logs everything automatically. No manual steps between operators.

If TRACE verification fails, the pipeline stops immediately and logs the failure — CONFLICT, CONSTRUCT, and EXPLAIN will not run.

---

## Folder structure

```
logger/
  main.py                    # pipeline runner — entry point
  constants.py               # all paths and filenames — edit here if anything moves
  state_logging/
    scripts/
      orchestration_logging/
        __init__.py
        db.py                # connection pool
        service.py           # OrchestrationLogger — main interface
        worker.py            # background flush thread
        operator_wrapper.py
        operator_loggers.py  # per-operator typed logger classes
    sql/
      01_schema.sql          # Enums, Tables, Indexes
      02_functions.sql       # Stored Functions
  STATE_LOGGING_README.md    # documentation file
```

---

## Outcome name reference

Every row in `intermediate_outcome_event` has an `outcome_name`. This table explains what each one means.

| operator_name | outcome_name | metric_value_num | payload |
|---|---|---|---|
| ALIGN | `retrieval_pool_size` | candidates retrieved | — |
| ALIGN | `selected_artifact_count` | artifacts selected | families, collections |
| ALIGN | `anchor_count` | anchors found | — |
| ALIGN | `mention_count` | mentions extracted | suppressed count |
| ALIGN | `entity_hypothesis_count` | entity hypotheses | — |
| ALIGN | `link_hypothesis_count` | link hypotheses | — |
| ALIGN | `subgraph_counts` | valid subgraphs | discovered, witnesses_generated |
| ALIGN | `best_subgraph_score` | subgraph score | hard_coverage, soft_coverage, coherence_score |
| ALIGN | `retrieval_mode` | — | text value: e.g. `neo4j+lexical` |
| ALIGN | `scope_size` | family count | truncated flag |
| ALIGN | `retrieval_latency` | latency in ms | — |
| TRACE | `witnesses_kept` | witness count | — |
| TRACE | `claims_written` | ranked chain count | — |
| TRACE | `inferences_written` | RG writes | — |
| TRACE | `frame_witnesses` | witness-complete chains | — |
| TRACE | `coref_resolution` | mapping count | corroborates_edges |
| TRACE | `eg_size` | EG node count | edge_count |
| TRACE | `slot_coverage` | filled slots | total slots |
| TRACE | `trace_latency` | latency in ms | — |
| CONFLICT | `witnesses_indexed` | witness count | slot_groups |
| CONFLICT | `pairs_evaluated` | pairs compared | — |
| CONFLICT | `conflicts_found` | conflict count | by_rule breakdown |
| CONFLICT | `defeaters_created` | total defeaters | rebutting, undercutting |
| CONFLICT | `claims_contested` | contested claim count | slot_names |
| CONFLICT | `contradicts_edges_written` | edge count | — |
| CONFLICT | `negation_backend` | — | text value: e.g. `spacy+negspacy` |
| CONFLICT | `conflict_summary` | defeaters_created | clusters_written, scope_size, pairs_evaluated |
| CONFLICT | `conflict_latency` | latency in ms | — |
| CONSTRUCT | `construct_input_loaded` | chains loaded | inferences, defeaters, contested |
| CONSTRUCT | `inferences_weakened` | weakened count | updates list |
| CONSTRUCT | `construct_graph_writes` | new nodes | new_edges |
| CONSTRUCT | `synthesis_result` | synthesis confidence | synthesis_type, contested_slots |
| CONSTRUCT | `chain_selection` | selected chain score | effective_score, slot_weighted_confidence, findings, citations, limitations, g_ans_nodes |
| CONSTRUCT | `construct_latency` | latency in ms | — |
| EXPLAIN | `slots_answered` | answered count | total, contested_slots, missing_slots |
| EXPLAIN | `answer_confidence` | confidence score | label: HIGH/MODERATE/LOW/VERY LOW |
| EXPLAIN | `citations_collected` | citation count | — |
| EXPLAIN | `defeaters_in_answer` | defeater count | — |
| EXPLAIN | `explain_detail` | tether count | tether_failures, tether_complete, uncertainties, conflict_count, provenance_narratives, conflict_explanations, decision_explanations |
| EXPLAIN | `explain_latency` | latency in ms | — |

---

## pgAdmin queries

### Successful run queries

**1. Most recent runs and their status**
```sql
SELECT run_id, status, created_at, kg0_snapshot_id
FROM exploration_run
ORDER BY created_at DESC
LIMIT 5;
```

**2. Did all five operators succeed?**
```sql
SELECT operator_name, status, latency_ms, stage_order
FROM operator_invocation
WHERE run_id = '<your-run-id>'
ORDER BY stage_order;
```

**3. All metrics with operator name**
```sql
SELECT
    oi.operator_name,
    ioe.outcome_kind,
    ioe.outcome_name,
    ioe.metric_value_num,
    ioe.metric_value_text,
    ioe.severity,
    ioe.payload
FROM intermediate_outcome_event ioe
JOIN operator_invocation oi ON ioe.invocation_id = oi.invocation_id
WHERE ioe.run_id = '<your-run-id>'
ORDER BY ioe.event_time;
```

**4. Outcome event count per operator**
```sql
SELECT oi.operator_name, ioe.outcome_kind, COUNT(*)
FROM intermediate_outcome_event ioe
JOIN operator_invocation oi ON ioe.invocation_id = oi.invocation_id
WHERE ioe.run_id = '<your-run-id>'
GROUP BY oi.operator_name, ioe.outcome_kind
ORDER BY oi.operator_name;
```

**5. Confidence evolution through the pipeline**
```sql
SELECT snapshot_seq, current_stage, avg_confidence,
       unfilled_slots, unresolved_conflicts
FROM orchestration_state_snapshot
WHERE run_id = '<your-run-id>'
ORDER BY snapshot_seq;
```

**6. Pipeline decisions after each stage**
```sql
SELECT decision_kind, selected_action, rationale
FROM orchestration_decision
WHERE run_id = '<your-run-id>'
ORDER BY decision_time;
```

**7. Operator latency — performance baseline**
```sql
SELECT operator_name, latency_ms,
       ROUND(latency_ms / 1000.0, 2) AS latency_seconds
FROM operator_invocation
WHERE run_id = '<your-run-id>'
ORDER BY stage_order;
```

**8. Latency comparison across multiple runs**
```sql
SELECT
    er.run_id,
    er.created_at,
    oi.operator_name,
    oi.latency_ms
FROM operator_invocation oi
JOIN exploration_run er ON oi.run_id = er.run_id
ORDER BY er.created_at DESC, oi.stage_order;
```

---

### CONFLICT queries

**9. Conflict breakdown by rule**
```sql
SELECT metric_value_num AS conflicts_found, payload
FROM intermediate_outcome_event ioe
JOIN operator_invocation oi ON ioe.invocation_id = oi.invocation_id
WHERE ioe.run_id = '<your-run-id>'
  AND oi.operator_name = 'CONFLICT'
  AND ioe.outcome_name = 'conflicts_found';
```

**10. Defeater summary — clusters, scope, pairs evaluated**
```sql
SELECT metric_value_num AS defeaters_created, payload
FROM intermediate_outcome_event ioe
JOIN operator_invocation oi ON ioe.invocation_id = oi.invocation_id
WHERE ioe.run_id = '<your-run-id>'
  AND oi.operator_name = 'CONFLICT'
  AND ioe.outcome_name = 'conflict_summary';
```

---

### CONSTRUCT queries

**11. Chain selection — which chain was chosen and why**
```sql
SELECT metric_value_num AS selected_chain_score, payload
FROM intermediate_outcome_event ioe
JOIN operator_invocation oi ON ioe.invocation_id = oi.invocation_id
WHERE ioe.run_id = '<your-run-id>'
  AND oi.operator_name = 'CONSTRUCT'
  AND ioe.outcome_name = 'chain_selection';
```

**12. Full CONSTRUCT snapshot — all fields**
```sql
SELECT avg_confidence, state_payload
FROM orchestration_state_snapshot
WHERE run_id = '<your-run-id>'
  AND current_stage = 'CONSTRUCT';
```

---

### EXPLAIN queries

**13. Final confidence and contested slots**
```sql
SELECT
    ioe.outcome_name,
    ioe.metric_value_num  AS confidence_score,
    ioe.metric_value_text AS confidence_label,
    ioe.payload
FROM intermediate_outcome_event ioe
JOIN operator_invocation oi ON ioe.invocation_id = oi.invocation_id
WHERE ioe.run_id = '<your-run-id>'
  AND oi.operator_name = 'EXPLAIN'
  AND ioe.outcome_kind = 'quality_estimate'
ORDER BY ioe.event_time;
```

**14. Tether and explanation detail**
```sql
SELECT metric_value_num AS tether_count, payload
FROM intermediate_outcome_event ioe
JOIN operator_invocation oi ON ioe.invocation_id = oi.invocation_id
WHERE ioe.run_id = '<your-run-id>'
  AND oi.operator_name = 'EXPLAIN'
  AND ioe.outcome_name = 'explain_detail';
```

---

### Failure and debugging queries

**15. Any warnings or critical events in this run?**
```sql
SELECT
    oi.operator_name,
    ioe.outcome_name,
    ioe.severity,
    ioe.payload,
    ioe.event_time
FROM intermediate_outcome_event ioe
JOIN operator_invocation oi ON ioe.invocation_id = oi.invocation_id
WHERE ioe.run_id = '<your-run-id>'
  AND ioe.severity IN ('warning', 'critical')
ORDER BY ioe.event_time;
```

**16. Where did a failed run stop?**
```sql
SELECT operator_name, status, latency_ms, error_message, stage_order
FROM operator_invocation
WHERE run_id = '<your-run-id>'
ORDER BY stage_order;
```
Operators that never started will have no row. The failed operator will show `status = 'failed'` and `error_message` with the reason.

**17. TRACE verify failure detail**
```sql
SELECT ioe.payload, ioe.event_time
FROM intermediate_outcome_event ioe
JOIN operator_invocation oi ON ioe.invocation_id = oi.invocation_id
WHERE ioe.run_id = '<your-run-id>'
  AND oi.operator_name = 'TRACE'
  AND ioe.outcome_name = 'trace_verify_failed';
```
If this returns a row, the pipeline stopped after TRACE verification failed. The `payload` field contains the full error message.

**18. All failed runs in the last 7 days**
```sql
SELECT run_id, status, created_at
FROM exploration_run
WHERE status = 'failed'
  AND created_at > NOW() - INTERVAL '7 days'
ORDER BY created_at DESC;
```

---

## Key design decisions

**No overhead on operators** — log calls are enqueued in memory and flushed to PostgreSQL by a background worker thread (`queue.Queue`). Operators never block on database writes.

**No hardcoded values** — all file paths live in `constants.py`, all credentials in `.env`. If your teammate renames a script or moves an output file, update one line in `constants.py`.

**Append-only outcome events** — every metric is a new row. Nothing is overwritten. Adding a new operator or a new metric never requires a schema change.

**TEXT operator names** — `operator_name` is stored as free text, not a PostgreSQL enum. Adding a new operator requires no schema migration.

**Partial runs are fully queryable** — if an operator fails, everything up to that point is in the database. The failure event and error message are logged before the run closes.

**TRACE verify is a hard stop** — if `verify_eg_rg.py` fails, the pipeline stops immediately with a `critical` severity event logged. CONFLICT, CONSTRUCT, and EXPLAIN will not run.

---

## .env template

```
NEO4J_URI=neo4j+s://xxxxxxxx.databases.neo4j.io
NEO4J_DB=neo4j
NEO4J_USER=neo4j
NEO4J_PASS=yourpassword

PG_HOST=localhost
PG_DB=test
PG_USER=postgres
PG_PASSWORD=yourpassword
```
