import React from '../../vendor/react.bundle.mjs';
import { html } from '../html.js';
import { pct } from '../format.js';
import { answerCitationHealth, groupCitationSentences } from '../domain/workbenchUx.js';

function citationWitnessContexts(model, citation) {
  return (citation.eg_object_ids || [])
    .map((id) => model.witnesses?.by_id?.[id])
    .filter(Boolean);
}

function escapeRegex(value) {
  return String(value || '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function placeCitations(answerText, citations) {
  const text = String(answerText || '');
  const placed = [];
  const unplaced = [];
  let cursor = 0;

  for (const citation of citations || []) {
    const phrase = String(citation.sentence_text || '').trim();
    if (!phrase) {
      unplaced.push(citation);
      continue;
    }

    const pattern = new RegExp(escapeRegex(phrase), 'i');
    const remaining = text.slice(cursor);
    const match = remaining.match(pattern);
    if (!match || match.index == null) {
      unplaced.push(citation);
      continue;
    }

    const start = cursor + match.index;
    const end = start + match[0].length;
    if (start > cursor) {
      placed.push({ kind: 'text', text: text.slice(cursor, start) });
    }
    placed.push({ kind: 'citation', text: text.slice(start, end), citation });
    cursor = end;
  }

  if (cursor < text.length) {
    placed.push({ kind: 'text', text: text.slice(cursor) });
  }

  return { placed, unplaced };
}

function CitationChip({ citation, contexts, onSelectWitness }) {
  const label = `C${Number(citation.sentence_index ?? 0) + 1}`;
  const primaryContext = contexts[0];
  return html`
    <button
      type="button"
      disabled=${!primaryContext}
      title=${contexts.length ? `${contexts.length} witness context(s)` : 'No resolved witness context'}
      onClick=${() => primaryContext && onSelectWitness && onSelectWitness(primaryContext)}
      className=${`ml-1 inline-flex align-baseline rounded-full border px-1.5 py-0.5 text-[10px] font-bold leading-none ${primaryContext ? 'border-sky-300 bg-sky-50 text-sky-800 hover:bg-sky-100' : 'border-amber-200 bg-amber-50 text-amber-800'}`}
    >
      [${label}]
    </button>
  `;
}

function CitedAnswerNarrative({ model, onSelectWitness }) {
  const { placed, unplaced } = placeCitations(model.overview.answer_text, model.answer.citations);

  return html`
    <div className="mt-3 whitespace-pre-wrap text-sm leading-8 text-slate-700">
      ${placed.map((part, index) => {
        if (part.kind === 'text') return html`<span key=${index}>${part.text}</span>`;
        const contexts = citationWitnessContexts(model, part.citation);
        return html`
          <span key=${index} className="rounded bg-sky-50 px-1 font-semibold text-slate-900">
            ${part.text}
            <${CitationChip} citation=${part.citation} contexts=${contexts} onSelectWitness=${onSelectWitness} />
          </span>
        `;
      })}
      ${!placed.length ? html`<span>${model.overview.answer_text || 'No answer text emitted.'}</span>` : null}
      ${unplaced.length ? html`
        <div className="mt-4 rounded-2xl border border-amber-200 bg-amber-50 p-3">
          <p className="text-xs font-bold uppercase tracking-wide text-amber-800">Unplaced citations</p>
          <p className="mt-1 text-xs leading-5 text-amber-800">
            These citation entries did not match a unique forward phrase in the answer text, so they are kept here rather than dropped.
          </p>
          <div className="mt-2 flex flex-wrap gap-2">
            ${unplaced.map((citation) => {
              const contexts = citationWitnessContexts(model, citation);
              return html`
                <button
                  key=${citation.sentence_index}
                  disabled=${!contexts[0]}
                  onClick=${() => contexts[0] && onSelectWitness && onSelectWitness(contexts[0])}
                  className=${`rounded-full border px-3 py-1 text-xs font-semibold ${contexts[0] ? 'border-sky-200 bg-white text-sky-800 hover:bg-sky-50' : 'border-amber-200 bg-white/70 text-amber-800'}`}
                >
                  C${Number(citation.sentence_index ?? 0) + 1}: ${citation.sentence_text}
                </button>
              `;
            })}
          </div>
        </div>
      ` : null}
    </div>
  `;
}

function CopyButton({ text, label = 'Copy' }) {
  return html`
    <button
      type="button"
      onClick=${() => globalThis.navigator?.clipboard?.writeText?.(String(text || '')).catch?.(() => {})}
      className="rounded-full border border-slate-200 bg-white px-3 py-1 text-xs font-bold text-slate-700 hover:bg-slate-50"
    >
      ${label}
    </button>
  `;
}

export function AnswerView({ model, onSelectWitness, onSelectAnswer }) {
  const [activeSentenceIndex, setActiveSentenceIndex] = React.useState(null);
  const citationHealth = answerCitationHealth(model);
  const citationSentences = groupCitationSentences(model);
  const activeCitation = activeSentenceIndex == null
    ? null
    : citationSentences.find((item) => Number(item.sentence_index ?? 0) === Number(activeSentenceIndex));
  const visibleCitations = activeCitation ? [activeCitation] : model.answer.citations;
  const finalAnswer = String(model.overview.answer_text || '');
  const constructAnswer = String(model.answer.construct_answer_text || '');
  const answerDiffers = finalAnswer && constructAnswer && finalAnswer.trim() !== constructAnswer.trim();

  return html`
    <div className="space-y-5 p-5">
      <section className="grid gap-4 md:grid-cols-3">
        <div className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
          <p className="text-sm font-semibold text-slate-500">Confidence</p>
          <p className="mt-2 text-3xl font-bold text-slate-900">${pct(model.overview.confidence_score)}</p>
          <p className="text-sm text-slate-500">${model.overview.confidence_label || 'No label'}</p>
        </div>
        <div className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
          <p className="text-sm font-semibold text-slate-500">Citations</p>
          <p className="mt-2 text-3xl font-bold text-slate-900">${model.explanation.citations.length}</p>
        </div>
        <div className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
          <p className="text-sm font-semibold text-slate-500">Warnings</p>
          <p className="mt-2 text-3xl font-bold text-slate-900">${model.overview.warnings.length}</p>
        </div>
      </section>

      <section className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="text-lg font-bold text-slate-900">Investigator answer</h2>
            <p className="mt-1 text-sm leading-6 text-slate-500">
              Inline citation chips open the underlying witness context. Citation health summarizes placement and witness resolution.
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <${CopyButton} text=${model.overview.answer_text} label="Copy answer" />
            <button
              onClick=${() => onSelectAnswer && onSelectAnswer({ object_id: `answer:${model.run_id}`, text: model.overview.answer_text, confidence: model.overview.confidence_score })}
              className="rounded-full border border-emerald-200 bg-emerald-50 px-3 py-1 text-xs font-bold text-emerald-800 hover:bg-emerald-100"
            >
              Review answer
            </button>
            <span className="rounded-full bg-sky-50 px-3 py-1 text-xs font-bold text-sky-800">${model.answer.citations.length} inline citation(s)</span>
          </div>
        </div>
        <div className="mt-4 grid gap-3 md:grid-cols-4">
          <div className="rounded-2xl bg-slate-50 p-3">
            <p className="text-xs font-bold uppercase tracking-wide text-slate-500">Placed citations</p>
            <p className="mt-1 text-xl font-bold text-slate-900">${citationHealth.placed}/${citationHealth.total}</p>
          </div>
          <div className=${`rounded-2xl p-3 ${citationHealth.unplaced ? 'bg-amber-50' : 'bg-emerald-50'}`}>
            <p className="text-xs font-bold uppercase tracking-wide text-slate-500">Unplaced</p>
            <p className="mt-1 text-xl font-bold text-slate-900">${citationHealth.unplaced}</p>
          </div>
          <div className=${`rounded-2xl p-3 ${citationHealth.unresolvedWitnessContexts ? 'bg-amber-50' : 'bg-emerald-50'}`}>
            <p className="text-xs font-bold uppercase tracking-wide text-slate-500">No witness id</p>
            <p className="mt-1 text-xl font-bold text-slate-900">${citationHealth.unresolvedWitnessContexts}</p>
          </div>
          <div className="rounded-2xl bg-sky-50 p-3">
            <p className="text-xs font-bold uppercase tracking-wide text-sky-700">Warnings</p>
            <p className="mt-1 text-xl font-bold text-slate-900">${model.overview.warnings.length}</p>
          </div>
        </div>
        <${CitedAnswerNarrative} model=${model} onSelectWitness=${onSelectWitness} />
      </section>

      ${answerDiffers ? html`
        <section className="rounded-3xl border border-indigo-200 bg-indigo-50 p-5 shadow-sm">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <h2 className="text-lg font-bold text-slate-900">Final answer vs CONSTRUCT answer</h2>
            <${CopyButton} text=${constructAnswer} label="Copy construct answer" />
          </div>
          <div className="mt-4 grid gap-4 xl:grid-cols-2">
            <div className="rounded-2xl bg-white/80 p-4">
              <p className="text-xs font-bold uppercase tracking-wide text-indigo-700">Final answer</p>
              <p className="mt-2 whitespace-pre-wrap text-sm leading-7 text-slate-800">${finalAnswer}</p>
            </div>
            <div className="rounded-2xl bg-white/80 p-4">
              <p className="text-xs font-bold uppercase tracking-wide text-indigo-700">CONSTRUCT answer</p>
              <p className="mt-2 whitespace-pre-wrap text-sm leading-7 text-slate-800">${constructAnswer}</p>
            </div>
          </div>
        </section>
      ` : null}

      <section className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
        <div className="mb-3">
          <h2 className="text-lg font-bold text-slate-900">Source coverage</h2>
          <p className="mt-1 text-sm leading-6 text-slate-500">
            Each answer sentence can jump back to the witness context that grounded it. Use the sentence chips to inspect one sentence at a time.
          </p>
        </div>
        ${citationSentences.length ? html`
          <div className="mb-4 flex flex-wrap gap-2">
            <button onClick=${() => setActiveSentenceIndex(null)} className=${`rounded-full px-3 py-1 text-xs font-bold ${activeSentenceIndex == null ? 'bg-slate-900 text-white' : 'bg-slate-100 text-slate-700 hover:bg-slate-200'}`}>All sentences</button>
            ${citationSentences.map((citation) => html`
              <button
                key=${citation.sentence_index}
                onClick=${() => setActiveSentenceIndex(citation.sentence_index)}
                className=${`rounded-full px-3 py-1 text-xs font-bold ${Number(activeSentenceIndex) === Number(citation.sentence_index) ? 'bg-sky-600 text-white' : 'bg-sky-50 text-sky-800 hover:bg-sky-100'}`}
              >
                ${citation.label}
              </button>
            `)}
          </div>
        ` : null}
        <div className="space-y-3">
          ${visibleCitations.length ? visibleCitations.map((citation) => {
            const contexts = citationWitnessContexts(model, citation);
            return html`
              <article key=${citation.sentence_index} className="rounded-2xl border border-slate-200 bg-slate-50 p-4">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="rounded-full bg-sky-100 px-3 py-1 text-xs font-bold text-sky-800">Sentence ${citation.sentence_index}</span>
                  ${citation.confidence != null ? html`<span className="rounded-full bg-slate-100 px-3 py-1 text-xs font-semibold text-slate-700">confidence ${citation.confidence}</span>` : null}
                  <span className="rounded-full bg-white px-3 py-1 text-xs font-semibold text-slate-600">${contexts.length} witness context(s)</span>
                  <${CopyButton} text=${citation.sentence_text} label="Copy sentence" />
                </div>
                <p className="mt-3 text-sm leading-6 text-slate-800">${citation.sentence_text}</p>
                <div className="mt-3 flex flex-wrap gap-2">
                  ${contexts.map((context) => html`
                    <button
                      key=${context.witness.witness_id}
                      onClick=${() => onSelectWitness && onSelectWitness(context)}
                      className="rounded-full border border-sky-200 bg-white px-3 py-1 text-xs font-semibold text-sky-800 hover:bg-sky-50"
                    >
                      ${context.witness.witness_id}
                    </button>
                  `)}
                  ${!contexts.length ? html`<span className="text-xs text-amber-700">No matching witness context found for this citation.</span>` : null}
                </div>
              </article>
            `;
          }) : html`<p className="text-sm text-slate-500">No citation map emitted.</p>`}
        </div>
      </section>
      ${model.overview.warnings.length ? html`
        <section className="rounded-3xl border border-amber-200 bg-amber-50 p-5 shadow-sm">
          <h2 className="text-lg font-bold text-amber-900">Warnings</h2>
          <div className="mt-3 space-y-2">
            ${model.overview.warnings.map((item, index) => html`<div key=${index} className="rounded-2xl bg-white/70 p-3 text-sm text-slate-700">${item}</div>`)}
          </div>
        </section>
      ` : null}
      <section className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
        <h2 className="text-lg font-bold text-slate-900">Limitations</h2>
        <div className="mt-3 space-y-2">
          ${model.answer.limitations.length ? model.answer.limitations.map((item, index) => html`<div key=${index} className="rounded-2xl bg-slate-50 p-3 text-sm text-slate-700">${item.description || item.text || JSON.stringify(item)}</div>`) : html`<p className="text-sm text-slate-500">No limitations emitted.</p>`}
        </div>
      </section>
    </div>
  `;
}
