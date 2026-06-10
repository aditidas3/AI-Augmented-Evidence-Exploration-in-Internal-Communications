import test from 'node:test';
import assert from 'node:assert/strict';

import { buildStageProgress, RunStatus, statusBadgeClass } from './RunStatus.js';

function collectText(node) {
  if (node == null || typeof node === 'boolean') return '';
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  const props = node.props || {};
  return [props.children, ...(node.children || [])].flat().map(collectText).join(' ');
}

function findByType(node, type, matches = []) {
  if (!node || typeof node !== 'object') return matches;
  if (node.type === type) matches.push(node);
  const props = node.props || {};
  [props.children, ...(node.children || [])].flat().forEach((child) => findByType(child, type, matches));
  return matches;
}

test('RunStatus renders pending run details and back action', () => {
  const events = [];
  const view = RunStatus({
    run: {
      run_id: 'run-123',
      status: 'running',
      stage: 'align',
      updated_at: '2026-06-02T12:00:00Z',
      request: {
        question: {
          text: 'What evidence supports the claim?'
        }
      }
    },
    onBack: () => events.push('back')
  });

  const text = collectText(view);
  assert.match(text, /run-123/);
  assert.match(text, /running/);
  assert.match(text, /align/);
  assert.match(text, /Intent validation/);
  assert.match(text, /ALIGN/);
  assert.match(text, /CONFLICT/);
  assert.match(text, /What evidence supports the claim\?/);

  const [backButton] = findByType(view, 'button');
  backButton.props.onClick();
  assert.deepEqual(events, ['back']);
});

test('RunStatus distinguishes failed and completed states', () => {
  const failed = RunStatus({
    run: {
      run_id: 'run-failed',
      status: 'failed',
      error: 'Alignment failed',
      request: { question: { text: 'Why?' } }
    },
    onBack: () => {}
  });

  assert.match(collectText(failed), /Alignment failed/);
  assert.match(statusBadgeClass('failed'), /rose/);
  assert.match(statusBadgeClass('completed'), /emerald/);
  assert.match(statusBadgeClass('running'), /amber/);
});

test('buildStageProgress marks active, completed, and failed pipeline steps', () => {
  const tracing = buildStageProgress({ status: 'tracing', stage: 'trace' });
  assert.equal(tracing.find((stage) => stage.key === 'queued').state, 'complete');
  assert.equal(tracing.find((stage) => stage.key === 'intent').state, 'complete');
  assert.equal(tracing.find((stage) => stage.key === 'trace').state, 'active');
  assert.equal(tracing.find((stage) => stage.key === 'conflict').state, 'pending');

  const completed = buildStageProgress({ status: 'completed', stage: 'completed', result_index_ref: '/runs/run-1/result_index.json' });
  assert(completed.every((stage) => stage.state === 'complete'));

  const failed = buildStageProgress({ status: 'failed', stage: 'construct' });
  assert.equal(failed.find((stage) => stage.key === 'construct').state, 'failed');
});
