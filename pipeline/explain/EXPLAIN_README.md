# EXPLAIN Operator

**Pipeline position:** `ALIGN → TRACE → CONFLICT → CONSTRUCT → EXPLAIN`

EXPLAIN is the fifth and final operator in the evidence exploration pipeline. It reads the synthesised answer bundle produced by CONSTRUCT and generates a natural language answer to the original investigator question — with per-finding provenance narratives, structured conflict explanations, analytical decision point analysis, and a full uncertainty map.

Implements Algorithm 5 from the professor's paper across five phases.

---

## Table of Contents

- [Overview](#overview)
- [How to Run](#how-to-run)
- [Input](#input)
- [Output](#output)
- [Architecture](#architecture)
- [Algorithm 5 — Five Phases](#algorithm-5--five-phases)
  - [Phase 1 — Derivation Subgraph and Decision Points](#phase-1--derivation-subgraph-and-decision-points)
  - [Phase 2 — Provenance Narratives](#phase-2--provenance-narratives)
  - [Phase 3 — Conflict and Limitation Explanations](#phase-3--conflict-and-limitation-explanations)
  - [Phase 4 — Decision Explanations and Sensitivity](#phase-4--decision-explanations-and-sensitivity)
  - [Phase 5 — UncertaintyMap, TetherMap, TetherComplete](#phase-5--uncertaintymap-tethermap-tethercomplete)
- [Neo4j Write-Back](#neo4j-write-back)
- [Data Structures](#data-structures)
- [Design Decisions](#design-decisions)
- [Example Output](#example-output)
- [Next Step](#next-step)

---

## Overview

By the time EXPLAIN runs, CONSTRUCT has produced:

- A **Synthesised Claim** node with all five slot answers
- A **cite_map** mapping each slot to its witnesses
- A **limitations** list describing answer gaps
- **constructScore** values reflecting post-contradiction confidence

EXPLAIN reads all of these and produces a complete explanation bundle:

```
Phase 1  ->  Derivation subgraph (focused RG view) + decision points
Phase 2  ->  Provenance narrative per slot finding
Phase 3  ->  Conflict explanation (WHAT) + limitation explanations (HOW, WHEN)
Phase 4  ->  Decision point analysis with analytical sensitivity
Phase 5  ->  UncertaintyMap + TetherMap + TetherComplete assertion
```

---

## How to Run

### Option 1 — Standard in-memory run

```bash
python explain.py
```

**Files needed:**
```
construct_bundle.json    <- from CONSTRUCT
explain.py
```

**Output produced:**
```
explain_bundle.json      <- full ExplBundle for dashboard
explain_output.txt       <- plain text for investigator
```

---

### Option 2 — Neo4j write-back

Writes all Algorithm 5 components directly to Neo4j in addition to saving the JSON files.

**Files needed:**
```
construct_bundle.json
explain.py
neo4j_writer.py
run_explain_neo4j.py
```

**Step 1 — Run the full pipeline first:**
```bash
# CONFLICT
cd CONFLICT
python run_conflict_neo4j.py
cp conflict_bundle.json ../CONSTRUCT/

# CONSTRUCT
cd ../CONSTRUCT
python run_construct_neo4j.py
cp construct_bundle.json ../EXPLAIN/

# EXPLAIN
cd ../EXPLAIN
```

**Step 2 — Set Neo4j connection:**
```bash
export NEO4J_URI=bolt://localhost:7687
export NEO4J_USER=neo4j
export NEO4J_PASSWORD=yourpassword
export NEO4J_DATABASE=neo4j
```

**Step 3 — Run:**
```bash
python run_explain_neo4j.py
```

**What gets written to Neo4j:**
```
RG  <-  ExplainNode              (root of this explanation run)
RG  <-  ProvenanceNarrative      (one per slot finding — Phase 2)
RG  <-  ConflictExplanation      (one per conflict/limitation — Phase 3)
RG  <-  DecisionPoint            (one per Inference — Phase 4)
RG  <-  UncertaintyMap           (aggregated uncertainty — Phase 5)
RG  <-  EXPLAINS edge            (ExplainNode -> Synthesised Claim)
RG  <-  HAS_NARRATIVE edges      (ExplainNode -> ProvenanceNarrative)
RG  <-  HAS_CONFLICT_EXPL edges  (ExplainNode -> ConflictExplanation)
RG  <-  HAS_DECISION edges       (ExplainNode -> DecisionPoint)
RG  <-  TETHERED_TO edges        (each explanation -> its RG node)
```

**Fallback** — if Neo4j is not running the script falls back to in-memory automatically.

---

### Which script to use

| Scenario | Script |
|---|---|
| Development and testing | `explain.py` |
| Production with Neo4j backend | `run_explain_neo4j.py` |

---

## Input

EXPLAIN reads `construct_bundle.json` — the full pipeline bundle including the new CONSTRUCT fields:

```json
{
  "construct_result": {
    "cite_map":    { "WHAT": {...}, "WHO": {...}, "HOW": {...}, "WHEN": {...}, "EVIDENCE": {...} },
    "limitations": ["The WHAT answer is contested...", "The HOW answer is supported by only one witness..."],
    "exhibits":    [...],
    "timeline":    [...],
    "all_tethered": true
  },
  "rg": { "nodes": [...], "edges": [...] },
  "eg": { "nodes": [...], "edges": [...] }
}
```

**What EXPLAIN reads:**

| Source | Field | Used by |
|---|---|---|
| construct_result | cite_map | Phase 2 witness tethering |
| construct_result | limitations | Phase 3 conflict/limitation explanations |
| construct_result | updated_inferences | Phase 4 decision points |
| conflict_result | conflicts | Phase 3 ConflictEdge lookup |
| rg.nodes | Synthesised Claim | All phases |
| rg.nodes | Inference nodes | Phase 1 derivation subgraph |
| rg.nodes | Defeater nodes | Phase 3 cluster_size |
| eg.nodes | EvidenceNode Testimony | Phase 3 witness surfaces |

---

## Output

### explain_bundle.json

Full ExplBundle with all Algorithm 5 components:

```json
{
  "explain_result": {
    "answer_text":           "The focal document is ...",
    "confidence_score":      0.8823,
    "confidence_label":      "HIGH",
    "contested_slots":       ["WHAT"],
    "citations":             ["gjbx0257", "kg0:17def9bf066e", ...],
    "provenance_narratives": [
      { "finding": "WHAT", "narrative": "The focal document is ...",
        "rg_tether": "1ea9a18c-...", "derivation_path": [...] }
    ],
    "conflict_explanations": [
      { "slot": "WHAT", "is_conflict": true,
        "explanation": "The WHAT answer is disputed because 3 source(s)...",
        "context": "Slot WHAT: witness '...' contradicts '...'",
        "rg_tether": "936e42c3-..." }
    ],
    "decision_points": [
      { "rule_name": "slot_evidence_supports_what",
        "slot": "WHAT", "original_conf": 0.9427,
        "construct_score": 0.5656, "sensitivity": "STABLE",
        "sensitivity_margin": 0.443, "rationale": "..." }
    ],
    "uncertainty_map": {
      "slots": {
        "WHAT": { "uncertainty_level": "HIGH", "sensitivity": "STABLE", "witnesses": 6 }
      },
      "overall_confidence": 0.8823,
      "contested_slots":    ["WHAT"],
      "stable_slots":       ["WHAT", "WHO", "HOW", "EVIDENCE", "WHEN"]
    },
    "tether_map":      { "pn::WHAT": { "tether": "1ea9a18c-...", "valid": true }, ... },
    "tether_complete": true,
    "derivation_subgraph": { "node_count": 23, "edge_count": 101 }
  }
}
```

### explain_output.txt

Plain text for the investigator with four sections: Answer, Citations, Limitations, Uncertainty Map, Evidence Chain.

---

## Architecture

EXPLAIN is a **single-use stateful operator**. Instantiate it, call `execute()` once, then discard.

```
construct_bundle.json
       |
       v
  Explain(data)
       |
       |-- _load_synthesis()              <- find Synthesised Claim
       |-- _load_slots()                  <- load slot Claims + cite_map
       |-- _load_defeaters()              <- load Defeater nodes
       |-- _load_witnesses_per_slot()     <- GROUNDED_BY from EG + RG
       |
       |-- Phase 1: _extract_derivation_subgraph()
       |            _identify_decision_points()
       |
       |-- Phase 2: _build_provenance_narratives()
       |
       |-- Phase 3: _build_conflict_explanations()
       |
       |-- Phase 4: _build_decision_explanations()
       |
       |-- Phase 5: _build_uncertainty_map()
       |            _build_tether_map()
       |            _assert_tether_complete()
       |
       |-- _generate_answer()             <- GenDerivationSummary
       |-- _build_evidence_chain()
       |-- _collect_citations()
       |
       v
  ExplainResult
       |
       +--> explain_bundle.json
       +--> explain_output.txt
```

---

## Algorithm 5 — Five Phases

---

### Phase 1 — Derivation Subgraph and Decision Points

**Algorithm 5 Lines 1-3**

**ExtractDerivationSubgraph** builds a focused view of the RG containing only the nodes that contributed to the final answer:
- Synthesised Claim and synthesis Inference
- All slot Claims and their supporting Inferences
- All Defeater nodes

This keeps Phase 2-5 working on a small manageable graph regardless of how large the full RG is.

**IdentifyDecisionPoints** walks the derivation subgraph and identifies every Inference node where CONSTRUCT made a confidence decision. For each decision point it computes **analytical sensitivity** — the margin between constructScore and the acceptance floor (0.50):

```
STABLE    : margin > 0.20  — conclusion robust, evidence strong
MARGINAL  : margin 0.10-0.20 — conclusion somewhat sensitive
SENSITIVE : margin 0.00-0.10 — conclusion could change with small evidence shift
BELOW_FLOOR: margin < 0     — confidence below acceptance threshold
```

**Why analytical sensitivity instead of pipeline re-runs?**

The algorithm specifies `probe_budget` to cap expensive sensitivity probes. We use `probe_budget=0` and compute sensitivity analytically — no re-runs needed, fully scalable to any corpus size.

```
Example on Walgreens sample:
  slot_evidence_supports_what  conf=0.5656  margin=0.443  STABLE
  slot_support_what            conf=0.5656  margin=0.443  STABLE
  construct_composite_synthesis conf=0.8823 margin=0.382  STABLE
```

All decision points are STABLE because even with the cluster_size=3 penalty the confidence is well above the acceptance floor.

---

### Phase 2 — Provenance Narratives

**Algorithm 5 Lines 4-11**

For each slot finding (up to 5) EXPLAIN:

1. Traces the derivation path — synthesis Claim -> slot Claim -> witness UIDs
2. Identifies relevant decision points for this slot
3. Generates a narrative sentence using a slot-specific template
4. Asserts HasRGTether — the narrative is connected to a Claim UID in the derivation subgraph

```
WHAT      The focal document is Target Drug Good Faith Dispensing Checklist (contested). [conf=0.93]
WHO       The responsible parties are district leaders and Pharmacy Supervisors. [conf=0.85]
HOW       This was carried out via Senior Attorney, Litigation & Regulatory Law. [conf=0.80]
WHEN      The relevant date is June 26, 2006. [conf=0.77]
EVIDENCE  Supporting evidence: Target Drug Good Faith Dispensing Checklist. [conf=0.96]
```

Each narrative carries a `rg_tether` field pointing to the Claim UID that justifies it.

---

### Phase 3 — Conflict and Limitation Explanations

**Algorithm 5 Lines 12-23**

For each limitation from CONSTRUCT, EXPLAIN classifies it as either conflict-derived or non-conflict:

**Conflict-derived (WHAT slot):**
1. ConflictEdge lookup — finds the CONTRADICTS edge in the EG for this slot
2. GatherConflictContext — retrieves witness surfaces from the EG
3. GenConflictExplanation — generates explanation naming the specific instruments in conflict

```
[CONFLICT] The WHAT answer is disputed because 3 source(s) cite different
answers for the same question. Specifically: 'California Code of Civil
Procedure section 664.6' vs 'FULL AND COMPLETE CONFIDENTIAL SETTLEMENT
AGREEMENT'. This is a surface mismatch — the investigator should verify
which instrument applies.
```

**Non-conflict limitations (HOW, WHEN):**

```
[LIMITATION] The HOW answer is supported by only one witness — corroboration
from additional documents would strengthen it.

[LIMITATION] The WHEN answer is supported by only one witness — corroboration
from additional documents would strengthen it.
```

Each explanation carries an `rg_tether` to its supporting RG node.

---

### Phase 4 — Decision Explanations and Sensitivity

**Algorithm 5 Lines 24-37**

Returns the decision points from Phase 1 with their full analytical sensitivity assessment. No pipeline re-runs — sensitivity is computed as confidence margin to acceptance floor.

On the Walgreens sample all 16 decision points are STABLE:

```
slot_evidence_supports_what    margin=0.443  STABLE
slot_who_about_what            margin=0.388  STABLE
slot_how_about_what            margin=0.362  STABLE
slot_when_qualifies_what       margin=0.348  STABLE
slot_support_what              margin=0.425  STABLE
construct_composite_synthesis  margin=0.382  STABLE
```

This tells the investigator the conclusions are robust — the answer would not change unless the evidence quality shifted significantly.

---

### Phase 5 — UncertaintyMap, TetherMap, TetherComplete

**Algorithm 5 Lines 38-43**

**BuildUncertaintyMap** aggregates confidence scores, contested slots, sensitivity levels, and limitation counts into one structured object per slot:

```
WHAT      uncertainty=HIGH    sensitivity=STABLE   witnesses=6
WHO       uncertainty=LOW     sensitivity=STABLE   witnesses=2
HOW       uncertainty=MEDIUM  sensitivity=STABLE   witnesses=1
EVIDENCE  uncertainty=LOW     sensitivity=STABLE   witnesses=12
WHEN      uncertainty=MEDIUM  sensitivity=STABLE   witnesses=1
```

WHAT is HIGH because it is contested — three sources cite different legal instruments for the same question.

HOW and WHEN are MEDIUM because each has only one witness — grounded but not corroborated across documents.

WHO is LOW because it is well supported by two witnesses and not contested.

EVIDENCE is LOW because it is the best supported slot with 12 witnesses across multiple documents.

**BuildTetherMap** maps every narrative and explanation sentence to its RG node UID.

**TetherComplete** asserts all 24 entries in the tether map have valid RG tethers. On the current bundle: `TetherComplete: True`.

---

## Neo4j Write-Back

When connected to Neo4j the following nodes and edges are written:

| Node label | Count | Phase |
|---|---|---|
| ExplainNode | 1 | Root |
| ProvenanceNarrative | 5 (one per slot) | Phase 2 |
| ConflictExplanation | 3 (1 conflict + 2 limitations) | Phase 3 |
| DecisionPoint | 16 (one per Inference) | Phase 4 |
| UncertaintyMap | 1 | Phase 5 |

Edge types written:
```
EXPLAINS         ExplainNode -> Synthesised Claim
HAS_NARRATIVE    ExplainNode -> ProvenanceNarrative
HAS_CONFLICT_EXPL ExplainNode -> ConflictExplanation
HAS_DECISION     ExplainNode -> DecisionPoint
HAS_UNCERTAINTY  ExplainNode -> UncertaintyMap
TETHERED_TO      each explanation node -> its RG tether node
```

---

## Data Structures

### DecisionPoint

```python
@dataclass
class DecisionPoint:
    inference_uid:       str
    rule_name:           str
    slot:                str
    original_conf:       float    # TRACE confidenceScore
    construct_score:     float    # post-penalty from CONSTRUCT
    defeaters:           int      # number of Defeaters applied
    sensitivity:         str      # STABLE | MARGINAL | SENSITIVE | BELOW_FLOOR
    sensitivity_margin:  float    # constructScore - ACCEPTANCE_FLOOR (0.50)
    rationale:           str
```

### ProvenanceNarrative

```python
@dataclass
class ProvenanceNarrative:
    finding:         str        # slot name
    statement:       str        # cleaned slot answer
    derivation_path: List[str]  # UIDs from synthesis to witness
    decisions:       List[str]  # relevant decision point UIDs
    narrative:       str        # generated sentence
    rg_tether:       str        # UID of supporting RG node
```

### ConflictExplanation

```python
@dataclass
class ConflictExplanation:
    limitation:      str
    is_conflict:     bool
    conflict_rule:   str    # e.g. SURFACE_MISMATCH
    witness_a:       str    # surface text of first witness
    witness_b:       str    # surface text of second witness
    slot:            str
    context:         str    # gathered from EG
    explanation:     str    # generated text
    rg_tether:       str
```

### ExplainResult

```python
@dataclass
class ExplainResult:
    answer_text:            str
    confidence_label:       str
    confidence_score:       float
    contested_slots:        List[str]
    missing_slots:          List[str]
    citations:              List[str]
    evidence_chain:         List[str]
    warnings:               List[str]
    stats:                  Dict
    explain_node_uid:       str
    # Algorithm 5
    provenance_narratives:  List[ProvenanceNarrative]
    conflict_explanations:  List[ConflictExplanation]
    decision_points:        List[DecisionPoint]
    uncertainty_map:        Dict
    tether_map:             Dict
    tether_complete:        bool
    derivation_subgraph:    Dict
```

---

## Design Decisions

**Why analytical sensitivity instead of probe_budget re-runs?**

EstimateSensitivity in its full form requires re-running the pipeline with slightly modified confidence scores. This is expensive at scale. We compute sensitivity analytically as the margin between constructScore and the acceptance floor. This gives the same signal — how robust is the conclusion — without any re-runs, making Phase 4 fully scalable to the 30M+ corpus.

**Why read cite_map and limitations from CONSTRUCT?**

CONSTRUCT now writes cite_map (WitnessTether) and limitations (DeriveLimitations) into the bundle. EXPLAIN reads these directly instead of reconstructing them. This avoids duplication and ensures both operators agree on which witnesses support which slot and what the answer gaps are.

**Why per-finding provenance narratives instead of one flat audit trail?**

The algorithm specifies HasRGTether per narrative sentence — every sentence must trace back to an RG node. A flat audit trail cannot satisfy this assertion. Per-finding narratives make the provenance explicit and allow the dashboard to highlight exactly which source document supports each part of the answer.

**Why use template-based generation for narratives?**

Template-based generation is deterministic, fast, and fully auditable. Every sentence follows the same pattern so the investigator knows what to expect. A future improvement is to add an optional LLM rewriting step that produces more fluent prose while keeping the template as a fallback.

---

## Example Output

```
==============================================================
  EXPLAIN OPERATOR
==============================================================

Loading construct_bundle.json...

-- Answer -------------------------------------------------------
The focal document is Target Drug Good Faith Dispensing Checklist
⚠ [contested], involving district leaders and Pharmacy Supervisors.
The responsible party was Senior Attorney, Litigation & Regulatory Law.
This occurred on June 26, 2006.
Supporting evidence: Target Drug Good Faith Dispensing Checklist [Doc: gjbx0257].

⚠ NOTE: The WHAT answer is disputed because 3 source(s) cite different
answers for the same question. Specifically: 'California Code of Civil
Procedure section 664.6' vs 'FULL AND COMPLETE CONFIDENTIAL SETTLEMENT
AGREEMENT'. This is a surface mismatch — the investigator should verify
which instrument applies.

[Confidence: HIGH (0.88) | Contested: WHAT]

-- Derivation Subgraph ------------------------------------------
  Nodes : 23
  Edges : 101

-- Decision Points ----------------------------------------------
  [STABLE    ] slot_evidence_supports_what    margin=0.443
  [STABLE    ] slot_who_about_what            margin=0.388
  [STABLE    ] slot_how_about_what            margin=0.362
  [STABLE    ] slot_when_qualifies_what       margin=0.348
  [STABLE    ] construct_composite_synthesis  margin=0.382

-- Conflict Explanations ----------------------------------------
  [CONFLICT]   The WHAT answer is disputed because 3 source(s)...
  [LIMITATION] The HOW answer is supported by only one witness...
  [LIMITATION] The WHEN answer is supported by only one witness...

-- Uncertainty Map ----------------------------------------------
  WHAT         uncertainty=HIGH     sensitivity=STABLE   witnesses=6
  WHO          uncertainty=LOW      sensitivity=STABLE   witnesses=2
  HOW          uncertainty=MEDIUM   sensitivity=STABLE   witnesses=1
  EVIDENCE     uncertainty=LOW      sensitivity=STABLE   witnesses=12
  WHEN         uncertainty=MEDIUM   sensitivity=STABLE   witnesses=1

-- Tether Map ---------------------------------------------------
  Entries  : 24
  Complete : True

-- Stats --------------------------------------------------------
  slots_answered              : 5
  slots_contested             : 1
  citations                   : 11
  provenance_narratives       : 5
  conflict_explanations       : 3
  decision_points             : 16
  tether_complete             : True
  derivation_nodes            : 23

Saved to explain_bundle.json
Plain text saved to explain_output.txt
```

---

## Next Step

The output of EXPLAIN — `explain_bundle.json` — is consumed by the **investigator dashboard**.

The dashboard reads:
- `answer_text` — the natural language answer to display
- `uncertainty_map.slots` — to show per-slot confidence and uncertainty indicators
- `conflict_explanations` — to show the conflict detail panel
- `decision_points` — to show the reasoning transparency panel
- `citations` — to link to source documents in the IDL
- `tether_complete` — to show a provenance integrity indicator
