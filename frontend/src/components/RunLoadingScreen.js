import { html } from '../html.js';

const LOADING_STAGES = [
  ['ALIGN', 'Evidence bindings'],
  ['TRACE', 'Reasoning chains'],
  ['CONFLICT', 'Claim pressure'],
  ['CONSTRUCT', 'Answer bundle'],
  ['EXPLAIN', 'Citations']
];

export function RunLoadingScreen({ runId, runRecord }) {
  const question = runRecord?.request?.question?.text || runRecord?.question?.text || '';

  return html`
    <div className="run-loading-screen">
      <section className="run-loading-panel" aria-label="Loading run artifacts">
        <div className="run-loading-panel__main">
          <p className="run-loading-panel__eyebrow">Loading run artifacts</p>
          <h1>${runId || 'Preparing run workspace'}</h1>
          <p className="run-loading-panel__copy">
            Reconstructing evidence graph, citations, and answer bundle.
          </p>
          ${question ? html`
            <p className="run-loading-panel__question">${question}</p>
          ` : null}

          <div className="run-loading-flow" aria-label="Pipeline artifact stages">
            ${LOADING_STAGES.map(([stage, label], index) => html`
              <div key=${stage} className="run-loading-flow__stage" style=${{ '--stage-index': index }}>
                <span className="run-loading-flow__pulse" aria-hidden="true"></span>
                <strong>${stage}</strong>
                <small>${label}</small>
              </div>
            `)}
          </div>
        </div>

        <div className="run-loading-graph" aria-hidden="true">
          <span className="run-loading-graph__node run-loading-graph__node--a"></span>
          <span className="run-loading-graph__node run-loading-graph__node--b"></span>
          <span className="run-loading-graph__node run-loading-graph__node--c"></span>
          <span className="run-loading-graph__node run-loading-graph__node--d"></span>
          <span className="run-loading-graph__node run-loading-graph__node--e"></span>
          <span className="run-loading-graph__trace run-loading-graph__trace--one"></span>
          <span className="run-loading-graph__trace run-loading-graph__trace--two"></span>
          <span className="run-loading-graph__trace run-loading-graph__trace--three"></span>
          <span className="run-loading-graph__core"></span>
        </div>
      </section>
    </div>
  `;
}
