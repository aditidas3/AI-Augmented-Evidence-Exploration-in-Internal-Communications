import React, { useEffect, useMemo, useState } from '../vendor/react.bundle.mjs';
import { html } from './html.js';
import { loadRunBundles } from './loaders/bundleLoader.js';
import { adaptCorrectedIntent } from './adapters/correctedIntentAdapter.js';
import { deriveRunModel } from './adapters/deriveRunModel.js';
import { LeftNav } from './components/LeftNav.js';
import { Overview } from './components/Overview.js';
import { InvestigationComposer } from './components/InvestigationComposer.js';
import { RunStatus } from './components/RunStatus.js';
import { getRun, getRunExploration, listAnalystReviews, listConflictReviews, saveAnalystReview, saveConflictReview } from './services/authoringService.js';
import { AnswerView } from './components/AnswerView.js';
import { FindingsView } from './components/FindingsView.js';
import { ConflictsView } from './components/ConflictsView.js';
import { ExplainView } from './components/ExplainView.js';
import { TraceView } from './components/TraceView.js';
import { DetailDrawer } from './components/DetailDrawer.js';
import { AlignView } from './components/AlignView.js';
import { GraphsView } from './components/GraphsView.js';
import { RunLoadingScreen } from './components/RunLoadingScreen.js';
import { buildUserFriendlyRunReport, downloadTextFile, safeReportFilename } from './utils/reportExport.js';

const viewNotes = {
  align: 'Will expose slot bindings, localized subgraphs, witnesses, anchors, mentions, and ALIGN diagnostics from the current bundle output.',
  trace: 'Will expose ranked chains, slot candidates, EG deltas, RG trace material, and execution provenance from TRACE.',
  conflicts: 'Will expose detected conflicts, contested claims, defeaters, and explanation narratives.',
  answer: 'Will expose findings, citation map, limitations, and the constructed answer graph.',
  explain: 'Will expose provenance narratives, tethers, uncertainty entries, decision explanations, and evidence chains.',
  graphs: 'Will separate answer graph, evidence-graph delta, and reasoning-graph trace into distinct lenses.'
};

