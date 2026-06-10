import { reviewForConflict } from '../domain/conflictReview.js';
import { witnessLabel, witnessLocation } from '../domain/witnessContext.js';
import { findExactOrApproximateRanges, focusTextAroundRanges } from './textMatch.js';

function asDate(value = new Date()) {
  try {
    return new Date(value).toLocaleString();
  } catch {
    return String(value);
  }
}

function clean(value) {
  return String(value ?? '')
    .replace(/\r\n/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

function bullet(items) {
  const values = (items || []).filter(Boolean);
  return values.length ? values.map((item) => `- ${clean(item)}`).join('\n') : '- None recorded';
}

function section(title, body) {
  return `\n\n## ${title}\n\n${clean(body) || '_No information recorded._'}`;
}

function shortText(value, max = 900) {
  const text = clean(value).replace(/\s+/g, ' ');
  if (text.length <= max) return text;
  return `${text.slice(0, max).trim()}...`;
}

function evidenceSnippet(context) {
  const witness = context?.witness;
  const associatedValue = context?.associatedValue;
  const rawText = witness?.anchor?.raw_text || associatedValue?.raw_text || '';
  const candidates = [
    witness?.mention?.surface,
    associatedValue?.surface,
    ...(witness?.anchor?.metadata?._kg0_mentions || []).flatMap((item) => [item?.witness, item?.name])
  ].filter(Boolean);
  const ranges = findExactOrApproximateRanges(rawText, candidates);
  if (!rawText) return 'No raw source text emitted.';
  if (!ranges.length) return shortText(rawText, 700);
  const focused = focusTextAroundRanges(rawText, ranges, 220);
  return shortText(focused.text, 700);
}

function formatWitness(context, index) {
  const witness = context?.witness;
  if (!witness) return '';
  return [
    `### Evidence excerpt ${index}`,
    '',
    `**Witness:** ${witness.witness_id}`,
    `**Label:** ${witnessLabel(witness)}`,
    `**Location:** ${witnessLocation(witness)}`,
    context.slot ? `**Slot:** ${context.slot.slot_id} / ${context.slot.slot_type}` : '',
    witness.quality ? `**Quality:** ${witness.quality}` : '',
    '',
    '> ' + evidenceSnippet(context).replace(/\n/g, '\n> ')
  ].filter(Boolean).join('\n');
}

function topWitnessContexts(model, limit = 8) {
  const seen = new Set();
  const selectedChain = model.trace.ranked_chains?.[0];
  const fromTrace = (selectedChain?.nodes || [])
    .map((node) => model.witnesses.by_id[node.object_id])
    .filter(Boolean);
  const fromFindings = (model.answer.findings || [])
    .flatMap((finding) => finding.supporting_object_ids || [])
    .map((id) => model.witnesses.by_id[id])
    .filter(Boolean);

  return [...fromTrace, ...fromFindings, ...Object.values(model.witnesses.by_id || {})]
    .filter((context) => {
      const id = context?.witness?.witness_id;
      if (!id || seen.has(id)) return false;
      seen.add(id);
      return true;
    })
    .slice(0, limit);
}

function findingsSection(model) {
  const findings = model.answer.findings || [];
  if (!findings.length) return 'No findings were emitted.';
  return findings.slice(0, 20).map((finding, index) => [
    `### ${index + 1}. ${finding.display_id || finding.finding_id || 'Finding'}`,
    '',
    clean(finding.statement || finding.headline || JSON.stringify(finding)),
    '',
    finding.slot_type ? `- Slot: ${finding.slot_type}` : '',
    finding.evidence_strength != null ? `- Evidence strength: ${Math.round(finding.evidence_strength * 100)}%` : '',
    `- Supporting objects: ${(finding.supporting_object_ids || []).length}`,
    `- Conflicts: ${(finding.conflict_edge_ids || []).length}`
  ].filter(Boolean).join('\n')).join('\n\n');
}

function conflictsSection(model, conflictReviews) {
  const workups = model.conflicts.workups || [];
  if (!workups.length) return 'No conflict workups were emitted.';
  return workups.slice(0, 12).map((workup, index) => {
    const conflict = workup.edge;
    const review = reviewForConflict(conflictReviews, conflict.edge_id);
    return [
      `### ${index + 1}. ${workup.assessment?.label || conflict.rule || 'Conflict workup'}`,
      '',
      `- Edge: ${conflict.edge_id}`,
      `- Detector stance: ${conflict.stance || 'not recorded'}`,
      `- Analyst label: ${review.review_label}${review.review_status === 'default' ? ' (default)' : ''}`,
      `- Source: ${workup.source?.candidate?.surface || conflict.source_object_id}`,
      `- Target: ${workup.target?.candidate?.surface || conflict.target_object_id}`,
      '',
      clean(workup.assessment?.rationale || conflict.description || 'No rationale recorded.')
    ].join('\n');
  }).join('\n\n');
}

function scopeSummary(explorationContext) {
  const scope = explorationContext?.scope;
  if (!scope) return 'Scope metadata was not available for this run.';
  const collections = (scope.collection_ids || []).join(', ') || 'None recorded';
  const predicates = (scope.predicates || [])
    .map((predicate) => `${predicate.field_id} ${predicate.operator || 'in'} ${(predicate.value || []).join(', ')}`)
    .join('\n');
  return [
    `**Scope:** ${scope.name || scope.scope_id || 'Unnamed scope'}`,
    `**Collections:** ${collections}`,
    '',
    '**Predicates:**',
    predicates || 'None recorded'
  ].join('\n');
}

export function buildUserFriendlyRunReport({ model, runRecord, explorationContext, conflictReviews = {} }) {
  const generatedAt = asDate();
  const question = model.overview.question_text || model.question?.text || 'Question not recorded';
  const answerText = model.overview.answer_text || 'No answer text emitted.';
  const confidence = model.overview.confidence_score == null
    ? 'Not recorded'
    : `${Math.round(model.overview.confidence_score * 100)}%${model.overview.confidence_label ? ` (${model.overview.confidence_label})` : ''}`;
  const witnessContexts = topWitnessContexts(model, 8);

  return [
    '# Evidence Explorer Investigation Report',
    '',
    `Generated: ${generatedAt}`,
    `Run ID: ${runRecord?.run_id || model.run_id || 'unknown'}`,
    runRecord?.status ? `Run status: ${runRecord.status}` : '',
    '',
    '## Research question',
    '',
    question,
    section('Plain-language answer', answerText),
    section('Confidence and interpretation notes', [
      `**Confidence:** ${confidence}`,
      '',
      model.overview.selected_chain_id ? `**Selected reasoning chain:** ${model.overview.selected_chain_id}` : '',
      '',
      '**Warnings:**',
      bullet(model.overview.warnings)
    ].filter(Boolean).join('\n')),
    section('Document scope', scopeSummary(explorationContext)),
    section('Key counts', [
      `- ALIGN witnesses: ${model.alignment.witnesses.length}`,
      `- TRACE chains: ${model.trace.ranked_chains.length}`,
      `- Findings: ${model.answer.findings.length}`,
      `- Conflict edges: ${model.conflicts.edges.length}`,
      `- Answer citations: ${model.answer.citations.length}`,
      `- Explanation tethers: ${model.explanation.tethers.length}`
    ].join('\n')),
    section('Findings', findingsSection(model)),
    section('Conflict review summary', conflictsSection(model, conflictReviews)),
    section('Representative evidence excerpts', witnessContexts.length
      ? witnessContexts.map((context, index) => formatWitness(context, index + 1)).join('\n\n')
      : 'No witness contexts were available.'),
    section('Limitations and uncertainty', [
      '**Limitations:**',
      bullet((model.answer.limitations || []).map((item) => item.description || item.text || JSON.stringify(item))),
      '',
      '**Uncertainty entries:**',
      bullet((model.explanation.uncertainty_entries || []).slice(0, 12).map((item) => item.description || item.text || JSON.stringify(item)))
    ].join('\n')),
    section('Bundle lineage', (model.lineage.nodes || [])
      .map((node, index) => `${index + 1}. ${node.operator}: ${node.bundle_id}`)
      .join('\n')),
    section('How to read this report', [
      'This report is a human-readable export of the current Evidence Explorer run.',
      'It is not a replacement for the underlying JSON bundles.',
      'Use the run ID and bundle lineage above to replay or audit the full operator outputs later.'
    ].join('\n'))
  ].filter(Boolean).join('\n');
}

export function downloadTextFile(filename, contents, mimeType = 'text/markdown;charset=utf-8') {
  const blob = new Blob([contents], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

export function safeReportFilename(runId) {
  const safeRun = String(runId || 'run').replace(/[^a-z0-9_-]+/gi, '-').replace(/^-+|-+$/g, '') || 'run';
  const stamp = new Date().toISOString().slice(0, 10);
  return `evidence-explorer-${safeRun}-${stamp}.md`;
}
