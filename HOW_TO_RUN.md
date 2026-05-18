# PIPELINE TO BUILD BASELINE KNOWLEDGE GRAPH USING GRAPH DEPENDENCIES RULES 

## Overview

The pipeline has stages that run in sequence:

```
LLM-extracted JSON files
        │
        ▼
[ pre_kg_rules.py ]   - validates, normalises, assigns UIDs
        │
        ▼
   clean/*.jsonl
        │
        ▼
[  kg_loader.py  ]    - loads nodes and edges into Neo4j
        │
        ▼
     Neo4j KG
        │
        ▼
[ post_kg_rules.py ]  - checks structure, cardinality, detects duplicate candidates
        │
        ├──────────────────────────────────────────────┐
        ▼                                              ▼
[ resolve_operator.py ]                    [ external_libs.py ]
  - LLM scores candidate pairs               - enriches Drug/Vocab nodes
  - merges duplicates in Neo4j                 from RxNorm, FDA Orange Book
```

---

## Prerequisites

Install dependencies:
```bash
pip install -r requirements.txt
```

---

## Files

| File | Role |
|---|---|
| `pre_kg_rules.py` | Node/edge definitions, runs rule_engine |
| `rule_engine.py` | Validation and normalisation rules (auto-generated from YAML) |
| `pre_kg_config.yaml` | Rule configuration - edit this to change rule behaviour |
| `kg_loader.py` | Loads cleaned JSONL into Neo4j |
| `post_kg_rules.py` | Checks structure, cardinality, detects duplicate entity candidates |
| `resolve_operator.py` | LLM-powered entity resolution - scores and merges duplicate nodes |
| `external_libs.py` | Enriches Drug/Vocab nodes from external APIs (RxNorm, FDA Orange Book) |

---

## Stage 1 - Pre-KG Bundle

### What it does
- Validates record structure (violations block the pipeline, warnings are logged)
- Normalises dates, emails, URLs, language fields
- Assigns stable UIDs to every entity that will become a Neo4j node
- Deduplicates abbreviations and normalises entity strings
- Outputs one clean `.jsonl` file per document type

### Input formats accepted
Each `--doc`, `--email`, `--ppt`, `--xls`, `--txt` argument accepts any of:

| Format | Description |
|---|---|
| `.jsonl` file | One JSON record per line |
| `.json` file | A single record object, or a JSON array of records |
| Folder path | All `*.json` files inside the folder, each treated as one record |

### Expected JSON structure
Every input file must be a JSON object with this top-level shape:

```json
{
  "id": "<record_id>",
  "output": { ... }
}
```

- `id` must be non-empty and stable - it is the anchor for the Document node UID in Neo4j. The value should be the document's unique identifier.
- `output` contains all extracted content and follows one of the five document schemas depending on type.


### Run command

Provide only the types you have - any omitted type is skipped:

```bash
python pre_kg_rules.py \
    --doc   ./inputs/docs/ \
    --email ./inputs/emails/ \
    --ppt   ./inputs/ppts/ \
    --xls   ./inputs/xls/ \
    --txt   ./inputs/txts/ \
    --out-dir ./clean
```

Each argument accepts a `.jsonl` file, a single `.json` file, or a folder of `.json` files.

### Outputs

```
clean/
├── DOC_clean.jsonl
├── EMAIL_clean.jsonl
├── PPT_clean.jsonl
├── XLS_clean.jsonl
├── TXT_clean.jsonl
└── pre_kg_report.json      ← violations, warnings, stats
```

`pre_kg_report.json` lists every violation and warning with the record ID and rule that triggered it. Fix all violations before proceeding to Stage 2.

---

## Stage 2 - KG Loader

### What it does
- Reads the cleaned JSONL files from Stage 1
- Creates Neo4j constraints and indexes on first run
- Merges all nodes and edges into Neo4j using the definitions in `pre_kg_rules.py`

### Run command

```bash
python kg_loader.py \
    --clean-dir ./clean \
    --uri   <url> \
    --user  <user> \
    --password <your_password>
```

### All arguments

| Argument | Default | Description |
|---|---|---|
|--clean-dir | none | Folder containing *_clean.jsonl files (overrides individual file args) |
| `--doc` | `clean/DOC_clean.jsonl` | Cleaned DOC records |
| `--email` | `clean/EMAIL_clean.jsonl` | Cleaned EMAIL records |
| `--ppt` | `clean/PPT_clean.jsonl` | Cleaned PPT records |
| `--xls` | `clean/XLS_clean.jsonl` | Cleaned XLS records |
| `--txt` | `clean/TXT_clean.jsonl` | Cleaned TXT records |
| `--uri` | `bolt://localhost:7687` | Neo4j connection URI |
| `--user` | `neo4j` | Neo4j username |
| `--password` | `neo4j` | Neo4j password |
| `--dry-run` | off | Parse and validate without writing to Neo4j |
| `--batch-size` | `500` | Records per Neo4j transaction |
| `--limit` | none | Max records per file, useful for testing |


### Outputs

- Live Neo4j graph with all nodes and edges
- `kg_load_failures.json` - written only if any individual records fail to load; contains the record identifier, stage, and Neo4j error for each failure

---

## Stage 3 - Post-KG Bundle

### What it does
- Runs directly against the live Neo4j graph after Stage 2
- Validates graph structure and relationship cardinality
- Detects duplicate entity nodes and writes candidate pairs to `candidates.json` for the resolver
- Applies enrichment rules, derives new relationships and adds secondary labels
- Does not merge or modify nodes, that is handled by `resolve_operator.py`

