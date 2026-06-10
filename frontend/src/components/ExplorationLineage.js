import React from '../../vendor/react.bundle.mjs';
import { html } from '../html.js';

function Pill({ children, tone = 'slate' }) {
  const tones = {
    slate: 'bg-slate-100 text-slate-700',
    sky: 'bg-sky-100 text-sky-800',
    emerald: 'bg-emerald-100 text-emerald-800',
    amber: 'bg-amber-100 text-amber-800',
    violet: 'bg-violet-100 text-violet-800'
  };
  return html`<span className=${`rounded-full px-3 py-1 text-xs font-bold ${tones[tone] || tones.slate}`}>${children}</span>`;
}

function RelatedRun({ run, onOpenRun }) {
  return html`
    <button onClick=${() => onOpenRun(run.run_id)} className="block w-full rounded-2xl border border-slate-200 bg-white p-3 text-left hover:border-sky-300">
      <div className="flex flex-wrap items-center gap-2">
        <${Pill} tone=${run.status === 'completed' ? 'emerald' : 'amber'}>${run.status}</${Pill}>
        <span className="break-all text-xs font-semibold text-slate-500">${run.run_id}</span>
      </div>
      <p className="mt-2 line-clamp-2 text-xs leading-5 text-slate-600">${run.request?.question?.text || 'No question recorded.'}</p>
    </button>
  `;
}

const SHOW_COMPARISON_AND_EXPLORATION_PANELS = false;

export function ExplorationLineage({ context, onOpenRun }) {
  if (!context) {
    return html`
      <section className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
        <p className="text-sm text-slate-500">Loading exploration context…</p>
      </section>
    `;
  }

  const { workspace, question, scope, run, related_runs = [], comparisons = [], composites = [], exploration_affordances = [] } = context;
  const lineageDetailGridClass = SHOW_COMPARISON_AND_EXPLORATION_PANELS
    ? 'mt-4 grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(280px,0.75fr)]'
    : 'mt-4 grid gap-4';

  return html`
    <section className="rounded-3xl border border-violet-200 bg-white p-5 shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs font-bold uppercase tracking-[0.2em] text-violet-700">Exploration lineage</p>
          <h2 className="mt-1 text-lg font-bold text-slate-900">${workspace?.name || 'Workspace'}</h2>
          <p className="mt-1 text-sm leading-6 text-slate-500">
            This run is one point in a larger analyst exploration: questions, scopes, runs, comparisons, and composite graph records.
          </p>
        </div>
        <${Pill} tone="violet">${run?.lineage_kind || 'run'}</${Pill}>
      </div>

      <div className="mt-4 grid gap-3 lg:grid-cols-3">
        <div className="rounded-2xl bg-slate-50 p-4">
          <p className="text-xs font-bold uppercase tracking-wide text-slate-500">Question</p>
          <p className="mt-2 text-sm font-semibold leading-6 text-slate-800">${question?.text || run?.request?.question?.text || 'No question record.'}</p>
          ${question?.question_id ? html`<p className="mt-2 break-all text-xs text-slate-500">${question.question_id}</p>` : null}
        </div>
        <div className="rounded-2xl bg-slate-50 p-4">
          <p className="text-xs font-bold uppercase tracking-wide text-slate-500">Scope</p>
          <p className="mt-2 text-sm font-semibold leading-6 text-slate-800">${scope?.name || 'Embedded run scope'}</p>
          <p className="mt-2 text-xs text-slate-500">${(scope?.collection_ids || []).length} collection(s) · ${(scope?.predicates || []).length} predicate(s)</p>
          ${scope?.relationship_to_parent ? html`<div className="mt-2"><${Pill} tone="sky">${scope.relationship_to_parent}</${Pill}></div>` : null}
        </div>
        <div className="rounded-2xl bg-slate-50 p-4">
          <p className="text-xs font-bold uppercase tracking-wide text-slate-500">Current run</p>
          <p className="mt-2 break-all text-sm font-semibold text-slate-800">${run?.run_id}</p>
          <div className="mt-2 flex flex-wrap gap-2">
            <${Pill} tone=${run?.status === 'completed' ? 'emerald' : 'amber'}>${run?.status}</${Pill}>
            ${run?.parent_run_id ? html`<${Pill} tone="sky">parent ${run.parent_run_id}</${Pill}>` : null}
          </div>
        </div>
      </div>

      <div className=${lineageDetailGridClass}>
        <div className="rounded-2xl border border-slate-200 bg-slate-50 p-4">
          <div className="mb-3 flex items-center justify-between gap-3">
            <p className="text-xs font-bold uppercase tracking-wide text-slate-500">Related runs</p>
            <span className="text-xs font-semibold text-slate-400">${related_runs.length}</span>
          </div>
          <div className="space-y-2">
            ${related_runs.length ? related_runs.slice(0, 4).map((item) => html`<${RelatedRun} key=${item.run_id} run=${item} onOpenRun=${onOpenRun} />`) : html`
              <p className="rounded-2xl bg-white p-3 text-sm leading-6 text-slate-500">
                No related runs yet. Future runs with the same question, same scope, parent/child lineage, or comparison group will appear here.
              </p>
            `}
          </div>
        </div>

        ${SHOW_COMPARISON_AND_EXPLORATION_PANELS ? html`<div className="space-y-3">
          <div className="rounded-2xl border border-slate-200 bg-slate-50 p-4">
            <p className="text-xs font-bold uppercase tracking-wide text-slate-500">Comparison / composite records</p>
            <p className="mt-2 text-sm text-slate-700">${comparisons.length} comparison(s) · ${composites.length} composite(s)</p>
            <p className="mt-2 text-xs leading-5 text-slate-500">The local adapter now has records for these objects; analytics/materialization can move behind those seams later.</p>
          </div>
          <div className="rounded-2xl border border-slate-200 bg-slate-50 p-4">
            <p className="text-xs font-bold uppercase tracking-wide text-slate-500">Available exploration moves</p>
            <div className="mt-3 flex flex-wrap gap-2">
              ${exploration_affordances.map((item) => html`
                <${Pill} key=${item.kind} tone=${item.enabled ? 'sky' : 'slate'}>${item.label}</${Pill}>
              `)}
            </div>
          </div>
        </div>` : null}
      </div>
    </section>
  `;
}
