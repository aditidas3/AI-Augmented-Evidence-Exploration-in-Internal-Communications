import React from '../../vendor/react.bundle.mjs';
import { html } from '../html.js';
import { pct } from '../format.js';
import { filterTraceChains, traceChainHealth } from '../domain/workbenchUx.js';

export function chainScoreForDisplay(chain) {
  if (chain?.score != null && !Number.isNaN(Number(chain.score))) return Number(chain.score);
  if (chain?.confidence != null && !Number.isNaN(Number(chain.confidence))) return Number(chain.confidence);
  return null;
}

export function TraceView({ model, onSelectWitness }) {
  const topChain = model.trace.ranked_chains[0];
  const [expandedChains, setExpandedChains] = React.useState(() => new Set());
  const [chainFilter, setChainFilter] = React.useState('all');
  const [sortKey, setSortKey] = React.useState('rank');
  const selectedChainId = model.overview.selected_chain_id || topChain?.chain_id;
  const visibleChains = filterTraceChains(model.trace.ranked_chains, { filter: chainFilter, sortKey });

  function toggleExpanded(chainId) {
    setExpandedChains((prev) => {
      const next = new Set(prev);
      if (next.has(chainId)) next.delete(chainId);
      else next.add(chainId);
      return next;
    });
  }

  return html`
    <div className="space-y-4 p-5">
      <section className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
        <h2 className="text-lg font-bold text-slate-900">Reasoning chain selection</h2>
        ${topChain ? html`
          <p className="mt-3 text-sm leading-7 text-slate-700">
            TRACE produced ${model.trace.ranked_chains.length} ranked chain(s). The leading chain is
            <strong>${topChain.chain_id}</strong> with slot coverage ${topChain.slot_coverage},
            ${topChain.witness_complete ? 'a complete witness' : 'an incomplete witness'}, and chain score ${pct(chainScoreForDisplay(topChain))}.
          </p>
        ` : html`<p className="mt-3 text-sm text-slate-500">No ranked chains emitted.</p>`}
      </section>

      <section className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 className="text-lg font-bold text-slate-900">Chain comparison</h2>
            <p className="mt-1 text-sm text-slate-500">Compare rank, score, coverage, and witness completeness before opening premises.</p>
          </div>
          <div className="flex flex-wrap gap-2">
            ${[
              ['all', 'All'],
              ['complete', 'Complete'],
              ['incomplete', 'Incomplete']
            ].map(([key, label]) => html`
              <button onClick=${() => setChainFilter(key)} className=${`rounded-full px-3 py-1.5 text-xs font-bold ${chainFilter === key ? 'bg-slate-900 text-white' : 'bg-slate-100 text-slate-700 hover:bg-slate-200'}`}>${label}</button>
            `)}
            <select value=${sortKey} onChange=${(event) => setSortKey(event.target.value)} className="rounded-full border border-slate-200 bg-white px-3 py-1.5 text-xs font-bold text-slate-700">
              <option value="rank">Sort: Rank</option>
              <option value="score">Sort: Score</option>
              <option value="coverage">Sort: Coverage</option>
            </select>
          </div>
        </div>
        <div className="mt-4 overflow-x-auto">
          <table className="w-full min-w-[640px] text-left text-sm">
            <thead className="text-xs uppercase tracking-wide text-slate-500">
              <tr>
                <th className="border-b border-slate-200 py-2">Chain</th>
                <th className="border-b border-slate-200 py-2">Rank</th>
                <th className="border-b border-slate-200 py-2">Score</th>
                <th className="border-b border-slate-200 py-2">Coverage</th>
                <th className="border-b border-slate-200 py-2">Witness</th>
                <th className="border-b border-slate-200 py-2">Premises</th>
              </tr>
            </thead>
            <tbody>
              ${visibleChains.map((chain) => {
                const health = traceChainHealth(chain);
                const selected = chain.chain_id === selectedChainId;
                return html`
                  <tr key=${chain.chain_id} className=${selected ? 'bg-sky-50' : ''}>
                    <td className="border-b border-slate-100 py-2 font-bold text-slate-900">${chain.chain_id}${selected ? ' · selected' : ''}</td>
                    <td className="border-b border-slate-100 py-2">${chain.rank}</td>
                    <td className="border-b border-slate-100 py-2">${pct(health.score)}</td>
                    <td className="border-b border-slate-100 py-2">${chain.slot_coverage}</td>
                    <td className="border-b border-slate-100 py-2">${health.witnessComplete ? 'complete' : 'incomplete'}</td>
                    <td className="border-b border-slate-100 py-2">${health.premiseCount}</td>
                  </tr>
                `;
              })}
            </tbody>
          </table>
        </div>
      </section>

      ${visibleChains.map((chain) => {
        const nodes = chain.nodes || [];
        const expanded = expandedChains.has(chain.chain_id);
        const visibleNodes = expanded ? nodes : nodes.slice(0, 8);
        const hiddenCount = Math.max(0, nodes.length - visibleNodes.length);
        const selected = chain.chain_id === selectedChainId;
        return html`
            <div key=${chain.chain_id} className=${`rounded-3xl border bg-white p-5 shadow-sm ${selected ? 'border-sky-300 ring-2 ring-sky-100' : 'border-slate-200'}`}>
              <div className="flex flex-wrap items-center gap-2">
                <span className="rounded-full bg-sky-100 px-3 py-1 text-xs font-bold text-sky-800">Rank ${chain.rank}</span>
                ${selected ? html`<span className="rounded-full bg-slate-900 px-3 py-1 text-xs font-bold text-white">selected chain</span>` : null}
                <span className="rounded-full bg-emerald-100 px-3 py-1 text-xs font-semibold text-emerald-800">chain score ${pct(chainScoreForDisplay(chain))}</span>
                <span className="rounded-full bg-slate-100 px-3 py-1 text-xs font-semibold text-slate-700">${chain.witness_complete ? 'witness complete' : 'incomplete witness'}</span>
              </div>
            <p className="mt-3 text-xs text-slate-500">${chain.chain_id}</p>
            <p className="mt-3 text-sm text-slate-700">Slot coverage: ${chain.slot_coverage} · Temporal consistency: ${String(chain.temporal_consistent)}</p>
            ${selected ? html`<p className="mt-3 rounded-2xl bg-sky-50 p-3 text-sm leading-6 text-sky-900">This chain is the selected downstream chain. It is shown with stronger emphasis so it can be compared against lower-ranked alternatives.</p>` : null}
            <div className="mt-4 flex flex-wrap gap-2">
              ${visibleNodes.map((node) => {
                const context = model.witnesses?.by_id?.[node.object_id];
                return html`
                  <button
                    key=${node.candidate_id}
                    disabled=${!context}
                    onClick=${() => context && onSelectWitness(context)}
                    className=${`rounded-full px-3 py-1 text-xs font-semibold ${context ? 'bg-sky-50 text-sky-800 hover:bg-sky-100' : 'bg-slate-100 text-slate-400'}`}
                  >
                    ${node.position}. ${node.slot_type} · ${node.object_id}
                  </button>
                `;
              })}
              ${nodes.length > 8 ? html`
                <button
                  type="button"
                  onClick=${() => toggleExpanded(chain.chain_id)}
                  className="rounded-full bg-slate-100 px-3 py-1 text-xs font-semibold text-slate-700 hover:bg-slate-200"
                >
                  ${expanded ? 'Show fewer premises' : `+${hiddenCount} more premises`}
                </button>
              ` : null}
            </div>
          </div>
        `;
      })}
    </div>
  `;
}
