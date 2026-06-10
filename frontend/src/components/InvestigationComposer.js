import React, { useEffect, useMemo, useState } from '../../vendor/react.bundle.mjs';
import { html } from '../html.js';
import { deleteRun, deleteScope, listCollections, resolveFilterConfig, listScopes, listRuns, previewEvidenceSubset, launchRun } from '../services/authoringService.js';

function CollectionCard({ collection, selected, onToggle }) {
  return html`
    <label className=${`block cursor-pointer rounded-3xl border p-4 transition ${selected ? 'border-sky-400 bg-sky-50 shadow-sm' : 'border-slate-200 bg-white hover:border-slate-300'}`}>
      <div className="flex items-start gap-3">
        <input type="checkbox" checked=${selected} onChange=${() => onToggle(collection.collection_id)} className="mt-1" />
        <div>
          <p className="text-sm font-bold text-slate-900">${collection.name}</p>
          <p className="mt-1 text-sm leading-6 text-slate-600">${collection.description}</p>
          <p className="mt-2 text-xs text-slate-500">${collection.document_count.toLocaleString()} documents · ${collection.source_family}</p>
        </div>
      </div>
    </label>
  `;
}


function PredicateRow({ predicate, field, onChange, onRemove }) {
  function setOperator(operator) {
    let value = predicate.value;
    if (field.value_type === 'date' && operator === 'between') value = { start: '', end: '' };
    if (field.value_type === 'enum' && ['in', 'not_in'].includes(operator)) value = [];
    onChange({ ...predicate, operator, value });
  }

  function setEnumValue(value) {
    onChange({ ...predicate, value: value ? [value] : [] });
  }

  return html`
    <div className="grid gap-3 rounded-2xl border border-slate-200 bg-white p-3 md:grid-cols-[1.1fr_0.8fr_1.4fr_auto] md:items-center">
      <div>
        <p className="text-xs font-bold uppercase tracking-wide text-slate-500">Field</p>
        <p className="mt-1 text-sm font-semibold text-slate-800">${field.label}</p>
      </div>
      <label className="block">
        <span className="text-xs font-bold uppercase tracking-wide text-slate-500">Operator</span>
        <select value=${predicate.operator} onChange=${(event) => setOperator(event.target.value)} className="mt-1 w-full rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm">
          ${field.operators.map((operator) => html`<option key=${operator} value=${operator}>${operator}</option>`)}
        </select>
      </label>
      <div>
        <p className="text-xs font-bold uppercase tracking-wide text-slate-500">Value</p>
        ${field.value_type === 'date' && predicate.operator === 'between' ? html`
          <div className="mt-1 flex gap-2">
            <input type="date" value=${predicate.value?.start || ''} onChange=${(event) => onChange({ ...predicate, value: { ...predicate.value, start: event.target.value } })} className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm" />
            <input type="date" value=${predicate.value?.end || ''} onChange=${(event) => onChange({ ...predicate, value: { ...predicate.value, end: event.target.value } })} className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm" />
          </div>
        ` : field.value_type === 'enum' ? html`
          <select value=${predicate.value?.[0] || ''} onChange=${(event) => setEnumValue(event.target.value)} className="mt-1 w-full rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm">
            <option value="">Select value</option>
            ${(field.enum_values || []).map((value) => html`<option key=${value} value=${value}>${value}</option>`)}
          </select>
        ` : html`
          <input value=${predicate.value || ''} onChange=${(event) => onChange({ ...predicate, value: event.target.value })} className="mt-1 w-full rounded-xl border border-slate-300 px-3 py-2 text-sm" />
        `}
      </div>
      <button onClick=${onRemove} className="rounded-xl border border-rose-200 bg-rose-50 px-3 py-2 text-sm font-semibold text-rose-700">Remove</button>
    </div>
  `;
}

export function defaultCollectionIds(collections = []) {
  return collections
    .filter((collection) => {
      const searchable = `${collection.collection_id || ''} ${collection.name || ''}`.toLowerCase();
      return searchable.includes('idl') || searchable.includes('industry documents library');
    })
    .map((collection) => collection.collection_id)
    .filter(Boolean);
}

