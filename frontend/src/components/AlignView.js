import React from '../../vendor/react.bundle.mjs';
import { html } from '../html.js';
import { pct } from '../format.js';
import { filterSlotBindings, slotFilterOptions } from '../domain/workbenchUx.js';

import { associatedValue, witnessLabel, witnessLocation } from '../domain/witnessContext.js';


export function AlignView({ model, onSelectWitness }) {
  const slots = model.alignment.slot_bindings || [];
  const witnesses = model.alignment.witnesses || [];
  const anchors = model.alignment.anchors || [];
  const mentions = model.alignment.mentions || [];
  const [query, setQuery] = React.useState('');
  const [slotType, setSlotType] = React.useState('ALL');
  const [quality, setQuality] = React.useState('ALL');
  const [minConfidence, setMinConfidence] = React.useState(0);
  const [expandedAll, setExpandedAll] = React.useState(false);
  const slotTypes = slotFilterOptions(slots);
  const filteredSlots = filterSlotBindings(slots, { query, slotType, quality, minConfidence });
  const weakSlots = slots.filter((slot) => !(slot.witnesses || []).length || Number(slot.confidence || 0) < 0.55);

  return html`
    <div className="space-y-5 p-5">
      <section className="grid gap-4 md:grid-cols-4">
        <div className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
          <p className="text-sm font-semibold text-slate-500">Slot bindings</p>
          <p className="mt-2 text-3xl font-bold text-slate-900">${slots.length}</p>
        </div>
        <div className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
          <p className="text-sm font-semibold text-slate-500">Witnesses</p>
          <p className="mt-2 text-3xl font-bold text-slate-900">${witnesses.length}</p>
        </div>
        <div className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
          <p className="text-sm font-semibold text-slate-500">Anchors</p>
          <p className="mt-2 text-3xl font-bold text-slate-900">${anchors.length}</p>
        </div>
        <div className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
          <p className="text-sm font-semibold text-slate-500">Mentions</p>
          <p className="mt-2 text-3xl font-bold text-slate-900">${mentions.length}</p>
        </div>
      </section>

      <section className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
        <div className="mb-4">
          <h2 className="text-lg font-bold text-slate-900">Bindings and their witnesses</h2>
          <p className="text-sm text-slate-500">
            ALIGN produced ${slots.length} binding${slots.length === 1 ? '' : 's'}. Each binding owns the witnesses that make it admissible enough to hand forward into TRACE.
          </p>
        </div>

        ${weakSlots.length ? html`
          <div className="mb-4 rounded-2xl border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
            ${weakSlots.length} slot binding(s) have low confidence or no witnesses. Use the filters to inspect them before trusting downstream TRACE chains.
          </div>
        ` : null}

        <div className="mb-4 grid gap-3 rounded-2xl bg-slate-50 p-3 lg:grid-cols-[1fr_auto_auto_auto_auto] lg:items-center">
          <input
            value=${query}
            onInput=${(event) => setQuery(event.target.value)}
            placeholder="Search slot, witness, artifact..."
            className="rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm outline-none focus:border-sky-300"
          />
          <select value=${slotType} onChange=${(event) => setSlotType(event.target.value)} className="rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm font-semibold">
            <option value="ALL">All slots</option>
            ${slotTypes.map((slot) => html`<option key=${slot} value=${slot}>${slot}</option>`)}
          </select>
          <select value=${quality} onChange=${(event) => setQuality(event.target.value)} className="rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm font-semibold">
            <option value="ALL">All quality</option>
            <option value="HIGH">HIGH</option>
            <option value="MEDIUM">MEDIUM</option>
            <option value="LOW">LOW</option>
          </select>
          <select value=${String(minConfidence)} onChange=${(event) => setMinConfidence(Number(event.target.value))} className="rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm font-semibold">
            <option value="0">Any confidence</option>
            <option value="0.55">55%+</option>
            <option value="0.8">80%+</option>
          </select>
          <button onClick=${() => setExpandedAll((value) => !value)} className="rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm font-bold text-slate-700 hover:bg-slate-100">
            ${expandedAll ? 'Collapse all' : 'Expand all'}
          </button>
        </div>

        <div className="space-y-4">
          ${filteredSlots.length ? filteredSlots.map((slot, index) => html`
            <details key=${slot.slot_id || index} open=${expandedAll || index === 0} className="group rounded-3xl border border-slate-200 bg-slate-50">
              <summary className="cursor-pointer list-none p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="rounded-full bg-sky-100 px-3 py-1 text-xs font-bold text-sky-800">${slot.slot_id}</span>
                      <span className="rounded-full bg-indigo-100 px-3 py-1 text-xs font-semibold text-indigo-800">${slot.slot_type}</span>
                      <span className="rounded-full bg-emerald-100 px-3 py-1 text-xs font-semibold text-emerald-800">${slot.quality}</span>
                      <span className="rounded-full bg-slate-100 px-3 py-1 text-xs font-semibold text-slate-700">confidence ${pct(slot.confidence)}</span>
                    </div>
                    <p className="mt-3 text-sm leading-6 text-slate-700">${slot.description}</p>
                  </div>
                  <div className="rounded-2xl bg-white px-4 py-3 text-right shadow-sm">
                    <p className="text-xs font-bold uppercase tracking-wide text-slate-500">Witnesses</p>
                    <p className="mt-1 text-2xl font-bold text-slate-900">${(slot.witnesses || []).length}</p>
                  </div>
                </div>
              </summary>

              <div className="border-t border-slate-200 p-4">
                <div className="mb-4 rounded-2xl bg-white p-4">
                  <p className="text-xs font-bold uppercase tracking-wide text-slate-500">Bound values</p>
                  <div className="mt-3 flex flex-wrap gap-2">
                    ${(slot.value || []).map((value, valueIndex) => html`
                      <span key=${valueIndex} className="rounded-full bg-sky-50 px-3 py-1 text-sm font-semibold text-sky-800">
                        ${value.surface}
                      </span>
                    `)}
                  </div>
                </div>

                <div className="space-y-3">
                  ${(slot.witnesses || []).map((witness) => html`
                    <button
                      key=${witness.witness_id}
                      onClick=${() => onSelectWitness({ witness, slot, associatedValue: associatedValue(slot, witness) })}
                      className="block w-full rounded-2xl border border-slate-200 bg-white p-4 text-left hover:border-sky-300 hover:shadow-sm"
                    >
                      <div className="flex flex-wrap items-start justify-between gap-3">
                        <div>
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="rounded-full bg-slate-100 px-3 py-1 text-xs font-bold text-slate-700">${witness.witness_id}</span>
                            <span className="rounded-full bg-indigo-50 px-3 py-1 text-xs font-semibold text-indigo-700">${witness.mention?.category || 'witness'}</span>
                            <span className="rounded-full bg-emerald-50 px-3 py-1 text-xs font-semibold text-emerald-700">${witness.quality}</span>
                          </div>
                          <h3 className="mt-3 text-base font-bold text-slate-900">${witnessLabel(witness)}</h3>
                          <p className="mt-1 text-sm text-slate-500">${witnessLocation(witness)}</p>
                        </div>
                        <div className="rounded-2xl bg-slate-50 px-4 py-3 text-right">
                          <p className="text-xs font-bold uppercase tracking-wide text-slate-500">Score</p>
                          <p className="mt-1 text-xl font-bold text-slate-900">${witness.score ?? '-'}</p>
                        </div>
                      </div>

                      <p className="mt-4 text-sm leading-6 text-slate-700">${witness.justification || 'No justification emitted.'}</p>

                      <div className="mt-4 grid gap-3 md:grid-cols-3">
                        <div className="rounded-2xl bg-slate-50 p-3">
                          <p className="text-xs font-bold uppercase tracking-wide text-slate-500">Mention</p>
                          <p className="mt-2 text-sm font-semibold text-slate-800">${witness.mention?.surface || '—'}</p>
                        </div>
                        <div className="rounded-2xl bg-slate-50 p-3">
                          <p className="text-xs font-bold uppercase tracking-wide text-slate-500">Anchor</p>
                          <p className="mt-2 break-all text-sm font-semibold text-slate-800">${witness.anchor?.anchor_id || '—'}</p>
                        </div>
                        <div className="rounded-2xl bg-slate-50 p-3">
                          <p className="text-xs font-bold uppercase tracking-wide text-slate-500">Artifact</p>
                          <p className="mt-2 break-all text-sm font-semibold text-slate-800">${witness.anchor?.artifact_id || witness.mention?.artifact_id || witness.metadata?.artifact_name || '—'}</p>
                        </div>
                      </div>
                    </button>
                  `)}
                </div>
              </div>
            </details>
          `) : html`<p className="rounded-2xl bg-slate-50 p-4 text-sm text-slate-500">No slot bindings match the current filters.</p>`}
        </div>
      </section>
    </div>
  `;
}


