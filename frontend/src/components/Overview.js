import React from '../../vendor/react.bundle.mjs';
import { html } from '../html.js';
import { pct } from '../format.js';
import { answerCitationHealth, buildViewBadges } from '../domain/workbenchUx.js';
import { ExplorationLineage } from './ExplorationLineage.js';

function StatCard({ label, value, tone = 'slate' }) {
  const tones = {
    slate: 'border-slate-200 bg-white',
    sky: 'border-sky-200 bg-sky-50',
    amber: 'border-amber-200 bg-amber-50',
    emerald: 'border-emerald-200 bg-emerald-50'
  };
  return html`
    <div className=${`rounded-3xl border p-4 shadow-sm ${tones[tone] || tones.slate}`}>
      <p className="text-xs font-bold uppercase tracking-wide text-slate-500">${label}</p>
      <p className="mt-2 text-2xl font-bold text-slate-900">${value}</p>
    </div>
  `;
}

function ActionButton({ label, detail, tone = 'slate', onClick }) {
  const tones = {
    slate: 'border-slate-200 bg-white text-slate-800 hover:border-slate-300',
    sky: 'border-sky-200 bg-sky-50 text-sky-900 hover:border-sky-300',
    amber: 'border-amber-200 bg-amber-50 text-amber-900 hover:border-amber-300',
    emerald: 'border-emerald-200 bg-emerald-50 text-emerald-900 hover:border-emerald-300'
  };
  return html`
    <button onClick=${onClick} className=${`rounded-2xl border p-4 text-left shadow-sm ${tones[tone] || tones.slate}`}>
      <span className="block text-sm font-bold">${label}</span>
      <span className="mt-1 block text-xs leading-5 opacity-80">${detail}</span>
    </button>
  `;
}

