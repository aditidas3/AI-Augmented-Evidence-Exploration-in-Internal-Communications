# CONFLICT Operator

**Pipeline position:** `ALIGN → TRACE → CONFLICT → CONSTRUCT → EXPLAIN`

CONFLICT is the third operator in the evidence exploration pipeline. It reads witness nodes from the Evidence Graph produced by TRACE, compares them pairwise using five typed rules, and detects contradictions between documents. When contradictions are found it writes CONTRADICTS edges, Defeater nodes, and updates Claim status so CONSTRUCT can apply appropriate confidence penalties.

---

## Table of Contents

- [Overview](#overview)
- [How to Run](#how-to-run)
- [What Changed in This Version](#what-changed-in-this-version)
- [Five Detection Rules](#five-detection-rules)
- [Configuration — All Parameters](#configuration--all-parameters)
- [Corpus Profiles — Running on New Datasets](#corpus-profiles--running-on-new-datasets)
- [Scalability Features](#scalability-features)
- [Known Limitations](#known-limitations)
- [Example Output](#example-output)
- [Next Step](#next-step)

---

## Overview

CONFLICT asks one question: **do any two witnesses in the same slot say incompatible things?**

If yes it writes:
- A `CONTRADICTS` edge between the two witnesses in the Evidence Graph
- A `Defeater` node in the Reasoning Graph
- Updates the Claim status to `contested` for rebutting conflicts

CONSTRUCT then reads these Defeaters and applies confidence penalties — reducing the reliability of any reasoning that depends on a contested answer.

---

## How to Run

There are three ways to run CONFLICT depending on your setup.

---

### Option 1 — Standard in-memory run (development and testing)

**Files needed in the same folder:**

```
trace_bundle.json
conflict.py
run_conflict_on_trace.py
```

**Run:**

```bash
python run_conflict_on_trace.py
```

**Output produced:**

```
conflict_bundle.json   <- ready for CONSTRUCT
```

---

### Option 2 — Neo4j write-back (production)

Writes CONTRADICTS edges and Defeater nodes directly to the Neo4j
database in addition to saving conflict_bundle.json.

**Files needed in the same folder:**

```
trace_bundle.json
conflict.py
neo4j_writer.py
run_conflict_neo4j.py
```

**Step 1 — Install the Neo4j package:**

```bash
pip install neo4j
```

**Step 2 — Set your connection details:**

```bash
export NEO4J_URI=bolt://localhost:7687
export NEO4J_USER=neo4j
export NEO4J_PASSWORD=yourpassword
export NEO4J_DATABASE=neo4j
```

Or edit the defaults at the top of run_conflict_neo4j.py directly.

**Step 3 — Run:**

```bash
python run_conflict_neo4j.py
```

**What gets written to Neo4j:**

```
EG  ->  CONTRADICTS edges between conflicting witnesses
EG  ->  Symmetric CONTRADICTS back-edges
RG  ->  Defeater nodes (type, description, cluster_size)
RG  ->  HAS_DEFEATER edges from Inferences to Defeaters
RG  ->  REFERENCES_CLAIM edges from Defeaters to Claims
RG  ->  Claim.status updated to "contested" for rebutting conflicts
```

**What if Neo4j is not running:**

The script falls back to in-memory automatically — no crash:

```
Connecting to Neo4j at bolt://localhost:7687...
  Could not connect: [reason]
  Falling back to in-memory mode
```

The pipeline continues and still saves conflict_bundle.json for
CONSTRUCT. Neo4j write-back is skipped but everything else works.

**Example terminal output:**

```
==============================================================
  CONFLICT OPERATOR — Neo4j Write-Back Mode
==============================================================

Loading trace_bundle.json...
  eg_root_uid : 2f46e22b-...
  rg_root_uid : d4884aab-...

Connecting to Neo4j at bolt://localhost:7687...
  Connected
  Writer: Neo4jGraphWriter (batch_size=100)

Seeding graph writers...
  EG seeded: 3855 nodes, 10921 edges
  RG seeded: 29 nodes, 63 edges

Running CONFLICT...

Results:
  Backend          : Neo4j
  Witnesses        : 30
  Conflicts found  : 3
  Defeaters created: 3
  Claims contested : 1

  Neo4j writes:
    CONTRADICTS edges -> written to EG in Neo4j
    Defeater nodes    -> written to RG in Neo4j
    Claim status      -> updated in Neo4j

Saved to conflict_bundle.json
Neo4j connection closed.
```

---

### Option 3 — Save output for CONSTRUCT only

Runs CONFLICT in memory and saves the output in the format CONSTRUCT
expects. Use this when you do not need Neo4j write-back but want a
clean conflict_bundle.json.

```bash
python save_conflict_output.py
```

---

### Which script to use

| Scenario | Script |
|---|---|
| Development and testing | `run_conflict_on_trace.py` |
| Production with Neo4j backend | `run_conflict_neo4j.py` |
| Save output for CONSTRUCT only | `save_conflict_output.py` |

---

---

## What Changed in This Version

### Previously hardcoded — now configurable

Every value that was hardcoded for the Walgreens corpus is now a parameter in `ConflictConfig`. This means CONFLICT can be tuned for any new IDL collection without changing the code — only the config changes.

| Was hardcoded | Now configurable via |
|---|---|
| Token overlap threshold 0.60 (Rule 1) | `tau_surface_overlap` |
| Token overlap threshold 0.50 (Rule 4) | `tau_cross_artifact_overlap` |
| Token overlap threshold 0.30 (Rule 3 negation) | `tau_negation_window_overlap` |
| Document ID pattern (lowercase alphanumeric 4-12 chars) | `doc_id_pattern` |
| Reliability gap threshold 0.40 (Rule 5) | `tau_reliability_gap` |
| Temporal window 6 months (Rule 2) | `tau_temporal_months` |
| Pair cap 200 | `max_pairs_per_slot` |
| spaCy model en_core_web_sm | `spacy_model` |

### New features added

| Feature | Config parameter | Default |
|---|---|---|
| Cross-run deduplication | `enable_cross_run_dedup` | `True` |
| Stratified pair sampling | `use_stratified_sampling` | `True` |
| Corpus profile name for logging | `corpus_profile` | `"default"` |
| Configurable regex doc ID pattern | `doc_id_pattern` | `r'^[a-z0-9]{4,12}$'` |
| Config values logged at startup | automatic | always on |

---

## Five Detection Rules

### Rule 1 — Surface Mismatch

**Type:** Rebutting | **Groups by:** slot + var_name

Fires when two witnesses answer the same slot with the same graph variable but give incompatible text answers.

**Configurable thresholds:**
- `tau_surface_overlap` (default 0.60) — pairs with overlap above this are treated as paraphrases not conflicts
- `doc_id_pattern` — regex to detect corpus reference IDs and skip them
- `tau_doc_id_max_length` (default 12) — max length for doc ID detection

**Example:**
```
Witness A: "California Code of Civil Procedure section 664.6"
Witness B: "California Civil Code Section 1542"
Token overlap = 0.15 < 0.60  ->  SURFACE_MISMATCH fires
Rebutting Defeater  ->  WHAT Claim contested
```

---

### Rule 2 — Temporal Clash

**Type:** Rebutting | **Groups by:** slot

Fires when two witnesses in the same slot contain dates more than `tau_temporal_months` apart.

**Configurable thresholds:**
- `tau_temporal_months` (default 6) — set higher for older collections

```
Walgreens (2000s):    tau_temporal_months = 6    (default)
Tobacco (1950-1990):  tau_temporal_months = 24   (recommended)
```

---

### Rule 3 — Negation Conflict

**Type:** Rebutting | **Groups by:** slot

Three-layer detection system:
1. **Regex** — fast keyword scan (never, denied, failed to, no evidence)
2. **spaCy dep=neg** — long-range clausal negation via parse tree
3. **Custom quantifier** — None, Neither, Nobody as grammatical subjects

**Configurable thresholds:**
- `tau_negation_window_overlap` (default 0.30) — overlap required for negation match
- `spacy_model` (default `en_core_web_sm`) — swap for domain-specific model
- `negation_backend` — `"auto"`, `"spacy"`, or `"regex"`

---

### Rule 4 — Cross-Artifact Entity

**Type:** Rebutting | **Groups by:** kg0_entity_id

Fires when two witnesses from different documents share the same KG0 entity ID but describe that entity incompatibly.

**Configurable thresholds:**
- `tau_cross_artifact_overlap` (default 0.50) — lower than Rule 1 because wording legitimately varies across documents

---

### Rule 5 — Reliability Diverge

**Type:** Undercutting | **Groups by:** slot

Fires when two witnesses in the same slot have a reliability score gap above `tau_reliability_gap`. Does not set Claim status to contested — reduces Inference confidence via CONSTRUCT.

**Configurable thresholds:**
- `tau_reliability_gap` (default 0.40) — calibrate per corpus score distribution

---

## Configuration — All Parameters

```python
from conflict import ConflictConfig

cfg = ConflictConfig(

    # Rule on/off switches
    enable_rule1_surface_mismatch    = True,
    enable_rule2_temporal_clash      = True,
    enable_rule3_negation            = True,
    enable_rule4_cross_artifact      = True,
    enable_rule5_reliability_diverge = True,

    # Negation backend (Rule 3)
    negation_backend = "auto",           # "auto" | "spacy" | "regex"
    spacy_model      = "en_core_web_sm", # swap for domain-specific model

    # Numeric thresholds
    tau_min_witness_reliability  = 0.05, # drop witnesses below this
    tau_reliability_gap          = 0.40, # Rule 5 gap threshold
    tau_min_surface_length       = 3,    # minimum surface length
    tau_temporal_months          = 6,    # Rule 2 date gap threshold
    tau_surface_overlap          = 0.60, # Rule 1 overlap threshold      NEW
    tau_cross_artifact_overlap   = 0.50, # Rule 4 overlap threshold      NEW
    tau_negation_window_overlap  = 0.30, # Rule 3 negation overlap       NEW
    max_pairs_per_slot           = 200,  # pair cap

    # Document ID guard
    skip_document_id_surfaces    = True,
    tau_doc_id_max_length        = 12,
    doc_id_pattern               = r'^[a-z0-9]{4,12}$',  # NEW configurable

    # Scalability
    enable_cross_run_dedup       = True, # NEW idempotent runs
    use_stratified_sampling      = True, # NEW smarter pair selection

    # Logging
    corpus_profile               = "walgreens", # NEW for traceability

    # Behaviour
    update_claim_status          = True,
    write_symmetric_contradicts  = True,
)
```

---

## Corpus Profiles — Running on New Datasets

When applying CONFLICT to a new IDL collection create a corpus-specific config. No code changes needed — only the config changes.

### Walgreens Opioids (current default)

```python
cfg = ConflictConfig(
    corpus_profile         = "walgreens",
    tau_temporal_months    = 6,
    tau_reliability_gap    = 0.40,
    tau_surface_overlap    = 0.60,
    doc_id_pattern         = r'^[a-z0-9]{4,12}$',
    max_pairs_per_slot     = 200,
    spacy_model            = "en_core_web_sm",
)
```

### Tobacco Collection (recommended settings)

```python
cfg = ConflictConfig(
    corpus_profile         = "tobacco",
    tau_temporal_months    = 24,  # documents span 50 years
    tau_reliability_gap    = 0.35,
    tau_surface_overlap    = 0.55, # older docs use more varied phrasing
    doc_id_pattern         = r'^[a-z0-9]{4,20}$', # longer IDs possible
    max_pairs_per_slot     = 500,  # larger corpus
    spacy_model            = "en_core_web_lg", # better recall on formal language
)
```

### Chemicals or Drugs Collection (starting point)

```python
cfg = ConflictConfig(
    corpus_profile         = "chemicals",
    tau_temporal_months    = 12,
    tau_reliability_gap    = 0.40,
    tau_surface_overlap    = 0.60,
    doc_id_pattern         = r'^[a-z0-9]{4,16}$',
    max_pairs_per_slot     = 300,
)
```

### How to calibrate thresholds for a new collection

Before running on a full new collection run on a small sample of 50-100 documents first and follow these steps:

**Step 1 — Check reliability score distribution**

```python
import statistics
scores = [w.reliability_score for w in witnesses]
print(f"Mean={statistics.mean(scores):.3f}  Stdev={statistics.stdev(scores):.3f}")
print(f"Min={min(scores):.3f}  Max={max(scores):.3f}")
# If stdev < 0.10 lower tau_reliability_gap
# If stdev > 0.30 keep default 0.40
```

**Step 2 — Check document ID format**

Look at the surface text of var=D witnesses in the TRACE output. If IDs do not match the default pattern update `doc_id_pattern` accordingly.

**Step 3 — Check date range**

If the collection spans more than 5 years increase `tau_temporal_months` proportionally.

**Step 4 — Inspect all conflicts manually**

Every conflict flagged on the sample should be explainable. If you see false positives adjust the relevant threshold. If you see missed conflicts lower the threshold.

---

## Scalability Features

### Cross-run deduplication

When `enable_cross_run_dedup=True` CONFLICT loads existing CONTRADICTS edges at the start and skips any pair already detected in a previous run. This makes CONFLICT safe to re-run on the same data without creating duplicate Defeater nodes.

### Stratified pair sampling

When `use_stratified_sampling=True` and a slot group exceeds `max_pairs_per_slot`, instead of silently dropping excess pairs CONFLICT uses stratified sampling. High-reliability witnesses are always compared and low-reliability ones are sampled proportionally. A WARNING is logged when the cap is reached.

### Neo4j write-back

Use `run_conflict_neo4j.py` with `Neo4jGraphWriter` to write directly to Neo4j instead of keeping everything in memory. Required for large corpora.

```bash
export NEO4J_URI=bolt://localhost:7687
export NEO4J_USER=neo4j
export NEO4J_PASSWORD=yourpassword
python run_conflict_neo4j.py
```

---

## Known Limitations

**No ClusterConflicts**
Related conflicts are emitted as separate records. Three conflicts about the same legal question produce three Defeaters instead of one cluster. Planned for a future iteration.

**No EvalSupersession**
Rule 2 detects temporal gaps but does not distinguish genuine contradictions from temporal supersession. Important for collections spanning many decades such as Tobacco.

**No ExpandConflictScope**
Conflict scope is limited to direct witnesses from TRACE chains. Contextually related objects in KG0 not in the same chain are not compared.

**Thresholds require calibration per corpus**
All thresholds default to values calibrated on the Walgreens sample. See Corpus Profiles above for guidance on new collections.

---

## Example Output

```
==============================================================
  CONFLICT OPERATOR
==============================================================

Loading trace_bundle.json...
  Config: profile=walgreens | surface_overlap=0.6 | reliability_gap=0.4
          temporal_months=6 | max_pairs=200 | stratified=True | dedup=True

Conflicts found   : 3
Defeaters created : 3 (all rebutting)
Claims contested  : 1 (WHAT slot)

Rule 1 Surface Mismatch   : 3
Rule 2 Temporal Clash     : 0
Rule 3 Negation Conflict  : 0
Rule 4 Cross Artifact     : 0
Rule 5 Reliability Diverge: 0

Conflicts:
  [1] SURFACE_MISMATCH (rebutting)
       California Code of Civil Procedure section 664.6
       vs FULL AND COMPLETE CONFIDENTIAL SETTLEMENT AGREEMENT

  [2] SURFACE_MISMATCH (rebutting)
       California Code of Civil Procedure section 664.6
       vs California Civil Code Section 1542

  [3] SURFACE_MISMATCH (rebutting)
       FULL AND COMPLETE CONFIDENTIAL SETTLEMENT AGREEMENT
       vs California Civil Code Section 1542

Saved to conflict_bundle.json -- ready for CONSTRUCT
```

---

## Next Step

The output of CONFLICT — `conflict_bundle.json` — is the input to **CONSTRUCT**.

CONSTRUCT reads the Defeater nodes and applies confidence penalties:
- Rebutting Defeater multiplies confidence by 0.60
- Undercutting Defeater multiplies confidence by 0.80

See [CONSTRUCT_README.md](./CONSTRUCT_README.md) for full details.

---

## Algorithm 6 Improvements Implemented

Three steps from Algorithm 6 (professor's paper) have been implemented in this version.

---

### ClusterConflicts (Algorithm 6 Line 22)

**What it does:**
Groups raw conflict pairs that share the same contested Claim UID into clusters before writing Defeater nodes. Each cluster produces one Defeater with a `cluster_size` field instead of one Defeater per raw pair.

**Why it matters:**
Without clustering, three conflicts about the same legal question produce three Defeaters. CONSTRUCT then applies three sequential penalty multiplications — 0.9427 × 0.60 × 0.60 × 0.60 = 0.20 — which over-penalises confidence. With clustering, one Defeater with `cluster_size=3` represents all three, and CONSTRUCT applies one proportional penalty.

**Result on Walgreens sample:**
```
Before ClusterConflicts:  3 Defeater nodes  (3 separate penalties in CONSTRUCT)
After  ClusterConflicts:  1 Defeater node   (cluster_size=3, one weighted penalty)
```

**Config:**
```python
cfg = ConflictConfig(
    enable_cluster_conflicts = True,   # default True
)
```

**Defeater node written to RG:**
```json
{
  "labels": ["Defeater"],
  "properties": {
    "type":         "rebutting",
    "cluster_size": 3,
    "description":  "[CLUSTER size=3] Slot WHAT: California Code of Civil Procedure... | ..."
  }
}
```

---

### EvalSupersession (Algorithm 6 Line 14)

**What it does:**
When Rule 2 detects a temporal clash, checks whether the later document explicitly supersedes the earlier one. If supersession language is found — "supersedes", "replaces", "amends", "revokes" — the pair is classified as `TEMPORAL_SUPERSESSION` (undercutting) instead of `TEMPORAL_CLASH` (rebutting).

**Why it matters:**
A 2003 document and a 2008 document saying different things are not necessarily a contradiction — the 2008 document may simply replace the 2003 one. Without EvalSupersession both are flagged as REFUTES and the Claim is set to contested, which misleads investigators. With EvalSupersession the pair is correctly classified as resolved-by-time.

**Especially important for:**
- Tobacco collection — documents span 50 years of policy evolution
- Any collection where company policies changed over time

**Config:**
```python
cfg = ConflictConfig(
    enable_eval_supersession = True,    # default True
    supersession_keywords    = (        # extend for domain-specific language
        "supersedes", "replaces", "amends", "revokes",
        "effective from", "cancels", "in lieu of",
    ),
)
```

**Comparison:**
```
Without EvalSupersession:
  2003 doc vs 2008 doc  ->  TEMPORAL_CLASH (rebutting)
  Claim set to contested  ->  heavy confidence penalty

With EvalSupersession:
  2003 doc vs 2008 doc  ->  TEMPORAL_SUPERSESSION (undercutting)
  Claim not contested   ->  light confidence penalty
  Investigator informed: "2008 document supersedes 2003 via 'replaces'"
```

---

### ExpandConflictScope (Algorithm 6 Lines 1-3) — Stub

**What it does:**
Expands the witness set beyond direct TRACE chains by querying KG0 for entities related to seed witnesses up to `max_scope_hops` hops.

**Current status:**
Implemented as a stub — the hook is in place and the config parameter is available, but KG0 must be passed to CONFLICT at runtime to activate. When enabled CONFLICT logs a message explaining what it would do.

**Why it improves recall:**
Two witnesses in different document chains may not be directly compared if they are not in the same slot group. ExpandConflictScope would find them by following KG0 entity relationships.

**Config:**
```python
cfg = ConflictConfig(
    enable_expand_scope = False,  # default False — requires KG0 access
    max_scope_hops      = 2,
)
```

**To activate in a future iteration:**
Pass a `kg0_writer` to `Conflict()` and set `enable_expand_scope=True`. The `_expand_conflict_scope()` method will then query KG0 and add contextually related witnesses to the comparison pool.
