import React from '../../vendor/react.bundle.mjs';
import { html } from '../html.js';
import { pct } from '../format.js';
import { classifyConflictRelation, relationTone } from '../conflictSemantics.js';

import { CONFLICT_REVIEW_FILTERS, reviewForConflict } from '../domain/conflictReview.js';
import { filterConflictWorkups } from '../domain/workbenchUx.js';



export function ConflictsView({ model, conflictReviews = {}, onSelectConflict }) {
  const [activeReviewLabel, setActiveReviewLabel] = React.useState('ALL');
  const [relationFilter, setRelationFilter] = React.useState('all');
  const [sortKey, setSortKey] = React.useState('confidence');
  const [query, setQuery] = React.useState('');
  const workups = model.conflicts.workups || [];
  const visibleWorkups = filterConflictWorkups(workups, conflictReviews, {
    reviewLabel: activeReviewLabel,
    relationFilter,
    sortKey,
    query
  });

  return html`
    <div className="space-y-4 p-5">
      <section className="grid gap-4 md:grid-cols-3">
        <div className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
          <p className="text-sm font-semibold text-slate-500">Conflict edges</p>
          <p className="mt-2 text-3xl font-bold text-slate-900">${model.conflicts.edges.length}</p>
        </div>
        <div className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
          <p className="text-sm font-semibold text-slate-500">Clusters</p>
          <p className="mt-2 text-3xl font-bold text-slate-900">${model.conflicts.clusters.length}</p>
        </div>
        <div className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
          <p className="text-sm font-semibold text-slate-500">Contested claims</p>
          <p className="mt-2 text-3xl font-bold text-slate-900">${model.conflicts.contested_claims.length}</p>
        </div>
      </section>
      <section className="rounded-3xl border border-slate-200 bg-white p-4 shadow-sm">
        <div className="flex flex-wrap items-center gap-2">
          <span className="mr-1 text-xs font-bold uppercase tracking-wide text-slate-500">Analyst label</span>
          ${CONFLICT_REVIEW_FILTERS.map(([value, label]) => html`
            <button
              key=${value}
              onClick=${() => setActiveReviewLabel(value)}
              className=${`rounded-full border px-3 py-1 text-xs font-bold ${activeReviewLabel === value ? 'border-emerald-400 bg-emerald-50 text-emerald-800' : 'border-slate-200 bg-white text-slate-600 hover:bg-slate-50'}`}
            >
              ${label}
            </button>
          `)}
        </div>
        <div className="mt-3 grid gap-3 lg:grid-cols-[1fr_auto_auto] lg:items-center">
          <input
            value=${query}
            onInput=${(event) => setQuery(event.target.value)}
            placeholder="Search conflict, source, target..."
            className="rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-sm outline-none focus:border-amber-300"
          />
          <select value=${relationFilter} onChange=${(event) => setRelationFilter(event.target.value)} className="rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm font-semibold">
            <option value="all">All relationship types</option>
            <option value="semantic">Semantic conflicts</option>
            <option value="diagnostic">Diagnostic edges</option>
          </select>
          <select value=${sortKey} onChange=${(event) => setSortKey(event.target.value)} className="rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm font-semibold">
            <option value="confidence">Sort: Confidence</option>
            <option value="review">Sort: Review label</option>
          </select>
        </div>
        <p className="mt-3 text-xs leading-5 text-slate-500">
          Unreviewed conflicts are treated as <strong>AGREEMENT</strong> until the analyst deliberately chooses another label or marks the workup unresolved.
        </p>
      </section>
      ${visibleWorkups.length ? visibleWorkups.map((workup) => {
        const conflict = workup.edge;
        const review = reviewForConflict(conflictReviews, conflict.edge_id);
        return html`
        ${(() => {
          const relation = classifyConflictRelation(conflict);
          return html`<button key=${conflict.edge_id} onClick=${() => onSelectConflict(conflict)} className="block w-full rounded-3xl border border-slate-200 bg-white p-5 text-left shadow-sm hover:border-amber-300">
          <div className="flex flex-wrap items-center gap-2">
            <span className=${`rounded-full px-3 py-1 text-xs font-bold ${relationTone(relation)}`}>${relation.label}</span>
            <span className="rounded-full bg-amber-100 px-3 py-1 text-xs font-bold text-amber-800">${conflict.rule || conflict.defeater_type || 'relationship'}</span>
            <span className="rounded-full bg-slate-100 px-3 py-1 text-xs font-semibold text-slate-700">${conflict.stance}</span>
            <span className="rounded-full bg-slate-100 px-3 py-1 text-xs font-semibold text-slate-700">confidence ${pct(conflict.confidence)}</span>
            <span className=${`rounded-full px-3 py-1 text-xs font-bold ${review.review_label === 'UNRESOLVED' ? 'bg-rose-100 text-rose-800' : review.review_status === 'default' ? 'bg-slate-100 text-slate-600' : 'bg-emerald-100 text-emerald-800'}`}>
              analyst: ${review.review_label}${review.review_status === 'default' ? ' default' : ''}
            </span>
          </div>
          <p className="mt-3 text-sm leading-7 text-slate-700">${workup.assessment.rationale}</p>
          <div className="mt-3 grid gap-3 md:grid-cols-2">
            <div className="rounded-2xl bg-slate-50 p-3">
              <p className="text-xs font-bold uppercase tracking-wide text-slate-500">Source</p>
              <p className="mt-2 text-sm font-semibold leading-6 text-slate-800">${workup.source.candidate?.surface || conflict.source_object_id || 'Unknown source'}</p>
            </div>
            <div className="rounded-2xl bg-slate-50 p-3">
              <p className="text-xs font-bold uppercase tracking-wide text-slate-500">Target</p>
              <p className="mt-2 text-sm font-semibold leading-6 text-slate-800">${workup.target.candidate?.surface || conflict.target_object_id || 'Unknown target'}</p>
            </div>
          </div>
          <div className="mt-3 rounded-2xl bg-slate-50 p-3 text-xs leading-5 text-slate-700">
            <p className="font-bold text-slate-800">Fused assessment: ${workup.assessment.label}</p>
            <p className="mt-2 text-slate-500">
              Source: ${workup.source.candidate?.surface || conflict.source_object_id}
              · Target: ${workup.target.candidate?.surface || conflict.target_object_id}
              · Chains: ${new Set([...(workup.source.chains || []), ...(workup.target.chains || []), ...(conflict.chain_ids || []).map((chain_id) => ({ chain_id }))].map((item) => item.chain_id)).size}
            </p>
            <p className="mt-2 text-slate-400">Raw detector note: ${conflict.description}</p>
          </div>
          ${!relation.isSemantic ? html`<p className="mt-3 text-xs leading-5 text-violet-700">${relation.explanation}</p>` : null}
        </button>`;
        })()}
      `}) : html`
        <section className="rounded-3xl border border-slate-200 bg-white p-8 text-center shadow-sm">
          <h3 className="text-lg font-bold text-slate-900">No matching conflicts</h3>
          <p className="mt-2 text-sm text-slate-500">Change the analyst label, relationship type, or search query.</p>
        </section>
      `}
    </div>
  `;
}


