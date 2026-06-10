import assert from 'node:assert/strict';

import {
  filterAndSortFindings,
  groupFindingsBySlot,
  slotTypesForFindings,
  summarizeFindings
} from './FindingsView.js';

const findings = [
  {
    finding_id: 'finding-low',
    display_id: 'F2',
    slot_type: 'WHAT',
    statement: 'A low strength policy finding.',
    evidence_strength: 0.35,
    supporting_object_ids: ['w1'],
    conflict_edge_ids: []
  },
  {
    finding_id: 'finding-high',
    display_id: 'F1',
    slot_type: 'WHO',
    statement: 'Walgreens legal team owned the policy.',
    evidence_strength: 0.92,
    supporting_object_ids: ['w1', 'w2', 'w3'],
    conflict_edge_ids: ['c1']
  },
  {
    finding_id: 'finding-when',
    display_id: 'F3',
    slot_type: 'WHEN',
    statement: 'The policy became effective in early April.',
    confidence: 0.8,
    supporting_object_ids: ['w4'],
    conflict_edge_ids: []
  }
];

assert.deepEqual(slotTypesForFindings(findings), ['WHAT', 'WHEN', 'WHO']);

assert.deepEqual(
  summarizeFindings(findings),
  {
    total: 3,
    slotCount: 3,
    averageStrength: 0.69,
    supportCount: 5,
    conflictCount: 1
  }
);

assert.deepEqual(
  filterAndSortFindings(findings, { query: 'legal team', slotType: 'WHO', sortKey: 'strength' }).map((finding) => finding.finding_id),
  ['finding-high']
);

assert.deepEqual(
  filterAndSortFindings(findings, { query: '', slotType: 'ALL', sortKey: 'strength' }).map((finding) => finding.finding_id),
  ['finding-high', 'finding-when', 'finding-low']
);

assert.deepEqual(
  filterAndSortFindings(findings, { query: '', slotType: 'ALL', sortKey: 'slot' }).map((finding) => finding.finding_id),
  ['finding-low', 'finding-when', 'finding-high']
);

assert.deepEqual(
  filterAndSortFindings([
    { finding_id: 'first-who', slot_type: 'WHO', statement: 'First', evidence_strength: 0.1 },
    { finding_id: 'second-who', slot_type: 'WHO', statement: 'Second', evidence_strength: 0.9 }
  ], { query: '', slotType: 'ALL', sortKey: 'slot' }).map((finding) => finding.finding_id),
  ['first-who', 'second-who']
);

assert.deepEqual(
  groupFindingsBySlot(filterAndSortFindings(findings, { query: '', slotType: 'ALL', sortKey: 'slot' })).map(([slot, items]) => [slot, items.length]),
  [['WHAT', 1], ['WHEN', 1], ['WHO', 1]]
);
