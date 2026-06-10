import React from '../../vendor/react.bundle.mjs';
import { html } from '../html.js';
import { pct } from '../format.js';
import { classifyConflictRelation, relationTone } from '../conflictSemantics.js';
import { graphEdgeId, graphNodeId, graphNodeLabel, graphNodeType } from '../domain/graphModel.js';
import { CONFLICT_REVIEW_OPTIONS, reviewStatusForLabel } from '../domain/conflictReview.js';
import { ANALYST_REVIEW_OPTIONS, analystReviewStatusForLabel, reviewForObject } from '../domain/analystReview.js';
import { findExactOrApproximateRanges, focusTextAroundRanges, splitTextByRanges } from '../utils/textMatch.js';
import { witnessProvenancePath } from '../domain/witnessContext.js';

function CopyButton({ text, label = 'Copy' }) {
  return html`
    <button
      type="button"
      onClick=${() => globalThis.navigator?.clipboard?.writeText?.(String(text || '')).catch?.(() => {})}
      className="rounded-lg border border-slate-200 bg-white px-2 py-1 text-xs font-semibold text-slate-600 hover:bg-slate-50"
    >
      ${label}
    </button>
  `;
}

function HighlightedContext({ text, needle }) {
  const source = String(text || '');
  const targets = Array.isArray(needle) ? needle.filter(Boolean) : [needle].filter(Boolean);
  if (!source || !targets.length) {
    return html`<span>${source || 'No source text emitted.'}</span>`;
  }
  const ranges = findExactOrApproximateRanges(source, targets);
  if (!ranges.length) {
    return html`
      <span>
        <span className="mb-2 block rounded-lg bg-amber-50 px-3 py-2 text-xs font-semibold text-amber-800">
          No textual occurrence was found for this witness label in the bounded raw span.
        </span>
        ${source}
      </span>
    `;
  }

  const focused = focusTextAroundRanges(source, ranges);
  const parts = splitTextByRanges(focused.text, focused.ranges);

  return html`
    <span>
      ${focused.clippedStart ? html`<span className="text-slate-400">… </span>` : null}
      ${parts.map((part, index) => {
        return part.hit
          ? html`<mark key=${index} className="rounded bg-yellow-200 px-1 font-bold text-slate-950">${part.text}</mark>`
          : html`<span key=${index}>${part.text}</span>`;
      })}
      ${focused.clippedEnd ? html`<span className="text-slate-400"> …</span>` : null}
    </span>
  `;
}

function witnessHighlightTargets(witness, associatedValue, bundle = {}) {
  const kg0MentionTargets = (witness?.anchor?.metadata?._kg0_mentions || [])
    .flatMap((item) => [item?.witness, item?.name])
    .filter(Boolean);
  const isArtifactTitleReference =
    witness?.mention?.qualifiers?.synthetic === 'artifact_title'
    || witness?.intent_element?.element_detail?.supplemented === 'artifact_title'
    || ['ENTITY_DOCUMENT', 'ENTITY_DOCUMENT_REF'].includes(witness?.mention?.category);

  return [
    witness?.mention?.surface,
    associatedValue?.surface,
    witness?.intent_element?.element_detail?.surface,
    bundle.surface,
    bundle.statement,
    ...(isArtifactTitleReference ? kg0MentionTargets : [])
  ].filter(Boolean);
}

function witnessHighlightStatus(rawText, targets) {
  const source = String(rawText || '');
  if (!source) return { matched: false, label: 'No raw source text emitted.' };
  if (!targets?.length) return { matched: false, label: 'No witness label emitted.' };

  const ranges = findExactOrApproximateRanges(source, targets);
  if (!ranges.length) {
    return {
      matched: false,
      label: `No occurrence found after checking witness label and resolved reference targets: ${targets[0]}`
    };
  }

  const matchedText = source.slice(ranges[0].start, ranges[0].end);
  const resolved = ranges[0].needle && ranges[0].needle !== targets[0];
  return {
    matched: true,
    label: `${resolved ? 'Resolved reference and highlighted occurrence' : 'Highlighted occurrence'}: ${matchedText}`,
    method: ranges[0].method,
    score: ranges[0].score
  };
}

function WitnessContextCard({ title, context, fallbackBundle }) {
  const witness = context?.witness;
  const slot = context?.slot;
  const associatedValue = context?.associatedValue;
  const bundle = witness || fallbackBundle || {};
  const surface = witness?.mention?.surface || associatedValue?.surface || bundle.surface || bundle.statement || '';
  const highlightTargets = witnessHighlightTargets(witness, associatedValue, bundle);
  const rawText = witness?.anchor?.raw_text || associatedValue?.raw_text || '';
  const highlightStatus = witnessHighlightStatus(rawText, highlightTargets);
  const artifactName = witness?.anchor?.metadata?.artifact_name || witness?.anchor?.artifact_id || bundle.artifact_id || 'Unknown artifact';
  const pageLabel = witness?.anchor?.metadata?.page_label;
  const provenancePath = witness ? witnessProvenancePath(witness) : (bundle.path || []).filter(Boolean);

  return html`
    <section className="rounded-2xl border border-slate-200 bg-white p-4">
      <p className="text-xs font-bold uppercase tracking-wide text-slate-500">${title}</p>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        ${slot ? html`<span className="rounded-full bg-sky-100 px-3 py-1 text-xs font-bold text-sky-800">${slot.slot_id} ${slot.slot_type}</span>` : null}
        ${witness?.quality ? html`<span className="rounded-full bg-emerald-100 px-3 py-1 text-xs font-semibold text-emerald-800">${witness.quality}</span>` : null}
      </div>
      <p className="mt-3 text-sm font-semibold text-slate-900">${surface || bundle.witness_id || bundle.object_id || 'Witness'}</p>
      ${associatedValue ? html`
        <p className="mt-1 text-xs text-slate-500">Associated value: ${associatedValue.surface} · ${associatedValue.category || 'value'} · confidence ${pct(associatedValue.confidence)}</p>
      ` : null}
      ${rawText ? html`
        <p className="mt-3 text-xs text-slate-500">${artifactName}${pageLabel ? ` · ${pageLabel}` : ''}</p>
        <blockquote className="mt-2 max-h-48 overflow-y-auto rounded-xl border-l-2 border-indigo-300 bg-indigo-50 px-3 py-2 text-sm leading-6 text-slate-700">
          <${HighlightedContext} text=${rawText} needle=${highlightTargets} />
        </blockquote>
        <p className=${`mt-2 text-xs ${highlightStatus.matched ? 'text-slate-500' : 'text-amber-700'}`}>
          ${highlightStatus.label}
        </p>
      ` : html`
        <p className="mt-3 rounded-xl bg-amber-50 p-3 text-sm leading-6 text-amber-800">
          This object carries a witness id/path but not the raw occurrence context directly. The system should resolve it through the run witness-context index.
        </p>
      `}
      ${witness?.justification || bundle.justification ? html`<p className="mt-3 text-sm leading-6 text-slate-700"><strong>Why:</strong> ${witness?.justification || bundle.justification}</p>` : null}
      ${provenancePath.length ? html`
        <div className="mt-3 rounded-xl bg-slate-50 p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-xs font-bold uppercase tracking-wide text-slate-500">Provenance path</p>
            <${CopyButton} text=${provenancePath.join(' -> ')} label="Copy path" />
          </div>
          <p className="mt-2 break-all text-xs text-slate-600">${provenancePath.join(' → ')}</p>
        </div>
      ` : null}
    </section>
  `;
}

