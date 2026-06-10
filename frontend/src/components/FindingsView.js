import React, { useMemo, useState } from '../../vendor/react.bundle.mjs';
import { html } from '../html.js';
import { pct } from '../format.js';
import { ANALYST_REVIEW_OPTIONS, analystReviewStatusForLabel, reviewForObject } from '../domain/analystReview.js';

const SORT_OPTIONS = [
  ['strength', 'Strength'],
  ['slot', 'Slot'],
  ['original', 'Original']
];

function normalizeSlot(slotType) {
  return String(slotType || 'UNCLASSIFIED').trim().toUpperCase() || 'UNCLASSIFIED';
}

export function findingStrength(finding) {
  const value = finding?.evidence_strength ?? finding?.confidence ?? finding?.score;
  return value == null || Number.isNaN(Number(value)) ? null : Number(value);
}

function displayStrength(finding) {
  const value = findingStrength(finding);
  return value == null ? '?' : pct(value);
}

function findingSupportCount(finding) {
  if (Array.isArray(finding?.supporting_object_ids)) return finding.supporting_object_ids.length;
  if (Array.isArray(finding?.supporting_objects)) return finding.supporting_objects.length;
  return 0;
}

function findingConflictCount(finding) {
  if (Array.isArray(finding?.conflict_edge_ids)) return finding.conflict_edge_ids.length;
  if (Array.isArray(finding?.conflicts)) return finding.conflicts.length;
  return 0;
}

function findingSearchText(finding) {
  return [
    finding?.finding_id,
    finding?.display_id,
    finding?.slot_type,
    finding?.statement,
    finding?.description,
    ...(finding?.supporting_object_ids || []),
    ...(finding?.conflict_edge_ids || [])
  ].filter(Boolean).join(' ').toLowerCase();
}

export function slotTypesForFindings(findings) {
  return Array.from(new Set((findings || []).map((finding) => normalizeSlot(finding.slot_type))))
    .sort((a, b) => a.localeCompare(b));
}

export function summarizeFindings(findings) {
  const items = findings || [];
  const strengths = items
    .map(findingStrength)
    .filter((value) => value != null);
  const averageStrength = strengths.length
    ? Number((strengths.reduce((sum, value) => sum + value, 0) / strengths.length).toFixed(2))
    : null;

  return {
    total: items.length,
    slotCount: slotTypesForFindings(items).length,
    averageStrength,
    supportCount: items.reduce((sum, finding) => sum + findingSupportCount(finding), 0),
    conflictCount: items.reduce((sum, finding) => sum + findingConflictCount(finding), 0)
  };
}

export function filterAndSortFindings(findings, { query = '', slotType = 'ALL', sortKey = 'strength' } = {}) {
  const normalizedQuery = String(query || '').trim().toLowerCase();
  const normalizedSlot = normalizeSlot(slotType);
  const filtered = (findings || []).filter((finding) => {
    if (normalizedSlot !== 'ALL' && normalizeSlot(finding.slot_type) !== normalizedSlot) return false;
    if (normalizedQuery && !findingSearchText(finding).includes(normalizedQuery)) return false;
    return true;
  });

  return filtered.map((finding, index) => ({ finding, index }))
    .sort((left, right) => {
      if (sortKey === 'slot') {
        const slotCompare = normalizeSlot(left.finding.slot_type).localeCompare(normalizeSlot(right.finding.slot_type));
        if (slotCompare) return slotCompare;
        return left.index - right.index;
      }
      if (sortKey === 'original') return left.index - right.index;
      const leftStrength = findingStrength(left.finding) ?? -1;
      const rightStrength = findingStrength(right.finding) ?? -1;
      if (rightStrength !== leftStrength) return rightStrength - leftStrength;
      return left.index - right.index;
    })
    .map(({ finding }) => finding);
}

export function groupFindingsBySlot(findings) {
  const groups = new Map();
  for (const finding of findings || []) {
    const slot = normalizeSlot(finding.slot_type);
    if (!groups.has(slot)) groups.set(slot, []);
    groups.get(slot).push(finding);
  }
  return Array.from(groups.entries());
}

function strengthTone(finding) {
  const strength = findingStrength(finding);
  if (strength == null) return 'bg-slate-100 text-slate-700';
  if (strength >= 0.8) return 'bg-emerald-100 text-emerald-800';
  if (strength >= 0.55) return 'bg-sky-100 text-sky-800';
  return 'bg-amber-100 text-amber-800';
}

