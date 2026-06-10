# KG0 Code Walkthrough

A step-by-step map of `pipeline/kg0/` for readers who want to know
**how raw PostgreSQL extraction tables become a live Neo4j knowledge
graph, what every node and edge means, and what the rule engines
are for**.

This document pairs with the source files. When you see a reference
like `kg0_from_db.py:449`, open the file at that line — the code is
the ground truth; this walkthrough is a guide.

---

## 1. What KG0 is

KG0 is the **"level-zero" knowledge graph** — the first graph in the
pipeline, built directly from structured data the upstream
extraction and labeling stages have already produced. Later operators
(ALIGN, TRACE, CONFLICT, CONSTRUCT, EXPLAIN) all read from KG0 as
their authoritative source of entities, documents, and mentions.

```
Postgres tables          KG0 Builder          Neo4j / Memgraph
──────────────────      ────────────          ─────────────────
raw_data_nodes   ──┐                         (:Collection)
raw_data_edges   ──┼──► pipeline/kg0/ ──►    (:Document {kg_id, …})
opioid_catalog   ──┤                         (:Page {label, …})
{doc_id}/labels.jsonl ─┘                     (:Person|Organization|Drug|…)
                                             …plus all edges
```

KG0 is **page-based**: every mention is anchored to a specific
`:Page` inside a specific `:Document`. This matches the fact that
~63% of corpus PDFs are mixed-label (a single PDF can contain both
email-style and table-style pages), so family labels live on pages,
not documents.

The build is **idempotent**: every node and edge uses MERGE on a
deterministic `kg_id`, so re-running the builder against the same
Postgres snapshot produces the same graph byte-for-byte.

---

## 2. Package layout

```
pipeline/kg0/
├── __init__.py                 (empty)
├── kg0_from_db.py              KG0Builder + Postgres loader + CLI (~1,030 lines)
├── kg0_utils.py                sha_id, resolve_labels, LABEL_TO_REL (~115 lines)
├── rule_engine.py              Pre-KG rules (~520 lines, auto-generated)
├── post_kg_rules.py            Post-KG checks and enrichments (~890 lines)
├── resolve_operator.py         LLM-based duplicate-entity merger (~485 lines)
├── export_kg0_json.py          Export KG0 subgraphs to JSON (~195 lines)
├── external_libs.py            Third-party library adapters (~520 lines)
└── CODE_WALKTHROUGH.md         This file
```

Three layers:

1. **Build** — `kg0_from_db.py` reads Postgres and writes Neo4j.
   `kg0_utils.py` provides the deterministic-ID and label-resolution
   primitives.
2. **Clean / enrich** — `rule_engine.py` runs **before** KG0 (over
   raw extraction records); `post_kg_rules.py` runs **after** KG0
   (over the live graph) to verify invariants and add derived edges.
3. **Resolve** — `resolve_operator.py` uses an LLM to adjudicate
   candidate duplicates that `post_kg_rules.detect_entity_candidates`
   surfaces.

---

## 3. Data model at a glance

### 3.1 Node families

```
(:Collection  {kg_id, name})
(:Document    {kg_id, document_id, name, collection,
               batesNumber, case, source, documentDate,
               industry, documentType})
(:Page        {kg_id, document_id, page_index, label,
               confidence, image_path})
(:Person|Organization|Drug|Product|Location|…
              {kg_id, name, top_category, specific_category,
               confidence, witness, wikipedia_url,
               wikipedia_category})
(:Abbreviation {kg_id, name, expanded_form})
```

**Dynamic entity labels**: the primary label on an entity node comes
from `top_category` in Postgres, PascalCased via
`kg0_utils.to_label()`. The secondary label (when present) is
derived from `specific_category`. So an entity row with
`top_category = "person"` and `specific_category = "employee"`
becomes `(:Person:Employee {kg_id: …, name: …})`.

Three labels are **reserved** and excluded from the dynamic path to
avoid collisions: `Document`, `Collection`, `Abbreviation`
(`kg0_utils.py:INFRA_LABELS`).

### 3.2 Edge families

