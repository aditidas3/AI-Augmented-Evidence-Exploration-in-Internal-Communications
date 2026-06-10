export function graphNodeId(node) {
  return node?.id || node?.properties?.uid;
}

export function graphNodeLabel(node) {
  return node?.label
    || node?.properties?.label
    || node?.properties?.name
    || node?.properties?.statement
    || node?.properties?.title
    || graphNodeId(node);
}

export function graphNodeType(node) {
  return node?.type || node?.properties?.type || node?.labels?.[0] || 'Node';
}

export function graphNodeCategory(node) {
  const type = String(graphNodeType(node) || '');
  const labels = node?.labels || [];
  const domainType = String(node?.properties?.domainType || '');

  if (type === 'GraphRoot' || labels.includes('GraphRoot')) return 'root';
  if (type === 'Artifact') return 'artifact';
  if (type === 'Document') return 'document';
  if (type === 'Observation') return 'observation';
  if (type === 'Testimony') return 'testimony';
  if (type === 'Record') return 'record';
  if (type === 'Derived') return 'derived';
  if (type === 'ReliabilityFactor') return 'reliability';
  if (type === 'finding' || type === 'Claim' || labels.includes('Claim')) return 'claim';
  if (type === 'SlotCandidate') return 'candidate';
  if (labels.includes('Inference') || ['inductive', 'deductive', 'abductive', 'other'].includes(type)) return 'inference';
  if (labels.includes('OperatorInvocation')) return 'operator';
  if (labels.includes('ArtifactSet')) return 'artifact-set';
  if (node?.properties?.type === 'Witness' || domainType.includes('Witness')) return 'witness';
  if (type === 'system' || labels.includes('Agent')) return 'system';
  return 'other';
}

export const GRAPH_NODE_STYLES = {
  root: { label: 'Root / graph object', color: '#0ea5e9' },
  artifact: { label: 'Artifact / source item', color: '#059669' },
  document: { label: 'Document span / anchor', color: '#14b8a6' },
  observation: { label: 'Observation', color: '#2563eb' },
  testimony: { label: 'Testimony', color: '#7c3aed' },
  record: { label: 'Record', color: '#0891b2' },
  derived: { label: 'Derived evidence', color: '#db2777' },
  reliability: { label: 'Reliability factor', color: '#64748b' },
  claim: { label: 'Claim / finding', color: '#4f46e5' },
  candidate: { label: 'Answer candidate', color: '#9333ea' },
  inference: { label: 'Reasoning / inference', color: '#f97316' },
  operator: { label: 'Operator invocation', color: '#475569' },
  'artifact-set': { label: 'Runtime artifact set', color: '#94a3b8' },
  witness: { label: 'Witness', color: '#f59e0b' },
  system: { label: 'System actor', color: '#334155' },
  other: { label: 'Other graph object', color: '#a16207' }
};

export function graphNodeColor(node) {
  return GRAPH_NODE_STYLES[graphNodeCategory(node)]?.color || GRAPH_NODE_STYLES.other.color;
}

export function graphEdgeId(edge) {
  return edge?.id || edge?.properties?.uid || `${edge?.from || edge?.source}->${edge?.to || edge?.target}:${edge?.type}`;
}

export function summarizeGraphNodeTypes(nodes) {
  const counts = {};
  for (const node of nodes || []) {
    const type = graphNodeType(node);
    counts[type] = (counts[type] || 0) + 1;
  }
  return Object.entries(counts).sort((a, b) => b[1] - a[1]);
}

export function graphEdgeEndpoint(edge, side) {
  if (side === 'source') return edge?.from || edge?.source;
  return edge?.to || edge?.target;
}

export function graphEdgeLabel(edge) {
  return edge?.label || edge?.type || edge?.properties?.relationship || edge?.properties?.role || 'edge';
}

