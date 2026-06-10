import React from '../../vendor/react.bundle.mjs';
import { html } from '../html.js';

const PIPELINE_STAGES = [
  ['queued', 'Queued', 'Run request saved and waiting for the pipeline worker.'],
  ['intent', 'Intent validation', 'Normalize and validate the research intent.'],
  ['align', 'ALIGN', 'Bind the question to document evidence and witnesses.'],
  ['trace', 'TRACE', 'Build ranked reasoning chains from the aligned evidence.'],
  ['conflict', 'CONFLICT', 'Detect disputes, defeaters, and contested claims.'],
  ['construct', 'CONSTRUCT', 'Package findings and the answer graph.'],
  ['explain', 'EXPLAIN', 'Generate explanations, citations, limits, and uncertainty.'],
  ['completed', 'Ready', 'Artifacts are indexed and the workbench can open.']
];

const STATUS_TO_STAGE = {
  queued: 'queued',
  aligning: 'align',
  tracing: 'trace',
  conflict_checking: 'conflict',
  constructing: 'construct',
  explaining: 'explain',
  completed: 'completed'
};

export function statusBadgeClass(status) {
  const normalized = String(status || '').toLowerCase();
  if (normalized === 'failed') {
    return 'rounded-full bg-rose-100 px-3 py-1 text-xs font-bold uppercase text-rose-800';
  }
  if (normalized === 'completed') {
    return 'rounded-full bg-emerald-100 px-3 py-1 text-xs font-bold uppercase text-emerald-800';
  }
  return 'rounded-full bg-amber-100 px-3 py-1 text-xs font-bold uppercase text-amber-800';
}

function statusMessage(run) {
  if (run.status === 'failed') {
    return html`<span className="text-rose-700">${run.error || 'The pipeline failed before producing artifacts.'}</span>`;
  }
  if (run.status === 'completed') {
    return 'The run is complete. The result workbench will open once artifacts are indexed.';
  }
  return `The pipeline is running${run.stage ? `: ${run.stage}` : ''}. This view will open the result workbench once artifacts are ready.`;
}

function currentStageKey(run) {
  if (run.result_index_ref || run.status === 'completed') return 'completed';
  return run.stage || STATUS_TO_STAGE[run.status] || 'queued';
}

function stageStateLabel(state) {
  if (state === 'active') return 'running';
  if (state === 'complete') return 'done';
  return state;
}

export function buildStageProgress(run) {
  const failed = run.status === 'failed';
  const completed = Boolean(run.result_index_ref) || run.status === 'completed';
  const currentKey = currentStageKey(run);
  const currentIndex = Math.max(0, PIPELINE_STAGES.findIndex(([key]) => key === currentKey));

  return PIPELINE_STAGES.map(([key, label, description], index) => {
    let state = 'pending';
    if (completed || index < currentIndex) {
      state = 'complete';
    } else if (failed && index === currentIndex) {
      state = 'failed';
    } else if (!failed && index === currentIndex) {
      state = 'active';
    }
    return { key, label, description, state };
  });
}

export function RunStatus({ run, onBack }) {
  const progress = buildStageProgress(run);

  return html`
    <div className="min-h-screen bg-slate-50 p-6 text-slate-800">
      <div className="mx-auto max-w-3xl space-y-5">
        <button onClick=${onBack} className="rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm font-semibold text-slate-700 hover:bg-slate-50">New investigation</button>
        <section className="rounded-3xl border border-slate-200 bg-white p-6 shadow-sm">
          <p className="text-xs font-bold uppercase tracking-[0.2em] text-sky-700">Run status</p>
          <div className="mt-3 flex flex-wrap items-center gap-3">
            <h1 className="text-2xl font-bold text-slate-900">${run.run_id}</h1>
            <span className=${statusBadgeClass(run.status)}>${run.status}</span>
          </div>
          <p className="mt-4 text-sm leading-7 text-slate-700">${run.request?.question?.text || 'No question recorded.'}</p>
          <div className="mt-5 rounded-2xl bg-slate-50 p-4 text-sm text-slate-600">
            ${statusMessage(run)}
          </div>
          <div className="stage-progress" role="list" aria-label="Pipeline progress">
            ${progress.map((stage) => html`
              <div key=${stage.key} className=${`stage-progress__item stage-progress__item--${stage.state}`} role="listitem">
                <span className=${`stage-ring stage-ring--${stage.state}`} aria-hidden="true">
                  ${stage.state === 'active' ? html`<span className="stage-spinner"></span>` : null}
                </span>
                <div>
                  <p className="stage-progress__label">${stage.label}</p>
                  <p className="stage-progress__description">${stage.description}</p>
                </div>
                <span className="stage-progress__state">${stageStateLabel(stage.state)}</span>
              </div>
            `)}
          </div>
          ${run.updated_at ? html`<p className="mt-3 text-xs text-slate-500">Last update: ${new Date(run.updated_at).toLocaleString()}</p>` : null}
        </section>
      </div>
    </div>
  `;
}
