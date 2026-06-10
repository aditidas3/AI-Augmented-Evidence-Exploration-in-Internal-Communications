import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { explainCounts, fallbackText, visibleTab } from './ExplainView.js';

const model = {
  explanation: {
    provenance_narratives: [{ narrative: 'The selected span contributes to the evidence map.' }],
    decision_explanations: [{ rationale: 'CONSTRUCT selected the highest effective chain.' }],
    uncertainty_entries: [{ description: 'QUALIFIER_DROP affected interpretation.' }],
    conflict_explanations: [{ explanation: 'One candidate refutes another.' }],
    evidence_chain: ['TRACE: rank_chains'],
    tethers: [{ sentence_index: 0 }],
    tether_failures: [{ reason: 'No witness path citation.' }]
  }
};

assert.deepEqual(explainCounts(model), {
  provenance: 1,
  decisions: 1,
  uncertainty: 1,
  conflicts: 1,
  tethers: 1,
  tetherFailures: 1
});

assert.equal(visibleTab('all', 'tethers'), true);
assert.equal(visibleTab('uncertainty', 'tethers'), false);
assert.equal(fallbackText({ rationale: 'CONSTRUCT selected the highest effective chain.' }), 'CONSTRUCT selected the highest effective chain.');

const source = readFileSync(new URL('./ExplainView.js', import.meta.url), 'utf8');
assert.match(source, /Provenance/);
assert.match(source, /Decisions/);
assert.match(source, /Uncertainty/);
assert.match(source, /Tethers/);
assert.match(source, /Open finding/);