```
(:Collection)-[:CONTAINS_DOCUMENTS]->(:Document)
(:Document)-[:HAS_PAGE]->(:Page)
(:Page)-[:MENTIONS_PERSON|MENTIONS_ORG|MENTIONS_DRUG|…]->(:Entity)
(:Entity)-[<canonical rel>]->(:Entity)
(:Entity)-[:HAS_ABBREVIATION]->(:Abbreviation)
```

The Page→Entity relationship type is chosen by `LABEL_TO_REL` in
`kg0_utils.py`:

```python
LABEL_TO_REL = {
    "Person":       "MENTIONS_PERSON",
    "Organization": "MENTIONS_ORG",
    "Drug":         "MENTIONS_DRUG",
    "Product":      "MENTIONS_PRODUCT",
    "Location":     "MENTIONS_LOCATION_IN_TEXT",
    "Health":       "MENTIONS_HEALTH",
    "Date":         "MENTIONS_DATE",
    "Event":        "HAS_EVENT",
    "Risk":         "HAS_RISK",
    "Decision":     "HAS_DECISION",
    "Requirement":  "HAS_REQUIREMENT",
    "Claim":        "HAS_CLAIM",
    "Procedure":    "HAS_PROCEDURE",
    "Finance":      "HAS_FINANCE",
    "Metric":       "HAS_METRIC",
    "Identifier":   "HAS_IDENTIFIER",
    # default: DEFAULT_REL = "HAS_CLAIM"
}
```

Entity↔entity edges use the canonical relationship name from the
Postgres edge row, slugified via `slugify_rel()`.

**No page-to-page edges** — structural adjacency is implied by
`page_index` ordering under the same Document. This keeps the graph
shallow and makes ALIGN's Phase 1 retrieval Cypher simple.

### 3.3 Deterministic IDs

Every node carries a 12-character hex `kg_id` computed as:

```python
def sha_id(namespace: str, *parts: str) -> str:
    raw = "|".join([namespace, *parts])
    return hashlib.sha256(raw.encode()).hexdigest()[:12]
```

Namespaces used across the builder:

```
collection_strong   → Collection nodes (name lowercased)
doc_strong          → Document nodes (document_id as-is)
page_strong         → Page nodes (document_id + page_index)
entity_strong       → Entity nodes (canonical name + top_category)
abbrev_strong       → Abbreviation nodes (short_form + expansion)
```

Because everything hashes canonicalized inputs, two builders against
the same Postgres snapshot produce the same `kg_id`s, which is what
makes MERGE idempotent.

---

## 4. Entry points

Three ways to invoke KG0:

```bash
# 1. CLI against a live Neo4j
python -m pipeline.kg0.kg0_from_db \
  --neo4j-uri bolt://127.0.0.1:7687 \
  --neo4j-user neo4j \
  --neo4j-password secret \
  --node-table opioid_raw_nodes \
  --edge-table opioid_raw_edges \
  --docs-dir /data/docs \
  --batch-size 500

# 2. Dry run — load from PG, preview label distribution, no writes
python -m pipeline.kg0.kg0_from_db --dry-run

# 3. Wipe + rebuild — DELETE everything then rebuild
python -m pipeline.kg0.kg0_from_db --wipe
```

From Python:

```python
from pipeline.kg0.kg0_from_db import load_from_postgres, KG0Builder
from neo4j import GraphDatabase

data = load_from_postgres(
    node_table="opioid_raw_nodes",
    edge_table="opioid_raw_edges",
    docs_dir=Path("/data/docs"),
)

driver = GraphDatabase.driver("bolt://127.0.0.1:7687",
                              auth=("neo4j", "secret"))
try:
    builder = KG0Builder(driver, dry_run=False, batch_size=500)
    builder.build(data)
finally:
    driver.close()
```

---

## 5. Execution flow at a glance

`KG0Builder.build(data)` (`kg0_from_db.py:938`) runs six steps in
order:

