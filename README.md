# AI-Augmented Evidence Exploration for Discovering Latent Corporate Strategies in Internal Communications

## Overview

Investigating corporate behavior across large document collections traditionally requires researchers to manually review thousands of files. This project automates that process by extracting and connecting people, organizations, topics, products, and legal frameworks from the corpus into a structured knowledge graph, surfaced through an interactive research dashboard.

## Team
This project is being built in collaboration with Advanced Database and Intelligence Lab (ADIL).

---

## Project Design

Documents are collected and made into JSON items to process them into a baseline Knowledge Graph. User questions are entered through the Dashboard and processed into a structured intent JSON object. Backend operators are run to extract evidence based on the question and results are returned to the dashboard for the user.

---

## Tech Stack

- **Neo4j** — graph database
- **Python** — pipeline scripts
- **PostgreSQL** — project database, logging and audit trail
- **LLM API** — LLM-based schema design, entity resolution

---

## Components

### Intent Analyzer - Intent Validation & Correction Pipeline

A pipeline that validates structured JSON intent objects generated from natural language questions, scores them across six independent layers, and optionally applies automatic corrections.

---

#### Overview

```
Question + Intent Object
        ↓
  [Stage 1] Parse Question       → ground truth reference
        ↓
  [Stage 2] Validate (6 layers)  → scores + issues per layer
        ↓
  [Stage 3] Score & Verdict      → overall score, PASS/PARTIAL_PASS/FAIL
        ↓
  [Stage 4] Correct (optional)   → patched intent copy + correction log
```

---

#### Stage 1: Parsing the Question

`question_parser.py` reads the raw question and extracts ground truth **independently** of the intent object. This becomes the reference that all validation layers check against.

Extracted fields include:

- Entities (canonical node type + `ENTITY_*` intent category)
- Time range
- Required slot types
- Temporal constraints
- Flags: `intentionality_required`, `cross_track_awareness_required`

> Nothing here reads the intent — it only reads the question.

---

#### Stage 2: Validation Layers

Six independent layers each produce a score between `0.0` and `1.0`, plus a list of issues at `HIGH`, `MEDIUM`, or `LOW` priority.

#### Scoring Formula

```
score = 1.0 - (penalty / (total_checks × 3))
```

| Priority | Penalty |
|----------|---------|
| HIGH     | 3       |
| MEDIUM   | 2       |
| LOW      | 1       |

Score floors at `0.0`.

#### Layer Weights

| Layer                  | Weight |
|------------------------|--------|
| Graph Spec Correctness | 0.25   |
| Entity Completeness    | 0.20   |
| Retrieval Quality      | 0.18   |
| Slot Completeness      | 0.15   |
| Scope Correctness      | 0.12   |
| Internal Consistency   | 0.10   |

#### What Each Layer Checks

**Entity Completeness** — Every entity extracted from the question exists in `EntityHints` with the correct `ENTITY_*` category; no hint uses an unrecognised category.

**Scope Correctness** — Time filter matches the question's date range; artifact types are declared; scope mode is appropriate (e.g. `REQUIRE` is flagged on investigative questions as it may exclude exculpatory documents).

**Retrieval Quality** — Every non-implicit entity has coverage in `query_text` or `query_expansions`; implicit constraints (intentionality, cross-track awareness) are reflected in expansions; generic terms like "compliance" or "records" are absent.

**Slot Completeness** — All slot types inferred from the question are present, with special enforcement on `AWARENESS` slots for cross-track knowledge questions.

**Graph Spec Correctness** — Edge validity (no self-referential edges, no causal relations on investigative questions, no raw datetimes in temporal constraints); vars reference declared `EntityHints`; concurrent temporal constraints are modeled when required.

**Internal Consistency** — Cross-section alignment: slot artifact types declared in `ScopeSpec`; graph vars reference `EntityHint` surfaces; temporal entity hints fall within the declared time filter.

#### Minimality Auditor

A separate auditor runs cross-cutting checks for bloat:

- Unused `EntityHints` with no graph var
- Duplicate artifact types
- Oversized diagnostic sections
- Excessive secondary objectives

Minimality findings are **reported separately and do not affect the score**.

---

#### Stage 3: Overall Score & Verdict

```
overall = Σ (layer_score × layer_weight)
```

#### Score Verdicts

| Score    | Verdict       |
|----------|---------------|
| ≥ 0.85   | `PASS`        |
| ≥ 0.60   | `PARTIAL_PASS`|
| < 0.60   | `FAIL`        |

