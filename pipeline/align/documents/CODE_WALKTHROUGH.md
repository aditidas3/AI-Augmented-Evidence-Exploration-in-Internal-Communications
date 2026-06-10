# ALIGN Code Walkthrough

This document maps the current `pipeline/align/` implementation. It is
written for readers who need to understand what ALIGN retrieves, how it
uses Solr, Qdrant, and Neo4j, what it emits for TRACE, and which files
own each piece of the behavior.

ALIGN is the query-time retrieval and grounding operator:

```text
Intent JSON -> ALIGN -> AlignBundle -> TRACE -> TraceBundle -> CONFLICT/CONSTRUCT/EXPLAIN
                 |
                 +-> Solr lexical retrieval
                 +-> Qdrant semantic retrieval
                 +-> Neo4j KG0 graph retrieval / backfill
```

The code is the source of truth. Use this walkthrough as a map, then
open the referenced modules when behavior matters.

---

## 1. Package Layout

```text
pipeline/align/
├── engine.py                         AlignEngine orchestrator
├── core_types.py                     Dataclasses and intent parser
├── collections-tobacco-ontology.yaml Collection ontology fixture
├── phases/
│   ├── shared.py                     Shared phase imports and helpers
│   ├── phase0.py                     Intent validation, scope, graph skeleton, retrieval query
│   ├── phase1.py                     Solr lexical + Qdrant semantic retrieval and fusion
│   ├── phase2.py                     Artifact selection with intent relevance gating
│   ├── phase3.py                     Anchor and mention extraction
│   ├── phase4.py                     Entity and link hypotheses
│   ├── phase5.py                     Subgraph discovery
│   └── phase6.py                     Slot binding and witness construction
├── infrastructure/
│   ├── index_facade.py               Solr, Neo4j, Qdrant, embedding facade
│   ├── adapters.py                   Family-specific artifact adapters
│   └── contract_check.py             KG0 schema drift checks
├── relevance/
│   └── intent_relevance.py           Transparent intent relevance scoring
├── retrieval/helpers.py              Retrieval term and metadata helpers
├── binding/                          Slot binding confidence helpers
├── graph/                            Graph/Cypher helper functions
├── linking/                          Entity and link scoring helpers
├── search/                           Subgraph search helpers
├── selection/                        Artifact selection helpers
├── utils/                            Serialization, text, and temporal utilities
└── documents/CODE_WALKTHROUGH.md     This file
```

`AlignConfig` lives in `pipeline/operators/configs.py`. It owns the
Neo4j, Qdrant, embedding, and phase tuning knobs.

---

## 2. Input and Output Shape

### Input: IntentObject

`parse_intent_object()` in `core_types.py` parses the raw intent JSON
into:

```text
IntentObject
├── header           intent_id, question_id, question_text
├── entity_hints     typed hint surfaces and normalized aliases
├── scope_spec       collection, artifact family, time, feature filters
├── retrieval_spec   query_text, query_expansions, top_k_lex, top_k_sem, fusion_method
├── slot_spec        slots and optional GraphSpec
└── diagnostics      upstream notes
```

The optional GraphSpec describes hard and soft variables/edges that
Phase 5 later tries to satisfy.

### Output: AlignBundle

The canonical test runner `pipeline_test/align/neo4j/gen_align_bundle.py`
writes:

```text
results/align/align_bundle.json
```

`align_bundle.json` has the shape:

```python
{
    "result": AlignResult-as-json,
    "corpus_stats": {
        "artifact_count": ...,
        "node_count": ...,
        "edge_count": ...,
        "families": ...
    }
}
```

TRACE consumes `result.artifact_set`, `all_anchors`, `all_mentions`,
`all_witnesses`, `subgraphs`, `slot_bindings`, `entity_hypotheses`,
`link_hypotheses`, `suppressed_mentions`, and `replay_plan`.

---

## 3. Current Retrieval Architecture

ALIGN no longer uses Postgres lexical retrieval. The live retrieval
stack is:

```text
Solr lexical retrieval       -> CandidateArtifact.lex_score / lex_rank
Qdrant semantic retrieval    -> CandidateArtifact.sem_score / sem_rank
IndexFacade.union_and_score  -> CandidateArtifact.fused_score
```

### Solr lexical retrieval

`phase1.py` calls `IndexFacade.lexical_retrieve()`, which issues an
eDisMax query to the configured Solr collection. Strict scope filters
from Phase 0 are passed as Solr filter queries:

- `solr_query` is built from the intent query text, query expansions,
  and graph variable hints.
- `scope.solr_fqs` carries strict collection, family, date, and feature
  filters.
- Solr document fields such as `title`, `subject`, `body`, `family`,
  `date`, and `collection` become candidate metadata.
- Lexical candidates are deduplicated by artifact id and ranked by
  Solr score.

### Qdrant semantic retrieval

`phase0.py` embeds the current query text, plus up to five query
expansions, through `IndexFacade.embedder`. The resulting vector is
stored on `CompiledRetrievalQuery.qdrant_vector`.

