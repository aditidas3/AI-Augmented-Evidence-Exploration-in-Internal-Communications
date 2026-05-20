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

### Intent Analyzer

Takes a raw user question as input and corrects and enriches it into a structured intent object (`corrected_intent.json`). This file is the starting point for the pipeline — it defines the question, slot structure, entity hints, retrieval parameters, and scope constraints that all downstream operators work from.

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
