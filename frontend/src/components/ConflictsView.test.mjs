import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const conflictsSource = readFileSync(new URL('./ConflictsView.js', import.meta.url), 'utf8');

assert.match(conflictsSource, /Analyst label/);
assert.match(conflictsSource, /CONFLICT_REVIEW_FILTERS\.map/);
assert.doesNotMatch(conflictsSource, /reviewCounts/);
assert.doesNotMatch(conflictsSource, /workups\.length/);
