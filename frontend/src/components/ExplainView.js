import React from '../../vendor/react.bundle.mjs';
import { html } from '../html.js';

function tetherWitnessContext(model, tether) {
  const path = tether.tethered_to || [];
  const witnessId = path.find((item) => String(item || '').startsWith('wit-')) || path[path.length - 1];
  return witnessId ? model.witnesses?.by_id?.[witnessId] : null;
}

export function fallbackText(item) {
  return item?.description || item?.explanation || item?.text || item?.summary || item?.rationale || JSON.stringify(item);
}

export function visibleTab(activeTab, section) {
  return activeTab === 'all' || activeTab === section;
}

export function explainCounts(model) {
  return {
    provenance: model?.explanation?.provenance_narratives?.length || 0,
    decisions: model?.explanation?.decision_explanations?.length || 0,
    uncertainty: model?.explanation?.uncertainty_entries?.length || 0,
    conflicts: model?.explanation?.conflict_explanations?.length || 0,
    tethers: model?.explanation?.tethers?.length || 0,
    tetherFailures: model?.explanation?.tether_failures?.length || 0
  };
}

export function ExplainView({ model, onSelectWitness, onSelectFinding }) {
  const [activeTab, setActiveTab] = React.useState('all');
  const [uncertaintyFilter, setUncertaintyFilter] = React.useState('all');
  const uncertaintyTypes = Array.from(new Set((model.explanation.uncertainty_entries || []).map((item) => item.uncertainty_type || item.entry_id || 'Unclassified'))).sort();
  const visibleUncertainties = uncertaintyFilter === 'all'
    ? model.explanation.uncertainty_entries
    : model.explanation.uncertainty_entries.filter((item) => (item.uncertainty_type || item.entry_id || 'Unclassified') === uncertaintyFilter);
  const findingById = Object.fromEntries((model.answer.findings || []).map((finding) => [finding.finding_id, finding]));

  return html`
    <div className="space-y-5 p-5">
      <section className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="text-lg font-bold text-slate-900">Summary</h2>
            <p className="mt-3 text-sm leading-7 text-slate-700">${model.explanation.summary || 'No summary emitted.'}</p>
          </div>
          <div className="flex flex-wrap gap-2">
            ${[
              ['all', 'All'],
              ['provenance', 'Provenance'],
              ['decisions', 'Decisions'],
              ['uncertainty', 'Uncertainty'],
              ['tethers', 'Tethers']
            ].map(([key, label]) => html`
              <button onClick=${() => setActiveTab(key)} className=${`rounded-full px-3 py-1.5 text-xs font-bold ${activeTab === key ? 'bg-slate-900 text-white' : 'bg-slate-100 text-slate-700 hover:bg-slate-200'}`}>${label}</button>
            `)}
          </div>
        </div>
      </section>

      <section className="grid gap-4 lg:grid-cols-3">
        <div className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
          <p className="text-sm font-semibold text-slate-500">Provenance narratives</p>
          <p className="mt-2 text-3xl font-bold text-slate-900">${model.explanation.provenance_narratives.length}</p>
        </div>
        <div className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
          <p className="text-sm font-semibold text-slate-500">Tethers</p>
          <p className="mt-2 text-3xl font-bold text-slate-900">${model.explanation.tethers.length}</p>
        </div>
        <div className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
          <p className="text-sm font-semibold text-slate-500">Uncertainty entries</p>
          <p className="mt-2 text-3xl font-bold text-slate-900">${model.explanation.uncertainty_entries.length}</p>
        </div>
      </section>

      ${visibleTab(activeTab, 'provenance') || visibleTab(activeTab, 'decisions') ? html`
        <section className="grid gap-5 xl:grid-cols-2">
          ${visibleTab(activeTab, 'provenance') ? html`
            <div className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
              <h2 className="text-lg font-bold text-slate-900">Provenance narratives</h2>
              <div className="mt-3 space-y-3">
                ${model.explanation.provenance_narratives.length ? model.explanation.provenance_narratives.map((item, index) => {
                  const finding = findingById[item.finding_id];
                  return html`
                    <article key=${item.display_id || index} className="rounded-2xl bg-slate-50 p-4">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <p className="text-xs font-bold uppercase tracking-wide text-slate-500">${item.display_id || item.finding_id || `Narrative ${index + 1}`}</p>
                        ${finding ? html`<button onClick=${() => onSelectFinding && onSelectFinding(finding)} className="rounded-full border border-sky-200 bg-white px-3 py-1 text-xs font-bold text-sky-800 hover:bg-sky-50">Open finding</button>` : null}
                      </div>
                      <p className="mt-2 text-sm leading-6 text-slate-700">${item.narrative || fallbackText(item)}</p>
                    </article>
                  `;
                }) : html`<p className="text-sm text-slate-500">No provenance narratives emitted.</p>`}
              </div>
            </div>
          ` : null}
          ${visibleTab(activeTab, 'decisions') ? html`
            <div className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
              <h2 className="text-lg font-bold text-slate-900">Decision explanations</h2>
              <div className="mt-3 space-y-3">
                ${model.explanation.decision_explanations.length ? model.explanation.decision_explanations.map((item, index) => html`
                  <article key=${item.decision_node_id || index} className="rounded-2xl bg-slate-50 p-4">
                    <p className="text-xs font-bold uppercase tracking-wide text-slate-500">${item.decision_node_id || `Decision ${index + 1}`}</p>
                    <p className="mt-2 text-sm leading-6 text-slate-700">${item.rationale || fallbackText(item)}</p>
                  </article>
                `) : html`<p className="text-sm text-slate-500">No decision explanations emitted.</p>`}
              </div>
            </div>
          ` : null}
        </section>
      ` : null}

      ${visibleTab(activeTab, 'uncertainty') ? html`
        <section className="grid gap-5 xl:grid-cols-2">
          <div className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <h2 className="text-lg font-bold text-slate-900">Uncertainty map</h2>
              <select value=${uncertaintyFilter} onChange=${(event) => setUncertaintyFilter(event.target.value)} className="rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm font-semibold">
                <option value="all">All uncertainty</option>
                ${uncertaintyTypes.map((type) => html`<option key=${type} value=${type}>${type}</option>`)}
              </select>
            </div>
            <div className="mt-3 space-y-3">
              ${visibleUncertainties.length ? visibleUncertainties.map((item, index) => html`
                <article key=${item.entry_id || index} className="rounded-2xl bg-amber-50 p-4">
                  <p className="text-xs font-bold uppercase tracking-wide text-amber-800">${item.uncertainty_type || item.entry_id || `Uncertainty ${index + 1}`}</p>
                  <p className="mt-2 text-sm leading-6 text-slate-700">${fallbackText(item)}</p>
                </article>
              `) : html`<p className="text-sm text-slate-500">No uncertainty entries match this filter.</p>`}
            </div>
          </div>
          <div className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
            <h2 className="text-lg font-bold text-slate-900">Conflict explanations</h2>
            <div className="mt-3 space-y-3">
              ${(model.explanation.conflict_explanations || []).length ? model.explanation.conflict_explanations.map((item, index) => html`
                <article key=${item.conflict_edge_id || index} className="rounded-2xl bg-rose-50 p-4">
                  <p className="text-xs font-bold uppercase tracking-wide text-rose-800">${item.stance || item.conflict_edge_id || `Conflict ${index + 1}`}</p>
                  <p className="mt-2 text-sm leading-6 text-slate-700">${fallbackText(item)}</p>
                </article>
              `) : html`<p className="text-sm text-slate-500">No conflict explanations emitted.</p>`}
            </div>
          </div>
        </section>
      ` : null}

      ${visibleTab(activeTab, 'tethers') ? html`
        <section className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
          <h2 className="text-lg font-bold text-slate-900">Evidence chain</h2>
          <div className="mt-3 space-y-2">
            ${(model.explanation.evidence_chain || []).length ? model.explanation.evidence_chain.map((item, index) => html`
              <p key=${index} className="rounded-2xl bg-slate-50 p-3 text-sm text-slate-700">${item}</p>
            `) : html`<p className="text-sm text-slate-500">No evidence chain emitted.</p>`}
          </div>
        </section>

        <section className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
          <div className="mb-3">
            <h2 className="text-lg font-bold text-slate-900">Tethers</h2>
            <p className="mt-1 text-sm leading-6 text-slate-500">
              Tethers connect explanatory sentences back to witness paths. When a path resolves to a witness, it opens the shared context drawer.
            </p>
          </div>
          <div className="space-y-3">
            ${model.explanation.tethers.length ? model.explanation.tethers.map((tether, index) => {
              const context = tetherWitnessContext(model, tether);
              return html`
                <article key=${index} className="rounded-2xl border border-slate-200 bg-slate-50 p-4">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="rounded-full bg-indigo-100 px-3 py-1 text-xs font-bold text-indigo-800">Sentence ${tether.sentence_index}</span>
                    <span className="rounded-full bg-white px-3 py-1 text-xs font-semibold text-slate-600">${tether.tether_type || 'tether'}</span>
                  </div>
                  <p className="mt-3 text-sm leading-6 text-slate-800">${tether.sentence_text}</p>
                  <p className="mt-2 break-all text-xs text-slate-500">${(tether.tethered_to || []).join(' -> ')}</p>
                  ${context ? html`
                    <button
                      onClick=${() => onSelectWitness && onSelectWitness(context)}
                      className="mt-3 rounded-full border border-sky-200 bg-white px-3 py-1 text-xs font-semibold text-sky-800 hover:bg-sky-50"
                    >
                      Open witness context ${context.witness.witness_id}
                    </button>
                  ` : html`<p className="mt-3 text-xs text-amber-700">This tether path did not resolve to a witness in the current context index.</p>`}
                </article>
              `;
            }) : html`<p className="text-sm text-slate-500">No tethers emitted.</p>`}
          </div>
        </section>
        ${(model.explanation.tether_failures || []).length ? html`
          <section className="rounded-3xl border border-amber-200 bg-amber-50 p-5 shadow-sm">
            <h2 className="text-lg font-bold text-amber-900">Tether failures</h2>
            <div className="mt-3 space-y-2">
              ${model.explanation.tether_failures.map((item, index) => html`
                <p key=${index} className="rounded-2xl bg-white/70 p-3 text-sm text-slate-700">${item.reason || fallbackText(item)}</p>
              `)}
            </div>
          </section>
        ` : null}
      ` : null}
    </div>
  `;
}
