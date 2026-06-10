# TRACE Code Walkthrough

This document maps the current `pipeline/trace/` implementation. TRACE
still materializes the Evidence Graph and Reasoning Graph, and it now
also packages those writes into the chain-first `trace_bundle.json`
contract consumed by CONFLICT, CONSTRUCT, and EXPLAIN.

```text
AlignBundle
  -> Trace.execute()              -> EG/RG graph writes
  -> build_trace_bundle()         -> trace_bundle.chain.v1
  -> CONFLICT/CONSTRUCT/EXPLAIN   -> conflict/answer/explanation bundles
```

---

## 1. Package Layout

```text
pipeline/trace/
├── __init__.py              Public API exports
├── config.py                TraceConfig, TRACE_NS, ACTION_KEYWORDS
├── result.py                TraceResult dataclass
├── writers.py               In-memory, Memgraph, and Neo4j graph writers
├── trace2.py                Legacy-compatible EG/RG materializer
├── bundle_builder.py        Chain-first TraceBundle builder
├── EG-RG schemas.md         Graph schema reference
└── CODE_WALKTHROUGH.md      This file
```

The current TRACE runner in `pipeline_test/trace/neo4j/run_trace_to_neo4j.py`
does four things:

1. loads `results/align/align_bundle.json`;
2. runs `Trace.execute()` in memory;
3. calls `build_trace_bundle()` and writes
   `results/trace/trace_bundle.json`;
4. loads `eg_delta` and `rg_trace` into local Neo4j when available.

---

## 2. Inputs and Outputs

### Input: AlignBundle

TRACE expects:

```python
{
    "corpus_stats": {...},
    "result": {
        "intent_id": "...",
        "question_text": "...",
        "artifact_set": [...],
        "all_anchors": {...},
        "all_mentions": {...},
        "all_witnesses": [...],
        "subgraphs": [...],
        "slot_bindings": [...],
        "entity_hypotheses": [...],
        "suppressed_mentions": {...},
        "replay_plan": {...}
    }
}
```

### Output 1: EG/RG writes

`Trace.execute()` writes through graph writers:

- EG: artifacts, anchors, observations, testimony, snapshots, frame
  witnesses, provenance events, reliability factors;
- RG: graph root, agent, claims, inferences, optional defeaters, and
  trace metadata nodes.

### Output 2: chain-first TraceBundle

`bundle_builder.py` emits:

```python
{
    "schema_version": "trace-bundle.chain.v1",
    "trace_bundle_id": "...",
    "trace_spec": {...},
    "slot_candidates": {...},
    "map_transform": {...},
    "ranked_chains": [...],
    "provenance_manifest": {...},
    "eg_delta": {"root_uid": "...", "nodes": [...], "edges": [...]},
    "rg_trace": {"root_uid": "...", "nodes": [...], "edges": [...]},
    "accuracy_report": {...}
}
```

The legacy top-level `trace_result` field is intentionally not allowed
by `validate_trace_bundle()`.

---

## 3. Trace.execute() Materialization Flow

File: `trace2.py`

`Trace.execute(bundle)` runs the graph materialization phases in order:

```text
prepare alignment indices
Phase 0  EG preconditions and root
Phase 1  artifacts
Phase 2  anchors
Phase 3  mentions
Phase 3b entity co-reference edges
Phase 4  testimony witnesses
Phase 5  KG0 snapshots
Phase 6  frame witnesses and provenance events
Phase 7  RG claims, inferences, defeaters
Phase 8  IDENTICAL_TO chains
Phase 9  schema completion and reliability factors
finalise TraceResult
```

The graph materializer is still the source of EG/RG nodes and edges.
`bundle_builder.py` does not replace it; it reads the in-memory writers
after materialization and packages the chain-first contract.

---

## 4. Phase-by-Phase Graph Writes

### Pre-indexing

`_prepare_alignment_indices()` scans the AlignBundle once and builds:

- mention hypothesis lookup;
- KG0 entity snapshot metadata;
- suppressed mention lookup.

These indexes let later phases annotate nodes without repeatedly
walking the full AlignBundle.

### Phase 0: Preconditions

Writes:

- EG `Agent`;
- EG `GraphRoot`;
- `GraphRoot -[AUTHORED_BY]-> Agent`.