### Run command

```bash
python post_kg_rules.py \
    --uri      bolt://localhost:7687 \
    --user     neo4j \
    --password <your_password>
```

### All arguments

| Argument | Default | Description |
|---|---|---|
| `--uri` | `bolt://localhost:7687` | Neo4j connection URI |
| `--user` | `neo4j` | Neo4j username |
| `--password` | required | Neo4j password |
| `--out` | `post_kg_report.json` | Path to write the quality report |
| `--candidates-out` | `candidates.json` | Path to write candidate pairs for resolve_operator.py |
| `--skip-resolution` | off | Skip candidate detection — run checks and enrichments only |

### Outputs

- `post_kg_report.json` - all check results (PASS / WARN / FAIL) with counts and enrichment summary
- `candidates.json` - candidate duplicate pairs for each entity type, consumed by `resolve_operator.py`

---

## Stage 4 - Entity Resolution

### What it does
- Reads `candidates.json` produced by Stage 3
- For each candidate pair, formats the witness context and calls an LLM to score similarity (0.0 – 1.0)
- Auto-merges pairs that score at or above the threshold directly in Neo4j
- The lower-degree node (fewer connections) is merged into the higher-degree node
- Writes a full resolution report including score, canonical form, and reasoning for every pair
- Can be re-run on the same `candidates.json` without re-running Stage 3

### Environment variables required

```
LLM_API_KEY=<your_openrouter_key>
BASE_URL=<base_url>
MODEL_NAME=<model_name>
```

Place these in a `.env` file in the same directory and the script loads it automatically.

### Run command

```bash
python resolve_operator.py \
    --candidates candidates.json \
    --uri        <url> \
    --user       <user> \
    --password   <your_password>
```

### All arguments

| Argument | Default | Description |
|---|---|---|
| `--candidates` | `candidates.json` | Path to candidates.json written by post_kg_rules.py |
| `--uri` | `<url>` | Neo4j connection URI |
| `--user` | `<user>` | Neo4j username |
| `--password` | required | Neo4j password |
| `--threshold` | `0.85` | Minimum LLM score to auto-merge |
| `--dry-run` | off | Score pairs but do not write any merges to Neo4j |
| `--out` | `resolution_report.json` | Path to write the resolution report |

### Outputs

- `resolution_report.json` - every candidate pair with score, canonical form, reasoning, and action taken (`merged`, `skipped`, `dry_run_merge`, or `merge_failed`)

---

## Stage 5 - External Libraries (Vocab Enrichment)

### What it does
- Runs after Stage 2 against the live Neo4j graph
- Looks up every Drug node by name against external vocabulary APIs
- Writes enrichment results as properties onto existing Vocab nodes linked via `HAS_VOCAB` from each Drug
- Re-runs are fully idempotent - results are written with `MERGE` so re-running the same drug updates in place without duplicates

### Supported sources

| Source | API | Key required |
|---|---|---|
| `rxnorm` | NLM RxNorm REST API | No |
| `fda_orange_book` | FDA Orange Book API | No |

### Properties written onto Vocab nodes

| Property | Source |
|---|---|
| `rxcui` | RxNorm |
| `canonicalName` | RxNorm |
| `drugClass` | RxNorm (ATC classification) |
| `synonyms` | RxNorm |
| `applicationNumber` | FDA Orange Book |
| `approvalDate` | FDA Orange Book |
| `manufacturer` | FDA Orange Book |
| `raw_json` | All sources - any API fields not mapped above |
| `sourceUrl` | All sources - exact API endpoint called |
| `fetchedAt` | All sources - UTC timestamp of the call |

### Run command - all sources

```bash
python external_libs.py \
    --uri      <url> \
    --user     <user> \
    --password <your_password>
```

### Run command - single source

```bash
# RxNorm only
python external_libs.py \
    --uri <uri> --user <user> --password <pw> \
    --source rxnorm

# FDA Orange Book only
python external_libs.py \
    --uri <uri> --user <user> --password <pw> \
    --source fda_orange_book
```

### All arguments

| Argument | Default | Description |
|---|---|---|
| `--uri` | `bolt://localhost:7687` | Neo4j connection URI |
| `--user` | `neo4j` | Neo4j username |
| `--password` | required | Neo4j password |
| `--source` | `all` | Comma-separated source names or `all` |
| `--limit` | none | Max Drug nodes to process per source - useful for testing |
| `--dry-run` | off | Show what would be fetched without writing to Neo4j |

---

## Fields populated at a later metadata stage

The following fields on the Document node will be empty at KG creation time and populated separately. Neo4j stores them as empty string properties until then.

```
url, documentDate, type, industry, tid, collection, source, case, bates_number, dateAdded
```

When the metadata pipeline runs, re-running `kg_loader.py` with the enriched JSONL will update these properties on existing nodes via `MERGE ... SET` without creating duplicates. The Document node UID is keyed on `record id` only, so it remains stable across all pipeline stages.

---

## Re-running the pipeline

The pipeline is safe to re-run on the same data. All Neo4j writes use `MERGE`, so re-running will update existing nodes rather than creating duplicates.

---

## Updating rules

Rules live in `pre_kg_config.yaml`. To change a rule:

1. Edit `pre_kg_config.yaml`
2. Send the updated YAML to an LLM with the generation prompt at the bottom of the file
3. Replace `rule_engine.py` with the generated output
4. Re-run Stage 1
5. Re-run Stage 2 (safe to re-run - uses MERGE)

To disable a rule without deleting it, set `enabled: false` in the YAML.
