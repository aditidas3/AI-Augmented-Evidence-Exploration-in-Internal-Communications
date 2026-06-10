import React, { useMemo, useRef, useState } from '../../vendor/react.bundle.mjs';

import { html } from '../html.js';

import { GRAPH_NODE_STYLES, graphEdgeEndpoint, graphEdgeId, graphEdgeLabel, graphNodeCategory, graphNodeColor, graphNodeId, graphNodeLabel, graphNodeType, layoutGraph, pickEvidenceGraphPreview, pickGraphNeighborhood, summarizeGraphNodeTypes } from '../domain/graphModel.js';

export const GRAPH_CANVAS_VIEWBOX = Object.freeze({
  minX: -90,
  minY: -110,
  width: 1100,
  height: 760
});

const GRAPH_DRAG_MARGIN = 56;

function nodeDegreeMap(nodes, edges) {
  const degrees = Object.fromEntries((nodes || []).map((node) => [graphNodeId(node), 0]));
  for (const edge of edges || []) {
    const source = graphEdgeEndpoint(edge, 'source');
    const target = graphEdgeEndpoint(edge, 'target');
    if (source in degrees) degrees[source] += 1;
    if (target in degrees) degrees[target] += 1;
  }
  return degrees;
}

function filteredGraph(nodes, edges, searchTerm, activeCategories) {
  const query = String(searchTerm || '').trim().toLowerCase();
  const categorySet = new Set(activeCategories || []);
  let visibleNodes = nodes || [];
  if (categorySet.size) {
    visibleNodes = visibleNodes.filter((node) => categorySet.has(graphNodeCategory(node)));
  }
  if (query) {
    visibleNodes = visibleNodes.filter((node) => {
      const haystack = [
        graphNodeLabel(node),
        graphNodeType(node),
        graphNodeCategory(node),
        node.properties?.semantic_kind,
        node.properties?.slot_type,
        ...(node.properties?.slot_types || []),
        ...(node.properties?.candidate_surfaces || [])
      ].filter(Boolean).join(' ').toLowerCase();
      return haystack.includes(query);
    });
  }
  const visibleIds = new Set(visibleNodes.map(graphNodeId));
  const visibleEdges = (edges || []).filter((edge) =>
    visibleIds.has(graphEdgeEndpoint(edge, 'source')) && visibleIds.has(graphEdgeEndpoint(edge, 'target'))
  );
  return { nodes: visibleNodes, edges: visibleEdges };
}

function focusSetForSelection(nodes, edges, selectedNodeIds, selectedEdgeIds) {
  const focused = new Set(selectedNodeIds || []);
  for (const edge of edges || []) {
    if ((selectedEdgeIds || []).includes(graphEdgeId(edge))) {
      focused.add(graphEdgeEndpoint(edge, 'source'));
      focused.add(graphEdgeEndpoint(edge, 'target'));
    }
  }
  if (!focused.size) return null;
  for (const edge of edges || []) {
    const source = graphEdgeEndpoint(edge, 'source');
    const target = graphEdgeEndpoint(edge, 'target');
    if (focused.has(source) || focused.has(target)) {
      focused.add(source);
      focused.add(target);
    }
  }
  return focused;
}

function graphSearchMatches(node, searchTerm) {
  const query = String(searchTerm || '').trim().toLowerCase();
  if (!query) return false;
  const haystack = [
    graphNodeLabel(node),
    graphNodeType(node),
    graphNodeCategory(node),
    node.properties?.semantic_kind,
    node.properties?.slot_type,
    ...(node.properties?.slot_types || []),
    ...(node.properties?.candidate_surfaces || [])
  ].filter(Boolean).join(' ').toLowerCase();
  return haystack.includes(query);
}

export function shouldShowGraphNodeLabel({ node, nodeCount, selected = false, searchMatch = false }) {
  if (selected || searchMatch) return true;
  if (nodeCount <= 18) return true;
  const type = graphNodeType(node);
  if (type === 'GraphRoot') return true;
  if (nodeCount <= 40) return ['SemanticConcept', 'Claim', 'SlotCandidate'].includes(type);
  return false;
}

export function nextGraphPanOffset(panState, event) {
  if (!panState || !event) return { x: 0, y: 0 };
  return {
    x: panState.originX + (Number(event.clientX) - panState.startX),
    y: panState.originY + (Number(event.clientY) - panState.startY)
  };
}

