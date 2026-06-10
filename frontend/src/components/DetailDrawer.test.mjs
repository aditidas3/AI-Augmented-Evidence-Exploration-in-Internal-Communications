import assert from 'node:assert/strict';

import { DetailDrawer } from './DetailDrawer.js';

const model = {
  run_id: 'run-test',
  overview: {
    answer_text: 'Final answer text.'
  },
  witnesses: {
    by_id: {}
  },
  trace: {
    candidate_by_id: {},
    eg_delta: { nodes: [], edges: [] },
    rg_trace: { nodes: [], edges: [] }
  },
  alignment: {
    slot_bindings: []
  },
  conflicts: {
    workups: []
  }
};

assert.doesNotThrow(() => DetailDrawer({
  model,
  selection: {
    kind: 'graph',
    item: {
      lens: 'rg',
      nodes: [{ id: 'node-1', type: 'Claim', properties: { statement: 'Claim node' } }],
      edges: []
    }
  },
  onClose: () => {}
}));

assert.doesNotThrow(() => DetailDrawer({
  model,
  selection: {
    kind: 'finding',
    item: {
      finding_id: 'finding-1',
      display_id: 'F1',
      statement: 'Finding statement.',
      slot_type: 'WHAT',
      supporting_object_ids: [],
      conflict_edge_ids: []
    }
  },
  onClose: () => {}
}));

assert.doesNotThrow(() => DetailDrawer({
  model,
  selection: {
    kind: 'witness',
    item: {
      slot: { slot_id: 'slot-1', slot_type: 'WHO' },
      associatedValue: { surface: 'Associated value', category: 'entity', confidence: 0.8 },
      witness: {
        witness_id: 'wit-1',
        quality: 'HIGH',
        mention: { surface: 'Witness surface' },
        anchor: { raw_text: 'Witness surface appears here.', artifact_id: 'doc-1', metadata: {} }
      }
    }
  },
  onClose: () => {}
}));

assert.doesNotThrow(() => DetailDrawer({
  model,
  selection: {
    kind: 'answer',
    item: {
      object_id: 'answer:run-test',
      text: 'Answer drawer text.',
      confidence: 0.8
    }
  },
  onClose: () => {}
}));

assert.doesNotThrow(() => DetailDrawer({
  model,
  selection: {
    kind: 'conflict',
    item: {
      edge_id: 'conflict-1',
      stance: 'REFUTES',
      confidence: 0.7,
      description: 'Conflict description.',
      source_object_id: 'wit-a',
      target_object_id: 'wit-b',
      chain_ids: []
    }
  },
  onClose: () => {}
}));
