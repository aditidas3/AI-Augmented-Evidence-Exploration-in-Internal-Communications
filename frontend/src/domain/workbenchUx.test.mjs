import assert from 'node:assert/strict';

import {
  answerCitationHealth,
  buildViewBadges,
  filterConflictWorkups,
  filterSlotBindings,
  filterTraceChains,
  groupCitationSentences
} from './workbenchUx.js';

const model = {
  overview: {
    answer_text: 'Sentence one is supported. Sentence two is not.',
    warnings: ['warn']
  },
  alignment: {
    slot_bindings: [
      {
        slot_id: 'slot-who',
        slot_type: 'WHO',
        confidence: 0.9,
        quality: 'HIGH',
        description: 'Responsible team',
        value: [{ surface: 'Walgreens legal team' }],
        witnesses: [
          { witness_id: 'wit-2', quality: 'LOW', score: 0.2, anchor: { artifact_id: 'doc-b' }, mention: { surface: 'team' } },
          { witness_id: 'wit-1', quality: 'HIGH', score: 0.9, anchor: { artifact_id: 'doc-a' }, mention: { surface: 'legal team' } }
        ]
      },
      {
        slot_id: 'slot-what',
        slot_type: 'WHAT',
        confidence: 0.4,
        quality: 'LOW',
        description: 'Policy',
        value: [{ surface: 'Dispensing policy' }],
        witnesses: []
      }
    ],
    witnesses: [{}, {}]
  },
  trace: {
    ranked_chains: [
      { chain_id: 'chain-1', rank: 1, score: 0.9, slot_coverage: 1, witness_complete: true, nodes: [] },
      { chain_id: 'chain-2', rank: 2, score: 0.4, slot_coverage: 0.4, witness_complete: false, nodes: [] }
    ],
    rg_trace: { nodes: [{ id: 'rg' }], edges: [] },
    eg_delta: { nodes: [{ id: 'eg' }], edges: [] }
  },
  conflicts: {
    edges: [{ edge_id: 'conflict-1' }],
    workups: [
      {
        edge: { edge_id: 'conflict-1', confidence: 0.8, stance: 'REFUTES' },
        assessment: { label: 'Refutes', rationale: 'Different facts' },
        source: { candidate: { surface: 'A' } },
        target: { candidate: { surface: 'B' } }
      }
    ]
  },
  answer: {
    findings: [
      { finding_id: 'f1', evidence_strength: 0.9, conflict_edge_ids: [] },
      { finding_id: 'f2', evidence_strength: 0.3, conflict_edge_ids: ['c1'] }
    ],
    citations: [
      { sentence_index: 0, sentence_text: 'Sentence one is supported.', eg_object_ids: ['wit-1'] },
      { sentence_index: 1, sentence_text: 'Missing sentence.', eg_object_ids: [] }
    ],
    graph: { nodes: [{ id: 'answer' }], edges: [] },
    limitations: []
  },
  explanation: {
    tethers: [{}],
    uncertainty_entries: [{}],
    tether_failures: [{}],
    citations: []
  }
};

assert.deepEqual(answerCitationHealth(model), {
  total: 2,
  placed: 1,
  unplaced: 1,
  unresolvedWitnessContexts: 1
});

assert.deepEqual(
  groupCitationSentences(model).map((item) => item.label),
  ['Sentence 1', 'Sentence 2']
);

assert.equal(buildViewBadges(model).answer.attention, 3);
assert.equal(buildViewBadges(model).findings.attention, 2);
assert.equal(buildViewBadges(model).graphs.count, 3);

const filteredSlots = filterSlotBindings(model.alignment.slot_bindings, {
  query: 'legal',
  slotType: 'WHO',
  quality: 'ALL',
  minConfidence: 0
});
assert.equal(filteredSlots.length, 1);
assert.deepEqual(filteredSlots[0].witnesses.map((witness) => witness.witness_id), ['wit-1', 'wit-2']);

assert.deepEqual(
  filterTraceChains(model.trace.ranked_chains, { filter: 'incomplete', sortKey: 'score' }).map((chain) => chain.chain_id),
  ['chain-2']
);

assert.deepEqual(
  filterConflictWorkups(model.conflicts.workups, {}, { reviewLabel: 'ALL', relationFilter: 'semantic', sortKey: 'confidence' }).map((workup) => workup.edge.edge_id),
  ['conflict-1']
);