function GraphCanvas({ title, nodes, edges, selectedNodeIds, selectedEdgeIds, onSelectNode, onSelectEdge, onClearSelection, onFocusNode, controls }) {
  const [searchTerm, setSearchTerm] = useState('');
  const [activeCategories, setActiveCategories] = useState([]);
  const [zoom, setZoom] = useState(1);
  const [panOffset, setPanOffset] = useState({ x: 0, y: 0 });
  const [panState, setPanState] = useState(null);
  const [dragPositions, setDragPositions] = useState({});
  const [dragState, setDragState] = useState(null);
  const [detailsCollapsed, setDetailsCollapsed] = useState(false);
  const svgRef = useRef(null);
  const draggedRef = useRef(false);
  const canvasDraggedRef = useRef(false);
  const filtered = useMemo(() => filteredGraph(nodes, edges, searchTerm, activeCategories), [nodes, edges, searchTerm, activeCategories]);
  const positioned = useMemo(() => layoutGraph(filtered.nodes).map((item) => {
    const override = dragPositions[graphNodeId(item.node)];
    return override ? { ...item, ...override } : item;
  }), [filtered.nodes, dragPositions]);
  const byId = Object.fromEntries(positioned.map((item) => [graphNodeId(item.node), item]));
  const degrees = useMemo(() => nodeDegreeMap(filtered.nodes, filtered.edges), [filtered.nodes, filtered.edges]);
  const visibleCategories = useMemo(() => {
    const counts = {};
    for (const node of nodes || []) {
      const category = graphNodeCategory(node);
      counts[category] = (counts[category] || 0) + 1;
    }
    return Object.entries(counts).sort((a, b) => b[1] - a[1]);
  }, [nodes]);
  const focusSet = focusSetForSelection(filtered.nodes, filtered.edges, selectedNodeIds, selectedEdgeIds);
  const selectedNode = (filtered.nodes || []).find((node) => selectedNodeIds.includes(graphNodeId(node)));
  const selectedEdge = (filtered.edges || []).find((edge) => selectedEdgeIds.includes(graphEdgeId(edge)));
  const denseGraph = filtered.nodes.length > 40;

  function toggleCategory(category) {
    setActiveCategories((current) => current.includes(category)
      ? current.filter((item) => item !== category)
      : [...current, category]);
  }

  function eventToSvgPoint(event) {
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect) return { x: 0, y: 0 };
    return {
      x: GRAPH_CANVAS_VIEWBOX.minX + ((event.clientX - rect.left) / rect.width) * GRAPH_CANVAS_VIEWBOX.width,
      y: GRAPH_CANVAS_VIEWBOX.minY + ((event.clientY - rect.top) / rect.height) * GRAPH_CANVAS_VIEWBOX.height
    };
  }

  function beginNodeDrag(event, node, x, y) {
    if (event.button != null && event.button !== 0) return;
    event.stopPropagation();
    const point = eventToSvgPoint(event);
    draggedRef.current = false;
    setDragState({
      nodeId: graphNodeId(node),
      offsetX: point.x - x,
      offsetY: point.y - y
    });
  }

  function beginCanvasPan(event) {
    if (event.button != null && event.button !== 0) return;
    event.stopPropagation();
    canvasDraggedRef.current = false;
    setPanState({
      startX: Number(event.clientX),
      startY: Number(event.clientY),
      originX: panOffset.x,
      originY: panOffset.y
    });
  }

  function updateDrag(event) {
    if (!dragState) return;
    event.preventDefault();
    const point = eventToSvgPoint(event);
    draggedRef.current = true;
    setDragPositions((current) => ({
      ...current,
      [dragState.nodeId]: {
        x: Math.max(
          GRAPH_CANVAS_VIEWBOX.minX + GRAPH_DRAG_MARGIN,
          Math.min(GRAPH_CANVAS_VIEWBOX.minX + GRAPH_CANVAS_VIEWBOX.width - GRAPH_DRAG_MARGIN, point.x - dragState.offsetX)
        ),
        y: Math.max(
          GRAPH_CANVAS_VIEWBOX.minY + GRAPH_DRAG_MARGIN,
          Math.min(GRAPH_CANVAS_VIEWBOX.minY + GRAPH_CANVAS_VIEWBOX.height - GRAPH_DRAG_MARGIN, point.y - dragState.offsetY)
        )
      }
    }));
  }

  function updateCanvasPan(event) {
    if (!panState) return;
    event.preventDefault();
    const nextOffset = nextGraphPanOffset(panState, event);
    if (Math.abs(nextOffset.x - panState.originX) > 1 || Math.abs(nextOffset.y - panState.originY) > 1) {
      canvasDraggedRef.current = true;
    }
    setPanOffset(nextOffset);
  }

  function updateInteractionDrag(event) {
    updateDrag(event);
    updateCanvasPan(event);
  }

  function finishInteractionDrag() {
    setDragState(null);
    setPanState(null);
    window.setTimeout(() => { draggedRef.current = false; }, 0);
    window.setTimeout(() => { canvasDraggedRef.current = false; }, 0);
  }

  function handleCanvasClick() {
    if (canvasDraggedRef.current) return;
    onClearSelection && onClearSelection();
  }

  function focusImmediateNeighborhood(node) {
    onSelectNode(node, false);
    setActiveCategories([]);
    setSearchTerm('');
    onFocusNode && onFocusNode(graphNodeId(node));
  }

  return html`
    <section className="graph-explorer">
      <div className="graph-explorer__header">
        <div>
          <h2><span>${title}</span></h2>
          <p>${filtered.nodes.length} nodes · ${filtered.edges.length} edges · ${nodes.length} available</p>
        </div>
        <div className="graph-explorer__zoom">
          <button type="button" onClick=${() => { setZoom(0.86); setPanOffset({ x: 0, y: 0 }); }}>Fit</button>
          <button type="button" onClick=${() => setZoom((value) => Math.max(0.72, Number((value - 0.12).toFixed(2))))}>-</button>
          <span>${Math.round(zoom * 100)}%</span>
          <button type="button" onClick=${() => setZoom((value) => Math.min(1.45, Number((value + 0.12).toFixed(2))))}>+</button>
          <button type="button" onClick=${() => setDragPositions({})}>Reset layout</button>
          <button type="button" onClick=${() => { setZoom(1); setPanOffset({ x: 0, y: 0 }); setDragPositions({}); onClearSelection && onClearSelection(); }}>Reset</button>
        </div>
      </div>
      <div className="graph-explorer__body">
        <aside className="graph-explorer__left">
          <label className="graph-explorer__label">Search</label>
          <input
            value=${searchTerm}
            onInput=${(event) => setSearchTerm(event.target.value)}
            placeholder="concept, actor, source..."
            className="graph-explorer__search"
          />
          <div className="graph-explorer__section">
            <p className="graph-explorer__label">Highlight by node role</p>
            <div className="graph-explorer__chips">
              ${visibleCategories.map(([category, count]) => {
                const style = GRAPH_NODE_STYLES[category] || GRAPH_NODE_STYLES.other;
                const active = activeCategories.includes(category);
                return html`
                  <button
                    key=${category}
                    type="button"
                    onClick=${() => toggleCategory(category)}
                    className=${`graph-explorer__chip ${active ? 'graph-explorer__chip--active' : ''}`}
                  >
                    <span style=${{ backgroundColor: style.color }}></span>${style.label} <small>${count}</small>
                  </button>
                `;
              })}
            </div>
          </div>
          <div className="graph-explorer__section">
            <p className="graph-explorer__label">Legend</p>
            <div className="graph-explorer__legend">
              ${visibleCategories.slice(0, 8).map(([category]) => {
                const style = GRAPH_NODE_STYLES[category] || GRAPH_NODE_STYLES.other;
                return html`<p key=${category}><span style=${{ backgroundColor: style.color }}></span>${style.label}</p>`;
              })}
            </div>
          </div>
          <div className="graph-explorer__tips">
            <strong>Tips</strong>
            <p>Click any node to focus its connections.</p>
            <p>Double-click a node to open its 1-hop neighborhood.</p>
            <p>Drag nodes to rearrange the map.</p>
            <p>Shift-click adds to the current selection.</p>
            <p>Edge width and numbers reflect support strength.</p>
          </div>
        </aside>

        <div className=${`graph-explorer__canvas ${panState ? 'graph-explorer__canvas--panning' : ''}`} onClick=${handleCanvasClick}>
          <svg
            ref=${svgRef}
            viewBox=${`${GRAPH_CANVAS_VIEWBOX.minX} ${GRAPH_CANVAS_VIEWBOX.minY} ${GRAPH_CANVAS_VIEWBOX.width} ${GRAPH_CANVAS_VIEWBOX.height}`}
            className=${`graph-explorer__svg ${panState ? 'graph-explorer__svg--dragging' : ''}`}
            style=${{ transform: `translate(${panOffset.x}px, ${panOffset.y}px) scale(${zoom})` }}
            onMouseMove=${updateInteractionDrag}
            onMouseUp=${finishInteractionDrag}
            onMouseLeave=${finishInteractionDrag}
          >
            <defs>
              <radialGradient id="graph-glow" cx="50%" cy="50%" r="60%">
                <stop offset="0%" stop-color="#2563eb" stop-opacity="0.18" />
                <stop offset="65%" stop-color="#1e3a8a" stop-opacity="0.06" />
                <stop offset="100%" stop-color="#020617" stop-opacity="0" />
              </radialGradient>
            </defs>
            <rect
              x=${GRAPH_CANVAS_VIEWBOX.minX}
              y=${GRAPH_CANVAS_VIEWBOX.minY}
              width=${GRAPH_CANVAS_VIEWBOX.width}
              height=${GRAPH_CANVAS_VIEWBOX.height}
              fill="url(#graph-glow)"
              onMouseDown=${beginCanvasPan}
            />
            ${filtered.edges.map((edge) => {
            const source = byId[graphEdgeEndpoint(edge, 'source')];
            const target = byId[graphEdgeEndpoint(edge, 'target')];
            if (!source || !target) return null;
            const selected = selectedEdgeIds.includes(graphEdgeId(edge));
            const related = !focusSet || (focusSet.has(graphEdgeEndpoint(edge, 'source')) && focusSet.has(graphEdgeEndpoint(edge, 'target')));
            return html`
              <g
                key=${graphEdgeId(edge)}
                onClick=${(event) => { event.stopPropagation(); onSelectEdge(edge, event.shiftKey); }}
                className="cursor-pointer"
                opacity=${related ? '1' : '0.08'}
              >
                <line
                  x1=${source.x}
                  y1=${source.y}
                  x2=${target.x}
                  y2=${target.y}
                  stroke="transparent"
                  stroke-width="14"
                />
                <line
                  x1=${source.x}
                  y1=${source.y}
                  x2=${target.x}
                  y2=${target.y}
                  stroke=${selected ? '#fb923c' : edge.type === 'EVIDENCED_BY' ? '#64748b' : '#7dd3fc'}
                  stroke-width=${selected ? '4' : Math.max(1.5, 1.5 + Number(edge.properties?.strength || edge.properties?.weight || 0) * 4)}
                  stroke-opacity=${edge.type === 'EVIDENCED_BY' ? '0.45' : '0.9'}
                />
                ${edge.properties?.support_count != null && edge.type !== 'SUMMARIZES' ? html`
                  <text
                    x=${(source.x + target.x) / 2}
                    y=${(source.y + target.y) / 2 - 6}
                    text-anchor="middle"
                    className="pointer-events-none graph-explorer__edge-count"
                  >${edge.properties.support_count}</text>
                ` : null}
                ${selected ? html`
                  <text
                    x=${(source.x + target.x) / 2}
                    y=${(source.y + target.y) / 2 - 8}
                    text-anchor="middle"
                    className="pointer-events-none graph-explorer__edge-label"
                  >${String(graphEdgeLabel(edge)).slice(0, 22)}</text>
                ` : null}
              </g>
            `;
          })}
          ${positioned.map(({ node, x, y }) => {
            const type = graphNodeType(node);
            const selected = selectedNodeIds.includes(graphNodeId(node));
            const fill = graphNodeColor(node);
            const support = Number(node.properties?.support_count || 0);
            const degree = degrees[graphNodeId(node)] || 0;
            const searchMatch = graphSearchMatches(node, searchTerm);
            const showLabel = shouldShowGraphNodeLabel({ node, nodeCount: filtered.nodes.length, selected, searchMatch });
            const radius = type === 'GraphRoot'
              ? 34
              : type === 'SemanticConcept'
                ? Math.min(52, 30 + Math.log1p(Math.max(support, degree)) * 5)
                : 22 + Math.min(10, degree);
            const textFill = type === 'SemanticConcept' || type === 'GraphRoot' ? '#0f172a' : '#ffffff';
            const related = !focusSet || focusSet.has(graphNodeId(node));
            return html`
              <g
                key=${graphNodeId(node)}
                transform=${`translate(${x}, ${y})`}
                onMouseDown=${(event) => beginNodeDrag(event, node, x, y)}
                onDoubleClick=${(event) => {
                  event.stopPropagation();
                  focusImmediateNeighborhood(node);
                }}
                onClick=${(event) => {
                  event.stopPropagation();
                  if (draggedRef.current) return;
                  onSelectNode(node, event.shiftKey);
                }}
                className="cursor-pointer"
                opacity=${related ? '1' : '0.16'}
              >
                <circle r=${radius + (selected || searchMatch ? 10 : 0)} fill=${selected ? 'rgba(125,211,252,0.26)' : searchMatch ? 'rgba(250,204,21,0.24)' : 'transparent'}></circle>
                <circle r=${radius} fill=${fill} stroke=${selected ? '#93c5fd' : searchMatch ? '#fde68a' : 'rgba(255,255,255,0.22)'} stroke-width=${selected || searchMatch ? '4' : '1.5'} opacity=${type === 'Artifact' ? '0.72' : '0.98'}></circle>
                ${showLabel ? html`<text y=${radius + 24} text-anchor="middle" className="pointer-events-none graph-explorer__node-label">${String(graphNodeLabel(node)).slice(0, denseGraph ? 28 : 44)}</text>` : null}
                ${type === 'SemanticConcept' && support > 1 ? html`
                  <text y="5" text-anchor="middle" fill=${textFill} className="pointer-events-none text-[12px] font-bold">${support}</text>
                ` : null}
                ${type !== 'SemanticConcept' ? html`
                  <text y=${type === 'GraphRoot' ? '5' : '4'} text-anchor="middle" fill=${textFill} className="pointer-events-none text-[9px] font-bold">${type === 'GraphRoot' ? 'Map' : String(type).slice(0, 8)}</text>
                ` : null}
              </g>
            `;
          })}
        </svg>
          <div className="graph-explorer__footer">Drag background to pan · drag nodes to rearrange · shift-click to add</div>
      </div>
        <aside className=${`graph-explorer__right ${detailsCollapsed ? 'graph-explorer__right--collapsed' : ''}`}>
          <button
            type="button"
            className="graph-explorer__panel-toggle"
            onClick=${(event) => {
              event.stopPropagation();
              setDetailsCollapsed((value) => !value);
            }}
          >
            ${detailsCollapsed ? 'Show details' : 'Hide details'}
          </button>
          ${detailsCollapsed ? null : html`
          ${selectedNode ? html`
            <p className="graph-explorer__label">Selected node</p>
            <h3>${graphNodeLabel(selectedNode)}</h3>
            <p className="graph-explorer__badge">${graphNodeType(selectedNode)} · ${GRAPH_NODE_STYLES[graphNodeCategory(selectedNode)]?.label || graphNodeCategory(selectedNode)}</p>
            <p>${selectedNode.properties?.description || selectedNode.properties?.statement || 'Select related graph objects to inspect the evidence context in the main detail drawer.'}</p>
            <div className="graph-explorer__facts">
              ${selectedNode.properties?.support_count != null ? html`<span>${selectedNode.properties.support_count} witness support</span>` : null}
              ${selectedNode.properties?.artifact_ids?.length ? html`<span>${selectedNode.properties.artifact_ids.length} source docs</span>` : null}
              ${(degrees[graphNodeId(selectedNode)] || 0) ? html`<span>${degrees[graphNodeId(selectedNode)]} connections</span>` : null}
            </div>
          ` : selectedEdge ? html`
            <p className="graph-explorer__label">Selected relationship</p>
            <h3>${graphEdgeLabel(selectedEdge)}</h3>
            <p className="graph-explorer__badge">${selectedEdge.type || 'edge'}</p>
            <p>${selectedEdge.properties?.description || 'This relationship connects two visible graph objects.'}</p>
            <div className="graph-explorer__facts">
              ${selectedEdge.properties?.support_count != null ? html`<span>${selectedEdge.properties.support_count} supporting source(s)</span>` : null}
              ${selectedEdge.properties?.strength != null ? html`<span>${Math.round(selectedEdge.properties.strength * 100)}% strength</span>` : null}
            </div>
          ` : html`
            <p className="graph-explorer__label">No selection</p>
            <h3>Explore the evidence map</h3>
            <p>Choose a semantic concept or edge to focus its local neighborhood. The full evidence drawer will open on the right side of the app.</p>
          `}
          `}
        </aside>
      </div>
      ${controls || null}
    </section>
  `;
}