The EG root stores schema version, graph type, corpus stats, replay
plan, and suppressed mention metadata.

### Phase 1: Artifacts

Consumes `result.all_anchors` and subgraph frame artifacts. Writes one
`EvidenceNode` with `type="Artifact"` per artifact.

Edges:

- `GraphRoot -[CONTAINS_NODE]-> Artifact`.

### Phase 2: Anchors

Consumes `result.all_anchors`. Writes one
`EvidenceNode[Document]` per anchor.

Edges:

- `GraphRoot -[CONTAINS_NODE]-> Document`;
- `Document -[DERIVES_FROM]-> Artifact`.

### Phase 3: Mentions

Consumes `result.all_mentions`. Writes one
`EvidenceNode[Observation]` per mention.

Edges:

- `Observation -[DERIVES_FROM]-> Anchor`.

Mention categories come from ALIGN. Person, role, and population must
remain semantically distinct because downstream slot candidates are
built from these observations and testimony nodes.

### Phase 3b: Entity co-reference

Consumes mention groups keyed by entity/coreference ID. Writes no new
nodes. Emits chained `Observation -[CORROBORATES]-> Observation` edges
for cross-document co-reference groups.

### Phase 4: Testimony witnesses

Consumes `result.all_witnesses` or falls back to slot binding
witnesses.

Critical behavior:

- witnesses are deduped by `content_hash`;
- the surviving testimony UID is cached so claims can ground to the
  canonical witness;
- scores are clamped to `[0, 1]`.

Writes:

- `EvidenceNode[Testimony]`.

Edges:

- `Testimony -[DERIVES_FROM]-> Anchor`;
- `Testimony -[DERIVES_FROM]-> Observation`.

### Phase 5: Snapshots

Consumes subgraph snapshots and entity-hypothesis KG0 metadata. Writes
`EvidenceNode[Record]` nodes for KG0 snapshot records.

### Phase 6: Frame witnesses and provenance events

Consumes `subgraphs[*].frame_witness`. Writes:

- `EvidenceNode[Derived]` for frame witnesses;
- one `ProvenanceEvent` per evidence chain step.

Edges:

- `Derived -[DERIVES_FROM]-> Anchor`;
- `Derived -[DERIVES_FROM]-> Observation`;
- `Derived -[DERIVES_FROM]-> Record`;
- `Derived -[HAS_PROVENANCE_EVENT]-> ProvenanceEvent`;
- `ProvenanceEvent -[PERFORMED_BY]-> Agent`.

Action labels are normalized through `ACTION_KEYWORDS` in `config.py`.

### Phase 7: Claims, inferences, defeaters

Consumes `result.slot_bindings`. Writes:

- RG `GraphRoot` and `Agent`;
- one `Claim` per surviving slot;
- `Inference` nodes from the slot relation template;
- optional undercutting `Defeater` nodes for weak inferences.

Edges:

- `RG.GraphRoot -[CONTAINS_CLAIM]-> Claim`;
- `Claim -[ASSERTED_BY]-> Agent`;
- `Claim -[CONFIDENCE_ASSESSED_BY]-> Agent`;
- `Claim -[GROUNDED_BY]-> Testimony` through the bridge writer;
- inference premise/conclusion/agent edges.

When bridge and RG are different writers, TRACE writes shadow Claim
nodes through the bridge so cross-graph `GROUNDED_BY` edges can
resolve. The Neo4j runner filters those shadows from merged JSON
output because real Claims live in `rg_trace`.

### Phase 8: IDENTICAL_TO chains

Uses the collection-to-artifacts map from Phase 1 and emits
`OTHER_EVIDENCE_REL {subtype:"IDENTICAL_TO"}` chains between
artifacts in the same collection.

### Phase 9: Schema completion

For every EvidenceNode, TRACE adds schema-supporting edges and
reliability factors:

- `CREATED_BY`;
- `INTEGRITY_VERIFIED_BY`;
- `RELIABILITY_ASSESSED_BY`;
- `HAS_RELIABILITY_FACTOR`.

---

## 5. Chain-First TraceBundle Builder

File: `bundle_builder.py`

`build_trace_bundle()` takes:

```python
build_trace_bundle(
    align_bundle=align_bundle,
    trace_result=trace_result,
    eg_writer=eg_mem,
    rg_writer=rg_mem,
)
```