```
PGData ─► setup_constraints()
        │   └─► uniqueness constraint on kg_id for
        │       Collection, Document, Page, Abbreviation
        │
        ▼
        create_collections(collections)               → coll_kg_ids
        │   Collection nodes only
        │
        ▼
        create_documents(documents, coll_kg_ids,      → doc_kg_ids
                         catalog)
        │   Document nodes + (Collection)-[:CONTAINS_DOCUMENTS]->(Document)
        │
        ▼
        create_pages(doc_kg_ids, page_labels,         → page_kg_ids
                     entities)
        │   Page nodes + (Document)-[:HAS_PAGE]->(Page)
        │
        ▼
        create_entities_and_page_links(entities,
                                       doc_kg_ids,
                                       page_kg_ids)
        │   Entity nodes + (Page)-[:MENTIONS_*]->(Entity)
        │   + (Entity)-[:HAS_ABBREVIATION]->(Abbreviation)
        │
        ▼
        create_entity_to_entity_edges(edges, entities)
            (Entity)-[<rel>]->(Entity) via EndpointResolver
```

Every step batches writes via `_run_batch(cypher, rows)` with
`batch_size=500` by default. `_ensure_constraint(label)` is called
lazily the first time a dynamic entity label appears, so the primary
constraint on `kg_id` is always present before any MERGE against that
label.

Post-build, two separate CLIs run over the live graph:

```bash
# Run structural checks + enrichments + candidate detection
python -m pipeline.kg0.post_kg_rules --neo4j-uri …

# Adjudicate candidates with an LLM and merge duplicates
python -m pipeline.kg0.resolve_operator --candidates candidates.json …
```

---

## 6. Step-by-step walkthrough

### Step 0 — Load from Postgres
**Function**: `load_from_postgres()` (`kg0_from_db.py:382`)

Populates a `PGData` dataclass from three Postgres tables plus one
on-disk directory:

```
PGData
├── collections      set[str]                        — unique collection_name values
├── documents        dict[doc_id → collection_name]  — every (doc, collection) pair
├── catalog          dict[doc_id → catalog_row]      — bn, case, source, dd, industry, dt
├── entities         list[raw_row]                   — from raw_data_nodes table
├── edges            list[raw_row]                   — from raw_data_edges table
└── page_labels      dict[doc_id → dict[page_idx → {label, confidence, image_path}]]
```

- `raw_data_nodes`: `collection_name, document_id, page_number, term,
  top_category, specific_category, wikipedia_category, wikipedia_url,
  confidence, witness`.
- `raw_data_edges`: `collection_name, document_id, term_1,
  semantic_category_1, term_2, semantic_category_2, relationship,
  relation_category, confidence`.
- `opioid_catalog`: per-document metadata (bates number, case, source,
  document date, industry, document type).
- `{docs_dir}/{doc_id}/labels.jsonl`: per-page family label from the
  labeler — see `labeler/CODE_WALKTHROUGH.md`. Missing files are
  tolerated; the builder falls back to an `UNKNOWN_PAGE` placeholder.

`_load_page_labels_from_docs()` (line ~332) walks every document
directory and fills the `page_labels` dict, logging how many
documents had labels vs. how many were missing.

### Step 1 — Setup constraints
**Method**: `KG0Builder.setup_constraints()` (`kg0_from_db.py:494`)

Creates `CREATE CONSTRAINT IF NOT EXISTS FOR (n:Label) REQUIRE
n.kg_id IS UNIQUE` for the four infra labels (`Collection`,
`Document`, `Page`, `Abbreviation`). Dynamic entity labels get their
constraints lazily via `_ensure_constraint(label)` the first time the
builder sees that label in Step 4.

The constraint creation runs a Neo4j 5 statement first, falls back to
the older `CREATE CONSTRAINT ON (n:Label) ASSERT …` syntax if the
server rejects it, and caches successfully-created labels in
`_constraint_cache` so subsequent calls are free.

### Step 2 — Create collections
**Method**: `create_collections()` (`kg0_from_db.py:499`)

For every unique collection name:

```python
kg_id = sha_id("collection_strong", name.lower())
MERGE (c:Collection {kg_id: r.kg_id})
SET c.id = r.kg_id, c.name = r.name
```

Returns a `{collection_name → kg_id}` map used by the next step.

### Step 3 — Create documents
**Method**: `create_documents()` (`kg0_from_db.py:514`)

For every `(document_id, collection_name)` pair in `data.documents`:

