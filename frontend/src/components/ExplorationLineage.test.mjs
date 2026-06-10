import test from 'node:test';
import assert from 'node:assert/strict';

import { ExplorationLineage } from './ExplorationLineage.js';

function collectText(node) {
  if (node == null || typeof node === 'boolean') return '';
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(collectText).join(' ');
  if (typeof node.type === 'function') return collectText(node.type(node.props || {}));
  if (typeof node === 'object') return collectText(node.props?.children);
  return '';
}

test('ExplorationLineage hides comparison/composite and exploration move panels', () => {
  const view = ExplorationLineage({
    context: {
      workspace: { name: 'IDL workspace' },
      question: { text: 'What happened?', question_id: 'question-1' },
      scope: { name: 'Current scope', collection_ids: ['collection-1'], predicates: [] },
      run: { run_id: 'run-1', status: 'completed', lineage_kind: 'run' },
      related_runs: [],
      comparisons: [{ comparison_id: 'comparison-1' }],
      composites: [{ composite_id: 'composite-1' }],
      exploration_affordances: [
        { kind: 'same_question_new_scope', enabled: true, label: 'Run same question on a changed scope' }
      ]
    },
    onOpenRun: () => {}
  });

  const text = collectText(view);
  assert.match(text, /Exploration lineage/);
  assert.doesNotMatch(text, /Comparison \/ composite records/);
  assert.doesNotMatch(text, /Available exploration moves/);
  assert.doesNotMatch(text, /Run same question on a changed scope/);
});
