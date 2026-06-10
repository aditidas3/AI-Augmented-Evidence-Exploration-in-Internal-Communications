import React from '../../vendor/react.bundle.mjs';
import { html } from '../html.js';
import { buildViewBadges } from '../domain/workbenchUx.js';

const groups = [
  {
    label: 'Run',
    items: [
      ['overview', 'Overview', 'Answer, coverage, lineage'],
      ['answer', 'Final answer', 'Citations and limits'],
      ['explain', 'Explanation', 'Tethers and uncertainty']
    ]
  },
  {
    label: 'Operators',
    items: [
      ['align', 'ALIGN', 'Bindings and witnesses'],
      ['trace', 'TRACE', 'Ranked chains'],
      ['findings', 'Findings', 'Construct outputs'],
      ['conflicts', 'Conflicts', 'Detected disputes'],
      ['graphs', 'Graphs', 'EG, RG, answer graph']
    ]
  }
];

function NavBadge({ badge }) {
  if (!badge?.attention) return null;
  return html`
    <span className="ml-auto flex items-center gap-1">
      <span
        aria-label="Needs attention"
        className="h-2 w-2 rounded-full bg-amber-500"
      ></span>
    </span>
  `;
}

function selectionLabel(selection) {
  if (!selection?.kind) return '';
  if (selection.kind === 'witness') return `Witness ${selection.item?.witness?.witness_id || ''}`.trim();
  if (selection.kind === 'finding') return `Finding ${selection.item?.display_id || selection.item?.finding_id || ''}`.trim();
  if (selection.kind === 'conflict') return `Conflict ${selection.item?.edge_id || ''}`.trim();
  if (selection.kind === 'graph') return 'Graph selection';
  if (selection.kind === 'answer') return 'Answer review';
  return selection.kind;
}

export function LeftNav({ activeView, setActiveView, model, conflictReviews = {}, selection }) {
  const badges = model ? buildViewBadges(model, conflictReviews) : {};
  const selectedLabel = selectionLabel(selection);

  return html`
    <aside className="workbench-sidebar">
      <div className="sidebar-brand">
        <p className="sidebar-brand__eyebrow">Evidence Explorer</p>
        <p className="sidebar-brand__title">Investigation workbench</p>
        <p className="sidebar-brand__subtitle">Read the final answer first, then inspect how each operator produced it.</p>
      </div>
      <nav className="sidebar-nav">
        ${groups.map((group) => html`
          <div key=${group.label} className="sidebar-nav__group">
            <p className="sidebar-nav__label">${group.label}</p>
            ${group.items.map(([key, label, description]) => html`
              <button
                key=${key}
                onClick=${() => setActiveView(key)}
                className=${`sidebar-item ${activeView === key ? 'sidebar-item--active' : ''}`}
              >
                <span className="flex w-full items-center gap-2">
                  <span className="sidebar-item__label">${label}</span>
                  <${NavBadge} badge=${badges[key]} />
                </span>
                <span className="sidebar-item__description">${description}</span>
              </button>
            `)}
          </div>
        `)}
      </nav>
      ${selectedLabel ? html`
        <div className="m-3 rounded-2xl border border-slate-200 bg-white/80 p-3">
          <p className="text-[10px] font-black uppercase tracking-wide text-slate-500">Selected</p>
          <p className="mt-1 break-words text-xs font-semibold text-slate-800">${selectedLabel}</p>
        </div>
      ` : null}
    </aside>
  `;
}