1. Compute `kg_id = sha_id("doc_strong", document_id)`.
2. Pull the catalog row (if any) and normalize the date via
   `_pick_newest_dd()` — the catalog's `dd` column can contain
   multiple semicolon-separated date strings (`"2018 January 04;
   2019 March 11"`); `_pick_newest_dd` parses each via
   `_parse_dd_date` and returns the latest as `YYYY-MM-DD`.
3. Extract the source title from the catalog's JSON `source`
   column via `_extract_source_title()`.
4. Emit the Document row with `document_id`, `name`, `collection`,
   `batesNumber`, `case`, `source`, `documentDate`, `industry`,
   `documentType`.
5. Emit a `(c)-[:CONTAINS_DOCUMENTS]->(d)` link row.

Both lists get written in one `_run_batch` per Cypher statement.

### Step 4 — Create pages
**Method**: `create_pages()` (`kg0_from_db.py:569`)

Three-phase page materialization:

1. **Collect the set of `(doc_id, page_index)` pairs we need.**
   Seed from `page_labels` (the authoritative source when the
   labeler ran), then fill in any additional page indexes observed
   on entity rows via their `page_number` column. Rows whose
   `page_number` is NULL fall back to `UNKNOWN_PAGE_INDEX`.
2. **Ensure every document has at least one page** — if a doc had
   no labels file *and* no entity rows with page numbers, emit one
   `UNKNOWN_PAGE` row so downstream queries (`MATCH (d)-[:HAS_PAGE]->
   (p)`) never return a Document without children.
3. **Build and emit**. Each Page row carries `kg_id`,
   `document_id`, `page_index`, `label`, `confidence`, `image_path`.
   The `kg_id` is `sha_id("page_strong", doc_id, str(page_index))`,
   so every (doc, page) pair has a stable, deterministic ID.

Returns a `{(doc_id, page_index) → page_kg_id}` map used by the next
step.

### Step 5 — Create entities and Page→Entity edges
**Method**: `create_entities_and_page_links()` (`kg0_from_db.py:672`)

This is the **largest** and most subtle step. Core logic:

1. Build an abbreviation map (`_build_abbrev_map`): if any entity
   row has a term of the form `"Long Form (LF)"`, record both
   forms so downstream matching can resolve "LF" to the full term.
2. For every raw entity row:
   - Skip rows whose `top_category` cannot be resolved to a legal
     Neo4j label via `resolve_labels()` (`skipped_no_label`
     counter).
   - Compute `entity_kg_id = sha_id("entity_strong",
     canonical_name.lower(), top_category.lower())`. Canonicalization
     happens via `_canonical_for_row` which strips trailing
     parenthetical abbreviations.
   - Deduplicate by `(name, top_category)` key — the same person
     mentioned on 50 pages gets a single `:Person` node and 50
     Page→Entity edges.
   - Merge per-row witnesses: the `witness` column (raw extraction
     text) gets appended to the edge's `witness` list, not the
     node's. A given canonical person's node carries their
     cross-page aggregated metadata (Wikipedia URL, confidence
     average, etc.); each edge carries the specific sentence the
     mention came from.
3. Per-entity, look up `LABEL_TO_REL[primary_label]` to decide the
   Page→Entity relationship type. Unknown primary labels fall
   through to `DEFAULT_REL = "HAS_CLAIM"`.
4. Batch-write everything: entity nodes grouped by label
   (`"UNWIND $rows AS r MERGE (e:Person {kg_id: r.kg_id}) SET …"`),
   edge rows grouped by relationship type, and abbreviation
   nodes/edges as two smaller batches at the end.

Counters tracked: `entities`, `page_entity_edges`, `abbreviations`,
`skipped_no_label`, `skipped_no_page`.

### Step 6 — Create entity↔entity edges
**Method**: `create_entity_to_entity_edges()` (`kg0_from_db.py:~820`)

For every raw edge row:

1. Resolve `term_1` and `term_2` to entity `kg_id`s via
   `EndpointResolver`. The resolver is a 4-tier fuzzy matcher:

   ```
   Tier 1 — exact:         lowercased exact match
   Tier 2 — abbrev_alias:  "LF" → resolved via abbreviation map
   Tier 3 — normalized:    punctuation/whitespace stripped → unique match
   Tier 4 — token_set:     set of meaningful tokens → unique match
   Tier 5 — token_sub:     one side is a token subset of the other,
                           only when the smaller side carries a
                           substantive discriminator token
   ```

   Each `resolve(query)` call returns `(kg_id, tier)` or
   `(None, "miss")`.