function graphNodeWitnessContext(model, node) {
  const p = node.properties || {};
  const meta = p.domainMetadata || {};
  const candidates = [
    graphNodeId(node),
    p.sourceReference,
    p.object_id,
    p.witness_id,
    meta.witness_id
  ].filter(Boolean);

  for (const id of candidates) {
    if (model?.witnesses?.by_id?.[id]) return model.witnesses.by_id[id];
  }

  const answerCandidate = model?.trace?.candidate_by_id?.[graphNodeId(node)];
  if (answerCandidate?.object_id && model?.witnesses?.by_id?.[answerCandidate.object_id]) {
    return model.witnesses.by_id[answerCandidate.object_id];
  }

  return null;
}

function uniqueWitnessContexts(model, witnessIds) {
  const seen = new Set();
  return (witnessIds || [])
    .filter(Boolean)
    .filter((id) => {
      if (seen.has(id)) return false;
      seen.add(id);
      return true;
    })
    .map((witnessId) => model?.witnesses?.by_id?.[witnessId])
    .filter(Boolean);
}

function normalizeForWitnessLookup(value) {
  return String(value || '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, ' ')
    .trim();
}

function witnessIdsForEmbeddedWitnessLabels(model, text) {
  const source = String(text || '');
  if (!source.includes('Witness:')) return [];
  const references = [...source.matchAll(/Witness:\s*([A-Z]+)\/[^']*'([^']+)'/g)]
    .map((match) => ({ slotType: match[1], surface: match[2] }));
  if (!references.length) return [];

  const contexts = Object.values(model?.witnesses?.by_id || {});
  const ids = [];
  for (const reference of references) {
    const wantedSurface = normalizeForWitnessLookup(reference.surface);
    for (const context of contexts) {
      const witness = context?.witness;
      const associatedValue = context?.associatedValue;
      const slot = context?.slot;
      const surfaces = [
        witness?.mention?.surface,
        associatedValue?.surface,
        witness?.intent_element?.element_detail?.surface
      ].map(normalizeForWitnessLookup);
      if (slot?.slot_type === reference.slotType && surfaces.includes(wantedSurface)) {
        ids.push(witness.witness_id);
      }
    }
  }
  return ids;
}

function witnessIdsForSlots(model, slotPairs) {
  const witnessIds = [];
  for (const [slotId, slotType] of slotPairs || []) {
    if (!slotId && !slotType) continue;
    const slotBinding = (model?.alignment?.slot_bindings || []).find((slot) =>
      (slotId && slot.slot_id === slotId) || (slotType && slot.slot_type === slotType)
    );
    for (const witness of slotBinding?.witnesses || []) {
      if (witness?.witness_id) witnessIds.push(witness.witness_id);
    }
  }
  return witnessIds;
}

function graphNodeWitnessResolution(model, node) {
  const exact = graphNodeWitnessContext(model, node);
  if (exact) {
    return {
      contexts: [exact],
      reason: 'This graph node directly references a witness object.',
      checked: ['node id', 'source reference', 'object id', 'witness id']
    };
  }

  const p = node.properties || {};
  const meta = p.domainMetadata || {};
  const directWitnessIds = [
    graphNodeId(node),
    p.sourceReference,
    p.object_id,
    p.witness_id,
    meta.witness_id,
    meta.object_id,
    meta.sourceUID,
    meta.targetUID,
    meta.source_witness_id,
    meta.target_witness_id,
    ...(Array.isArray(p.itemIds) ? p.itemIds : []),
    ...(Array.isArray(meta.itemIds) ? meta.itemIds : []),
    ...(Array.isArray(meta.witness_ids) ? meta.witness_ids : []),
    ...witnessIdsForEmbeddedWitnessLabels(model, `${p.label || ''} ${p.description || ''} ${p.contentExcerpt || ''}`)
  ].filter((id) => typeof id === 'string' && (id.startsWith('wit-') || id.startsWith('witness-')));

  const requestedSlots = [
    [meta.slot_id || p.slot_id, meta.slot_type || p.slot_type],
    [meta.premise_slot_id || p.premise_slot_id, meta.premise_slot_type || p.premise_slot_type],
    [meta.conclusion_slot_id || p.conclusion_slot_id, meta.conclusion_slot_type || p.conclusion_slot_type]
  ].filter(([slotId, slotType]) => slotId || slotType);

  const witnessIds = [...directWitnessIds, ...witnessIdsForSlots(model, requestedSlots)];
  const contexts = uniqueWitnessContexts(model, witnessIds);
  const checked = [
    'direct witness ids',
    'source/object references',
    'source/target witness UIDs',
    'embedded Witness: SLOT→surface labels',
    'artifact-set item ids',
    'slot metadata',
    'premise slot metadata',
    'conclusion slot metadata'
  ];

  let reason = contexts.length
    ? 'This graph node resolves to witness structures through available graph metadata.'
    : `No witness structures resolved after checking ${checked.join(', ')}.`;
  if (contexts.length && (meta.premise_slot_id || meta.conclusion_slot_id)) {
    reason = `This reasoning node resolves witnesses through its premise slot ${meta.premise_slot_id || '—'} ${meta.premise_slot_type || ''} and conclusion slot ${meta.conclusion_slot_id || '—'} ${meta.conclusion_slot_type || ''}.`;
  } else if (contexts.length && requestedSlots.length) {
    reason = `This graph node resolves witnesses through slot metadata ${requestedSlots.map(([id, type]) => `${id || '—'} ${type || ''}`).join(', ')}.`;
  }

  return { contexts, reason, checked };
}

function graphNodeWitnessContexts(model, node) {
  return graphNodeWitnessResolution(model, node).contexts;
}

function graphNodesById(model) {
  const nodes = [
    ...(model?.trace?.eg_delta?.nodes || []),
    ...(model?.trace?.rg_trace?.nodes || []),
    ...(model?.answer?.graph?.nodes || [])
  ];
  return Object.fromEntries(nodes.map((node) => [graphNodeId(node), node]));
}

function graphEdgeWitnessResolution(model, edge) {
  const p = edge.properties || {};
  const directIds = [
    p.source_object_id,
    p.target_object_id,
    p.object_id,
    p.witness_id,
    ...(Array.isArray(p.witness_ids) ? p.witness_ids : [])
  ].filter(Boolean);
  const nodesById = graphNodesById(model);
  const sourceNode = nodesById[edge.from || edge.source];
  const targetNode = nodesById[edge.to || edge.target];
  const sourceResolution = sourceNode ? graphNodeWitnessResolution(model, sourceNode) : { contexts: [], checked: [] };
  const targetResolution = targetNode ? graphNodeWitnessResolution(model, targetNode) : { contexts: [], checked: [] };
  const contexts = [
    ...uniqueWitnessContexts(model, directIds),
    ...sourceResolution.contexts,
    ...targetResolution.contexts
  ];
  const uniqueContexts = uniqueWitnessContexts(model, contexts.map((context) => context.witness?.witness_id));
  const checked = [
    'edge witness ids',
    'edge object ids',
    'source endpoint node',
    'target endpoint node'
  ];
  return {
    contexts: uniqueContexts,
    reason: uniqueContexts.length
      ? 'This graph edge resolves witnesses through its direct metadata and/or source/target endpoint nodes.'
      : `No witness structures resolved after checking ${checked.join(', ')}.`,
    checked
  };
}

function AnalystReviewPanel({ objectType, objectId, objectLabel, analystReviews = {}, onSaveAnalystReview, context = {} }) {
  if (!objectType || !objectId) return null;
  const currentReview = reviewForObject(analystReviews, objectType, objectId);
  return html`
    <section className="rounded-2xl border border-emerald-200 bg-emerald-50 p-4">
      <p className="text-xs font-bold uppercase tracking-wide text-emerald-700">Analyst review</p>
      <p className="mt-2 text-sm leading-6 text-slate-700">
        Current decision:
        <strong>${currentReview.review_label === 'AGREE' && currentReview.review_status === 'default' ? 'AGREE (default)' : currentReview.review_label}</strong>
      </p>
      <p className="mt-1 break-all text-xs text-slate-500">${objectLabel || objectId}</p>
      ${currentReview.updated_at ? html`<p className="mt-1 text-xs text-slate-500">Updated ${currentReview.updated_at}</p>` : null}
      <div className="mt-3 grid gap-2">
        ${ANALYST_REVIEW_OPTIONS.map(([value, label, description]) => html`
          <button
            key=${value}
            onClick=${() => onSaveAnalystReview && onSaveAnalystReview(objectType, objectId, {
              review_label: value,
              review_status: analystReviewStatusForLabel(value),
              notes: description,
              context
            })}
            className=${`rounded-xl border px-3 py-2 text-left ${currentReview.review_label === value ? 'border-emerald-400 bg-white text-emerald-900' : 'border-slate-200 bg-white/70 text-slate-700 hover:bg-white'}`}
          >
            <span className="text-sm font-semibold">${label}</span>
            <span className="mt-1 block text-xs leading-5 text-slate-500">${description}</span>
          </button>
        `)}
      </div>
    </section>
  `;
}

function WitnessResolutionNote({ resolution }) {
  return html`
    <p className=${`mt-3 rounded-xl p-3 text-xs leading-5 ${resolution?.contexts?.length ? 'bg-indigo-50 text-indigo-900' : 'bg-amber-50 text-amber-900'}`}>
      ${resolution?.reason || 'No witness-resolution attempt was recorded for this graph object.'}
    </p>
  `;
}

function renderGraphNodeDetails(node, lens, model) {
  const p = node.properties || {};
  const meta = p.domainMetadata || {};
  const type = graphNodeType(node);
  const witnessResolution = graphNodeWitnessResolution(model, node);
  const witnessContexts = witnessResolution.contexts;
  const witnessContext = witnessContexts[0];

  if (witnessContext) {
    return html`
      <div className="mt-3 space-y-3">
        <p className="rounded-xl bg-indigo-50 p-3 text-xs leading-5 text-indigo-900">
          ${witnessResolution.reason}
        </p>
        ${witnessContexts.slice(0, 8).map((context) => html`
          <${WitnessContextCard} key=${context.witness.witness_id} title=${`Resolved witness ${context.witness.witness_id}`} context=${context} />
        `)}
        ${witnessContexts.length > 8 ? html`
          <p className="rounded-xl bg-slate-50 p-3 text-xs text-slate-600">
            ${witnessContexts.length - 8} additional witness structure(s) are associated with this reasoning node. They are visible in ALIGN/TRACE and can be added here if the drawer needs a full expansion control.
          </p>
        ` : null}
      </div>
    `;
  }

  if (lens === 'answer') {
    const candidate = model?.trace?.candidate_by_id?.[graphNodeId(node)];
    return html`
      <div className="mt-3 grid gap-2 text-sm text-slate-700">
        ${node.slot_type ? html`<p><strong>Slot:</strong> ${node.slot_type}</p>` : null}
        ${candidate ? html`
          <p><strong>Candidate surface:</strong> ${candidate.surface || '—'}</p>
          <p><strong>Witness object:</strong> ${candidate.object_id || '—'}</p>
          <p><strong>Quality:</strong> ${candidate.quality || '—'}</p>
        ` : null}
      </div>
    `;
  }

  if (type === 'Artifact') {
    return html`
      <div className="mt-3 space-y-2 text-sm text-slate-700">
        <p><strong>Date:</strong> ${p.temporalStart || meta.artifact_date || '—'}</p>
        <p><strong>Family:</strong> ${meta.family || '—'}</p>
        <p><strong>Reliability:</strong> ${p.reliabilityScore != null ? pct(p.reliabilityScore) : '—'}</p>
        ${p.contentExcerpt ? html`<p><strong>Excerpt:</strong> ${p.contentExcerpt}</p>` : null}
        ${lens !== 'answer' ? html`<${WitnessResolutionNote} resolution=${witnessResolution} />` : null}
      </div>
    `;
  }

  if (p.domainType === 'AlignBundle::Witness') {
    return html`
      <div className="mt-3 space-y-2 text-sm text-slate-700">
        <p><strong>Slot:</strong> ${meta.element_id || '—'} ${meta.slot_type || ''}</p>
        <p><strong>Surface:</strong> ${meta.surface || '—'}</p>
        <p><strong>Anchor:</strong> ${meta.anchor_id || '—'}</p>
        <p><strong>Mention:</strong> ${meta.mention_id || '—'}</p>
        <p><strong>Reliability:</strong> ${p.reliabilityScore != null ? pct(p.reliabilityScore) : '—'}</p>
        ${p.description ? html`<p><strong>Why:</strong> ${p.description}</p>` : null}
      </div>
    `;
  }

  if (type === 'Claim' || p.type === 'finding') {
    return html`
      <div className="mt-3 space-y-2 text-sm text-slate-700">
        <p><strong>Status:</strong> ${p.status || meta.status || '—'}</p>
        <p><strong>Confidence:</strong> ${p.confidenceScore != null ? pct(p.confidenceScore) : '—'}</p>
        <p><strong>Slot:</strong> ${meta.slot_id || '—'} ${meta.slot_type || ''}</p>
        <p><strong>Witness count:</strong> ${meta.witness_count ?? '-'}</p>
        ${meta.slot_description ? html`<p><strong>Slot meaning:</strong> ${meta.slot_description}</p>` : null}
        ${meta.primary_anchor_address ? html`<p><strong>Primary anchor:</strong> ${meta.primary_anchor_address}</p>` : null}
        ${p.confidenceRationale ? html`<p><strong>Rationale:</strong> ${p.confidenceRationale}</p>` : null}
        ${meta.support_summary ? html`<p><strong>Support:</strong> ${meta.support_summary}</p>` : null}
        ${lens !== 'answer' ? html`<${WitnessResolutionNote} resolution=${witnessResolution} />` : null}
      </div>
    `;
  }

  if (type === 'ProvenanceEvent') {
    return html`
      <div className="mt-3 space-y-2 text-sm text-slate-700">
        <p><strong>Action:</strong> ${p.action || '—'}</p>
        <p><strong>Timestamp:</strong> ${p.timestamp || '—'}</p>
        ${p.notes ? html`<p><strong>Notes:</strong> ${p.notes}</p>` : null}
        ${lens !== 'answer' ? html`<${WitnessResolutionNote} resolution=${witnessResolution} />` : null}
      </div>
    `;
  }

  if (['inductive', 'deductive', 'abductive', 'other'].includes(type) || node.labels?.includes('Inference')) {
    return html`
      <div className="mt-3 space-y-2 text-sm text-slate-700">
        <p><strong>Reasoning type:</strong> ${type}</p>
        <p><strong>Premise slot:</strong> ${meta.premise_slot_id || '?'} ${meta.premise_slot_type || ''}</p>
        <p><strong>Conclusion slot:</strong> ${meta.conclusion_slot_id || '?'} ${meta.conclusion_slot_type || ''}</p>
        <p className="rounded-xl bg-amber-50 p-3 text-xs leading-5 text-amber-900">
          ${witnessResolution.reason}
        </p>
      </div>
    `;
  }

  if (type === 'GraphRoot') {
    return html`
      <div className="mt-3 space-y-2 text-sm text-slate-700">
        <p><strong>Graph type:</strong> ${p.graphType || '—'}</p>
        ${p.question ? html`<p><strong>Question:</strong> ${p.question}</p>` : null}
        ${p.purpose ? html`<p><strong>Purpose:</strong> ${p.purpose}</p>` : null}
        ${lens !== 'answer' ? html`<${WitnessResolutionNote} resolution=${witnessResolution} />` : null}
      </div>
    `;
  }

  return html`
    <div className="mt-3 space-y-2 text-sm text-slate-700">
      ${p.description ? html`<p><strong>Description:</strong> ${p.description}</p>` : null}
      ${p.statement ? html`<p><strong>Statement:</strong> ${p.statement}</p>` : null}
      ${p.notes ? html`<p><strong>Notes:</strong> ${p.notes}</p>` : null}
      ${lens !== 'answer' ? html`<${WitnessResolutionNote} resolution=${witnessResolution} />` : null}
    </div>
  `;
}

function edgeEndpointLabel(model, edge, field) {
  const id = edge[field] || edge[field === 'from' ? 'source' : 'target'];
  const pools = [
    ...(model?.trace?.eg_delta?.nodes || []),
    ...(model?.trace?.rg_trace?.nodes || []),
    ...(model?.answer?.graph?.nodes || [])
  ];
  const node = pools.find((item) => graphNodeId(item) === id);
  return node ? graphNodeLabel(node) : id;
}

function renderGraphEdgeDetails(edge, model, lens) {
  const p = edge.properties || {};
  const sourceLabel = edgeEndpointLabel(model, edge, 'from');
  const targetLabel = edgeEndpointLabel(model, edge, 'to');
  const witnessResolution = lens !== 'answer' ? graphEdgeWitnessResolution(model, edge) : { contexts: [], reason: '' };
  return html`
    <div className="mt-3 space-y-2 text-sm text-slate-700">
      <p><strong>From:</strong> ${sourceLabel}</p>
      <p><strong>To:</strong> ${targetLabel}</p>
      ${p.role ? html`<p><strong>Role:</strong> ${p.role}</p>` : null}
      ${p.relationship ? html`<p><strong>Relationship:</strong> ${p.relationship}</p>` : null}
      ${p.description ? html`<p><strong>Description:</strong> ${p.description}</p>` : null}
      ${p.statement ? html`<p><strong>Statement:</strong> ${p.statement}</p>` : null}
      ${p.justification ? html`<p><strong>Why:</strong> ${p.justification}</p>` : null}
      ${p.confidence != null ? html`<p><strong>Confidence:</strong> ${pct(p.confidence)}</p>` : null}
      ${p.weight != null ? html`<p><strong>Weight:</strong> ${p.weight}</p>` : null}
      ${p.source ? html`<p><strong>Source:</strong> ${p.source}</p>` : null}
      ${lens !== 'answer' ? html`
        <div className="mt-3 space-y-3">
          <p className=${`rounded-xl p-3 text-xs leading-5 ${witnessResolution.contexts.length ? 'bg-indigo-50 text-indigo-900' : 'bg-amber-50 text-amber-900'}`}>
            ${witnessResolution.reason}
          </p>
          ${witnessResolution.contexts.slice(0, 6).map((context) => html`
            <${WitnessContextCard} key=${context.witness.witness_id} title=${`Resolved edge witness ${context.witness.witness_id}`} context=${context} />
          `)}
          ${witnessResolution.contexts.length > 6 ? html`
            <p className="rounded-xl bg-slate-50 p-3 text-xs text-slate-600">
              ${witnessResolution.contexts.length - 6} additional witness structure(s) are associated with this edge through its endpoints.
            </p>
          ` : null}
        </div>
      ` : null}
    </div>
  `;
}

export function DetailDrawer({ model, selection, conflictReviews = {}, analystReviews = {}, onSaveConflictReview, onSaveAnalystReview, onClose }) {
  if (!selection) {
    return html`<aside className="detail-drawer-empty border-l border-slate-200 bg-white p-5 text-center text-sm text-slate-500">Select a finding, conflict, or witness to inspect details.</aside>`;
  }

  if (selection.kind === 'finding') {
    const finding = selection.item;
    const witnessId = finding.witness_bundle?.witness_id || finding.supporting_object_ids?.[0];
    const witnessContext = model?.witnesses?.by_id?.[witnessId];
    const supportingContexts = (finding.supporting_object_ids || [])
      .map((id) => model?.witnesses?.by_id?.[id])
      .filter(Boolean);
    return html`
      <aside className="detail-drawer fixed inset-y-0 right-0 z-20 flex flex-col border-l border-slate-200 bg-white shadow-2xl">
        <div className="flex items-start justify-between gap-3 border-b border-slate-200 p-4">
          <div>
            <p className="text-xs font-bold uppercase tracking-wide text-sky-700">Finding ${finding.display_id}</p>
            <p className="mt-2 text-sm leading-6 text-slate-700">${finding.statement}</p>
          </div>
          <div className="flex flex-wrap gap-2">
            <${CopyButton} text=${finding.finding_id || finding.display_id} label="Copy ID" />
            <button onClick=${onClose} className="rounded-lg bg-slate-100 px-2 py-1 text-xs font-semibold text-slate-600">Close</button>
          </div>
        </div>
        <div className="flex-1 space-y-4 overflow-y-auto p-4 text-sm text-slate-700">
          <${AnalystReviewPanel}
            objectType="finding"
            objectId=${finding.finding_id || finding.display_id}
            objectLabel=${finding.statement}
            analystReviews=${analystReviews}
            onSaveAnalystReview=${onSaveAnalystReview}
            context=${{ display_id: finding.display_id, slot_type: finding.slot_type }}
          />
          <p><strong>Slot:</strong> ${finding.slot_type}</p>
          <p><strong>Evidence strength:</strong> ${pct(finding.evidence_strength)}</p>
          <p><strong>Supporting objects:</strong> ${(finding.supporting_object_ids || []).length}</p>
          <p><strong>Conflicts:</strong> ${(finding.conflict_edge_ids || []).length}</p>
          <${WitnessContextCard} title="Supporting witness in context" context=${witnessContext} fallbackBundle=${finding.witness_bundle} />
          ${supportingContexts.length > 1 ? html`
            <section className="rounded-2xl border border-slate-200 bg-white p-4">
              <p className="text-xs font-bold uppercase tracking-wide text-slate-500">All supporting witness contexts</p>
              <div className="mt-3 space-y-3">
                ${supportingContexts.map((context) => html`
                  <${WitnessContextCard} key=${context.witness.witness_id} title=${context.witness.witness_id} context=${context} />
                `)}
              </div>
            </section>
          ` : null}
        </div>
      </aside>
    `;
  }

  if (selection.kind === 'witness') {
    const { witness, slot, associatedValue } = selection.item;
    const highlightTargets = witnessHighlightTargets(witness, associatedValue);
    const highlightStatus = witnessHighlightStatus(witness.anchor?.raw_text, highlightTargets);
    const provenancePath = [
      witness.anchor?.artifact_id,
      witness.anchor?.anchor_id,
      witness.mention?.mention_id,
      witness.witness_id
    ].filter(Boolean);
    const pageImage = witness.anchor?.metadata?.page_image;
    return html`
      <aside className="detail-drawer fixed inset-y-0 right-0 z-20 flex flex-col border-l border-slate-200 bg-white shadow-2xl">
        <div className="flex items-start justify-between gap-3 border-b border-slate-200 p-4">
          <div>
            <p className="text-xs font-bold uppercase tracking-wide text-indigo-700">Witness ${witness.witness_id}</p>
            <p className="mt-2 text-base font-bold leading-6 text-slate-900">${witness.mention?.surface || 'Unnamed witness'}</p>
            <p className="mt-1 text-sm text-slate-500">${slot.slot_id} ${slot.slot_type} · ${witness.quality}</p>
          </div>
          <div className="flex flex-wrap gap-2">
            <${CopyButton} text=${witness.witness_id} label="Copy ID" />
            <button onClick=${onClose} className="rounded-lg bg-slate-100 px-2 py-1 text-xs font-semibold text-slate-600">Close</button>
          </div>
        </div>

        <div className="flex-1 space-y-4 overflow-y-auto p-4 text-sm text-slate-700">
          <${AnalystReviewPanel}
            objectType="witness"
            objectId=${witness.witness_id}
            objectLabel=${witness.mention?.surface || witness.witness_id}
            analystReviews=${analystReviews}
            onSaveAnalystReview=${onSaveAnalystReview}
            context=${{ slot_id: slot.slot_id, slot_type: slot.slot_type, quality: witness.quality }}
          />
          <section className="rounded-2xl bg-sky-50 p-4">
            <p className="text-xs font-bold uppercase tracking-wide text-sky-700">Associated value object</p>
            ${associatedValue ? html`
              <p className="mt-2 text-sm font-semibold text-slate-900">${associatedValue.surface}</p>
              <p className="mt-1 text-xs text-slate-600">${associatedValue.category} · confidence ${pct(associatedValue.confidence)}</p>
              ${associatedValue.anchor_address ? html`<p className="mt-1 text-xs text-slate-600">${associatedValue.anchor_address}</p>` : null}
            ` : html`<p className="mt-2 text-sm text-slate-600">No exact bound value object matched this witness surface.</p>`}
          </section>

          <section className="rounded-2xl border border-slate-200 bg-white p-4">
            <p className="text-xs font-bold uppercase tracking-wide text-slate-500">Source text / bounded span</p>
            <p className="mt-2 text-xs text-slate-500">
              ${witness.anchor?.metadata?.artifact_name || witness.anchor?.artifact_id || 'Unknown artifact'}
              ${witness.anchor?.metadata?.page_label ? ` · ${witness.anchor.metadata.page_label}` : ''}
            </p>
            <blockquote className="mt-3 max-h-48 overflow-y-auto rounded-xl border-l-2 border-indigo-300 bg-indigo-50 px-3 py-2 text-sm leading-6 text-slate-700">
              <${HighlightedContext} text=${witness.anchor?.raw_text} needle=${highlightTargets} />
            </blockquote>
            <p className="mt-2 text-xs text-slate-500">
              Requested string: ${witness.mention?.surface || '-'} | Mention span: ${witness.mention?.span_start ?? '-'}-${witness.mention?.span_end ?? '-'}
            </p>
            <p className=${`mt-1 text-xs ${highlightStatus.matched ? 'text-slate-500' : 'text-amber-700'}`}>
              ${highlightStatus.label}${highlightStatus.matched && highlightStatus.method ? ` | ${highlightStatus.method}${highlightStatus.score != null ? ` ${Math.round(highlightStatus.score * 100)}%` : ''}` : ''}
            </p>
          </section>

          <section className="rounded-2xl border border-slate-200 bg-white p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="text-xs font-bold uppercase tracking-wide text-slate-500">Provenance path</p>
              <${CopyButton} text=${provenancePath.join(' -> ')} label="Copy path" />
            </div>
            <div className="mt-3 space-y-2">
              ${provenancePath.map((step, index) => html`
                <div key=${step} className="flex items-center gap-3">
                  <div className="flex h-7 w-7 items-center justify-center rounded-full bg-slate-100 text-xs font-bold text-slate-700">${index + 1}</div>
                  <p className="break-all text-sm text-slate-700">${step}</p>
                </div>
              `)}
            </div>
          </section>

          <section className="rounded-2xl border border-slate-200 bg-white p-4">
            <p className="text-xs font-bold uppercase tracking-wide text-slate-500">Parent witnesses</p>
            ${witness.parent_witnesses?.length ? html`
              <div className="mt-3 space-y-2">
                ${witness.parent_witnesses.map((item) => html`
                  <div key=${item} className="rounded-xl bg-slate-50 p-3 text-sm text-slate-700">${item}</div>
                `)}
              </div>
            ` : html`<p className="mt-2 text-sm text-slate-500">No parent witnesses recorded for this witness.</p>`}
          </section>

          <section className="rounded-2xl border border-slate-200 bg-white p-4">
            <p className="text-xs font-bold uppercase tracking-wide text-slate-500">Page image</p>
            ${pageImage ? html`
              <p className="mt-2 break-all text-sm text-slate-700">${pageImage}</p>
              <p className="mt-2 text-xs leading-5 text-slate-500">
                The bundle includes a page-image reference. This sample workspace does not currently contain the backing image asset,
                but the drawer is ready to render it when the document store is mounted.
              </p>
            ` : html`<p className="mt-2 text-sm text-slate-500">No page image reference emitted.</p>`}
          </section>
        </div>
      </aside>
    `;
  }

  if (selection.kind === 'graph') {
    const { lens, nodes = [], edges = [] } = selection.item;
    return html`
      <aside className="detail-drawer fixed inset-y-0 right-0 z-20 flex flex-col border-l border-slate-200 bg-white shadow-2xl">
        <div className="flex items-start justify-between gap-3 border-b border-slate-200 p-4">
          <div>
            <p className="text-xs font-bold uppercase tracking-wide text-sky-700">Graph selection</p>
            <p className="mt-2 text-base font-bold text-slate-900">${lens === 'eg' ? 'Evidence Graph' : lens === 'rg' ? 'Reasoning Graph' : 'Answer Graph'}</p>
            <p className="mt-1 text-sm text-slate-500">${nodes.length} node(s) · ${edges.length} edge(s)</p>
          </div>
          <div className="flex flex-wrap gap-2">
            <${CopyButton} text=${[...nodes.map(graphNodeId), ...edges.map(graphEdgeId)].join('\n')} label="Copy IDs" />
            <button onClick=${onClose} className="rounded-lg bg-slate-100 px-2 py-1 text-xs font-semibold text-slate-600">Close</button>
          </div>
        </div>
        <div className="flex-1 space-y-4 overflow-y-auto p-4 text-sm text-slate-700">
          ${!nodes.length && !edges.length ? html`
            <div className="rounded-2xl bg-slate-50 p-4 text-sm text-slate-600">
              Select one or more nodes or edges in the graph. Shift-click adds to the current selection.
            </div>
          ` : null}

          ${nodes.length ? html`
            <section className="rounded-2xl border border-slate-200 bg-white p-4">
              <p className="text-xs font-bold uppercase tracking-wide text-slate-500">Selected nodes</p>
              <div className="mt-3 space-y-3">
                ${nodes.map((node) => {
                  const id = graphNodeId(node);
                  const label = graphNodeLabel(node);
                  const type = graphNodeType(node);
                  return html`
                    <article key=${id} className="rounded-2xl bg-slate-50 p-3">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="rounded-full bg-sky-100 px-3 py-1 text-xs font-bold text-sky-800">${type}</span>
                      </div>
                      <p className="mt-2 text-sm font-semibold leading-6 text-slate-900">${label}</p>
                      <p className="mt-1 break-all text-xs text-slate-500">${id}</p>
                      ${renderGraphNodeDetails(node, lens, model)}
                      <div className="mt-3">
                        <${AnalystReviewPanel}
                          objectType="graph_node"
                          objectId=${`${lens}:${id}`}
                          objectLabel=${label}
                          analystReviews=${analystReviews}
                          onSaveAnalystReview=${onSaveAnalystReview}
                          context=${{ lens, node_type: type, raw_id: id }}
                        />
                      </div>
                    </article>
                  `;
                })}
              </div>
            </section>
          ` : null}

          ${edges.length ? html`
            <section className="rounded-2xl border border-slate-200 bg-white p-4">
              <p className="text-xs font-bold uppercase tracking-wide text-slate-500">Selected edges</p>
              <div className="mt-3 space-y-3">
                ${edges.map((edge) => {
                  const id = graphEdgeId(edge);
                  return html`
                  <article key=${id} className="rounded-2xl bg-amber-50 p-3">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="rounded-full bg-amber-100 px-3 py-1 text-xs font-bold text-amber-800">${edge.type || 'edge'}</span>
                    </div>
                    <p className="mt-2 break-all text-xs text-slate-500">${id}</p>
                    ${renderGraphEdgeDetails(edge, model, lens)}
                    <div className="mt-3">
                      <${AnalystReviewPanel}
                        objectType="graph_edge"
                        objectId=${`${lens}:${id}`}
                        objectLabel=${`${edgeEndpointLabel(model, edge, 'from')} → ${edgeEndpointLabel(model, edge, 'to')}`}
                        analystReviews=${analystReviews}
                        onSaveAnalystReview=${onSaveAnalystReview}
                        context=${{ lens, edge_type: edge.type || 'edge', raw_id: id }}
                      />
                    </div>
                  </article>
                `;
                })}
              </div>
            </section>
          ` : null}
        </div>
      </aside>
    `;
  }

  if (selection.kind === 'answer') {
    const answer = selection.item;
    return html`
      <aside className="detail-drawer fixed inset-y-0 right-0 z-20 flex flex-col border-l border-slate-200 bg-white shadow-2xl">
        <div className="flex items-start justify-between gap-3 border-b border-slate-200 p-4">
          <div>
            <p className="text-xs font-bold uppercase tracking-wide text-emerald-700">Constructed answer</p>
            <p className="mt-2 text-sm leading-6 text-slate-700">Review the answer-level synthesis as an analyst object.</p>
          </div>
          <div className="flex flex-wrap gap-2">
            <${CopyButton} text=${answer.text || model.overview.answer_text} label="Copy answer" />
            <button onClick=${onClose} className="rounded-lg bg-slate-100 px-2 py-1 text-xs font-semibold text-slate-600">Close</button>
          </div>
        </div>
        <div className="flex-1 space-y-4 overflow-y-auto p-4 text-sm text-slate-700">
          <${AnalystReviewPanel}
            objectType="answer"
            objectId=${answer.object_id || `answer:${model.run_id}`}
            objectLabel="Constructed answer"
            analystReviews=${analystReviews}
            onSaveAnalystReview=${onSaveAnalystReview}
            context=${{ confidence: answer.confidence, run_model_id: model.run_id }}
          />
          <section className="rounded-2xl border border-slate-200 bg-white p-4">
            <p className="text-xs font-bold uppercase tracking-wide text-slate-500">Answer text</p>
            <p className="mt-3 whitespace-pre-wrap text-sm leading-7 text-slate-700">${answer.text || model.overview.answer_text || 'No answer text emitted.'}</p>
          </section>
        </div>
      </aside>
    `;
  }

  const conflict = selection.item;
  const workup = model?.conflicts?.workups?.find((item) => item.edge.edge_id === conflict.edge_id);
  const sourceContext = workup?.source?.witnessContext || model?.witnesses?.by_id?.[conflict.source_object_id];
  const targetContext = workup?.target?.witnessContext || model?.witnesses?.by_id?.[conflict.target_object_id];
  const relation = classifyConflictRelation(conflict);
  const currentReview = conflictReviews[conflict.edge_id] || {
    review_label: 'AGREEMENT',
    review_status: 'default'
  };
  return html`
    <aside className="detail-drawer detail-drawer--wide fixed inset-y-0 right-0 z-20 flex flex-col border-l border-slate-200 bg-white shadow-2xl">
      <div className="flex items-start justify-between gap-3 border-b border-slate-200 p-4">
        <div>
          <p className="text-xs font-bold uppercase tracking-wide text-amber-700">Conflict workup</p>
          <p className="mt-2 text-sm font-semibold leading-6 text-slate-800">${workup?.assessment?.label || relation.label}</p>
          <p className="mt-1 text-xs leading-5 text-slate-500">Raw detector note: ${conflict.description}</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <${CopyButton} text=${conflict.edge_id} label="Copy ID" />
          <button onClick=${onClose} className="rounded-lg bg-slate-100 px-2 py-1 text-xs font-semibold text-slate-600">Close</button>
        </div>
      </div>
      <div className="flex-1 space-y-4 overflow-y-auto p-4 text-sm text-slate-700">
        <p><strong>Detector stance:</strong> ${conflict.stance}</p>
        <p><strong>Confidence:</strong> ${pct(conflict.confidence)}</p>
        <p><strong>Rule:</strong> ${conflict.rule || conflict.defeater_type || 'conflict'}</p>
        <section className="rounded-2xl border border-slate-200 bg-white p-4">
          <div className="flex flex-wrap items-center gap-2">
            <span className=${`rounded-full px-3 py-1 text-xs font-bold ${workup?.assessment?.status === 'plurality' ? 'bg-cyan-100 text-cyan-800' : relationTone(relation)}`}>${workup?.assessment?.label || relation.label}</span>
            ${relation.isSemantic ? html`<span className="rounded-full bg-slate-100 px-3 py-1 text-xs font-semibold text-slate-700">semantic edge</span>` : html`<span className="rounded-full bg-violet-100 px-3 py-1 text-xs font-semibold text-violet-800">diagnostic edge</span>`}
          </div>
          <p className="mt-3 text-sm leading-6 text-slate-700">${relation.explanation}</p>
          <p className="mt-3 text-xs leading-5 text-slate-500">
            Treat this relationship as a claim-context comparison. Different strings are evidence for investigation,
            not by themselves proof of contradiction. Qualifications, temporal order, scope, and entity resolution matter.
          </p>
        </section>
        ${workup ? html`
          <section className="rounded-2xl border border-sky-200 bg-sky-50 p-4">
            <p className="text-xs font-bold uppercase tracking-wide text-sky-700">Fused ALIGN + TRACE + CONFLICT workup</p>
            <p className="mt-2 text-sm font-bold text-slate-900">${workup.assessment.label}</p>
            <p className="mt-2 text-sm leading-6 text-slate-700">${workup.assessment.rationale}</p>
            <div className="mt-3 grid gap-3 text-xs text-slate-600">
              <div className="rounded-xl bg-white/80 p-3">
                <p className="font-bold text-slate-700">Source candidate</p>
                <p>${workup.source.candidate?.surface || '—'} · ${workup.source.candidate?.slot_id || '—'} ${workup.source.candidate?.slot_type || ''}</p>
                <p className="mt-1">Claim: ${workup.source.claim?.properties?.statement || workup.source.candidate?.claim_uid || '—'}</p>
              </div>
              <div className="rounded-xl bg-white/80 p-3">
                <p className="font-bold text-slate-700">Target candidate</p>
                <p>${workup.target.candidate?.surface || '—'} · ${workup.target.candidate?.slot_id || '—'} ${workup.target.candidate?.slot_type || ''}</p>
                <p className="mt-1">Claim: ${workup.target.claim?.properties?.statement || workup.target.candidate?.claim_uid || '—'}</p>
              </div>
            </div>
          </section>
        ` : null}
        <div className="rounded-2xl bg-amber-50 p-4 text-sm leading-6 text-amber-900">
          The two sides below use the same witness-context highlighter as ALIGN. Use these contexts to decide whether this is refutation, corroboration, qualification, or only a surface-level diagnostic.
        </div>
        <section className="rounded-2xl border border-emerald-200 bg-emerald-50 p-4">
          <p className="text-xs font-bold uppercase tracking-wide text-emerald-700">Analyst resolution</p>
          <p className="mt-2 text-sm leading-6 text-slate-700">
            Current label:
            <strong>${currentReview.review_label === 'AGREEMENT' && currentReview.review_status === 'default' ? 'AGREEMENT (default)' : currentReview.review_label}</strong>
          </p>
          ${currentReview.updated_at ? html`<p className="mt-1 text-xs text-slate-500">Updated ${currentReview.updated_at}</p>` : null}
          <div className="mt-3 space-y-2">
            ${CONFLICT_REVIEW_OPTIONS.map(([value, label, description]) => html`
              <button
                key=${value}
                onClick=${() => onSaveConflictReview && onSaveConflictReview(conflict.edge_id, {
                  review_label: value,
                  review_status: reviewStatusForLabel(value),
                  notes: description
                })}
                className=${`block w-full rounded-xl border px-3 py-2 text-left ${currentReview.review_label === value ? 'border-emerald-400 bg-white text-emerald-900' : 'border-slate-200 bg-white/70 text-slate-700 hover:bg-white'}`}
              >
                <span className="text-sm font-semibold">${label}</span>
                <span className="mt-1 block text-xs leading-5 text-slate-500">${description}</span>
              </button>
            `)}
          </div>
        </section>
        <${WitnessContextCard} title=${`Source witness ${conflict.source_object_id}`} context=${sourceContext} fallbackBundle=${{ witness_id: conflict.source_object_id }} />
        <${WitnessContextCard} title=${`Target witness ${conflict.target_object_id}`} context=${targetContext} fallbackBundle=${{ witness_id: conflict.target_object_id }} />
        <section className="rounded-2xl border border-slate-200 bg-white p-4">
          <p className="text-xs font-bold uppercase tracking-wide text-slate-500">Affected chains</p>
          <p className="mt-2 text-sm text-slate-700">${(conflict.chain_ids || []).length} chain(s)</p>
        </section>
      </div>
    </aside>
  `;
}