`index_facade.py` owns the production implementation:

- `EmbeddingService` uses `sentence-transformers/all-mpnet-base-v2`.
- `QdrantIndex` queries the configured Qdrant collection.
- `IndexFacade.semantic_retrieve()` converts page-level Qdrant hits
  into document-level `CandidateArtifact`s using `payload.artifact_id`.
- Qdrant payload filters come from Phase 0 scope compilation and are
  rechecked in memory by `scope.evaluate()`.

If embedding or Qdrant retrieval fails, Phase 1 logs a warning and
continues with Solr lexical candidates.

### Fusion

When semantic candidates exist, `IndexFacade.union_and_score()` fuses
lexical and semantic rankings. RRF is the default method. The fused
candidate list is then filtered by `AlignConfig.min_retrieval_score`.

Neo4j is still required for KG0 graph traversal, artifact-name backfill
when Solr lacks a title/name, and later ALIGN phases.

---

## 4. Execution Flow

`AlignEngine.execute()` runs:

```text
Phase 0 -> Phase 1 -> Phase 2 -> Phase 3 -> Phase 4 -> Phase 5 -> Phase 6
```

Before returning, the engine runs post-hoc soundness checks:

- scope soundness: every selected artifact still satisfies the
  compiled scope predicate;
- graph spec soundness: hard GraphSpec variables must have surviving
  bindings when enforcement is enabled.

The engine also caches and runs `contract_check.py` unless
`config.skip_contract_check` is set.

---

## 5. Phase-by-Phase

### Phase 0: Intent validation and compilation

File: `phase0.py`

Responsibilities:

- validate required fields: `intent_id`, `question_text`, retrieval
  query text, slots, and GraphSpec internal references;
- compile `ScopeSpec` into Solr filter queries, Neo4j Cypher clauses,
  and Qdrant payload filters;
- compile GraphSpec variables and edges into a bounded graph skeleton;
- build `CompiledRetrievalQuery`.

Important current behavior:

- `qdrant_vector` is populated only if both Qdrant and the embedder are
  available.
- `solr_query` feeds Phase 1 lexical retrieval.
- `entity_hint_terms` and `required_hint_terms` are retained for
  diagnostics and compatibility.

### Phase 1: Scoped retrieval

File: `phase1.py`

Responsibilities:

- run Solr lexical retrieval under scope;
- run Qdrant semantic retrieval when `qdrant_vector` exists;
- fuse lexical and semantic candidates;
- backfill missing artifact names from Neo4j;
- keep only candidates above `min_retrieval_score`.

Each candidate carries:

```text
artifact_id, family, artifact_name,
lex_score, sem_score, fused_score,
lex_rank, sem_rank,
metadata fields returned by Solr and optional Qdrant payload metadata
```

### Phase 2: Artifact selection

Files: `phase2.py`, `selection/artifact_selection.py`,
`intent_relevance.py`

Phase 2 greedily selects up to `config.k_artifacts` candidates. The
score is the artifact selection score plus an intent relevance bonus:

```text
selection_score =
    retrieval/family/diversity score
  + intent_relevance_weight * intent_relevance.selection_bonus
```

`intent_relevance.py` is intentionally transparent and lexical. It
uses:

- hard entity coverage;
- required hard entity coverage;
- query/topic/focus term coverage;
- time filter match;
- family match;
- scope drift penalties.

Candidates are rejected before selection when they:

- miss required hard entities;
- violate time intent;
- look like scope drift;
- fall below `min_intent_relevance_score`.

The constructibility repair pass can add artifacts back in when
required cross-family bridge evidence is missing.

### Phase 3: Anchor and mention extraction

Files: `phase3.py`, `adapters.py`

Phase 3 materializes structural anchors and semantic mentions for each
selected artifact. Adapters handle family-specific traversal:

- email/thread;
- document/PDF;
- presentation;
- KG0-native fallback.

The mention taxonomy includes role/person/org/event/risk/policy/drug
categories and now includes `ENTITY_POPULATION`. The important
distinction for downstream correctness is:

- person means an actual person name;
- role means a title or group label such as manager, supervisors, team
  members, district leaders;
- population means a referenced group/population being discussed, not
  a named person.

Low-confidence or noisy mentions are preserved in
`suppressed_mentions` for audit rather than silently erased.

### Phase 4: Entity and link hypotheses

File: `phase4.py`

Responsibilities:

- cluster mentions into `EntityHypothesis` records by category and
  normalized surface;
- preserve KG0 entity IDs when present;
- find bounded KG0 paths between grounded hypotheses;
- emit `LinkHypothesis` records with witnesses.

The clustering logic avoids treating generic role labels as people.
Population mentions remain in their own category so Phase 6 and TRACE
do not convert populations into person claims.

### Phase 5: Subgraph discovery

File: `phase5.py`

Responsibilities:

- bind GraphSpec variables with beam search;
- enforce hard edge constraints;
- score coherence, temporal consistency, diversity, and bridge
  support;
