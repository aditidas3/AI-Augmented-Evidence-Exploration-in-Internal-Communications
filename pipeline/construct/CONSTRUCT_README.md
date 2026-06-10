# CONSTRUCT Operator

**Pipeline position:** `ALIGN → TRACE → CONFLICT → CONSTRUCT → EXPLAIN`

CONSTRUCT is the fourth operator in the evidence exploration pipeline. It reads the output of CONFLICT and builds a formal argument structure — applying confidence penalties for contradictions, confirming evidence support for each slot, and synthesising all slot answers into one top-level Claim that EXPLAIN reads to generate a natural language answer.

---

## Table of Contents

- [Overview](#overview)
- [How to Run](#how-to-run)
- [Input](#input)
- [Output](#output)
- [Architecture](#architecture)
- [Seven Rules](#seven-rules)
  - [Rule 1 — Slot Support](#rule-1--slot-support)
  - [Rule 2 — Cross-Slot Reasoning](#rule-2--cross-slot-reasoning)
  - [Rule 3 — Defeater Weakening](#rule-3--defeater-weakening)
  - [Rule 4 — Contested Premise Discount](#rule-4--contested-premise-discount)
  - [Rule 5 — Composite Synthesis](#rule-5--composite-synthesis)
  - [Rule 6 — Incomplete Answer](#rule-6--incomplete-answer)
  - [Rule 7 — Corroboration Boost](#rule-7--corroboration-boost)
- [Algorithm 4 Components](#algorithm-4-components)
  - [SelectTopChain](#selecttopchain)
  - [WitnessTether and AllSentencesTethered](#witnesstether-and-allsentencestethered)
  - [DeriveTimeline](#derivetimeline)
  - [DeriveExhibits](#deriveexhibits)
  - [DeriveLimitations](#derivelimitations)
- [Configuration](#configuration)
- [Data Structures](#data-structures)
- [Graph Writes](#graph-writes)
- [Design Decisions](#design-decisions)
- [Example Output](#example-output)
- [Next Step](#next-step)

---

## Overview

By the time CONSTRUCT runs, the Reasoning Graph (RG) already has:

- **5 Claims** — one per slot (WHO, WHAT, HOW, EVIDENCE, WHEN) written by TRACE
- **5 Inferences** — slot-to-slot reasoning links written by TRACE
- **Defeaters** — contradiction nodes written by CONFLICT
- **CONTRADICTS edges** — written by CONFLICT on the Evidence Graph

The problem is that Inferences still show high confidence even when CONFLICT found contradictions. There is also no single node that brings all five slot answers together for EXPLAIN to read.

CONSTRUCT solves both problems:

1. Applies contradiction penalties — reduces Inference confidence based on Defeater type and cluster size
2. Synthesises a final answer — creates one Synthesised Claim and Inference that EXPLAIN reads
3. Builds structured answer components — CiteMap, Timeline, Exhibits, Limitations

---

## How to Run

### Option 1 — Standard in-memory run

```bash
python construct.py
```

**Files needed:**
```
conflict_bundle.json    <- from CONFLICT
construct.py
```

**Output produced:**
```
construct_bundle.json   <- ready for EXPLAIN
```

---

### Option 2 — Neo4j write-back

Writes all new nodes and edges directly to the Neo4j database in addition to saving construct_bundle.json.

**Files needed:**
```
conflict_bundle.json     <- copy from CONFLICT folder after running CONFLICT
construct.py
neo4j_writer.py
run_construct_neo4j.py
```

**Step 1 — Run CONFLICT first and copy the output:**
```bash
cd CONFLICT
python run_conflict_neo4j.py

cp conflict_bundle.json ../CONSTRUCT/conflict_bundle.json
cd ../CONSTRUCT
```

**Step 2 — Install Neo4j package:**
```bash
pip install neo4j
```

**Step 3 — Set connection details:**
```bash
export NEO4J_URI=bolt://localhost:7687
export NEO4J_USER=neo4j
export NEO4J_PASSWORD=yourpassword
export NEO4J_DATABASE=neo4j
```

**Step 4 — Run:**
```bash
python run_construct_neo4j.py
```

**What gets written to Neo4j:**
```
RG  <-  12 new Inference nodes  (Rule 1 + Rule 2)
RG  <-  1 Synthesised Claim     (Rule 5)
RG  <-  1 synthesis Inference   (Rule 5)
RG  <-  constructScore          (Rule 3 -- added to TRACE Inferences via SET)
RG  <-  55 new edges            (HAS_PREMISE, HAS_CONCLUSION, CONTAINS_INFERENCE)
```

**Fallback** -- if Neo4j is not running the script falls back to in-memory automatically with no crash.

---

### Which script to use

| Scenario | Script |
|---|---|
| Development and testing | `construct.py` |
| Production with Neo4j backend | `run_construct_neo4j.py` |

---

## Input

CONSTRUCT reads conflict_bundle.json -- the same four-section structure passed through the pipeline:

```json
{
  "trace_result":    { "eg_root_uid": "...", "rg_root_uid": "...", "stats": {} },
  "conflict_result": { "conflicts": [...], "defeater_uids": [...], "stats": {} },
  "eg":              { "nodes": [...], "edges": [...] },
  "rg":              { "nodes": [...], "edges": [...] }
}
```

**What CONSTRUCT reads from the RG:**

| Node or Edge | Used by |
|---|---|
| Claim nodes | All rules -- slot answers |
| Inference nodes with HAS_DEFEATER edges | Rule 3 -- penalty calculation |
| Defeater nodes with type and cluster_size | Rule 3 -- proportional penalty |
| GROUNDED_BY edges | Rule 1 and WitnessTether |
| HAS_PREMISE and HAS_CONCLUSION edges | Rule 4 |

---

## Output

### construct_bundle.json

The full pipeline bundle updated with CONSTRUCT results:

```json
{
  "trace_result":    { "..." },
  "conflict_result": { "..." },
  "eg":              { "..." },
  "rg": {
    "nodes": [ "...original..." , "...12 new CONSTRUCT nodes..." ],
    "edges": [ "...original...", "...55 new CONSTRUCT edges..." ]
  },
  "construct_result": {
    "new_nodes":            [...],
    "new_edges":            [...],
    "updated_inferences":   [...],
    "synthesis_uid":        "936e42c3-...",
    "synthesis_confidence": 0.8823,
    "synthesis_type":       "composite",
    "stats":                {},
    "cite_map":             { "WHAT": {}, "WHO": {}, "HOW": {}, "WHEN": {}, "EVIDENCE": {} },
    "timeline":             [...],
    "exhibits":             [...],
    "limitations":          [...],
    "all_tethered":         true
  }
}
```

---

## Architecture

CONSTRUCT is a **single-use stateful operator**. Instantiate it, call execute() once, then discard.

```
conflict_bundle.json
       |
       v
  Construct(data)
       |
       |-- _load_records()               <- parse Claims, Inferences, Defeaters
       |-- _select_top_chain()           <- Algorithm 4 Line 1
       |
       |-- _rule1_slot_support()         <- Rule 1
       |-- _rule2_cross_slot_reasoning() <- Rule 2
       |-- _rule3_defeater_weakening()   <- Rule 3 (cluster-aware)
       |-- _rule4_contested_premise()    <- Rule 4
       |-- _rule7_corroboration_boost()  <- Rule 7
       |-- _rule5_or_6_synthesis()       <- Rule 5 or 6
       |
       |-- _witness_tether()             <- Algorithm 4 Line 9
       |-- _all_sentences_tethered()     <- Algorithm 4 Line 10
       |-- _derive_timeline()            <- Algorithm 4 Line 5
       |-- _derive_exhibits()            <- Algorithm 4 Line 6
       |-- _derive_limitations()         <- Algorithm 4 Line 7
       |
       v
  ConstructResult
       |
       v
construct_bundle.json
```

All UIDs are generated deterministically via uuid5(CONSTRUCT_NS, seed) -- re-running on the same input produces the same graph every time.

---

## Seven Rules

Rules fire in this order: **1 -> 2 -> 3 -> 4 -> 7 -> 5/6**

Rule 3 fires before Rule 5 so the synthesis confidence uses post-penalty Claim confidences.

---

### Rule 1 -- Slot Support

**What it does:** For each Claim with at least one grounded witness via GROUNDED_BY, creates a direct support Inference confirming the evidence backs that Claim.

**Condition:** `len(claim.witness_uids) > 0`

**Example:**
```
WHAT Claim  (conf=0.925)  ->  slot_support_what     Inference (conf=0.925)
WHO Claim   (conf=0.85)   ->  slot_support_who      Inference (conf=0.85)
HOW Claim   (conf=0.80)   ->  slot_support_how      Inference (conf=0.80)
EVIDENCE    (conf=0.9604) ->  slot_support_evidence Inference (conf=0.9604)
WHEN Claim  (conf=0.772)  ->  slot_support_when     Inference (conf=0.772)
```

**Edges written:**
```
Inference --HAS_CONCLUSION--> Claim
Inference --HAS_PREMISE-----> Witness (one per witness)
RG root   --CONTAINS_INFERENCE--> Inference
```

---

### Rule 2 -- Cross-Slot Reasoning

**What it does:** Confirms logical relationships between slot Claims. Only fires when both Claims are present.

| Premise | Conclusion | Rule name |
|---|---|---|
| EVIDENCE | WHAT | evidence_supports_what |
| WHO | WHAT | who_about_what |
| HOW | WHAT | how_about_what |
| WHEN | WHAT | when_qualifies_what |
| EVIDENCE | HOW | evidence_supports_how |

**Confidence:** Average of the two Claim confidence scores.

```
EVIDENCE (0.9604) + WHAT (0.925) -> evidence_supports_what  conf=0.9427
WHO      (0.85)   + WHAT (0.925) -> who_about_what          conf=0.8875
HOW      (0.80)   + WHAT (0.925) -> how_about_what          conf=0.8625
```

---

### Rule 3 -- Defeater Weakening

**What it does:** For each Inference with HAS_DEFEATER edges, reduces its confidence score. Cluster-aware -- a clustered Defeater (cluster_size > 1) applies a proportional penalty rather than a flat one.

**Penalty multipliers:**

| Defeater type | cluster_size=1 | cluster_size=3 | cluster_size=5 |
|---|---|---|---|
| Rebutting | x 0.60 | x 0.50 | x 0.45 |
| Undercutting | x 0.80 | x 0.80 flat | x 0.80 flat |

**Key design decision:** The original TRACE confidenceScore is **never modified**. CONSTRUCT adds a new constructScore field alongside it:

```json
{
  "confidenceScore":      0.9427,
  "constructScore":       0.5656,
  "constructScoreReason": "Rule3 -- 1 defeater(s) applied (rebutting), cluster_size=3"
}
```

EXPLAIN reads constructScore when it exists and falls back to confidenceScore when it does not.

**Example — 1 clustered Defeater (cluster_size=3) on WHAT slot:**

With cluster_size=3 the penalty is 0.60 - (3-1) × 0.05 = 0.50:

```
slot_evidence_supports_what : 0.9427 × 0.50 = 0.5656
slot_who_about_what         : 0.8875 × 0.50 = 0.4438
slot_how_about_what         : 0.8625 × 0.50 = 0.4313
slot_when_qualifies_what    : 0.8485 × 0.50 = 0.4243
```

Compare to the old flat penalty (3 separate Defeaters × 0.60 each):
```
0.9427 × 0.60 × 0.60 × 0.60 = 0.2036  ← over-penalised
0.9427 × 0.50               = 0.5656  ← calibrated (cluster_size=3)
```

---

### Rule 4 -- Contested Premise Discount

**What it does:** When an Inference uses a contested Claim as a premise, reduces confidence further.

```
1 contested premise  -> x 0.70
2+ contested premises -> x 0.50
```

---

### Rule 5 -- Composite Synthesis

**What it does:** Creates one top-level Synthesised Claim combining all present slot answers. This is the node EXPLAIN reads.

**Condition (per professor feedback):**
- Fires when at least 1 slot has an answer
- Returns None only when zero slots have any answer
- Configurable via TAU_MIN_SLOTS (default 0)

**Synthesis type:**
```
All 5 slots present -> synthesis_type = "composite"
1-4 slots present   -> synthesis_type = "partial"
0 slots present     -> returns None
```

**Confidence:** Weighted average of present slot Claim confidences.
```
Weights: WHAT=2.0  EVIDENCE=1.5  WHO=1.0  HOW=1.0  WHEN=0.8
Synthesis confidence = 0.8823 (all 5 slots present)
```

**Synthesised Claim node:**
```json
{
  "labels":         ["Claim", "Synthesised"],
  "sourceOperator": "CONSTRUCT",
  "sourceRule":     "Rule5_CompositeSynthesis",
  "status":         "contested",
  "constructScore": 0.8823
}
```

---

### Rule 6 -- Incomplete Answer

Reserved for when TAU_MIN_SLOTS is raised above 0. Currently does not fire at default setting.

---

### Rule 7 -- Corroboration Boost

**What it does:** When multiple witnesses from different source documents ground the same Claim, boosts the supporting Inference confidence.

```
2 documents agree  -> +0.05
3+ documents agree -> +0.10
Capped at 1.0
```

---

## Algorithm 4 Components

These components implement steps from Algorithm 4 of the professor's paper.

---

### SelectTopChain

**Algorithm 4 Line 1**

Before processing any rules CONSTRUCT ranks Claims by the average reliability score of their supporting witnesses. High-reliability Claims receive a trust multiplier applied to their in-memory confidence before rules run.

---

### WitnessTether and AllSentencesTethered

**Algorithm 4 Lines 9-10**

After synthesis CONSTRUCT builds a cite_map -- a dictionary mapping every slot answer to its supporting witness UIDs. This is the sentence-level provenance guarantee.

```json
"cite_map": {
  "WHAT": {
    "statement":    "Target Drug Good Faith Dispensing Checklist",
    "witness_uids": ["wit-a75583...", "wit-eca583...", "..."],
    "claim_uid":    "1ea9a18c-...",
    "status":       "contested",
    "confidence":   0.925
  }
}
```

AllSentencesTethered then asserts every slot has at least MIN_WITNESSES_PER_SLOT (default 1) grounded witnesses. Logs a WARNING for any untethered slots and sets all_tethered=False.

---

### DeriveTimeline

**Algorithm 4 Line 5**

Extracts dates from WHEN slot witnesses and builds a chronological event list. EXPLAIN can present this as a narrative timeline rather than a single date sentence.

---

### DeriveExhibits

**Algorithm 4 Line 6**

Collects all source document IDs referenced across slot Claims and witness anchor IDs into a structured exhibit catalogue. Replaces ad hoc citation collection previously done by EXPLAIN.

```json
"exhibits": [
  { "doc_id": "gjbx0257", "slot": "EVIDENCE", "source": "witness_anchor" }
]
```

---

### DeriveLimitations

**Algorithm 4 Line 7**

Explicitly states what the evidence does not cover. Four types of limitations are derived:

- Missing slots -- no grounded answer found
- Contested slots -- contradictions detected by CONFLICT
- Low confidence slots -- confidence below 0.50
- Single-witness slots -- only one document supports the answer

```json
"limitations": [
  "The WHAT answer is contested — 3 contradicting source(s) were found. The answer should be treated as disputed.",
  "The HOW answer is supported by only one witness — corroboration from additional documents would strengthen it.",
  "The WHEN answer is supported by only one witness — corroboration from additional documents would strengthen it."
]

Note: the conflict count reads cluster_size from the Defeater node, not
the number of Defeater nodes. A clustered Defeater with cluster_size=3
correctly reports 3 contradicting sources even though only 1 Defeater
node exists in the graph.
```

---

## Configuration

All constants at the top of construct.py:

```python
REBUTTING_PENALTY     = 0.60   # Rule 3 -- rebutting Defeater (cluster_size=1)
UNDERCUTTING_PENALTY  = 0.80   # Rule 3 -- undercutting Defeater
CONTESTED_1_PENALTY   = 0.70   # Rule 4 -- one contested premise
CONTESTED_2_PENALTY   = 0.50   # Rule 4 -- two or more contested premises
CORROBORATION_2_BOOST = 0.05   # Rule 7 -- two documents agree
CORROBORATION_3_BOOST = 0.10   # Rule 7 -- three or more documents agree
TAU_MIN_SLOTS         = 0      # Rule 5/6 -- minimum slots for synthesis

# Cluster-size aware penalty (Rule 3)
CLUSTER_PENALTY_BASE  = 0.60   # base penalty for cluster_size=1
CLUSTER_PENALTY_STEP  = 0.05   # reduction per additional cluster member
CLUSTER_PENALTY_FLOOR = 0.40   # minimum penalty regardless of cluster size

# Algorithm 4 switches
TOP_CHAIN_WEIGHT       = 1.0   # SelectTopChain trust multiplier
MIN_WITNESSES_PER_SLOT = 1     # AllSentencesTethered minimum
ENABLE_TIMELINE        = True  # DeriveTimeline
ENABLE_EXHIBITS        = True  # DeriveExhibits
ENABLE_LIMITATIONS     = True  # DeriveLimitations
```

---

## Data Structures

### ClaimRecord

```python
@dataclass
class ClaimRecord:
    uid:          str
    slot:         str        # WHO | WHAT | HOW | EVIDENCE | WHEN
    status:       str        # supported | contested | ambiguous
    confidence:   float      # updated in-memory by Rules 3 and 4
    statement:    str
    witness_uids: List[str]  # from GROUNDED_BY edges
```

### InferenceRecord

```python
@dataclass
class InferenceRecord:
    uid:            str
    rule_name:      str
    confidence:     float    # updated in-memory by Rules 3 and 4
    premise_uids:   List[str]
    conclusion_uid: str
    defeater_uids:  List[str]  # from HAS_DEFEATER edges
```

### ConstructResult

```python
@dataclass
class ConstructResult:
    new_nodes:          List[dict]
    new_edges:          List[dict]
    updated_inferences: List[dict]  # constructScore records
    synthesis_uid:      str
    synthesis_conf:     float
    synthesis_type:     str         # composite | partial | null
    stats:              dict
    diagnostics:        List[dict]
    # Algorithm 4 components
    cite_map:           dict        # WitnessTether -- slot -> witnesses
    timeline:           List[dict]  # DeriveTimeline
    exhibits:           List[dict]  # DeriveExhibits
    limitations:        List[str]   # DeriveLimitations
    all_tethered:       bool        # AllSentencesTethered assertion
```

---

## Graph Writes

| What | Count | Rule | Labels |
|---|---|---|---|
| Slot support Inferences | 5 | Rule 1 | Inference |
| Cross-slot Inferences | Up to 5 | Rule 2 | Inference |
| constructScore fields | Per weakened Inference | Rule 3 | Added to existing nodes |
| Synthesised Claim | 1 | Rule 5 | Claim + Synthesised |
| Synthesis Inference | 1 | Rule 5 | Inference |

**Edge types written:**
```
HAS_CONCLUSION       Inference -> Claim
HAS_PREMISE          Inference -> Claim or Witness
CONTAINS_INFERENCE   RG root   -> Inference
CONTAINS_CLAIM       RG root   -> Synthesised Claim
```

---

## Design Decisions

**Why not mutate the original TRACE confidenceScore?**

The original confidenceScore is preserved untouched. CONSTRUCT adds constructScore alongside it so the audit trail is preserved. EXPLAIN reads constructScore when it exists and falls back to confidenceScore when it does not.

**Why create a new Synthesised Claim node?**

The WHAT Claim represents only one slot answer. The Synthesised Claim carries all five slots and is clearly labelled with the Synthesised label so it is distinguishable from TRACE Claims.

**Why cluster-size aware penalties?**

Before clustering, three conflicts about the same question produced three Defeaters and three sequential penalties -- reducing confidence from 0.94 to 0.20. With cluster-aware penalties one Defeater with cluster_size=3 applies a proportional penalty -- more than one conflict but not triple the damage. This prevents over-penalisation.

**Why synthesise with any number of present slots?**

Per professor guidance the pipeline should return whatever it found. A partial answer with labelled limitations is more useful than no answer at all.

---

## Example Output

```
==============================================================
  CONSTRUCT OPERATOR
==============================================================

Loading conflict_bundle.json...

-- Input -------------------------------------------------------
  Claims loaded      : 5
  Inferences loaded  : 5
  Defeaters loaded   : 3  (cluster_size=3 on WHAT slot)
  Contested claims   : 1

-- Rules applied -----------------------------------------------
  Rule3: slot_evidence_supports_what  0.9427 -> 0.5656 (cluster_size=3, penalty=0.50)
  Rule3: slot_who_about_what          0.8875 -> 0.4438 (cluster_size=3, penalty=0.50)
  Rule3: slot_how_about_what          0.8625 -> 0.4313 (cluster_size=3, penalty=0.50)
  Rule3: slot_when_qualifies_what     0.8485 -> 0.4243 (cluster_size=3, penalty=0.50)
  Rule5 (composite):
    slots present : ['EVIDENCE', 'HOW', 'WHAT', 'WHEN', 'WHO']
    confidence    : 0.8823
    contested     : ['WHAT']

-- Output ------------------------------------------------------
  New nodes written   : 12
  New edges written   : 55
  Inferences updated  : 4
  Synthesis type      : composite
  Synthesis confidence: 0.8823

-- Algorithm 4 Components -------------------------------------
  WitnessTether    : 5 slots tethered
  AllTethered      : True
  Limitations      : 3
    ⚠ The WHAT answer is contested — 3 contradicting source(s) were found. The answer should be treated as disputed.
    ⚠ The HOW answer is supported by only one witness — corroboration from additional documents would strengthen it.
    ⚠ The WHEN answer is supported by only one witness — corroboration from additional documents would strengthen it.

Saved to construct_bundle.json
  This file is ready to use as input for EXPLAIN.
```

---

## Next Step

The output of CONSTRUCT -- `construct_bundle.json` -- is the input to **EXPLAIN**.

EXPLAIN reads the top-level synthesis Inference node and generates a natural language answer with inline citations from the cite_map, contested slot warnings from the limitations list, a confidence label from the synthesis constructScore, and a full evidence chain audit trail.

See [EXPLAIN_README.md](./EXPLAIN_README.md) for full details.
