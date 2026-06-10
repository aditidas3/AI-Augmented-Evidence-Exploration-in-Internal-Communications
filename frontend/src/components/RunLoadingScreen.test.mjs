import test from 'node:test';
import assert from 'node:assert/strict';

import { RunLoadingScreen } from './RunLoadingScreen.js';

function collectText(node) {
  if (node == null || typeof node === 'boolean') return '';
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  const props = node.props || {};
  return [props.children, ...(node.children || [])].flat().map(collectText).join(' ');
}

test('RunLoadingScreen renders run-specific artifact loading state', () => {
  const view = RunLoadingScreen({
    runId: 'run-loading-123',
    runRecord: {
      request: {
        question: {
          text: 'What legal document governed the policy?'
        }
      }
    }
  });

  const text = collectText(view);
  assert.match(text, /run-loading-123/);
  assert.match(text, /Loading run artifacts/);
  assert.match(text, /ALIGN/);
  assert.match(text, /TRACE/);
  assert.match(text, /CONFLICT/);
  assert.match(text, /CONSTRUCT/);
  assert.match(text, /EXPLAIN/);
  assert.match(text, /What legal document governed the policy\?/);
});