function valuesForSlot(model, slotType, limit = 6) {
  const seen = new Set();
  return (model.answer.findings || [])
    .filter((finding) => finding.slot_type === slotType)
    .map((finding) => finding.statement)
    .filter(Boolean)
    .filter((value) => {
      const key = String(value).toLowerCase();
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .slice(0, limit);
}

function readableAnswerGraphSummary(model) {
  const who = valuesForSlot(model, 'WHO', 5);
  const what = valuesForSlot(model, 'WHAT', 6);
  const how = valuesForSlot(model, 'HOW', 5);
  const when = valuesForSlot(model, 'WHEN', 3);
  const evidence = valuesForSlot(model, 'EVIDENCE', 5);

  return {
    headline: [
      what.length ? `The graph frames the answer around ${what.join(', ')}.` : '',
      who.length ? `It identifies ${who.join(', ')} as associated people or roles.` : '',
      how.length ? `It connects the answer to ${how.join(', ')}.` : '',
      when.length ? `The clearest time marker shown is ${when.join(', ')}.` : ''
    ].filter(Boolean).join(' '),
    groups: [
      ['What the response involved', what],
      ['People / roles surfaced', who],
      ['How the response was organized', how],
      ['Time period or date evidence', when],
      ['Evidence objects surfaced', evidence]
    ]
  };
}

function visibleGraphInterpretation({ lens, active, activeCategories, focusCenterIds, focusHops, egNodes, egEdges, model }) {
  const categoryNames = Object.fromEntries(Object.entries(GRAPH_NODE_STYLES).map(([key, value]) => [key, value.label]));
  const topCategories = activeCategories
    .slice(0, 4)
    .map(([category, count]) => `${count} ${categoryNames[category] || category}`)
    .join(', ');
  const nodeCount = active.nodes?.length || 0;
  const edgeCount = active.edges?.length || 0;

  if (lens === 'eg') {
    if (focusCenterIds.length) {
      return {
        title: `Focused ${focusHops}-hop Evidence Graph neighborhood`,
        body: `This view is centered on ${focusCenterIds.length} selected node(s), not on the whole graph. It currently shows ${nodeCount} node(s) and ${edgeCount} edge(s). The dominant visible object types are ${topCategories || 'not classified'}. Use this view to inspect local evidence structure around the selected object(s).`,
        detail: `The semantic projection is derived from the raw run-level Evidence Graph, but the visible slice is deliberately bounded around the selected concept(s).`
      };
    }
    return {
      title: 'Readable semantic evidence map',
      body: `This view is a customer-facing concept map, not a dump of witness/artifact objects. It currently shows ${nodeCount} node(s) and ${edgeCount} edge(s), dominated by ${topCategories || 'not classified'}. Edge numbers show how many source items support the connection.`,
      detail: `Documents and witnesses are still attached as support metadata. Select a concept or relationship to inspect the underlying witness contexts.`
    };
  }

  if (lens === 'rg') {
    if (focusCenterIds.length) {
      return {
        title: `Focused ${focusHops}-hop Reasoning Graph neighborhood`,
        body: `This view is centered on ${focusCenterIds.length} selected reasoning node(s). It currently shows ${nodeCount} node(s) and ${edgeCount} edge(s), dominated by ${topCategories || 'not classified'}. Use it to inspect local TRACE structure around the selected object(s).`,
        detail: 'Reset the overview to return to the full query-local reasoning graph.'
      };
    }
    return {
      title: 'Reasoning Graph for TRACE',
      body: `This view shows the query-local reasoning structure for the run: ${nodeCount} node(s) and ${edgeCount} edge(s), dominated by ${topCategories || 'not classified'}. Claim and inference nodes can resolve back to ALIGN witnesses when the graph carries slot or premise/conclusion metadata.`,
      detail: 'Use this lens to understand how TRACE connected premises, claims, and inference steps rather than to inspect the persistent evidence substrate.'
    };
  }

  if (focusCenterIds.length) {
    return {
      title: `Focused ${focusHops}-hop Answer Graph neighborhood`,
      body: `This view is centered on ${focusCenterIds.length} selected answer node(s). It currently shows ${nodeCount} node(s) and ${edgeCount} edge(s), dominated by ${topCategories || 'not classified'}. Use it to inspect the local answer package around selected candidate(s).`,
      detail: 'Reset the overview to return to the full constructed answer graph.'
    };
  }

  return {
    title: 'Constructed Answer Graph',
    body: `This view shows the packaged answer-level graph: ${nodeCount} node(s) and ${edgeCount} edge(s), dominated by ${topCategories || 'not classified'}. It should be read together with the human-readable answer summary above.`,
    detail: `The current answer summary identifies ${valuesForSlot(model, 'WHO', 4).join(', ') || 'no WHO candidates'} and ${valuesForSlot(model, 'WHAT', 4).join(', ') || 'no WHAT candidates'} as answer candidates.`
  };
}

export function resolveEvidenceGraphData(model) {
  const semanticSource = model?.semantic?.evidence_graph || {};
  const rawSource = model?.trace?.eg_delta || {};
  const semanticNodes = Array.isArray(semanticSource.nodes) ? semanticSource.nodes : [];
  const semanticEdges = Array.isArray(semanticSource.edges) ? semanticSource.edges : [];
  const rawEgNodes = Array.isArray(rawSource.nodes) ? rawSource.nodes : [];
  const rawEgEdges = Array.isArray(rawSource.edges) ? rawSource.edges : [];
  const hasSemanticProjection = semanticNodes.length > 0;
  const egNodes = hasSemanticProjection ? semanticNodes : rawEgNodes;
  const egEdges = hasSemanticProjection ? semanticEdges : rawEgEdges;

  return {
    semanticGraph: {
      ...semanticSource,
      nodes: egNodes,
      edges: egEdges,
      raw_counts: semanticSource.raw_counts || {},
      semantic_counts: semanticSource.semantic_counts || {}
    },
    egNodes,
    egEdges,
    rawEgNodes,
    rawEgEdges,
    hasSemanticProjection
  };
}

function normalizeGraphFocusHops(hops) {
  return Number(hops) === 2 ? 2 : 1;
}

export function graphFocusForLens(focusByLens, lens) {
  const focus = focusByLens?.[lens] || {};
  return {
    centerIds: Array.isArray(focus.centerIds) ? focus.centerIds.filter(Boolean) : [],
    hops: normalizeGraphFocusHops(focus.hops)
  };
}

function graphSourceForLens({ lens, evidencePreview, egNodes, egEdges, rgNodes, rgEdges, answerNodes, answerEdges }) {
  if (lens === 'rg') {
    return {
      overviewNodes: rgNodes || [],
      overviewEdges: rgEdges || [],
      fullNodes: rgNodes || [],
      fullEdges: rgEdges || []
    };
  }
  if (lens === 'answer') {
    return {
      overviewNodes: answerNodes || [],
      overviewEdges: answerEdges || [],
      fullNodes: answerNodes || [],
      fullEdges: answerEdges || []
    };
  }
  return {
    overviewNodes: evidencePreview?.nodes || egNodes || [],
    overviewEdges: evidencePreview?.edges || egEdges || [],
    fullNodes: egNodes || [],
    fullEdges: egEdges || []
  };
}

export function activeGraphForLens(args) {
  const source = graphSourceForLens(args);
  const focus = graphFocusForLens(args.focusByLens, args.lens);
  const focused = focus.centerIds.length > 0;
  const graph = focused
    ? pickGraphNeighborhood(source.fullNodes, source.fullEdges, focus.centerIds, focus.hops)
    : { nodes: source.overviewNodes, edges: source.overviewEdges };

  return {
    ...graph,
    fullNodes: source.fullNodes,
    fullEdges: source.fullEdges,
    focusCenterIds: focus.centerIds,
    focusHops: focus.hops,
    focused
  };
}

function graphLensName(lens) {
  if (lens === 'rg') return 'Reasoning Graph';
  if (lens === 'answer') return 'Answer Graph';
  return 'Evidence Graph';
}

export function GraphsView({ model, selection, onSelectionChange }) {
  const [lens, setLens] = useState('eg');
  const [evidenceDensity, setEvidenceDensity] = useState('readable');
  const [focusByLens, setFocusByLens] = useState({});
  const answerNodes = model.answer.graph?.nodes || [];
  const answerEdges = model.answer.graph?.edges || [];
  const { semanticGraph, egNodes, egEdges, rawEgNodes, rawEgEdges } = resolveEvidenceGraphData(model);
  const rgNodes = model.trace.rg_trace?.nodes || [];
  const rgEdges = model.trace.rg_trace?.edges || [];
  const evidencePreview = useMemo(() => pickEvidenceGraphPreview(egNodes, egEdges, evidenceDensity), [egNodes, egEdges, evidenceDensity]);
  const activeGraphState = useMemo(() => activeGraphForLens({
    lens,
    focusByLens,
    evidencePreview,
    egNodes,
    egEdges,
    rgNodes,
    rgEdges,
    answerNodes,
    answerEdges
  }), [lens, focusByLens, evidencePreview, egNodes, egEdges, rgNodes, rgEdges, answerNodes, answerEdges]);
  const active = { nodes: activeGraphState.nodes, edges: activeGraphState.edges };
  const focusCenterIds = activeGraphState.focusCenterIds;
  const focusHops = activeGraphState.focusHops;
  const activeLensName = graphLensName(lens);
  const activeTitle = lens === 'eg'
    ? focusCenterIds.length
      ? `Semantic Evidence Graph focused neighborhood (${focusHops}-hop)`
      : evidenceDensity === 'full' ? 'Semantic Evidence Graph full projection' : evidenceDensity === 'expanded' ? 'Semantic Evidence Graph expanded preview' : 'Semantic Evidence Graph readable preview'
    : lens === 'rg'
      ? focusCenterIds.length ? `Reasoning Graph focused neighborhood (${focusHops}-hop)` : 'Reasoning Graph'
      : focusCenterIds.length ? `Answer Graph focused neighborhood (${focusHops}-hop)` : 'Answer Graph';
  const activeTypes = summarizeGraphNodeTypes(lens === 'eg' ? egNodes : active.nodes);
  const answerSummary = useMemo(() => readableAnswerGraphSummary(model), [model]);
  const activeCategories = useMemo(() => {
    const counts = {};
    for (const node of active.nodes || []) {
      const category = graphNodeCategory(node);
      counts[category] = (counts[category] || 0) + 1;
    }
    return Object.entries(counts).sort((a, b) => b[1] - a[1]);
  }, [active.nodes]);
  const graphInterpretation = visibleGraphInterpretation({ lens, active, activeCategories, focusCenterIds, focusHops, egNodes, egEdges, model });
  const graphSelection = selection?.kind === 'graph' ? selection.item : { lens, nodes: [], edges: [] };
  const selectedNodeIds = graphSelection.lens === lens ? graphSelection.nodes.map(graphNodeId) : [];
  const selectedEdgeIds = graphSelection.lens === lens ? graphSelection.edges.map(graphEdgeId) : [];
  const selectedNeighborhood = useMemo(
    () => pickGraphNeighborhood(activeGraphState.fullNodes, activeGraphState.fullEdges, selectedNodeIds, focusHops),
    [activeGraphState.fullNodes, activeGraphState.fullEdges, selectedNodeIds, focusHops]
  );

  function updateLens(nextLens) {
    setLens(nextLens);
    onSelectionChange({ kind: 'graph', item: { lens: nextLens, nodes: [], edges: [] } });
  }

  function setFocusForLens(targetLens, updates) {
    setFocusByLens((current) => {
      const currentFocus = graphFocusForLens(current, targetLens);
      const nextFocus = typeof updates === 'function' ? updates(currentFocus) : { ...currentFocus, ...updates };
      return {
        ...current,
        [targetLens]: {
          centerIds: Array.isArray(nextFocus.centerIds) ? nextFocus.centerIds.filter(Boolean) : [],
          hops: normalizeGraphFocusHops(nextFocus.hops)
        }
      };
    });
  }

  function selectNode(node, additive) {
    const currentNodes = graphSelection.lens === lens ? graphSelection.nodes : [];
    const exists = currentNodes.some((item) => graphNodeId(item) === graphNodeId(node));
    const nodes = additive
      ? exists
        ? currentNodes.filter((item) => graphNodeId(item) !== graphNodeId(node))
        : [...currentNodes, node]
      : [node];
    onSelectionChange({ kind: 'graph', item: { lens, nodes, edges: additive && graphSelection.lens === lens ? graphSelection.edges : [] } });
  }

  function selectEdge(edge, additive) {
    const currentEdges = graphSelection.lens === lens ? graphSelection.edges : [];
    const exists = currentEdges.some((item) => graphEdgeId(item) === graphEdgeId(edge));
    const edges = additive
      ? exists
        ? currentEdges.filter((item) => graphEdgeId(item) !== graphEdgeId(edge))
        : [...currentEdges, edge]
      : [edge];
    onSelectionChange({ kind: 'graph', item: { lens, nodes: additive && graphSelection.lens === lens ? graphSelection.nodes : [], edges } });
  }

  function zoomToSelection() {
    const ids = selectedNodeIds.length
      ? selectedNodeIds
      : graphSelection.lens === lens ? graphSelection.nodes.map(graphNodeId) : [];
    if (!ids.length) return;
    setFocusForLens(lens, { centerIds: ids, hops: focusHops });
    if (lens === 'eg') setEvidenceDensity('readable');
  }

  function resetZoom() {
    setFocusForLens(lens, { centerIds: [], hops: focusHops });
  }

  function changeFocusHops(hop) {
    const centerIds = focusCenterIds.length ? focusCenterIds : selectedNodeIds;
    setFocusForLens(lens, { centerIds, hops: hop });
    if (lens === 'eg' && centerIds.length) setEvidenceDensity('readable');
  }

  const visibleNodeIds = new Set((active.nodes || []).map(graphNodeId));
  const visibleEdgeIds = new Set((active.edges || []).map(graphEdgeId));
  const selectedHiddenNodeCount = (selectedNeighborhood.nodes || [])
    .filter((node) => !visibleNodeIds.has(graphNodeId(node))).length;
  const selectedHiddenEdgeCount = (selectedNeighborhood.edges || [])
    .filter((edge) => !visibleEdgeIds.has(graphEdgeId(edge))).length;
  const graphCanvasControls = html`
    <div className="mt-3 rounded-2xl border border-sky-200 bg-sky-50 p-3">
      ${selectedNodeIds.length ? html`
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="text-xs font-bold uppercase tracking-wide text-sky-800">Selected ${activeLensName} node(s): ${selectedNodeIds.length}</p>
            <p className="mt-1 text-xs leading-5 text-slate-600">
              Their ${focusHops}-hop neighborhood contains
              <strong>${selectedNeighborhood.nodes.length}</strong> node(s) and
              <strong>${selectedNeighborhood.edges.length}</strong> edge(s).
              ${selectedHiddenNodeCount || selectedHiddenEdgeCount
                ? `${selectedHiddenNodeCount} node(s) and ${selectedHiddenEdgeCount} edge(s) are currently outside the visible graph view.`
                : 'That neighborhood is already visible in the current view.'}
            </p>
          </div>
          <button
            onClick=${zoomToSelection}
            className="rounded-full border border-sky-700 bg-white px-4 py-2 text-xs font-bold text-sky-800 shadow-sm hover:bg-sky-100"
          >
            Show selected neighborhood
          </button>
        </div>
      ` : html`
        <p className="text-xs leading-5 text-slate-600">
          ${lens === 'eg'
            ? `The semantic graph is projected from ${rawEgNodes.length} raw Evidence Graph node(s) and ${rawEgEdges.length} raw edge(s). The readable overview prioritizes semantic concepts and the strongest relationships. Select a node to inspect supporting witnesses.`
            : `Select a ${activeLensName} node, then choose 1-hop or 2-hop to inspect its neighborhood. Double-click opens 1-hop immediately.`}
        </p>
      `}
    </div>
  `;

  return html`
    <div className="space-y-5 p-5">
      <section className="grid gap-4 lg:grid-cols-3">
        ${[
          ['Semantic Evidence Graph', semanticGraph.semantic_counts?.concepts || egNodes.length, egEdges.length, 'Customer-facing concept map; documents and witnesses support the semantic nodes'],
          ['Reasoning Graph', rgNodes.length, rgEdges.length, 'Query-local reasoning state'],
          ['Answer Graph', answerNodes.length, answerEdges.length, 'Constructed answer graph']
        ].map(([title, nodes, edges, note]) => html`
          <div key=${title} className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
            <p className="text-sm font-semibold text-slate-500">${title}</p>
            <p className="mt-2 text-3xl font-bold text-slate-900">${nodes}</p>
            <p className="text-sm text-slate-500">${edges} edge(s)</p>
            <p className="mt-3 text-sm leading-6 text-slate-700">${note}</p>
          </div>
        `)}
      </section>

      <section className="rounded-3xl border border-slate-200 bg-white p-4 shadow-sm">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex flex-wrap gap-2">
          ${[
            ['eg', 'Evidence Graph'],
            ['rg', 'Reasoning Graph'],
            ['answer', 'Answer Graph']
          ].map(([key, label]) => html`
            <button
              key=${key}
              onClick=${() => updateLens(key)}
              className=${`rounded-full px-4 py-2 text-sm font-semibold ${lens === key ? 'bg-sky-500 text-white' : 'bg-slate-100 text-slate-700 hover:bg-slate-200'}`}
            >${label}</button>
          `)}
          </div>
          ${lens === 'eg' ? html`
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs font-bold uppercase tracking-wide text-slate-500">Density</span>
              ${[
                ['readable', 'Readable'],
                ['expanded', 'Expanded'],
                ['full', 'Full delta']
              ].map(([key, label]) => html`
                <button
                  key=${key}
                  onClick=${() => setEvidenceDensity(key)}
                  className=${`rounded-full px-3 py-1.5 text-xs font-semibold ${evidenceDensity === key ? 'bg-slate-900 text-white' : 'bg-slate-100 text-slate-700 hover:bg-slate-200'}`}
                >${label}</button>
              `)}
            </div>
          ` : null}
        </div>
      </section>

      ${html`
        <section className="rounded-3xl border border-sky-200 bg-sky-50 p-4 shadow-sm">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <p className="text-sm font-bold text-slate-900">${activeLensName} zoom</p>
              <p className="mt-1 text-xs leading-5 text-slate-600">
                Select one or more ${activeLensName} nodes, then zoom into their local neighborhood.
                This keeps the graph interpretable while still allowing drill-down.
                Shift-click only adds to the selection; it does not expand the graph by itself.
              </p>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <button
                disabled=${!selectedNodeIds.length}
                onClick=${zoomToSelection}
                className="rounded-full border border-sky-700 bg-white px-4 py-2 text-xs font-bold text-sky-800 shadow-sm hover:bg-sky-100 disabled:cursor-not-allowed disabled:border-slate-300 disabled:text-slate-400 disabled:opacity-70"
              >
                Zoom to selected node(s)
              </button>
              ${[1, 2].map((hop) => html`
                <button
                  key=${hop}
                  onClick=${() => changeFocusHops(hop)}
                  className=${`rounded-full px-3 py-2 text-xs font-semibold ${focusHops === hop ? 'bg-slate-900 text-white' : 'bg-white text-slate-700 hover:bg-slate-100'}`}
                >${hop}-hop</button>
              `)}
              <button
                disabled=${!focusCenterIds.length}
                onClick=${resetZoom}
                className="rounded-full border border-slate-300 bg-white px-3 py-2 text-xs font-semibold text-slate-700 disabled:cursor-not-allowed disabled:opacity-40"
              >
                Reset overview
              </button>
            </div>
          </div>
          ${focusCenterIds.length ? html`
            <p className="mt-3 break-all rounded-2xl bg-white/80 p-3 text-xs text-slate-600">
              Focus center(s): ${focusCenterIds.join(', ')}
            </p>
          ` : null}
        </section>
      `}

      <${GraphCanvas}
        title=${activeTitle}
        nodes=${active.nodes}
        edges=${active.edges}
        selectedNodeIds=${selectedNodeIds}
        selectedEdgeIds=${selectedEdgeIds}
        onSelectNode=${selectNode}
        onSelectEdge=${selectEdge}
        onClearSelection=${() => onSelectionChange({ kind: 'graph', item: { lens, nodes: [], edges: [] } })}
        onFocusNode=${(nodeId) => {
          setFocusForLens(lens, { centerIds: [nodeId], hops: 1 });
          if (lens === 'eg') setEvidenceDensity('readable');
        }}
        controls=${graphCanvasControls}
      />

      ${lens === 'answer' ? html`
        <section className="rounded-3xl border border-indigo-200 bg-indigo-50 p-5 shadow-sm">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <p className="text-xs font-bold uppercase tracking-wide text-indigo-700">Human-readable answer</p>
              <h2 className="mt-1 text-lg font-bold text-slate-900">Actual response to the research question</h2>
            </div>
            <span className="rounded-full bg-white px-3 py-1 text-xs font-bold text-indigo-800">${model.overview.confidence_label || 'confidence not labeled'}</span>
          </div>
          <p className="mt-3 text-sm leading-7 text-slate-800">
            ${answerSummary.headline || model.overview.answer_text || 'No answer narrative was emitted for this run.'}
          </p>
          <div className="mt-4 grid gap-3 md:grid-cols-2">
            ${answerSummary.groups.map(([label, values]) => html`
              <div key=${label} className="rounded-2xl bg-white/80 p-3">
                <p className="text-xs font-bold uppercase tracking-wide text-indigo-700">${label}</p>
                ${values.length ? html`
                  <div className="mt-2 flex flex-wrap gap-2">
                    ${values.map((value) => html`<span key=${value} className="rounded-full bg-indigo-50 px-3 py-1 text-xs font-semibold text-indigo-900">${value}</span>`)}
                  </div>
                ` : html`<p className="mt-2 text-xs text-slate-500">No graph candidate emitted.</p>`}
              </div>
            `)}
          </div>
          <p className="mt-3 text-xs leading-5 text-slate-500">
            This summary is derived from answer-graph candidates grouped by slot type. It is intentionally separate from the raw answer bundle text, which may still be operator-oriented.
          </p>
        </section>
      ` : null}

      <section className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_320px]">
        <div className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
          <h2 className="text-lg font-bold text-slate-900">${graphInterpretation.title}</h2>
          <p className="mt-2 text-sm leading-7 text-slate-700">${graphInterpretation.body}</p>
          <p className="mt-2 text-xs leading-5 text-slate-500">${graphInterpretation.detail}</p>
        </div>

        <div className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
          <h2 className="text-lg font-bold text-slate-900">Legend and node types</h2>
          <div className="mt-3 grid gap-2 text-xs text-slate-600">
            ${activeCategories.slice(0, 10).map(([category, count]) => {
              const style = GRAPH_NODE_STYLES[category] || GRAPH_NODE_STYLES.other;
              return html`
                <div key=${category} className="flex items-center justify-between gap-3">
                  <span><span style=${{ backgroundColor: style.color }} className="inline-block h-3 w-3 rounded-full"></span> ${style.label}</span>
                  <span className="text-slate-400">${count}</span>
                </div>
              `;
            })}
          </div>
          <div className="mt-3 space-y-2">
            ${activeTypes.slice(0, 8).map(([type, count]) => html`
              <div key=${type} className="flex items-center justify-between rounded-2xl bg-slate-50 px-3 py-2 text-sm">
                <span className="font-semibold text-slate-700">${type}</span>
                <span className="text-slate-500">${count}</span>
              </div>
            `)}
          </div>
        </div>
      </section>
    </div>
  `;
}