2. If either endpoint misses, the edge gets skipped and counted in
   `dropped`. Otherwise slugify the relationship label
   (`slugify_rel`) into a Neo4j-legal identifier.
3. Group edges by relationship type and batch-write:

   ```cypher
   UNWIND $rows AS r
   MATCH (a {kg_id: r.kg_id_1})
   MATCH (b {kg_id: r.kg_id_2})
   MERGE (a)-[e:RELATIONSHIP_TYPE]->(b)
   SET e.confidence = r.confidence,
       e.relation_category = r.relation_category
   ```

4. Log a resolution-tier breakdown so operators can see how much
   fuzzy matching was needed.

**Why this matters**: most of the recall loss in KG0 comes from the
edge pass, because the extraction rows call an entity by one surface
form in the node table and a different surface form in the edge
table. The tiered resolver is load-bearing — don't downgrade it to
exact-match only unless you also rewrite the upstream extraction
pipeline.

---

## 7. Supporting modules

### 7.1 `kg0_utils.py` — primitives

The smallest file but the most widely-used across KG0 and ALIGN:

- **`sha_id(namespace, *parts) → str`** — 12-char hex deterministic
  ID. Every node in KG0 uses this.
- **`to_label(text) → str`** — PascalCase, collision-proof against
  `INFRA_LABELS = {"Document", "Collection", "Abbreviation"}`. Used
  for every dynamic entity label.
- **`resolve_labels(top_category, specific_category) → list[str]`** —
  returns the ordered `[primary, secondary?]` labels Neo4j should use
  on an entity node. Returns `[]` (which causes the row to be
  skipped) when neither category is usable.
- **`LABEL_TO_REL: dict[str, str]`** — the Page→Entity rel type map.
- **`DEFAULT_REL = "HAS_CLAIM"`** — fallback for unknown labels.
- **`slugify_rel(text) → str`** — makes entity↔entity relationship
  names Neo4j-safe by upper-casing and replacing punctuation.

This module is imported by both `kg0_from_db.py` and
`pipeline/align/infrastructure/contract_check.py` so that the two agree on the
shape of KG0 without a shared config file.

### 7.2 `rule_engine.py` — pre-KG rules

**Auto-generated** from `pre_kg_config.yaml` (the file header says
so). DO NOT hand-edit; edit the YAML and regenerate.

Runs over **raw extraction records** (the upstream
document-segmentation output), not over the graph. Exports:

```python
apply_all_rules(rec, rid, file_type) -> rec
VIOLATIONS    # list of {record, rule, message}
WARNINGS      # list of {record, rule, message}
STATS         # defaultdict(int) of rule-hit counts
```

Five rule groups run in order:

1. `apply_structural_integrity` — A1..A5. Every record has `id`,
   `output` dict, type-specific mandatory fields (EMAIL.hasPart,
   PPT/XLS/TXT.hasContent, …). Missing `id` gets auto-generated via
   `_stable_id`; missing `output` becomes `{}` with a violation.
2. `apply_format_normalization` — emails lower-cased via
   `_clean_email`; URLs require `http(s)://` prefix via `_clean_url`;
   dates parsed into `YYYY-MM-DD` via `_norm_date`.
3. `apply_uid_assignment` — ensures every node-like sub-record has a
   stable `uid`, computed via `_stable_id(type, *discriminators)`.
4. `apply_graph_semantics` — enforces that relationship endpoints
   exist and that the semantic category is one of the legal values.
5. `apply_dedup_normalization` — drops exact duplicates within a
   record (e.g. the same hasPart twice).

`apply_txt_rules` runs only for `file_type == "TXT"` and handles
plain-text-specific cleanups. `apply_all_rules` is the entry point
that upstream ingestion calls per record.

Because the file is generated, adding a new rule means:
1. Add it to `pre_kg_config.yaml`.
2. Regenerate `rule_engine.py` (the generator lives outside this
   package).