- build `frame_witness` records and snapshots.

If the intent has no GraphSpec, Phase 5 emits a trivial subgraph so
TRACE still receives a valid frame-level witness structure.

### Phase 6: Slot binding

File: `phase6.py`

Responsibilities:

- map slots to GraphSpec variables;
- gather witness candidates from subgraphs;
- apply noise filtering;
- emit `SlotBinding` and final `Witness` records.

The `all_witnesses` list and slot-level witness lists are the direct
input to TRACE's testimony and TraceBundle candidate construction.

---

## 6. IndexFacade Details

File: `index_facade.py`

The facade owns three live backends:

```text
Neo4jStore       KG0 graph, lexical retrieval, graph traversal
QdrantIndex      semantic vector retrieval
EmbeddingService query embedding
```

Constructor injection lets tests provide fakes for any backend. By
default:

```python
IndexFacade(config)
```

creates `QdrantIndex(config)`, `EmbeddingService(config)`, and
`Neo4jStore(config)`.

The facade's most important methods are:

- `semantic_retrieve(query, scope)`;
- `union_and_score(lex_results, sem_results, method)`;
- Neo4j traversal methods exposed through `self.neo4j`.

---

## 7. Configuration

Main knobs from `AlignConfig`:

| Field | Used by | Meaning |
| --- | --- | --- |
| `neo4j` | Phase 1, 4, 5 | KG0 lexical and graph store |
| `qdrant` | Phase 0, 1 | semantic vector store |
| `embedding` | Phase 0 | query embedding model and dimensions |
| `rrf_k` | fusion | reciprocal-rank fusion constant |
| `min_retrieval_score` | Phase 1 | final retrieval score floor |
| `k_artifacts` | Phase 2 | max selected artifacts |
| `family_coverage_weight` | Phase 2 | required family bonus |
| `diversity_weight` | Phase 2 | same-family/collection penalty |
| `intent_relevance_weight` | Phase 2 | relevance bonus multiplier |
| `min_intent_relevance_score` | Phase 2 | candidate relevance floor |
| `mention_confidence_threshold` | Phase 3 | mention confidence floor |
| `entity_similarity_threshold` | Phase 4 | entity clustering threshold |
| `beam_width` | Phase 5 | graph binding beam width |
| `max_subgraphs` | Phase 5 | retained subgraph cap |
| `min_slot_confidence` | Phase 6 | slot confidence floor |
| `skip_contract_check` | engine | disable KG0 contract check |

The default embedding model is
`sentence-transformers/all-mpnet-base-v2`, dimension `768`.

---

## 8. Running ALIGN

### Generate the current test AlignBundle

```powershell
python pipeline_test\align\neo4j\gen_align_bundle.py corrected_intent.json
```

The generator uses the configured Neo4j KG0 source and the live
Qdrant semantic backend through `IndexFacade`.

### From Python

```python
from pipeline.align.engine import AlignEngine
from pipeline.align.infrastructure.index_facade import IndexFacade
from pipeline.operators.configs import AlignConfig

config = AlignConfig()
index = IndexFacade(config)
engine = AlignEngine(config=config, index=index)
try:
    result = engine.execute_from_raw(intent_dict)
finally:
    engine.close()
```

### Directly into TRACE

```python
from pipeline.trace.trace2 import Trace
from pipeline.trace.config import TraceConfig
from pipeline.trace.writers import InMemoryGraphWriter

bundle = {"result": align_result_json, "corpus_stats": {}}
eg = InMemoryGraphWriter()
rg = InMemoryGraphWriter()
trace_result = Trace(eg=eg, rg=rg, bridge=eg, cfg=TraceConfig()).execute(bundle)
```

For the canonical JSON + Neo4j TRACE run, use:

```powershell
python pipeline_test\trace\neo4j\run_trace_to_neo4j.py results\align\align_bundle.json
```

---

## 9. Editing Notes

1. Do not reintroduce Postgres lexical retrieval. Lexical retrieval is
   Neo4j KG0; semantic retrieval is Qdrant.
2. Keep Phase 1 scope enforcement in both stores: Cypher WHERE for
   Neo4j and payload filters plus in-memory `scope.evaluate()` for
   Qdrant candidates.
3. If mention categories change, update the ontology/schema loader,
   Phase 3 extraction, Phase 4 clustering expectations, and TRACE
   category handling together.
4. Treat person/role/population as separate semantic categories.
   Titles and groups are not people.
5. If `CandidateArtifact.metadata.document_text` changes, rerun Phase
   2 tests because intent relevance depends on it.
6. Preserve deterministic hashes in `replay_plan`; TRACE uses them to
   build stable downstream identifiers.

---

## 10. Tests to Run After ALIGN Changes

```powershell
python -m unittest pipeline_test.align.test_retrieval_backends
```

For end-to-end bundle generation:

```powershell
python pipeline_test\align\neo4j\gen_align_bundle.py corrected_intent.json
python pipeline_test\trace\neo4j\run_trace_to_neo4j.py results\align\align_bundle.json
```