export function App() {
  const [mode, setMode] = useState('compose');
  const [rawBundles, setRawBundles] = useState(null);
  const [selectedRunId, setSelectedRunId] = useState(null);
  const [selectedRunRecord, setSelectedRunRecord] = useState(null);
  const [selection, setSelection] = useState(null);
  const [conflictReviews, setConflictReviews] = useState({});
  const [analystReviews, setAnalystReviews] = useState({});
  const [explorationContext, setExplorationContext] = useState(null);
  const [activeView, setActiveView] = useState('overview');
  const [error, setError] = useState('');

  function openRun(runId, runRecord = null) {
    setSelectedRunId(runId);
    setSelectedRunRecord(runRecord);
    setRawBundles(null);
    setSelection(null);
    setMode('run');
  }

  useEffect(() => {
    if (!selectedRunId) return;
    let cancelled = false;
    let timer = null;
    setRawBundles(null);
    setSelectedRunRecord((prev) => prev?.run_id === selectedRunId ? prev : null);
    setConflictReviews({});
    setAnalystReviews({});
    setExplorationContext(null);
    setError('');

    async function loadRun() {
      try {
        const run = await getRun(selectedRunId);
        if (cancelled) return;
        setSelectedRunRecord(run);
        if (run.result_index_ref) {
          const [bundles, reviews, analystObjectReviews, exploration] = await Promise.all([
            loadRunBundles(selectedRunId),
            listConflictReviews(selectedRunId).catch(() => ({})),
            listAnalystReviews(selectedRunId).catch(() => ({})),
            getRunExploration(selectedRunId).catch(() => null)
          ]);
          if (!cancelled) setRawBundles(bundles);
          if (!cancelled) setConflictReviews(reviews || {});
          if (!cancelled) setAnalystReviews(analystObjectReviews || {});
          if (!cancelled) setExplorationContext(exploration);
        } else if (!['completed', 'failed'].includes(run.status)) {
          timer = setTimeout(loadRun, 1500);
        }
      } catch (err) {
        if (!cancelled) setError(err.message || 'Failed to load run.');
      }
    }

    loadRun();

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [selectedRunId]);

  const model = useMemo(() => {
    if (!rawBundles) return null;
    return deriveRunModel({
      question: adaptCorrectedIntent(rawBundles.correctedIntent),
      outputs: {
        align: rawBundles.align,
        trace: rawBundles.trace,
        conflict: rawBundles.conflict,
        construct: rawBundles.construct,
        explain: rawBundles.explain
      }
    });
  }, [rawBundles]);

  if (mode === 'compose') {
    return html`<${InvestigationComposer} onOpenRun=${openRun} />`;
  }

  if (error) return html`<div className="p-6 text-sm text-rose-700">${error}</div>`;
  if (selectedRunRecord && !selectedRunRecord.result_index_ref) {
    return html`<${RunStatus} run=${selectedRunRecord} onBack=${() => setMode('compose')} />`;
  }
  if (!model) {
    return html`<${RunLoadingScreen} runId=${selectedRunId} runRecord=${selectedRunRecord} />`;
  }

  async function handleSaveConflictReview(edgeId, review) {
    const optimistic = {
      ...(conflictReviews[edgeId] || {}),
      edge_id: edgeId,
      run_id: selectedRunId,
      ...review,
      updated_at: new Date().toISOString()
    };
    setConflictReviews((prev) => ({ ...prev, [edgeId]: optimistic }));
    const saved = await saveConflictReview(selectedRunId, edgeId, review);
    setConflictReviews((prev) => ({ ...prev, [edgeId]: saved }));
  }

  async function handleSaveAnalystReview(objectType, objectId, review) {
    const objectKey = `${objectType}:${objectId}`;
    const optimistic = {
      ...(analystReviews[objectKey] || {}),
      object_key: objectKey,
      object_type: objectType,
      object_id: objectId,
      run_id: selectedRunId,
      ...review,
      updated_at: new Date().toISOString()
    };
    setAnalystReviews((prev) => ({ ...prev, [objectKey]: optimistic }));
    const saved = await saveAnalystReview(selectedRunId, objectType, objectId, review);
    setAnalystReviews((prev) => ({ ...prev, [saved.object_key || objectKey]: saved }));
  }

  function handleDownloadReport() {
    const report = buildUserFriendlyRunReport({
      model,
      runRecord: selectedRunRecord,
      explorationContext,
      conflictReviews
    });
    downloadTextFile(safeReportFilename(selectedRunRecord?.run_id || selectedRunId || model.run_id), report);
  }

  return html`
    <div className="workbench-shell">
      <${LeftNav} activeView=${activeView} setActiveView=${setActiveView} model=${model} conflictReviews=${conflictReviews} selection=${selection} />
      <main className="workbench-main">
        <div className="workbench-topbar">
          <div>
            <p className="workbench-topbar__title">Run workspace</p>
            <p className="workbench-topbar__meta">
              ${selectedRunRecord?.run_id || selectedRunId || model.run_id}
              ${model.overview.selected_chain_id ? ` · selected chain ${model.overview.selected_chain_id}` : ''}
            </p>
          </div>
          <div className="workbench-actions">
            <button onClick=${() => setMode('compose')} className="button-secondary">New investigation</button>
            <button
              onClick=${handleDownloadReport}
              className="button-primary"
              title="Download a readable Markdown report for this run"
            >
              Download report
            </button>
          </div>
        </div>
        ${activeView === 'overview' ? html`<${Overview} model=${model} explorationContext=${explorationContext} onOpenRun=${(runId) => { setActiveView('overview'); openRun(runId); }} onNavigate=${setActiveView} />` : null}
        ${activeView === 'trace' ? html`<${TraceView} model=${model} onSelectWitness=${(item) => setSelection({ kind: 'witness', item })} />` : null}
        ${activeView === 'conflicts' ? html`<${ConflictsView} model=${model} conflictReviews=${conflictReviews} onSelectConflict=${(item) => setSelection({ kind: 'conflict', item })} />` : null}
        ${activeView === 'answer' ? html`<${AnswerView} model=${model} onSelectWitness=${(item) => setSelection({ kind: 'witness', item })} onSelectAnswer=${(item) => setSelection({ kind: 'answer', item })} />` : null}
        ${activeView === 'explain' ? html`<${ExplainView} model=${model} onSelectWitness=${(item) => setSelection({ kind: 'witness', item })} onSelectFinding=${(item) => setSelection({ kind: 'finding', item })} />` : null}
        ${activeView === 'findings' ? html`<${FindingsView} model=${model} analystReviews=${analystReviews} onReviewFinding=${handleSaveAnalystReview} onSelectFinding=${(item) => setSelection({ kind: 'finding', item })} />` : null}
        ${activeView === 'align' ? html`<${AlignView} model=${model} onSelectWitness=${(item) => setSelection({ kind: 'witness', item })} />` : null}
        ${activeView === 'graphs' ? html`<${GraphsView} model=${model} selection=${selection} onSelectionChange=${setSelection} />` : null}
      </main>
      <${DetailDrawer}
        model=${model}
        selection=${selection}
        conflictReviews=${conflictReviews}
        analystReviews=${analystReviews}
        onSaveConflictReview=${handleSaveConflictReview}
        onSaveAnalystReview=${handleSaveAnalystReview}
        onClose=${() => setSelection(null)}
      />
    </div>
  `;
}






