import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { defaultCollectionIds, RunSummary, ScopeSummary } from './InvestigationComposer.js';

function collectText(node) {
  if (node == null || typeof node === 'boolean') return '';
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(collectText).join(' ');
  if (typeof node === 'object') return collectText(node.props?.children);
  return '';
}

function findByType(node, type) {
  if (node == null || typeof node === 'boolean') return [];
  if (typeof node === 'string' || typeof node === 'number') return [];
  if (Array.isArray(node)) return node.flatMap((child) => findByType(child, type));
  const current = node.type === type ? [node] : [];
  return current.concat(findByType(node.props?.children, type));
}

assert.deepEqual(
  defaultCollectionIds([
    { collection_id: 'idl_all', name: 'Industry Documents Library' },
    { collection_id: 'other', name: 'Other collection' }
  ]),
  ['idl_all']
);

assert.deepEqual(defaultCollectionIds([{ collection_id: 'other', name: 'Other collection' }]), []);

const events = [];
const scope = {
  scope_id: 'scope-test',
  name: 'Reusable scope',
  collection_ids: ['idl_all'],
  predicates: []
};
const summary = ScopeSummary({
  scope,
  onUse: (nextScope) => events.push(`use:${nextScope.scope_id}`),
  onDelete: (nextScope) => events.push(`delete:${nextScope.scope_id}`)
});

assert.match(collectText(summary), /Reusable scope/);
assert.match(collectText(summary), /Delete/);

const buttons = findByType(summary, 'button');
assert.equal(buttons.length, 2);
buttons[0].props.onClick();
buttons[1].props.onClick({ stopPropagation: () => events.push('stop') });
assert.deepEqual(events, ['use:scope-test', 'stop', 'delete:scope-test']);

const composerSource = readFileSync(new URL('./InvestigationComposer.js', import.meta.url), 'utf8');
const stylesSource = readFileSync(new URL('../styles.css', import.meta.url), 'utf8');
const indexSource = readFileSync(new URL('../../index.html', import.meta.url), 'utf8');
assert.doesNotMatch(composerSource, /LaunchAction/);
assert.doesNotMatch(composerSource, /composer-launch-action/);
assert.match(composerSource, /Launch investigation/);
assert.doesNotMatch(composerSource, /history-tabs__count/);
assert.match(composerSource, /launch-primary-button/);
assert.match(composerSource, /bg-slate-950/);
assert.match(composerSource, /text-white/);
assert.match(composerSource, /disabled:bg-slate-800/);
assert.match(stylesSource, /\.launch-primary-button\s*\{/);
assert.match(stylesSource, /background-color:\s*#020617/);
assert.match(stylesSource, /\.launch-primary-button:disabled/);
const disabledLaunchRule = stylesSource.match(/\.launch-primary-button:disabled\s*\{[^}]*\}/)?.[0] || '';
assert.match(disabledLaunchRule, /background-color:\s*#1e293b/);
assert.doesNotMatch(disabledLaunchRule, /background(?:-color)?:\s*#fff/i);
assert.match(stylesSource, /\.disabled\\:bg-slate-800:disabled/);
assert.match(indexSource, /styles\.css\?v=css-restore-5/);
assert.match(indexSource, /main\.js\?v=ui-restore-4/);

const run = {
  run_id: 'run-test',
  status: 'completed',
  submitted_at: '2026-05-30T12:00:00Z',
  request: { question: { text: 'What happened?' } }
};
const runSummary = RunSummary({
  run,
  onOpenRun: (runId) => events.push(`open:${runId}`),
  onDelete: (nextRun) => events.push(`delete-run:${nextRun.run_id}`)
});
assert.match(collectText(runSummary), /What happened/);
assert.match(collectText(runSummary), /Delete/);

const runButtons = findByType(runSummary, 'button');
assert.equal(runButtons.length, 2);
runButtons[0].props.onClick();
runButtons[1].props.onClick({ stopPropagation: () => events.push('run-stop') });
assert.deepEqual(events.slice(-3), ['open:run-test', 'run-stop', 'delete-run:run-test']);