function StatCard({ label, value, note }) {
  return html`
    <div className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
      <p className="text-sm font-semibold text-slate-500">${label}</p>
      <p className="mt-2 text-3xl font-bold text-slate-900">${value}</p>
      ${note ? html`<p className="mt-1 text-xs font-semibold uppercase tracking-wide text-slate-400">${note}</p>` : null}
    </div>
  `;
}

function FindingCard({ finding, onSelectFinding, analystReviews = {}, onReviewFinding }) {
  const conflicts = findingConflictCount(finding);
  const support = findingSupportCount(finding);
  const objectId = finding.finding_id || finding.display_id || finding.statement || 'finding';
  const review = reviewForObject(analystReviews, 'finding', objectId);
  return html`
    <button
      key=${finding.finding_id || finding.display_id || finding.statement}
      onClick=${() => onSelectFinding?.(finding)}
      className=${`block w-full rounded-3xl border bg-white p-5 text-left shadow-sm transition hover:border-sky-300 hover:shadow-md ${conflicts ? 'border-amber-200' : 'border-slate-200'}`}
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <span className="rounded-full bg-slate-900 px-3 py-1 text-xs font-bold text-white">${finding.display_id || finding.finding_id || 'Finding'}</span>
          <span className="rounded-full bg-indigo-100 px-3 py-1 text-xs font-semibold text-indigo-800">${normalizeSlot(finding.slot_type)}</span>
          <span className=${`rounded-full px-3 py-1 text-xs font-semibold ${strengthTone(finding)}`}>strength ${displayStrength(finding)}</span>
          ${conflicts ? html`<span className="rounded-full bg-amber-100 px-3 py-1 text-xs font-bold text-amber-800">${conflicts} conflict${conflicts === 1 ? '' : 's'}</span>` : null}
        </div>
        <span className="rounded-full bg-slate-50 px-3 py-1 text-xs font-semibold text-slate-600">${support} support${support === 1 ? '' : 's'}</span>
      </div>
      <p className="mt-4 text-base font-semibold leading-7 text-slate-900">${finding.statement || finding.description || 'No finding statement emitted.'}</p>
      <div className="mt-4 flex flex-wrap gap-2 text-xs text-slate-500">
        ${finding.finding_id ? html`<span className="rounded-full bg-slate-50 px-3 py-1">${finding.finding_id}</span>` : null}
        ${(finding.supporting_object_ids || []).slice(0, 4).map((id) => html`<span key=${id} className="rounded-full bg-sky-50 px-3 py-1 font-semibold text-sky-800">${id}</span>`)}
        ${(finding.supporting_object_ids || []).length > 4 ? html`<span className="rounded-full bg-sky-50 px-3 py-1 font-semibold text-sky-800">+${finding.supporting_object_ids.length - 4} more</span>` : null}
      </div>
      <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-slate-100 pt-3">
        <span className="text-xs font-bold uppercase tracking-wide text-slate-500">Review: ${review.review_label === 'AGREE' && review.review_status === 'default' ? 'AGREE default' : review.review_label}</span>
        ${ANALYST_REVIEW_OPTIONS.slice(0, 3).map(([value, label, description]) => html`
          <button
            type="button"
            key=${value}
            onClick=${(event) => {
              event.stopPropagation();
              onReviewFinding && onReviewFinding('finding', objectId, {
                review_label: value,
                review_status: analystReviewStatusForLabel(value),
                notes: description,
                context: { slot_type: finding.slot_type, display_id: finding.display_id }
              });
            }}
            className=${`rounded-full border px-3 py-1 text-xs font-bold ${review.review_label === value ? 'border-emerald-300 bg-emerald-50 text-emerald-800' : 'border-slate-200 bg-white text-slate-600 hover:bg-slate-50'}`}
          >
            ${label}
          </button>
        `)}
      </div>
    </button>
  `;
}

