import assert from 'node:assert/strict';

import { chainScoreForDisplay } from './TraceView.js';

assert.equal(
  chainScoreForDisplay({ score: 0.7679, confidence: 1.0 }),
  0.7679
);

assert.equal(
  chainScoreForDisplay({ confidence: 0.42 }),
  0.42
);