#### Minimality Verdicts

| Findings | Verdict              |
|----------|----------------------|
| ≤ 2      | `MINIMAL`            |
| 3–6      | `PARTIALLY_MINIMAL`  |
| > 6      | `BLOATED`            |

Issues are grouped into priority fix groups (`P0` / `P1` / `P2`) in the report output.

---

#### Stage 4: Correction

`corrector.py` applies targeted fixes to a **deep copy** of the intent. The original is never modified.

#### Issue Selection

Two sets of issues are selected for correction:

- All `HIGH` priority issues, regardless of layer
- All issues (any priority) from layers whose score fell **below 0.65**

#### Fix Order

Fixes are applied in dependency order so earlier sections are stable before later ones that reference them:

```
Entity Completeness → Scope → Retrieval → Slots → Graph Spec → Internal Consistency
```

#### Fix Behavior

Each fix is surgical:

| Issue | Fix |
|-------|-----|
| Missing `EntityHint` | Adds exactly one hint entry using `intent_category` from ground truth |
| Wrong time filter | Replaces only `start` and `end` values |
| Missing slot | Appends exactly one slot entry with the required `slot_type` |

Every change is recorded in a `CorrectionEntry` with `before`, `after`, and `reason`. After correction, the intent is **re-validated** and the log includes the score delta and verdict change.

#### `--pipeline` Flag

Running `corrector.py --pipeline` executes all three stages (validate → correct → re-validate) in one shot and saves three output files:

| File | Contents |
|------|----------|
| Corrected intent | Patched intent JSON |
| Correction log | Per-change entries with score delta |
| Re-validation report | Full validation output post-correction |

---

### Pipeline Operators

The pipeline runs five operators in sequence. Each operator reads the previous operator's output and writes its own output for the next stage.

**ALIGN**
Retrieves relevant artifacts from the Knowledge Graph based on the intent object. Generates entity and link hypotheses, discovers subgraphs, and produces an align bundle containing ranked subgraphs, witnesses, anchors, and mentions. This is the retrieval stage.

**TRACE**
Reads the align bundle and traces evidence chains across the retrieved subgraphs. Extracts slot candidates for each question slot (WHO, WHAT, WHEN, HOW, WHY, EVIDENCE), assembles and ranks chains by coverage and confidence, and writes the Evidence Graph and Reasoning Graph. A verification step runs after TRACE to confirm the graph was written correctly — if it fails the pipeline stops.

**CONFLICT**
Compares witnesses within each slot and detects contradictions using five rules: surface mismatch, temporal clash, supersession, negation, cross-artifact entity conflict, and reliability divergence. Writes Defeater nodes to the Reasoning Graph and marks contested Claims. The output tells CONSTRUCT which answers are disputed.

**CONSTRUCT**
Selects the best evidence chain from TRACE, applies confidence reductions based on Defeaters from CONFLICT, and assembles a final answer bundle. Produces findings, citations, and limitations. The synthesis confidence score is a weighted average across slots.

**EXPLAIN**
Reads the construct bundle and generates a human-readable investigator answer. Produces provenance narratives, conflict explanations, decision explanations, and a full citation list with tether verification. Outputs both a structured JSON bundle and a plain text report.

---

### State Logging System

A lightweight infrastructure layer that runs alongside the pipeline and records every operator run into PostgreSQL — without adding overhead to operator execution. Designed so that every metric, decision, and state transition is queryable after a run without opening any output file.

**Four tables are populated per run:**
- `exploration_run` — one row per run, stores intent, config hash, and final status
- `operator_invocation` — one row per operator, stores timing and success/failure
- `intermediate_outcome_event` — many rows per operator, stores individual metrics (candidate counts, confidence scores, defeater counts, latency etc.)
- `orchestration_state_snapshot` — one row per operator, stores point-in-time pipeline state between stages

Log writes are enqueued in memory and flushed to PostgreSQL by a background worker thread, so operators are never blocked waiting on database writes. All paths are defined in `constants.py` and all credentials in `.env` — nothing is hardcoded. See `state_logging/STATE_LOGGING_README.md` for full details and SQL queries.

---

## Running the Pipeline

```bash
# 1. Set intent file path in constants.py
# 2. Fill in credentials in .env
# 3. Run
python pipeline.py
```

All five operators run in sequence automatically. Results are logged to PostgreSQL and the final answer is written to the explain output folder.