It builds these contract sections.

### trace_spec

`_build_trace_spec()` records:

- intent id and question text;
- AlignBundle hash;
- slot specs from ALIGN slot bindings;
- mapping label set;
- chain ranking weights;
- replay plan hash.

### slot_candidates

`_build_slot_candidates()` converts slot binding witnesses into
candidate records:

```text
slot_id -> [
  candidate_id,
  slot_id,
  slot_type,
  var_name,
  object_id,
  claim_uid,
  surface,
  normalized_surface,
  artifact_id,
  anchor_id,
  mention_id,
  kg0_entity_id,
  quality,
  confidence,
  witness_bundle
]
```

The `object_id` is resolved from EG Testimony by witness
`content_hash`. This is why Phase 4 witness dedup matters: every
slot candidate points to the canonical testimony node that exists in
`eg_delta`.

### map_transform

`_run_map_transform()` calls `pipeline/map/map_transform.py` over
candidate testimony object IDs. It uses:

- `IdentityEmbedder` for deterministic pair selection in the current
  TraceBundle packaging path;
- `DeepSeekTransformClassifier` when `TraceConfig.map_transform_classifier_backend`
  is `deepseek`;
- `HeuristicTransformClassifier` when no TraceConfig is supplied or
  the backend is explicitly set to `heuristic`;
- labels: `VERBATIM`, `PARAPHRASE`, `COMPRESSION`, `OMISSION`,
  `QUALIFIER_DROP`, `HEDGE_DROP`, `OTHER`.

The DeepSeek backend calls `deepseek-v4-pro` through the OpenAI-compatible
chat completions API with thinking enabled and
`reasoning_effort="high"`. It reads the API key from process env or root
`.env`; `DEEPSEEK_API_KEY` is preferred, with the existing `OPENAI_KEY`
alias also supported. When `OPENAI_KEY` is used, the classifier also reads
`OPENAI_BASE_URL` and `OPENAI_MODEL` so the repo's OpenRouter-style
`deepseek/deepseek-v4-pro` configuration can run without duplicating secrets.
For OpenRouter, reasoning is sent as `reasoning.effort="high"` with
`exclude=true` so the internal reasoning stays out of the returned JSON.
No `max_tokens` cap is sent by default. Phase 3 uses batch classification
when the classifier exposes `classify_batch()`: the default TraceConfig
uses batch size 8, concurrency 4, and 2 retries before falling back to
single-pair classification for failed or missing batch results.

Retained records include source/target candidate IDs, source/target
object IDs, label, confidence, validation state, derived object ID,
and source/target witness bundles.

### ranked_chains

`_build_ranked_chains()` builds one chain per ALIGN subgraph, or a
fallback chain when there are no subgraphs. A chain contains:

- candidate nodes;
- MAP-TRANSFORM edges among those candidates;
- `slot_candidate_ids`;
- `mapping_ids`;
- witness bundle references;
- scoring breakdown.

Ranking weights:

```python
{
    "slot_coverage": 0.4,
    "witness_complete": 0.25,
    "source_diversity": 0.2,
    "temporal_consistency": 0.1,
    "mapping_support": 0.05,
}
```

`_mark_selected_candidates()` sets `selected_in_chain` on candidates
referenced by ranked chains.

### provenance_manifest

`_build_provenance_manifest()` records:

- operator invocations: compile tracespec, extract slot candidates,
  map transform, assemble chains, rank chains, commit/package;
- artifact sets for each bundle section;
- deterministic model/solver call metadata;
- EG/RG write manifests;
- witness records;
- content hashes.

`_write_rg_trace_nodes()` writes `OperatorInvocation`, `ArtifactSet`,
and `ModelSolverCall` nodes into the RG writer and attaches them to
the RG root.

### accuracy_report

`_build_accuracy_report()` stores:

- validation errors from `validate_trace_bundle()`;
- selected document IDs from ranked chain witness bundles;
- slot candidate count;
- mapping count;
- ranked chain count;
- witness-complete chain count.

---

## 6. TraceBundle Validation

`validate_trace_bundle()` checks:

- required top-level fields exist;
- legacy top-level `trace_result` is absent;
- every MAP-TRANSFORM mapping references known candidates;
- every mapping carries source and target witness bundles;
- ranked chains are sorted by rank;
- ranked chains reference known candidate and mapping IDs;
- every ranked chain is witness-complete;
- `eg_delta` has no `DERIVES_FROM` cycle.

The Neo4j verifier also calls this validator:

```powershell
python pipeline_test\trace\neo4j\verify_eg_rg.py results\trace\trace_bundle.json
```

---

## 7. Graph Writers

File: `writers.py`

TRACE uses the `GraphWriter` protocol:

```python
create_node(labels, properties)
create_edge(from_uid, to_uid, rel_type, properties)
node_exists(uid)
```

Implementations:

- `InMemoryGraphWriter`: test and JSON generation;
- `MemgraphWriter`: Memgraph backend;
- `Neo4jWriter`: Neo4j backend.

Production writers use Cypher `MERGE` keyed by deterministic `uid`.
They intentionally do not client-side skip an already-seen node,
because a later real node may need to upsert over an earlier shadow
node. The in-memory writer can short-circuit because it is append-only.

---

## 8. Determinism

TRACE IDs are deterministic. The helpers in `trace2.py` and
`bundle_builder.py` use `uuid5(TRACE_NS, seed)` and stable JSON hashes.

Consequences:

- rerunning TRACE on the same AlignBundle produces the same UID set;
- changed input changes only affected sections;
- downstream bundles can safely reference `trace_bundle_id`,
  `candidate_id`, `mapping_id`, and `chain_id`.

---

## 9. Running TRACE

### Generate trace_bundle.json and optionally load Neo4j

```powershell
python pipeline_test\trace\neo4j\run_trace_to_neo4j.py results\align\align_bundle.json
```

Defaults:

```text
input  : results/align/align_bundle.json
output : results/trace/trace_bundle.json
Neo4j  : neo4j://127.0.0.1:7687, user neo4j, password testtest
```

The runner writes JSON before attempting Neo4j load. If Neo4j is not
available, the JSON TraceBundle can still be generated.

### Verify TraceBundle and EG/RG shape

```powershell
python pipeline_test\trace\neo4j\verify_eg_rg.py results\trace\trace_bundle.json
python -m unittest pipeline_test.trace.test_trace_bundle_contract
```

### Continue downstream

```powershell
python pipeline\conflict\save_conflict_output.py
python pipeline\construct\construct.py
python pipeline\explain\explain.py
```

These scripts read the current `trace_bundle.json` and write:

```text
results/conflict/conflict_bundle.json
results/construct/construct_bundle.json
results/explain/explain_bundle.json
results/explain/explain_output.txt
```

---

## 10. Editing Notes

1. Keep `Trace.execute()` as the EG/RG materializer. The
   chain-first builder packages its output; it does not replace the
   graph write phases.
2. Do not add random UUIDs in TRACE output paths. Use deterministic
   seeds based on intent, slot, candidate, mapping, chain, or content
   hash.
3. If witness construction changes, check `_build_slot_candidates()`;
   it depends on witness `content_hash` resolving to EG Testimony.
4. If MAP-TRANSFORM labels change, update `MAPPING_LABEL_SET`, tests,
   CONFLICT's lossy-transform handling, and CONSTRUCT limitations.
5. If ranked-chain scoring changes, update `CHAIN_RANKING_WEIGHTS`
   and downstream tests that assert chain selection behavior.
6. Preserve person/role/population category distinctions from ALIGN.
   TRACE should not relabel titles or populations as people.
7. Always run `validate_trace_bundle()` before loading into Neo4j or
   using the bundle downstream.

---

## 11. Key Tests

```powershell
python -m unittest pipeline_test.trace.test_trace_bundle_contract
python -m unittest pipeline_test.conflict.test_trace_bundle_conflict
python -m unittest pipeline_test.construct.test_chain_construct
python -m unittest pipeline_test.explain.test_explain_bundle
```

For a full smoke path from existing AlignBundle:

```powershell
python pipeline_test\trace\neo4j\run_trace_to_neo4j.py results\align\align_bundle.json
python pipeline_test\trace\neo4j\verify_eg_rg.py results\trace\trace_bundle.json
python pipeline\conflict\save_conflict_output.py
python pipeline\construct\construct.py
python pipeline\explain\explain.py
```