export function ScopeSummary({ scope, onUse, onDelete, deleting = false }) {
  return html`
    <div className="rounded-2xl border border-slate-200 bg-white p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-sm font-bold text-slate-900">${scope.name}</p>
          <p className="mt-1 text-xs text-slate-500">${scope.collection_ids.length} collection(s) · ${scope.predicates.length} predicate(s)</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button onClick=${() => onUse(scope)} className="rounded-xl border border-sky-300 bg-sky-50 px-3 py-2 text-xs font-semibold text-sky-700">Use</button>
          ${onDelete ? html`
            <button
              disabled=${deleting}
              onClick=${(event) => {
                event?.stopPropagation?.();
                onDelete(scope);
              }}
              className="rounded-xl border border-rose-200 bg-rose-50 px-3 py-2 text-xs font-semibold text-rose-700 disabled:cursor-not-allowed disabled:opacity-50"
            >
              Delete
            </button>
          ` : null}
        </div>
      </div>
    </div>
  `;
}

function FacetValue({ value, selectedIds, onToggle, depth = 0 }) {
  const selected = selectedIds.includes(value.id);
  return html`
    <div>
      <label className="flex cursor-pointer items-start gap-3 py-2 text-sm text-slate-700" style=${{ paddingLeft: `${depth * 18}px` }}>
        <input type="checkbox" checked=${selected} onChange=${() => onToggle(value.id)} className="mt-1" />
        <span>${value.label}${value.count != null ? ` (${value.count.toLocaleString()})` : ''}</span>
      </label>
      ${(value.children || []).map((child) => html`<${FacetValue} key=${child.id} value=${child} selectedIds=${selectedIds} onToggle=${onToggle} depth=${depth + 1} />`)}
    </div>
  `;
}

function flattenFacetValues(values = []) {
  return values.flatMap((value) => [value, ...flattenFacetValues(value.children || [])]);
}

function facetId(facet) {
  return facet?.filter_id || facet?.id;
}

function selectedValueLabels(facet, predicate) {
  const selectedValues = Array.isArray(predicate?.value) ? predicate.value : [predicate?.value].filter(Boolean);
  const valueById = Object.fromEntries(flattenFacetValues(facet?.values || []).map((value) => [value.id, value]));
  return selectedValues.map((valueId) => valueById[valueId]?.label || valueId);
}

function PredicateSummary({ predicate, facet, compact = false }) {
  const labels = selectedValueLabels(facet, predicate);
  const visible = compact ? labels.slice(0, 3) : labels.slice(0, 8);
  const extra = labels.length - visible.length;
  return html`
    <div className=${compact ? '' : 'rounded-2xl bg-slate-50 p-3'}>
      <p className=${compact ? 'font-semibold text-slate-800' : 'text-sm font-bold text-slate-800'}>${facet?.label || predicate.field_id}</p>
      <p className=${compact ? 'mt-1 text-sm leading-6 text-slate-600' : 'mt-1 text-sm leading-6 text-slate-600'}>
        ${visible.length ? visible.join(', ') : 'No values selected'}
        ${extra > 0 ? `, +${extra} more` : ''}
      </p>
    </div>
  `;
}

export function RunSummary({ run, onOpenRun, onDelete, deleting = false }) {
  return html`
    <div className="rounded-2xl border border-slate-200 bg-white p-4">
      <p className="text-sm font-bold text-slate-900">${run.run_id}</p>
      <p className="mt-1 text-xs text-slate-500">${run.status} · ${new Date(run.submitted_at).toLocaleString()}</p>
      <p className="mt-2 line-clamp-2 text-sm text-slate-600">${run.request?.question?.text || 'No question recorded.'}</p>
      <div className="mt-3 flex flex-wrap gap-2">
        <button onClick=${() => onOpenRun(run.run_id)} className="rounded-xl border border-sky-300 bg-sky-50 px-3 py-2 text-xs font-semibold text-sky-700">Open run</button>
        ${onDelete ? html`
          <button
            disabled=${deleting}
            onClick=${(event) => {
              event?.stopPropagation?.();
              onDelete(run);
            }}
            className="rounded-xl border border-rose-200 bg-rose-50 px-3 py-2 text-xs font-semibold text-rose-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            Delete
          </button>
        ` : null}
      </div>
    </div>
  `;
}