3. Run the pre-KG test suite.

### 7.3 `post_kg_rules.py` — post-KG checks and enrichments

Runs **after** `kg0_from_db.py` against the live Memgraph/Neo4j.
Three responsibilities:

1. **Structural checks.** Functions prefixed `check_*`. Examples:
   `check_node_counts`, `check_orphan_collections`,
   `check_orphan_documents`, `check_orphan_abbreviations`,
   `check_orphan_entities_by_label`, `check_high_degree_nodes`,
   `check_duplicate_entity_names`, `check_entities_missing_name`,
   `check_entities_missing_witness`,
   `check_documents_missing_industry`, `check_documents_missing_case`,
   `check_documents_missing_bates`, `check_relationship_counts`. Each
   check logs PASS / WARN / FAIL into `REPORT["checks"]`.

2. **Candidate detection.** `detect_entity_candidates` produces a
   list of `(entity_a, entity_b)` pairs that look like duplicates —
   same primary label, similar name, overlapping witnesses — and
   writes them to `candidates.json`. `resolve_operator.py` consumes
   that file.

3. **Enrichments.** Functions prefixed `enrich_*` add derived edges
   or tags to the graph:
   - `enrich_coauthor_persons` — `(:Person)-[:COAUTHORED_WITH]->
     (:Person)` when two people appear on the same document.
   - `enrich_org_in_same_doc` — `(:Organization)-[:CO_MENTIONED_IN_DOC]->
     (:Organization)` for org co-occurrence.
   - `enrich_drug_co_mentioned`,
     `enrich_person_associated_drug`,
     `enrich_person_speaks_topic`,
     `enrich_person_associated_gpe`,
     `enrich_gpe_co_occurrence` — cross-entity co-occurrence edges.
   - `enrich_same_industry`, `enrich_same_case` — link documents
     sharing metadata.
   - `enrich_country_label`, `enrich_state_label` — promote GPE
     mentions into `:Country` / `:State` secondary labels.

The `KGConn` class at the top is a thin wrapper over the neo4j
driver (`query`, `run`, `scalar`). `REPORT` is a module-level dict
accumulated across all checks and dumped to JSON at the end of
`main()`.

### 7.4 `resolve_operator.py` — LLM duplicate adjudicator

Consumes `candidates.json` from `post_kg_rules.detect_entity_candidates`
and decides whether each pair should be merged. Key functions:

- **`resolve_pair_llm(conn, a, b, model) -> {"merge": bool, "reason": str}`** —
  pulls context about both entities (labels, names, witnesses,
  Wikipedia categories), sends it to an LLM, parses the verdict.
- **`merge_pair(conn, a, b, keep="a")`** — merges two entity nodes
  using APOC when available (`_merge_nodes_apoc`) and falls back to
  manual Cypher (`_merge_nodes_cypher`) when it isn't. Preserves all
  incoming/outgoing edges on the kept node.
- **`run_resolution(conn, candidates, model)`** — iterates the
  candidate list, calls the LLM, and executes the merges. Logs a
  summary of merged / kept / skipped counts.
- **`_get_degree(conn, kg_id)`** — helper to pick the "keep" side
  when both sides look equally good — prefer the node with the
  higher combined degree.

The module's `main()` wires everything into a CLI so duplicate
resolution can run as a scheduled job after every rebuild.

### 7.5 `export_kg0_json.py` — subgraph exporter

Exports KG0 subgraphs to JSON for offline analysis and fixtures.
Takes a Cypher pattern or a document-level selector and writes a
`{nodes, edges}` JSON blob consumable by downstream tooling. Used
primarily to build the test fixtures under `tests/fixtures/kg0/`.

### 7.6 `external_libs.py` — third-party adapters

Wraps the third-party libraries KG0 depends on (currently the
fuzzy-matching utilities and the Unicode normalizer) so the
`kg0_from_db.py` file stays free of import gymnastics. When adding a
new dependency, isolate the import here.

---

## 8. Determinism and idempotency

KG0 is deterministic given:

1. **Fixed Postgres snapshot** — no new rows during the build.
2. **Fixed `pre_kg_config.yaml`** — same rule engine output.
3. **Fixed `{docs_dir}/{doc_id}/labels.jsonl`** — same page
   labels.

