import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { LeftNav } from './LeftNav.js';
import { Overview } from './Overview.js';
import { answerCitationHealth } from '../domain/workbenchUx.js';

function collectText(node) {
  if (node == null || typeof node === 'boolean') return '';
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(collectText).join(' ');
  if (typeof node.type === 'function') return collectText(node.type(node.props || {}));
  if (typeof node === 'object') return collectText(node.props?.children);
  return '';
}

function collectClasses(node) {
  if (node == null || typeof node === 'boolean') return [];
  if (typeof node === 'string' || typeof node === 'number') return [];
  if (Array.isArray(node)) return node.flatMap(collectClasses);
  if (typeof node.type === 'function') return collectClasses(node.type(node.props || {}));
  if (typeof node === 'object') {
    return [node.props?.className, ...collectClasses(node.props?.children)].filter(Boolean);
  }
  return [];
}

const model = {
  run_id: 'run-ui',
  overview: {
    question_text: 'How did the organization respond?',
    answer_text: 'EXPLAIN final investigator-facing answer.',
    confidence_score: 0.73,
    confidence_label: 'MODERATE',
    selected_chain_id: 'chain-1',
    warnings: ['review lossy extraction'],
    stats: {}
  },
  alignment: {
    witnesses: [{ witness_id: 'wit-1' }],
    slot_bindings: []
  },
  trace: {
    ranked_chains: [{ chain_id: 'chain-1', witness_complete: true }],
    eg_delta: { nodes: [], edges: [] },
    rg_trace: { nodes: [], edges: [] }
  },
  conflicts: {
    edges: [{ edge_id: 'conflict-1' }]
  },
  answer: {
    findings: [{ finding_id: 'finding-1' }],
    citations: [
      {
        sentence_index: 0,
        sentence_text: 'EXPLAIN final investigator-facing answer.',
        eg_object_ids: ['wit-1']
      }
    ],
    limitations: [{ description: 'Selected chain uses lossy MAP-TRANSFORM label QUALIFIER_DROP.' }],
    graph: { nodes: [], edges: [] }
  },
  explanation: {
    citations: ['doc-1', 'wit-1'],
    tethers: [{ sentence_index: 0 }],
    uncertainty_entries: [{ entry_id: 'uncertainty-1' }],
    tether_failures: []
  },
  lineage: {
    nodes: [
      { operator: 'ALIGN', bundle_id: 'align-1' },
      { operator: 'TRACE', bundle_id: 'trace-1' },
      { operator: 'CONSTRUCT', bundle_id: 'construct-1' },
      { operator: 'EXPLAIN', bundle_id: 'explain-1' }
    ]
  }
};

const nav = LeftNav({ activeView: 'answer', setActiveView: () => {}, model, conflictReviews: {}, selection: null });
assert.match(collectText(nav), /Final answer/);
assert.doesNotMatch(collectText(nav), /\b\d+\b/);
assert(collectClasses(nav).some((className) => String(className).includes('sidebar-item--active')));

const overviewText = collectText(Overview({ model, explorationContext: null, onOpenRun: () => {}, onNavigate: () => {} }));
assert.match(overviewText, /Pipeline coverage/);
assert.match(overviewText, /Next actions/);
assert.match(overviewText, /Answer health/);
assert.match(overviewText, /Final answer/);
assert.match(overviewText, /ALIGN/);
assert.match(overviewText, /EXPLAIN/);

assert.deepEqual(answerCitationHealth(model), {
  total: 1,
  placed: 1,
  unplaced: 0,
  unresolvedWitnessContexts: 0
});

const answerSource = readFileSync(new URL('./AnswerView.js', import.meta.url), 'utf8');
const utilitySource = readFileSync(new URL('../utility.css', import.meta.url), 'utf8');
assert.match(answerSource, /Investigator answer/);
assert.match(answerSource, /Source coverage/);
assert.match(answerSource, /Limitations/);
assert.match(answerSource, /Final answer vs CONSTRUCT answer/);
assert.match(answerSource, /bg-slate-900 text-white/);
assert.match(answerSource, /bg-sky-600 text-white/);
assert.match(utilitySource, /\.bg-slate-900\s*\{/);
assert.match(utilitySource, /\.bg-sky-600\s*\{/);
