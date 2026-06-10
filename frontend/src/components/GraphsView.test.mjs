import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { activeGraphForLens, GRAPH_CANVAS_VIEWBOX, nextGraphPanOffset, resolveEvidenceGraphData, shouldShowGraphNodeLabel } from './GraphsView.js';

const graphViewSource = readFileSync(new URL('./GraphsView.js', import.meta.url), 'utf8');

function nodeIds(graph) {
  return (graph.nodes || []).map((node) => node.id).sort();
}

test('resolveEvidenceGraphData falls back to raw trace graph when semantic projection is empty', () => {
  const rawNode = { id: 'raw-node-1', type: 'Witness', properties: { label: 'raw witness' } };
  const rawEdge = { id: 'raw-edge-1', from: 'raw-node-1', to: 'raw-node-2', type: 'EVIDENCED_BY' };

  const result = resolveEvidenceGraphData({
    semantic: {
      evidence_graph: {
        nodes: [],
        edges: [],
        raw_counts: { nodes: 2 },
        semantic_counts: { concepts: 0 }
      }
    },
    trace: {
      eg_delta: {
        nodes: [rawNode],
        edges: [rawEdge]
      }
    }
  });

  assert.equal(result.hasSemanticProjection, false);
  assert.deepEqual(result.egNodes, [rawNode]);
  assert.deepEqual(result.egEdges, [rawEdge]);
  assert.deepEqual(result.semanticGraph.nodes, [rawNode]);
  assert.deepEqual(result.semanticGraph.edges, [rawEdge]);
});

test('resolveEvidenceGraphData keeps semantic projection when semantic nodes exist', () => {
  const semanticNode = { id: 'semantic-node-1', type: 'SemanticConcept' };
  const rawNode = { id: 'raw-node-1', type: 'Witness' };

  const result = resolveEvidenceGraphData({
    semantic: {
      evidence_graph: {
        nodes: [semanticNode],
        edges: []
      }
    },
    trace: {
      eg_delta: {
        nodes: [rawNode],
        edges: []
      }
    }
  });

  assert.equal(result.hasSemanticProjection, true);
  assert.deepEqual(result.egNodes, [semanticNode]);
});

test('activeGraphForLens applies 1-hop focus to the Reasoning Graph', () => {
  const rgNodes = [
    { id: 'rg-a' },
    { id: 'rg-b' },
    { id: 'rg-c' },
    { id: 'rg-d' }
  ];
  const rgEdges = [
    { id: 'rg-ab', from: 'rg-a', to: 'rg-b' },
    { id: 'rg-bc', from: 'rg-b', to: 'rg-c' },
    { id: 'rg-cd', from: 'rg-c', to: 'rg-d' }
  ];

  const result = activeGraphForLens({
    lens: 'rg',
    focusByLens: { rg: { centerIds: ['rg-b'], hops: 1 } },
    evidencePreview: { nodes: [], edges: [] },
    egNodes: [],
    egEdges: [],
    rgNodes,
    rgEdges,
    answerNodes: [],
    answerEdges: []
  });

  assert.equal(result.focused, true);
  assert.equal(result.focusHops, 1);
  assert.deepEqual(nodeIds(result), ['rg-a', 'rg-b', 'rg-c']);
});

test('activeGraphForLens applies independent 2-hop focus to the Answer Graph', () => {
  const answerNodes = [
    { id: 'answer-a' },
    { id: 'answer-b' },
    { id: 'answer-c' }
  ];
  const answerEdges = [
    { id: 'answer-ab', from: 'answer-a', to: 'answer-b' },
    { id: 'answer-bc', from: 'answer-b', to: 'answer-c' }
  ];

  const result = activeGraphForLens({
    lens: 'answer',
    focusByLens: {
      eg: { centerIds: ['ignored-eg'], hops: 1 },
      answer: { centerIds: ['answer-a'], hops: 2 }
    },
    evidencePreview: { nodes: [], edges: [] },
    egNodes: [{ id: 'ignored-eg' }],
    egEdges: [],
    rgNodes: [],
    rgEdges: [],
    answerNodes,
    answerEdges
  });

  assert.equal(result.focused, true);
  assert.equal(result.focusHops, 2);
  assert.deepEqual(nodeIds(result), ['answer-a', 'answer-b', 'answer-c']);
});

test('Graph view restores dense-map controls and preserves requested graph fixes', () => {
  assert.match(graphViewSource, /shouldShowGraphNodeLabel/);
  assert.match(graphViewSource, /Reset layout/);
  assert.match(graphViewSource, />\s*Fit\s*<\/button>/);
  assert.match(graphViewSource, /1-hop/);
  assert.match(graphViewSource, /2-hop/);
  assert.equal(GRAPH_CANVAS_VIEWBOX.minX < 0, true);
  assert.equal(GRAPH_CANVAS_VIEWBOX.minY < 0, true);
  assert.equal(GRAPH_CANVAS_VIEWBOX.width > 980, true);
  assert.equal(GRAPH_CANVAS_VIEWBOX.height > 560, true);
});

test('shouldShowGraphNodeLabel hides nonessential labels on dense maps', () => {
  assert.equal(shouldShowGraphNodeLabel({
    node: { type: 'Document' },
    nodeCount: 64
  }), false);
  assert.equal(shouldShowGraphNodeLabel({
    node: { type: 'Document' },
    nodeCount: 64,
    selected: true
  }), true);
  assert.equal(shouldShowGraphNodeLabel({
    node: { type: 'GraphRoot' },
    nodeCount: 64
  }), true);
  assert.equal(shouldShowGraphNodeLabel({
    node: { type: 'Claim' },
    nodeCount: 34
  }), true);
});

test('Graph view supports canvas pan dragging separate from node drag', () => {
  assert.deepEqual(nextGraphPanOffset({
    startX: 100,
    startY: 80,
    originX: 12,
    originY: -4
  }, { clientX: 140, clientY: 120 }), { x: 52, y: 36 });
  assert.match(graphViewSource, /beginCanvasPan/);
  assert.match(graphViewSource, /panOffset/);
  assert.match(graphViewSource, /graph-explorer__canvas--panning/);
  assert.match(graphViewSource, /graph-explorer__svg--dragging/);
});