export function Overview({ model, explorationContext, onOpenRun, onNavigate }) {
  const { overview, alignment, trace, conflicts, answer, explanation, lineage } = model;
  const badges = buildViewBadges(model);
  const citationHealth = answerCitationHealth(model);
  const citationPlacementDetail = `${citationHealth.placed}/${citationHealth.total} citations placed`;
  const findingsDetail = `${answer.findings.length} construct finding(s), ${badges.findings.attention} need attention`;
  const conflictsDetail = `${conflicts.edges.length} conflict edge(s)`;
  const placedCitationValue = `${citationHealth.placed}/${citationHealth.total}`;
  return html`
    <div className="space-y-5 p-5">
      <section className="rounded-3xl border border-sky-200 bg-gradient-to-br from-sky-500 via-cyan-500 to-indigo-500 p-6 text-white shadow-sm">
        <p className="text-xs font-bold uppercase tracking-[0.2em] text-sky-100">Current run</p>
        <h1 className="mt-3 max-w-5xl text-2xl font-bold tracking-tight">${overview.question_text}</h1>
        <div className="mt-4 flex flex-wrap gap-2 text-sm">
          <span className="rounded-full bg-white/20 px-3 py-1">Confidence ${pct(overview.confidence_score)}</span>
          ${overview.confidence_label ? html`<span className="rounded-full bg-white/20 px-3 py-1">${overview.confidence_label}</span>` : null}
          ${overview.selected_chain_id ? html`<span className="rounded-full bg-white/20 px-3 py-1">Selected chain ${overview.selected_chain_id}</span>` : null}
        </div>
        <p className="mt-4 max-w-3xl text-sm leading-6 text-sky-50">
          Use <strong>Download final report</strong> above to save a readable Markdown summary with the answer, findings,
          conflicts, representative evidence excerpts, limitations, and bundle lineage.
        </p>
      </section>

      <section className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
        <h2 className="text-lg font-bold text-slate-900">Pipeline coverage</h2>
        <div className="mt-4 grid gap-4 md:grid-cols-2 xl:grid-cols-4">
          <${StatCard} label="ALIGN witnesses" value=${alignment.witnesses.length} tone="sky" />
          <${StatCard} label="TRACE chains" value=${trace.ranked_chains.length} tone="emerald" />
          <${StatCard} label="Conflicts" value=${conflicts.edges.length} tone="amber" />
          <${StatCard} label="Findings" value=${answer.findings.length} />
        </div>
      </section>

      <section className="grid gap-5 xl:grid-cols-[minmax(0,1.2fr)_minmax(320px,0.8fr)]">
        <div className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
          <h2 className="text-lg font-bold text-slate-900">Next actions</h2>
          <div className="mt-4 grid gap-3 sm:grid-cols-2">
            <${ActionButton} label="Review final answer" detail=${citationPlacementDetail} tone="sky" onClick=${() => onNavigate && onNavigate('answer')} />
            <${ActionButton} label="Inspect findings" detail=${findingsDetail} tone=${badges.findings.attention ? 'amber' : 'emerald'} onClick=${() => onNavigate && onNavigate('findings')} />
            <${ActionButton} label="Resolve conflicts" detail=${conflictsDetail} tone=${conflicts.edges.length ? 'amber' : 'emerald'} onClick=${() => onNavigate && onNavigate('conflicts')} />
            <${ActionButton} label="Open graphs" detail="Evidence, reasoning, and answer graph lenses" tone="slate" onClick=${() => onNavigate && onNavigate('graphs')} />
          </div>
        </div>

        <div className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
          <h2 className="text-lg font-bold text-slate-900">Answer health</h2>
          <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-1">
            <${StatCard} label="Placed citations" value=${placedCitationValue} tone=${citationHealth.unplaced ? 'amber' : 'emerald'} />
            <${StatCard} label="Warnings" value=${overview.warnings.length} tone=${overview.warnings.length ? 'amber' : 'emerald'} />
          </div>
          ${citationHealth.unplaced || citationHealth.unresolvedWitnessContexts ? html`
            <p className="mt-3 rounded-2xl bg-amber-50 p-3 text-xs leading-5 text-amber-800">
              ${citationHealth.unplaced} citation(s) were not placed in the answer text and ${citationHealth.unresolvedWitnessContexts} citation(s) have no resolved witness id.
            </p>
          ` : html`<p className="mt-3 text-sm text-slate-500">Citation placement and witness references look complete for the emitted citation map.</p>`}
        </div>
      </section>

      <${ExplorationLineage} context=${explorationContext} onOpenRun=${onOpenRun} />

      <section className="grid gap-5 xl:grid-cols-[minmax(0,1.6fr)_minmax(320px,0.9fr)]">
        <div className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
          <div className="mb-3 flex items-center justify-between gap-3">
            <h2 className="text-lg font-bold text-slate-900">Final answer</h2>
            <span className="rounded-full bg-slate-100 px-3 py-1 text-xs font-semibold text-slate-600">${answer.citations.length} citations</span>
          </div>
          <p className="whitespace-pre-wrap text-sm leading-7 text-slate-700">${overview.answer_text || 'No answer text emitted.'}</p>
        </div>

        <div className="space-y-5">
          <details className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
            <summary className="cursor-pointer text-lg font-bold text-slate-900">Bundle lineage</summary>
            <div className="mt-4 space-y-3">
              ${lineage.nodes.map((node, index) => html`
                <div key=${node.bundle_id} className="flex items-center gap-3">
                  <div className="flex h-8 w-8 items-center justify-center rounded-full bg-sky-100 text-xs font-bold text-sky-700">${index + 1}</div>
                  <div>
                    <p className="text-sm font-semibold text-slate-800">${node.operator}</p>
                    <p className="text-xs text-slate-500">${node.bundle_id}</p>
                  </div>
                </div>
              `)}
            </div>
          </details>

          <div className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
            <h2 className="text-lg font-bold text-slate-900">Explanation surface</h2>
            <div className="mt-4 grid gap-3 sm:grid-cols-2">
              <${StatCard} label="Tethers" value=${explanation.tethers.length} tone="sky" />
              <${StatCard} label="Uncertainties" value=${explanation.uncertainty_entries.length} tone="amber" />
            </div>
            ${overview.warnings.length ? html`<p className="mt-4 text-sm text-amber-700">${overview.warnings.length} warning(s) emitted by EXPLAIN.</p>` : html`<p className="mt-4 text-sm text-slate-500">No warnings emitted by EXPLAIN.</p>`}
          </div>
        </div>
      </section>
    </div>
  `;
}
