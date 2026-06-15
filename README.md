# AIDE - AI-Driven Evidence Exploration

A pipeline that transforms raw documents into a queryable knowledge graph, enabling researchers to explore entities, relationships, and evidence chains across large document corpora through interactive dashboard.

---

## Table of Contents

1. [Project Overview](#project-overview)
2. [Architecture](#architecture)
3. [Document Preprocessing - Labeler](#document-preprocessing--labeler)
   - [Stage 1: Page Classification](#stage-1-page-classification)
   - [Stage 2: Entity and Relationship Extraction](#stage-2-entity-and-relationship-extraction)
4. [KG0 - Baseline Knowledge Graph](#kg0--baseline-knowledge-graph)
   - [Graph Construction](#graph-construction)
   - [Post-KG Validation](#post-kg-validation)
   - [Entity Resolution](#entity-resolution)
   - [Drug Node Enrichment](#drug-node-enrichment)
5. [Intent Object](#intent-object--)
   - [Validation](#validation)
   - [Correction](#correction)
6. [Reasoning Pipeline - Five Operators](#reasoning-pipeline--five-operators)
   - [ALIGN](#align)
   - [TRACE](#trace)
   - [CONFLICT](#conflict)
   - [CONSTRUCT](#construct)
   - [EXPLAIN](#explain)
7. [Data Model](#data-model)
7. [Tech Stack](#tech-stack)
8. [Setup and Installation](#setup-and-installation)
9. [Running the Pipeline](#running-the-pipeline)

---

## Project Overview

Corporate litigation document corpora — spanning emails, memos, research reports, contracts, and legal filings — contain critical relational evidence buried across thousands of pages. This project constructs a page-anchored knowledge graph over the UCSF Industry Documents Library, enabling multi-hop evidence retrieval with full provenance tracing back to the source document and page.

The pipeline was developed and validated on 50 documents and extended to 1,800 documents across five document families: PDF, email, spreadsheet, plaintext, presentation.

---

## Architecture

```
UCSF IDL Documents
       │
       ▼
┌─────────────────────────────────┐
│         Labeler                 │
│  Stage 1: Page Classification   │
│  Stage 2: Entity Extraction     │
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────────────────────────┐
│      Intermediate Database (PostgreSQL)             │
│  collection · document · catalog · entities ·       │
│  edges · page_labels                                │
└────────────┬────────────────────────────────────────┘
             │
             ▼
       kg0_from_db
             │
             ▼
┌────────────────────┐       ┌──────────────────────┐
│   KG0 (Neo4j Aura) │◄──────│  External Libraries  │
│                    │       │  RxNorm · FDA Orange │
│  post_kg_rules     │       │  Book                │
│  resolve_operator  │       └──────────────────────┘
└────────────┬───────┘
             │
             ▼
  Five-Operator Reasoning Pipeline
  (ALIGN · TRACE · CONFLICT · CONSTRUCT · EXPLAIN)
             │
             ▼
       Dashboard (UI)
```

---

## Document Preprocessing - Labeler

The labeler is an offline process that processes raw documents into structured entity and relationship records. It runs once before the application starts and is computationally intensive. The pipeline supports five document families and processes them through two sequential stages.

**Supported input types:**
- PDFs - page rendering + OCR + LLM extraction
- Text-like files (txt, csv, xml, json, html, markdown, yaml) — direct text + LLM extraction
- Zip files - each supported member processed as its own document

### Stage 1: Page Classification

Each PDF page is rendered as a high-resolution PNG image. A Vision LLM receives the images in batches and classifies each page into one of five document families:

| Label | Description |
|---|---|
| `email` | Email-style page with sender, recipient, subject |
| `document` | General memo, report, or letter |
| `spreadsheet` | Tabular or grid-structured content |
| `presentation` | Slide-style page |
| `text` | Plain unstructured text |

Classification is stored at the **page level**, not the document level — 63% of the corpus contains mixed page types within a single PDF.

**Output:** `labels.jsonl`, `labels.json` (one row per page: `page_index`, `label`, `confidence`, `rationale`, `image_path`)

`labels.jsonl` is consumed directly by `kg0_from_db` via `create_pages()` to assign page family labels to Page nodes in KG0.

For non-PDF files, Stage 1 is skipped and a synthetic single-page label is generated automatically.

### Stage 2: Entity and Relationship Extraction

Stage 2 performs the actual extraction using a structured chain of LLM calls. PDF pages, already rendered as images in Stage 1, are passed through `olmocr` (GPU-based OCR) to extract raw text, which is concatenated into a single full document text. Non-PDF files bypass OCR entirely and pass source text directly to the Text LLM.

Both paths then go through the same extraction chain:

1. **Entity extraction** - LLM identifies all named entities in the document and produces an entity inventory (`entities.txt`)
2. **Wikipedia enrichment** - LLM generates contextual descriptions and disambiguations per entity (`wikipedia_enrichment.txt`)
3. **Relationship extraction** - LLM identifies relationships between confirmed entities only (`relationship.txt`). Relationships to entities not present in the confirmed inventory are excluded, preventing hallucinated connections.
4. **Output consolidation** - results packaged into `summary.json` and `stage2_manifest.json`

Collapsing these into a single prompt was found to degrade extraction accuracy by approximately 30%, so the chain is deliberately sequential.

At Stage 2, extracted text and entity outputs are also indexed into **Solr** (full-text retrieval) and **Qdrant** (vector semantic search) to support downstream query processing.

**Output files:** `entities.txt`, `wikipedia_enrichment.txt`, `relationship.txt`, `summary.json`, `stage2_manifest.json`

These are ingested into the intermediate PostgreSQL database as node and edge records.

---

## KG0 - Baseline Knowledge Graph

KG0 is a page-anchored knowledge graph where every entity mention is tied to a specific page within its source document. It is constructed from the intermediate database by the `kg0_from_db` script and stored in Neo4j Aura.

### Graph Construction

`kg0_from_db` reads all collections, documents, catalog entries, nodes, edges, and page labels from the intermediate database, along with `labels.jsonl` from disk for page family classification, and loads them into Neo4j using idempotent MERGE operations.

During construction:
- Entity labels are derived dynamically from `top_category` (PascalCased via `kg0_utils.to_label()`) with an optional secondary label from `specific_category`
- `"Canonical Name || ABBR"` terms are split into a canonical entity node and a linked `Abbreviation` node
- Free-text relationship types are slugified and normalized to a canonical set via `kg0_clean.normalize_rel`
- Records are written in batched transactions

Every node carries a 12-character hex `kg_id` computed as a SHA-256 hash over a namespace and canonical input parts, ensuring that two pipeline runs against the same database snapshot produce identical graphs.

**Run command:**
```bash
python pipeline/kg0/kg0_from_db.py \
    --uri      bolt://127.0.0.1:7687 \
    --user     <user> \
    --password <password>
```

### Post-KG Validation

`post_kg_rules.py` runs after graph construction and performs:
- Structural and cardinality checks
- Cross-document enrichments
- Duplicate candidate detection

It logs a summary of all collections, documents, entities, and edges ingested, and outputs `candidates.json` — a list of entity pairs flagged as potential duplicates, each with witness context drawn from surrounding source sentences.

**Run command:**
```bash
python pipeline/kg0/post_kg_rules.py \
    --uri      bolt://127.0.0.1:7687 \
    --user     <user> \
    --password <password>
```

### Entity Resolution

`resolve_operator.py` takes `candidates.json` as input and performs LLM-based entity resolution. The LLM reads the witness context for each candidate pair and scores it between 0 and 1. Pairs scoring at or above the threshold of **0.85** are merged in the graph. Every decision is logged for human review.

**Output:** `resolution_report.json`, `revalidated_report.json`

**Run command:**
```bash
python pipeline/kg0/resolve_operator.py \
    --candidates pipeline/kg0/results/candidates.json \
    --uri        bolt://127.0.0.1:7687 \
    --user       <user> \
    --password   <password>
```

### Drug Node Enrichment

`external_libs.py` runs after resolution and queries every Drug node in KG0 against the **RxNorm** and **FDA Orange Book** APIs, writing structured biomedical vocabulary data back to the graph.

---

## Intent Object - A structured JSON representation of what a natural language question is actually asking.
The intent object is the contract between what the user asked and what the retrieval system does. A raw question is passed through an LLM and converted into a structured JSON object with typed slots, entities, scope, query hints, and a graph spec. Without this structure there is nothing to validate, correct, or audit.
To validate whether our intent object is good we pass it throught validation layers and then correct it.


### Validation 

Six weighted layers score the intent against what the question actually requires:

| Layer | Weight |
|---|---|
| Graph Spec Correctness | 25% |
| Entity Completeness | 20% |
| Retrieval Quality | 18% |
| Slot Completeness | 15% |
| Scope Correctness | 12% |
| Internal Consistency | 10% |

Verdicts: **PASS** ≥ 0.85 / **PARTIAL_PASS** 0.60–0.85 / **FAIL** < 0.60

A separate **Minimality Auditor** checks for bloat and reports independently — it does not affect the score.


### Correction

`corrector.py` applies targeted fixes to a deep copy of the intent — the original is never modified. It targets all HIGH priority issues and any layer scoring below 0.65, applying fixes in dependency order. Every change is logged with before/after values. The intent is re-validated after correction and three output files are produced: corrected intent, correction log, and re-validation report.

**What it fixes:** missing entities, wrong artifact types, missing slots, timestamp placeholders.  
**What it doesn't fix:** semantically wrong but structurally valid intents.

See [pipeline/intent_analyzer/README.md](pipeline/intent_analyzer/README.md) for the full intent object CLI reference.

---

## Reasoning Pipeline - Five Operators

The five-operator pipeline runs per user query, constructing an evidence chain and reasoning graph from KG0 in response to natural language question from dashboard. Each operator reads the previous operator's output and writes its own output for the next stage.

**ALIGN** Retrieves relevant artifacts from the Knowledge Graph based on the intent object. Generates entity and link hypotheses, discovers subgraphs, and produces an align bundle containing ranked subgraphs, witnesses, anchors, and mentions. This is the retrieval stage.

**TRACE** Reads the align bundle and traces evidence chains across the retrieved subgraphs. Extracts slot candidates for each question slot (WHO, WHAT, WHEN, HOW, WHY, EVIDENCE), assembles and ranks chains by coverage and confidence, and writes the Evidence Graph and Reasoning Graph. A verification step runs after TRACE to confirm the graph was written correctly — if it fails the pipeline stops.

**CONFLICT** Compares witnesses within each slot and detects contradictions using five rules: surface mismatch, temporal clash, supersession, negation, cross-artifact entity conflict, and reliability divergence. Writes Defeater nodes to the Reasoning Graph and marks contested Claims. The output tells CONSTRUCT which answers are disputed.

**CONSTRUCT** Selects the best evidence chain from TRACE, applies confidence reductions based on Defeaters from CONFLICT, and assembles a final answer bundle. Produces findings, citations, and limitations. The synthesis confidence score is a weighted average across slots.

**EXPLAIN** Reads the construct bundle and generates a human-readable investigator answer. Produces provenance narratives, conflict explanations, decision explanations, and a full citation list with tether verification. Outputs both a structured JSON bundle and a plain text report.

---

## Logging 

The state logging system makes every stage run observable, debuggable, and auditable. As the pipeline executes, structured outcome data from all five operators - ALIGN, TRACE, CONFLICT, CONSTRUCT, and EXPLAIN - is written to a PostgreSQL database in real time, capturing what each operator produced, what the system believed after each stage, and what the orchestrator decided to do next.

The system is organised into four layers: a PostgreSQL schema of five core tables (runs, operator invocations, outcome events, state snapshots, and decisions), stored functions including a bulk batch insert to minimise round trips, a Python service layer using psycopg2 with a background flush thread and queue.Queue for non-blocking writes, and per-operator typed logger classes that expose named methods so each operator logs structured records rather than raw SQL. All file paths are configured in constants.py and credentials are managed via .env.

Key design principles: writes are non-blocking so operator execution is never delayed, events are append-only for a clean audit trail, operator names are stored as plain TEXT to allow new operators without schema migrations, and every run — including partial failures — is fully queryable by run ID, operator, and timestamp.

See [pipeline/logger/STATE_LOGGING_README.md](pipeline/logger/STATE_LOGGING_README.md) for the logger CLI reference.

---


## Data Model

### Node Types

| Node | Key Properties |
|---|---|
| `Collection` | kg_id, name |
| `Document` | kg_id, document_id, name, collection, batesNumber, documentDate, industry, documentType |
| `Page` | kg_id, document_id, page_index, label, confidence |
| `Entity` | kg_id, name, top_category, specific_category, confidence, witness, wikipedia_url, wikipedia_category |
| `Abbreviation` | kg_id, name, expanded_form |

Dynamic entity labels: `top_category = "person"` + `specific_category = "employee"` → `(:Person:Employee)`. Reserved labels excluded from dynamic path: `Document`, `Collection`, `Abbreviation`.

### Edge Types

```
(:Collection)-[:CONTAINS_DOCUMENTS]->(:Document)
(:Document)-[:HAS_PAGE]->(:Page)
(:Page)-[:MENTIONS_PERSON|MENTIONS_ORG|MENTIONS_DRUG|…]->(:Entity)
(:Entity)-[<canonical rel>]->(:Entity)
(:Entity)-[:HAS_ABBREVIATION]->(:Abbreviation)
```

No page-to-page edges — structural adjacency is implied by `page_index` ordering under the same Document.

---

## Tech Stack

| Component | Technology |
|---|---|
| Pipeline orchestration | Python |
| Document OCR | olmocr (GPU-based) |
| Page classification | Vision LLM |
| Entity extraction | Text LLM (OpenRouter / Qwen) |
| Entity resolution | DeepSeek V4 |
| Intermediate database | PostgreSQL |
| Full-text search | Solr |
| Vector search | Qdrant |
| Knowledge graph | Neo4j Aura |
| Drug enrichment | RxNorm API, FDA Orange Book API |
| Version control | GitHub |

---

## Setup and Installation

```bash
# Install dependencies
pip install -r requirements.txt
```

Configure your environment variables:
```bash
NEO4J_URI=bolt://127.0.0.1:7687
NEO4J_USER=<user>
NEO4J_PASSWORD=<password>
OPENROUTER_API_KEY=<key>
```

---

## Team
This project is being built in collaboration with Advanced Database and Intelligence Lab (ADIL) at UCSD.