Every node `kg_id` is derived via `sha_id(namespace, *canonicalized
inputs)`, so re-running the builder against the same inputs produces
the same graph byte-for-byte. Edges MERGE on `(from_kg_id, to_kg_id,
rel_type)`, which means re-runs are upserts: property values get
overwritten on repeat writes, so a later richer row (e.g. after a
rule-engine update) can freshen the graph without wiping it.

**When to `--wipe` instead of just re-running**: if you change a
`sha_id` namespace or the canonicalization logic, the old nodes
become orphans of a different ID space. Wipe first, then rebuild.

---

## 9. Configuration

KG0 has no dedicated config file. All tunables are either CLI flags
on `kg0_from_db.py` or constants at the top of the module:

| Knob | Location | What it does |
|---|---|---|
| `NODE_TABLE` | `kg0_from_db.py:~30` | Default Postgres node table |
| `EDGE_TABLE` | `kg0_from_db.py:~31` | Default Postgres edge table |
| `CATALOG_TABLE` | `kg0_from_db.py:~32` | Default catalog table |
| `UNKNOWN_PAGE_LABEL` | `kg0_from_db.py:~33` | Fallback label for pages with no labeler output |
| `UNKNOWN_PAGE_INDEX` | `kg0_from_db.py:~34` | Page index used when a mention has no page_number |
| `DEFAULT_DOCS_DIR` | `kg0_from_db.py:~35` | Default directory for labels.jsonl files |
| `INFRA_LABELS` | `kg0_utils.py` | Labels that are reserved and excluded from dynamic path |
| `LABEL_TO_REL` | `kg0_utils.py` | Page→Entity rel type per entity label |
| `DEFAULT_REL` | `kg0_utils.py` | Fallback rel type (`HAS_CLAIM`) |
| `--batch-size` | CLI | Rows per UNWIND batch |
| `--dry-run` | CLI | Load from PG, preview label dist, skip writes |
| `--wipe` | CLI | `MATCH (n) DETACH DELETE n` before building |
| `--docs-dir` | CLI | Override `DEFAULT_DOCS_DIR` |

---

## 10. How to run

### 10.1 First build

```bash
# Prerequisites: Postgres populated, Neo4j running, labeler outputs
# present under /data/docs/{doc_id}/labels.jsonl

python -m pipeline.kg0.kg0_from_db \
  --neo4j-uri bolt://127.0.0.1:7687 \
  --neo4j-user neo4j \
  --neo4j-password secret \
  --node-table opioid_raw_nodes \
  --edge-table opioid_raw_edges \
  --docs-dir /data/docs \
  --batch-size 500 \
  --wipe

# Then run the post-build checks + enrichments
python -m pipeline.kg0.post_kg_rules \
  --neo4j-uri bolt://127.0.0.1:7687 \
  --neo4j-user neo4j \
  --neo4j-password secret

# Finally adjudicate candidates
python -m pipeline.kg0.resolve_operator \
  --candidates candidates.json \
  --neo4j-uri bolt://127.0.0.1:7687 \
  --neo4j-user neo4j \
  --neo4j-password secret \
  --model claude-3-5-sonnet
```

### 10.2 Incremental rebuild

Same command **without** `--wipe`. The MERGE statements handle
upserts, so new entities/edges get added and existing ones get their
properties refreshed from the latest Postgres rows.

### 10.3 Previewing a change

Use `--dry-run` to see how many rows each table contributes and what
the label distribution will look like, without touching Neo4j:

```bash
python -m pipeline.kg0.kg0_from_db --dry-run

# ...
# Loaded 38 collections, 2,450 documents, 128,000 entities, 410,000 edges from PG
# Label distribution preview:
#   Person                                   48,210
#   Organization                             19,330
#   Drug:Opioid                              12,145
#   ...
# Dry run complete.
```

---

## 11. Editing gotchas

1. **Never hand-edit `rule_engine.py`.** It is generated from
   `pre_kg_config.yaml`. Hand-edits are silently overwritten the next
   time someone runs the generator.