export function pickEvidenceGraphPreview(nodes, edges, mode = 'readable') {
  if (mode === 'full') return { nodes: nodes || [], edges: edges || [] };

  const root = (nodes || []).find((node) => node.labels?.includes('GraphRoot'));
  const maxWitnesses = mode === 'expanded' ? 14 : 5;
  const maxArtifacts = mode === 'expanded' ? 8 : 3;
  const witnessLike = (nodes || []).filter((node) =>
    node.properties?.type === 'Witness'
    || node.properties?.domainType?.includes('Witness')
  ).slice(0, maxWitnesses);
  const selectedIds = new Set([root, ...witnessLike].filter(Boolean).map(graphNodeId));
  const frontierIds = new Set(witnessLike.map(graphNodeId));

  for (const edge of edges || []) {
    const source = graphEdgeEndpoint(edge, 'source');
    const target = graphEdgeEndpoint(edge, 'target');
    if (frontierIds.has(source) || frontierIds.has(target)) {
      selectedIds.add(source);
      selectedIds.add(target);
    }
  }

  const artifactNeighbors = (nodes || []).filter((node) =>
    node.properties?.type === 'Artifact' && selectedIds.has(graphNodeId(node))
  );
  artifactNeighbors.slice(0, maxArtifacts).forEach((node) => selectedIds.add(graphNodeId(node)));
  if (root) selectedIds.add(graphNodeId(root));

  let previewNodes = (nodes || []).filter((node) => selectedIds.has(graphNodeId(node)));
  const maxNodes = mode === 'expanded' ? 80 : 28;
  if (previewNodes.length > maxNodes) {
    const priority = (node) => {
      const type = graphNodeType(node);
      const domainType = node?.properties?.domainType || '';
      if (node === root || type === 'GraphRoot') return 0;
      if (type === 'Artifact' || node?.properties?.type === 'Artifact') return 1;
      if (node?.properties?.type === 'Witness' || domainType.includes('Witness')) return 2;
      if (type === 'Claim' || node?.properties?.type === 'finding') return 3;
      if (type === 'Document' || type === 'Testimony') return 4;
      return 5;
    };
    previewNodes = previewNodes
      .slice()
      .sort((a, b) => priority(a) - priority(b))
      .slice(0, maxNodes);
  }
  const previewNodeIds = new Set(previewNodes.map(graphNodeId));
  const previewEdges = (edges || [])
    .filter((edge) => previewNodeIds.has(graphEdgeEndpoint(edge, 'source')) && previewNodeIds.has(graphEdgeEndpoint(edge, 'target')))
    .slice(0, mode === 'expanded' ? 36 : 16);
  return { nodes: previewNodes, edges: previewEdges };
}

export function pickGraphNeighborhood(nodes, edges, centerIds = [], hops = 1, maxNodes = 90) {
  const centers = new Set((centerIds || []).filter(Boolean));
  if (!centers.size) return { nodes: [], edges: [] };

  const adjacency = new Map();
  for (const node of nodes || []) {
    adjacency.set(graphNodeId(node), new Set());
  }
  for (const edge of edges || []) {
    const source = graphEdgeEndpoint(edge, 'source');
    const target = graphEdgeEndpoint(edge, 'target');
    if (!source || !target) continue;
    if (!adjacency.has(source)) adjacency.set(source, new Set());
    if (!adjacency.has(target)) adjacency.set(target, new Set());
    adjacency.get(source).add(target);
    adjacency.get(target).add(source);
  }

  const selected = new Set(centers);
  let frontier = new Set(centers);
  for (let depth = 0; depth < hops; depth += 1) {
    const nextFrontier = new Set();
    for (const id of frontier) {
      for (const neighbor of adjacency.get(id) || []) {
        if (!selected.has(neighbor)) nextFrontier.add(neighbor);
        selected.add(neighbor);
      }
    }
    frontier = nextFrontier;
    if (!frontier.size || selected.size >= maxNodes) break;
  }

  let neighborhoodNodes = (nodes || []).filter((node) => selected.has(graphNodeId(node)));
  if (neighborhoodNodes.length > maxNodes) {
    const centerFirst = (node) => centers.has(graphNodeId(node)) ? 0 : 1;
    neighborhoodNodes = neighborhoodNodes
      .slice()
      .sort((a, b) => centerFirst(a) - centerFirst(b))
      .slice(0, maxNodes);
  }
  const nodeIds = new Set(neighborhoodNodes.map(graphNodeId));
  const neighborhoodEdges = (edges || []).filter((edge) =>
    nodeIds.has(graphEdgeEndpoint(edge, 'source')) && nodeIds.has(graphEdgeEndpoint(edge, 'target'))
  );

  return { nodes: neighborhoodNodes, edges: neighborhoodEdges };
}

export function layoutGraph(nodes) {
  const width = 920;
  const height = 460;
  const centerX = width / 2;
  const centerY = height / 2;
  return (nodes || []).map((node, index) => {
    if (index === 0) return { node, x: centerX, y: centerY };
    const ring = index <= 7 ? 1 : 2;
    const ringIndex = ring === 1 ? index - 1 : index - 8;
    const ringCount = ring === 1 ? Math.max(1, Math.min(7, nodes.length - 1)) : Math.max(1, nodes.length - 8);
    const angle = (Math.PI * 2 * ringIndex) / ringCount - Math.PI / 2;
    const radius = ring === 1 ? 150 : 260;
    return { node, x: centerX + Math.cos(angle) * radius, y: centerY + Math.sin(angle) * radius };
  });
}