export function FindingsView({ model, onSelectFinding, analystReviews = {}, onReviewFinding }) {
  const findings = model.answer.findings || [];
  const [query, setQuery] = useState('');
  const [slotType, setSlotType] = useState('ALL');
  const [sortKey, setSortKey] = useState('strength');

  const summary = useMemo(() => summarizeFindings(findings), [findings]);
  const slotTypes = useMemo(() => slotTypesForFindings(findings), [findings]);
  const visibleFindings = useMemo(
    () => filterAndSortFindings(findings, { query, slotType, sortKey }),
    [findings, query, slotType, sortKey]
  );
  const groupedFindings = useMemo(() => groupFindingsBySlot(visibleFindings), [visibleFindings]);
  const supportNote = `${summary.supportCount} support object${summary.supportCount === 1 ? '' : 's'}`;

  return html`
    <div className="space-y-5 p-5">
      <section className="grid gap-4 md:grid-cols-4">
        <${StatCard} label="Findings" value=${summary.total} note="Construct" />
        <${StatCard} label="Slot types" value=${summary.slotCount} note=${slotTypes.slice(0, 3).join(' · ') || 'None'} />
        <${StatCard} label="Average strength" value=${summary.averageStrength == null ? '?' : pct(summary.averageStrength)} note="Evidence" />
        <${StatCard} label="Conflicts" value=${summary.conflictCount} note=${supportNote} />
      </section>

      <section className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <h2 className="text-lg font-bold text-slate-900">Findings</h2>
            <p className="mt-1 text-sm text-slate-500">${visibleFindings.length} of ${findings.length} finding${findings.length === 1 ? '' : 's'} visible</p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <input
              value=${query}
              onInput=${(event) => setQuery(event.target.value)}
              placeholder="Search findings"
              className="min-w-[220px] rounded-2xl border border-slate-200 bg-slate-50 px-4 py-2 text-sm text-slate-800 outline-none focus:border-sky-300 focus:bg-white"
            />
            <select
              value=${slotType}
              onChange=${(event) => setSlotType(event.target.value)}
              className="rounded-2xl border border-slate-200 bg-slate-50 px-4 py-2 text-sm font-semibold text-slate-700 outline-none focus:border-sky-300 focus:bg-white"
            >
              <option value="ALL">All slots</option>
              ${slotTypes.map((slot) => html`<option key=${slot} value=${slot}>${slot}</option>`)}
            </select>
            <select
              value=${sortKey}
              onChange=${(event) => setSortKey(event.target.value)}
              className="rounded-2xl border border-slate-200 bg-slate-50 px-4 py-2 text-sm font-semibold text-slate-700 outline-none focus:border-sky-300 focus:bg-white"
            >
              ${SORT_OPTIONS.map(([value, label]) => html`<option key=${value} value=${value}>Sort: ${label}</option>`)}
            </select>
          </div>
        </div>

        ${slotTypes.length ? html`
          <div className="mt-4 flex flex-wrap gap-2">
            ${['ALL', ...slotTypes].map((slot) => html`
              <button
                key=${slot}
                onClick=${() => setSlotType(slot)}
                className=${`rounded-full px-3 py-1.5 text-xs font-bold ${slotType === slot ? 'bg-slate-900 text-white' : 'bg-slate-100 text-slate-700 hover:bg-slate-200'}`}
              >
                ${slot === 'ALL' ? 'All' : slot}
              </button>
            `)}
          </div>
        ` : null}
      </section>

      ${!findings.length ? html`
        <section className="rounded-3xl border border-slate-200 bg-white p-8 text-center shadow-sm">
          <h3 className="text-lg font-bold text-slate-900">No findings emitted</h3>
          <p className="mt-2 text-sm text-slate-500">CONSTRUCT did not emit first-class findings for this run.</p>
        </section>
      ` : !visibleFindings.length ? html`
        <section className="rounded-3xl border border-slate-200 bg-white p-8 text-center shadow-sm">
          <h3 className="text-lg font-bold text-slate-900">No matching findings</h3>
          <p className="mt-2 text-sm text-slate-500">Clear the search or change the slot filter.</p>
        </section>
      ` : html`
        <div className="space-y-5">
          ${groupedFindings.map(([slot, items]) => html`
            <section key=${slot} className="space-y-3">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <h3 className="text-sm font-black uppercase tracking-wide text-slate-700">${slot}</h3>
                <span className="rounded-full bg-slate-100 px-3 py-1 text-xs font-bold text-slate-600">${items.length} finding${items.length === 1 ? '' : 's'}</span>
              </div>
              <div className="space-y-3">
                ${items.map((finding) => html`<${FindingCard} key=${finding.finding_id || finding.display_id || finding.statement} finding=${finding} onSelectFinding=${onSelectFinding} analystReviews=${analystReviews} onReviewFinding=${onReviewFinding} />`)}
              </div>
            </section>
          `)}
        </div>
      `}
    </div>
  `;
}