export function InvestigationComposer({ onOpenRun }) {
  const [collections, setCollections] = useState([]);
  const [savedScopes, setSavedScopes] = useState([]);
  const [recentRuns, setRecentRuns] = useState([]);
  const [filterConfig, setFilterConfig] = useState(null);
  const [selectedCollectionIds, setSelectedCollectionIds] = useState([]);
  const [predicates, setPredicates] = useState([]);
  const [showFilters, setShowFilters] = useState(false);
  const [scopeName, setScopeName] = useState('');
  const [saveScope, setSaveScope] = useState(true);
  const [questionText, setQuestionText] = useState('');
  const [preview, setPreview] = useState(null);
  const [launchResult, setLaunchResult] = useState(null);
  const [launching, setLaunching] = useState(false);
  const [launchError, setLaunchError] = useState('');
  const [deletingScopeId, setDeletingScopeId] = useState('');
  const [scopeError, setScopeError] = useState('');
  const [deletingRunId, setDeletingRunId] = useState('');
  const [runError, setRunError] = useState('');
  const [historyTab, setHistoryTab] = useState('scopes');

  useEffect(() => {
    Promise.all([listCollections(), listScopes(), listRuns()]).then(([nextCollections, nextScopes, nextRuns]) => {
      setCollections(nextCollections);
      setSelectedCollectionIds((previous) => previous.length ? previous : defaultCollectionIds(nextCollections));
      setSavedScopes(nextScopes);
      setRecentRuns(nextRuns.slice().reverse());
    });
  }, []);

  useEffect(() => {
    if (!selectedCollectionIds.length) {
      setPreview(null);
      setFilterConfig(null);
      return;
    }
    previewEvidenceSubset({ collection_ids: selectedCollectionIds, predicates }).then(setPreview);
    resolveFilterConfig('a1', selectedCollectionIds).then(setFilterConfig);
  }, [selectedCollectionIds, predicates]);

  function toggleCollection(collectionId) {
    setSelectedCollectionIds((prev) => prev.includes(collectionId) ? prev.filter((id) => id !== collectionId) : [...prev, collectionId]);
  }

  function selectedValuesForFacet(facetId) {
    return predicates.find((predicate) => predicate.field_id === facetId)?.value || [];
  }

  function toggleFacetValue(facetId, valueId) {
    setPredicates((prev) => {
      const existing = prev.find((predicate) => predicate.field_id === facetId);
      const values = existing?.value || [];
      const nextValues = values.includes(valueId) ? values.filter((id) => id !== valueId) : [...values, valueId];
      const withoutFacet = prev.filter((predicate) => predicate.field_id !== facetId);
      return nextValues.length ? [...withoutFacet, { field_id: facetId, operator: 'in', value: nextValues }] : withoutFacet;
    });
  }

  function updatePredicate(index, next) {
    setPredicates((prev) => prev.map((item, itemIndex) => itemIndex === index ? next : item));
  }

  function useScope(scope) {
    setSelectedCollectionIds(scope.collection_ids || []);
    setPredicates(scope.predicates || []);
    setScopeName(scope.name || '');
  }

  async function handleDeleteScope(scope) {
    const scopeId = scope?.scope_id;
    if (!scopeId || deletingScopeId) return;
    setScopeError('');
    setDeletingScopeId(scopeId);
    try {
      await deleteScope(scopeId);
      setSavedScopes((previous) => previous.filter((item) => item.scope_id !== scopeId));
    } catch (error) {
      setScopeError(error.message || 'Unable to delete saved scope.');
    } finally {
      setDeletingScopeId('');
    }
  }

  async function handleDeleteRun(run) {
    const runId = run?.run_id;
    if (!runId || deletingRunId) return;
    setRunError('');
    setDeletingRunId(runId);
    try {
      await deleteRun(runId);
      setRecentRuns((previous) => previous.filter((item) => item.run_id !== runId));
    } catch (error) {
      setRunError(error.message || 'Unable to delete run.');
    } finally {
      setDeletingRunId('');
    }
  }

  async function handleLaunch() {
    if (!canLaunch || launching) return;
    setLaunchError('');
    setLaunching(true);
    try {
      const request = {
        request_id: `req-${Date.now()}`,
        question: { text: questionText },
        scope: {
          ...(scopeName ? { name: scopeName } : {}),
          collection_ids: selectedCollectionIds,
          predicates
        },
        options: { save_scope: saveScope, execution_mode: 'standard' }
      };
      const run = await launchRun(request);
      setLaunchResult(run);
      setRecentRuns((prev) => [run, ...prev.filter((item) => item.run_id !== run.run_id)]);
      if (onOpenRun) {
        onOpenRun(run.run_id, run);
        return;
      }
      const nextScopes = await listScopes();
      setSavedScopes(nextScopes);
    } catch (error) {
      setLaunchError(error.message || 'Unable to launch investigation.');
    } finally {
      setLaunching(false);
    }
  }

  const canLaunch = selectedCollectionIds.length > 0 && questionText.trim().length > 0;
  const groups = filterConfig?.groups || [];
  const facets = groups.flatMap((group) => group.filters);

  return html`
    <div className="min-h-screen bg-slate-50 text-slate-800">
      <header className="border-b border-slate-200 bg-white px-6 py-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="text-xs font-bold uppercase tracking-[0.2em] text-sky-700">Evidence Explorer</p>
            <h1 className="mt-1 text-2xl font-bold text-slate-950">New Investigation</h1>
          </div>
          <button onClick=${() => onOpenRun('example-completed')} className="rounded-xl border border-sky-300 bg-white px-4 py-2 text-sm font-semibold text-sky-700 hover:bg-sky-50">Open example completed run</button>
        </div>
      </header>

      <main className="grid gap-5 p-6 xl:grid-cols-[minmax(0,1.45fr)_360px]">
        <div className="space-y-5">
          <section className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
            <div className="mb-4">
              <p className="text-xs font-bold uppercase tracking-[0.2em] text-sky-700">1 · Research question</p>
              <h2 className="mt-1 text-lg font-bold text-slate-900">What do you want to investigate?</h2>
            </div>
            <label className="block">
              <span className="text-sm font-semibold text-slate-700">Research question</span>
              <textarea value=${questionText} onChange=${(event) => setQuestionText(event.target.value)} rows="4" placeholder="What do you want to know about these documents?" className="mt-2 w-full rounded-2xl border border-slate-300 px-4 py-3 text-sm leading-6"></textarea>
            </label>
          </section>

          <section className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
            <div className="mb-4">
              <div>
                <p className="text-xs font-bold uppercase tracking-[0.2em] text-sky-700">2 · Evidence scope</p>
                <h2 className="mt-1 text-lg font-bold text-slate-900">Choose collections</h2>
              </div>
            </div>
            <div className="grid gap-3 lg:grid-cols-2">
              ${collections.map((collection) => html`<${CollectionCard} key=${collection.collection_id} collection=${collection} selected=${selectedCollectionIds.includes(collection.collection_id)} onToggle=${toggleCollection} />`)}
            </div>
          </section>

          <section className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
            <div className="mb-4">
              <p className="text-xs font-bold uppercase tracking-[0.2em] text-sky-700">3 · Metadata filters</p>
              <h2 className="mt-1 text-lg font-bold text-slate-900">Narrow the selected documents</h2>
              <p className="mt-2 text-sm leading-6 text-slate-500">Facet definitions are loaded from collection metadata, rather than hard-coded into the product.</p>
            </div>
            <button onClick=${() => setShowFilters(true)} disabled=${!selectedCollectionIds.length} className="rounded-xl bg-sky-500 px-4 py-2 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-50">Open filters</button>
            <div className="mt-4 space-y-2">
              ${predicates.length ? predicates.map((predicate) => {
                const facet = facets.find((item) => facetId(item) === predicate.field_id);
                return html`<${PredicateSummary} key=${predicate.field_id} predicate=${predicate} facet=${facet} />`;
              }) : html`<p className="rounded-2xl bg-slate-50 p-4 text-sm text-slate-500">No active filters yet.</p>`}
            </div>
            <div className="mt-4 grid gap-3 md:grid-cols-[1fr_auto] md:items-end">
              <label className="block">
                <span className="text-xs font-bold uppercase tracking-wide text-slate-500">Scope name</span>
                <input value=${scopeName} onChange=${(event) => setScopeName(event.target.value)} placeholder="Optional reusable scope name" className="mt-1 w-full rounded-xl border border-slate-300 px-3 py-2 text-sm" />
              </label>
              <label className="flex items-center gap-2 pb-2 text-sm text-slate-700">
                <input type="checkbox" checked=${saveScope} onChange=${(event) => setSaveScope(event.target.checked)} />
                Save scope for reuse
              </label>
            </div>
          </section>
        </div>

        <aside className="space-y-5">
          <section className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
            <p className="text-xs font-bold uppercase tracking-[0.2em] text-sky-700">Active scope</p>
            <h2 className="mt-1 text-lg font-bold text-slate-900">${scopeName || 'Untitled scope'}</h2>
            ${preview ? html`
              <div className="mt-4 space-y-4">
                <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-1">
                  <div className="rounded-2xl bg-sky-50 p-4">
                    <p className="text-xs font-bold uppercase tracking-wide text-sky-700">Documents</p>
                    <p className="mt-1 text-2xl font-bold text-slate-900">${preview.estimated_document_count.toLocaleString()}</p>
                  </div>
                  <div className="rounded-2xl bg-indigo-50 p-4">
                    <p className="text-xs font-bold uppercase tracking-wide text-indigo-700">Artifacts</p>
                    <p className="mt-1 text-2xl font-bold text-slate-900">${preview.estimated_artifact_count.toLocaleString()}</p>
                  </div>
                </div>
                <div>
                  <p className="text-xs font-bold uppercase tracking-wide text-slate-500">Collections included</p>
                  <div className="mt-2 space-y-2 text-sm text-slate-700">
                    ${preview.matched_collections.map((item) => {
                      const collection = collections.find((candidate) => candidate.collection_id === item.collection_id);
                      return html`<p key=${item.collection_id}>${collection?.name || item.collection_id} · ${item.estimated_document_count.toLocaleString()} docs</p>`;
                    })}
                  </div>
                </div>
                <div>
                  <p className="text-xs font-bold uppercase tracking-wide text-slate-500">Active predicates</p>
                  <div className="mt-2 space-y-2 text-sm text-slate-700">
                    ${predicates.length ? predicates.map((predicate, index) => {
                      const facet = facets.find((item) => facetId(item) === predicate.field_id);
                      return html`<${PredicateSummary} key=${index} predicate=${predicate} facet=${facet} compact=${true} />`;
                    }) : html`<p>No predicates applied.</p>`}
                  </div>
                </div>
              </div>
            ` : html`<p className="mt-4 text-sm leading-6 text-slate-500">Select at least one collection to preview the evidence subset.</p>`}
            <button
              disabled=${!canLaunch || launching}
              onClick=${handleLaunch}
              style=${{ backgroundColor: '#020617', color: '#ffffff' }}
              className="launch-primary-button mt-5 flex w-full items-center justify-center gap-2 rounded-2xl bg-slate-950 px-4 py-3 text-sm font-bold text-white shadow-sm transition hover:bg-slate-800 disabled:cursor-not-allowed disabled:bg-slate-800 disabled:text-white"
            >
              ${launching ? html`<span className="launch-spinner" aria-hidden="true"></span>` : null}
              ${launching ? 'Launching investigation...' : 'Launch investigation'}
            </button>
            ${launchError ? html`<p className="mt-3 rounded-2xl bg-rose-50 p-3 text-sm text-rose-700">${launchError}</p>` : null}
          </section>

          ${launchResult ? html`
            <section className="rounded-3xl border border-emerald-200 bg-emerald-50 p-5 shadow-sm">
              <p className="text-xs font-bold uppercase tracking-[0.2em] text-emerald-700">Run created</p>
              <p className="mt-2 text-sm text-slate-700">${launchResult.run_id}</p>
              <p className="mt-1 text-sm text-slate-600">Status: ${launchResult.status}</p>
            </section>
          ` : null}

          <section className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
            <div className="mb-4">
              <p className="text-xs font-bold uppercase tracking-[0.2em] text-sky-700">History</p>
              <h2 className="mt-1 text-lg font-bold text-slate-900">Saved scopes and recent runs</h2>
            </div>
            <div className="history-tabs" role="tablist" aria-label="History">
              <span className=${`history-tabs__indicator ${historyTab === 'runs' ? 'history-tabs__indicator--runs' : ''}`} aria-hidden="true"></span>
              <button
                type="button"
                role="tab"
                aria-selected=${historyTab === 'scopes'}
                onClick=${() => setHistoryTab('scopes')}
                className=${`history-tabs__tab ${historyTab === 'scopes' ? 'history-tabs__tab--active' : ''}`}
              >
                <span>Saved scopes</span>
              </button>
              <button
                type="button"
                role="tab"
                aria-selected=${historyTab === 'runs'}
                onClick=${() => setHistoryTab('runs')}
                className=${`history-tabs__tab ${historyTab === 'runs' ? 'history-tabs__tab--active' : ''}`}
              >
                <span>Recent runs</span>
              </button>
            </div>

            <div className="mt-4">
              ${historyTab === 'scopes' ? html`
                ${scopeError ? html`<p className="mb-3 rounded-2xl bg-rose-50 p-3 text-sm text-rose-700">${scopeError}</p>` : null}
                <div className="composer-scroll-list composer-scroll-list--history space-y-3">
                  ${savedScopes.length ? savedScopes.map((scope) => html`<${ScopeSummary} key=${scope.scope_id} scope=${scope} onUse=${useScope} onDelete=${handleDeleteScope} deleting=${deletingScopeId === scope.scope_id} />`) : html`<p className="text-sm text-slate-500">No saved scopes yet.</p>`}
                </div>
              ` : html`
                ${runError ? html`<p className="mb-3 rounded-2xl bg-rose-50 p-3 text-sm text-rose-700">${runError}</p>` : null}
                <div className="composer-scroll-list composer-scroll-list--history space-y-3">
                  ${recentRuns.length ? recentRuns.map((run) => html`<${RunSummary} key=${run.run_id} run=${run} onOpenRun=${onOpenRun} onDelete=${handleDeleteRun} deleting=${deletingRunId === run.run_id} />`) : html`<p className="text-sm text-slate-500">No launched runs yet.</p>`}
                </div>
              `}
            </div>
          </section>
        </aside>
      </main>

      ${showFilters ? html`
        <div className="fixed inset-0 z-20 bg-slate-900/30">
          <div className="filter-drawer absolute right-0 top-0 flex h-full flex-col bg-white shadow-2xl">
            <div className="flex items-center justify-between border-b border-slate-200 px-5 py-4">
              <div>
                <p className="text-xs font-bold uppercase tracking-[0.2em] text-sky-700">Filters</p>
                <p className="mt-1 text-sm text-slate-500">${predicates.length} active facet(s)</p>
              </div>
              <button onClick=${() => setShowFilters(false)} className="rounded-xl border border-slate-300 px-3 py-2 text-sm font-semibold text-slate-700">Close</button>
            </div>
            <div className="flex-1 overflow-y-auto p-5">
              <div className="space-y-6">
                ${groups.map((group) => html`
                  <section key=${group.group_id}>
                    <p className="mb-3 text-xs font-bold uppercase tracking-wide text-slate-500">${group.label}</p>
                    <div className="space-y-5">
                      ${group.filters.map((facet) => html`
                        <section key=${facet.filter_id} className="border-b border-slate-200 pb-4">
                          <div className="mb-2 flex items-center justify-between gap-3">
                            <h3 className="text-sm font-bold text-slate-900">${facet.label}</h3>
                            <span className="text-xs text-slate-400">${selectedValuesForFacet(facet.filter_id).length} selected</span>
                          </div>
                          ${(facet.values || []).length ? facet.values.map((value) => html`
                            <${FacetValue}
                              key=${value.id}
                              value=${value}
                              selectedIds=${selectedValuesForFacet(facet.filter_id)}
                              onToggle=${(valueId) => toggleFacetValue(facet.filter_id, valueId)}
                            />
                          `) : html`<p className="text-sm text-slate-400">Values loaded from collection metadata.</p>`}
                        </section>
                      `)}
                    </div>
                  </section>
                `)}
              </div>
            </div>
          </div>
        </div>
      ` : null}
    </div>
  `;
}
