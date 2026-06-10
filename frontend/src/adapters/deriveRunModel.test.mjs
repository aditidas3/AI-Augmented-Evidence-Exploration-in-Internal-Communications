import assert from 'node:assert/strict';

import { deriveRunModel } from './deriveRunModel.js';

const raw = {
  question: { text: 'Who led the response?' },
  outputs: {
    align: { result: { slot_bindings: [] } },
    trace: { trace_bundle_id: 'trace-1', ranked_chains: [], slot_candidates: {} },
    conflict: { conflict_bundle_id: 'conflict-1', conflict_structure: { edges: {} } },
    construct: {
      construct_bundle_id: 'construct-1',
      selected_chain_id: 'chain-1',
      ans_bundle: {
        answer_text: 'CONSTRUCT draft answer.',
        confidence: 0.42,
        selected_chain_id: 'chain-1',
        citation_map: [
          {
            sentence_index: 0,
            sentence_text: 'CONSTRUCT citation text',
            eg_object_ids: ['wit-construct']
          }
        ],
        findings: [{ finding_id: 'finding-1' }],
        limitations: [{ description: 'construct limitation' }]
      }
    },
    explain: {
      explain_bundle_id: 'explain-1',
      answer_text: 'EXPLAIN investigator-facing answer.',
      confidence_score: 0.73,
      confidence_label: 'MODERATE',
      warnings: ['explain warning'],
      stats: { tethers: 1 },
      citations: ['doc-1', 'wit-1'],
      explain_bundle: {
        summary: 'EXPLAIN summary.',
        selected_chain_id: 'chain-1',
        construct_answer_text: 'CONSTRUCT draft answer.',
        construct_citation_map: [
          {
            sentence_index: 0,
            sentence_text: 'CONSTRUCT citation text',
            eg_object_ids: ['wit-construct']
          }
        ],
        citation_map: [
          {
            sentence_index: 0,
            sentence_text: 'EXPLAIN investigator-facing answer.',
            eg_object_ids: ['wit-1'],
            witness_paths: [['doc-1', 'anch-1', 'ment-1', 'wit-1']]
          }
        ],
        provenance_narratives: [{ finding_id: 'finding-1', narrative: 'narrative' }],
        conflict_explanations: [{ conflict_edge_id: 'conflict-1', explanation: 'conflict explanation' }],
        decision_explanations: [{ decision_node_id: 'decision-1' }],
        uncertainty_map: [{ entry_id: 'uncertainty-1' }],
        tether_map: [
          {
            sentence_index: 0,
            sentence_text: 'EXPLAIN investigator-facing answer.',
            tethered_to: ['doc-1', 'anch-1', 'ment-1', 'wit-1']
          }
        ],
        tether_failures: []
      }
    }
  }
};

const model = deriveRunModel(raw);

assert.equal(model.overview.answer_text, 'EXPLAIN investigator-facing answer.');
assert.equal(model.overview.confidence_score, 0.73);
assert.deepEqual(
  model.answer.citations.map((citation) => citation.sentence_text),
  ['EXPLAIN investigator-facing answer.']
);
assert.equal(model.answer.construct_answer_text, 'CONSTRUCT draft answer.');
assert.deepEqual(
  model.answer.construct_citation_map.map((citation) => citation.sentence_text),
  ['CONSTRUCT citation text']
);
assert.equal(model.explanation.summary, 'EXPLAIN summary.');
assert.equal(model.explanation.provenance_narratives.length, 1);
assert.equal(model.explanation.conflict_explanations.length, 1);
assert.equal(model.explanation.uncertainty_entries.length, 1);
assert.equal(model.explanation.tethers.length, 1);
assert.deepEqual(model.explanation.citations, ['doc-1', 'wit-1']);