2. **Changing a `sha_id` namespace is a schema break.** Every
   downstream operator assumes `sha_id("doc_strong", doc_id)` is the
   document's primary key. Renaming the namespace orphans every
   existing node and every downstream reference.

3. **`INFRA_LABELS` is load-bearing.** Removing `Document` from the
   reserved set means a Postgres row with `top_category = "document"`
   will collide with the infra `:Document` label and corrupt the
   graph on next MERGE.

4. **`LABEL_TO_REL` must stay in sync with ALIGN's Phase 1 Cypher
   retrieval.** Phase 1 scores Documents by Page→Entity edges and
   does not enumerate relationship types — it uses `-[m]->`.
   However, `post_kg_rules` and `resolve_operator` both reference
   specific rel types from `DOC_TO_ENTITY_RELS` in `post_kg_rules.py`,
   so if you add a new entry to `LABEL_TO_REL`, add it there too.

5. **The `HAS_PAGE` shape is assumed everywhere.** KG0 stores
   `(Document)-[:HAS_PAGE]->(Page)` with `page_index` as a Page
   property — no page-to-page edges. ALIGN, TRACE, and every enricher
   assumes this shape. Adding `NEXT_PAGE` / `PREVIOUS_PAGE` edges
   would require a coordinated change across all consumers.

6. **`UNKNOWN_PAGE_INDEX` is a valid page index.** It exists so
   entity rows with NULL `page_number` still get attached to
   *something*. Do not filter it out at read time without
   understanding the consequences — you will drop a meaningful
   fraction of mentions.

7. **`create_pages` ensures every Document has at least one Page.**
   The invariant "every Document has `HAS_PAGE` children" is enforced
   here and checked in `post_kg_rules.check_orphan_documents`. If you
   skip the "ensure at least one" block, the check will fail on every
   document that had no labels and no entity rows.

8. **`create_entities_and_page_links` writes witnesses on the edge,
   not the node.** The canonical entity node is cross-page; per-page
   witnesses belong on the `MENTIONS_*` edge. Moving the witness
   property up to the node collapses all evidence into a single
   string and breaks TRACE's evidence-chain walk.

9. **`EndpointResolver.resolve` is fuzzy on purpose.** The `token_sub`
   tier will return a match when one side's tokens are a strict
   subset of the other's, but only if the smaller side carries a
   "substantive" signal — otherwise "Inc." would match every
   corporation. Do not tighten the substantive-signal check without
   reviewing the tier-miss counts.

10. **`post_kg_rules.enrich_*` functions can be rerun safely but
    will accumulate edges.** They MERGE on endpoint pairs, so
    idempotent — but if you change a rule's predicate, remember to
    delete the old derived edges first. There is no auto-expiration.

---

## 12. Where to look next

- **`pipeline/align/CODE_WALKTHROUGH.md`** — the first consumer of
  KG0. Reading this together with KG0's walkthrough shows the full
  round-trip from raw PG rows to ALIGN witnesses.
- **`pipeline/kg0/kg0_from_db.py:1–60`** — the file header
  documents every constant and every namespace. Start here when
  debugging an unexpected `kg_id`.
- **`pipeline/kg0/post_kg_rules.py:14–30`** — the schema-as-comment
  block that describes what KG0 looks like after the build. The
  canonical "what does KG0 look like?" reference.
- **`pre_kg_config.yaml`** — the rule engine's source of truth.
- **`labeler/CODE_WALKTHROUGH.md`** — how `{doc_id}/labels.jsonl`
  gets produced. KG0's `create_pages` step reads those files
  directly.
- **`pipeline/trace/EG-RG schemas.md`** — the downstream schema TRACE
  writes into. Reading this clarifies which KG0 properties TRACE
  surfaces and which it ignores.

When a KG0 invariant fails, the recovery path is:
1. Re-run `kg0_from_db.py --dry-run` to check the raw PG input.
2. Re-run `kg0_from_db.py --wipe` for a clean build.
3. Run `post_kg_rules.py` and read the failing `check_*` — it
   always logs the Cypher query that found the problem.
4. If the failure is a fuzzy-match miss, look at the tier counts in
   the builder log first, then `post_kg_rules.detect_entity_candidates`
   second.
