import { classifyConflictRelation } from '../conflictSemantics.js';
import { reviewForConflict } from './conflictReview.js';

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

function lower(value) {
  return String(value || '').toLowerCase();
}

function strengthOf(item) {
  const value = item?.evidence_strength ?? item?.confidence ?? item?.score;
  return value == null || Number.isNaN(Number(value)) ? null : Number(value);
}

function chainScoreForDisplay(chain) {
  if (chain?.score != null && !Number.isNaN(Number(chain.score))) return Number(chain.score);
  if (chain?.confidence != null && !Number.isNaN(Number(chain.confidence))) return Number(chain.confidence);
  return null;
}

function objectCount(value) {
  return Array.isArray(value) ? value.length : Object.keys(value || {}).length;
}

export function answerCitationHealth(model) {
  const answerText = String(model?.overview?.answer_text || '');
  const citations = asArray(model?.answer?.citations);
  let placed = 0;
  let cursor = 0;
  let unresolvedWitnessContexts = 0;

  for (const citation of citations) {
    const phrase = String(citation?.sentence_text || '').trim();
    if (phrase) {
      const index = answerText.toLowerCase().indexOf(phrase.toLowerCase(), cursor);
      if (index >= 0) {
        placed += 1;
        cursor = index + phrase.length;
      }
    }
    if (!asArray(citation?.eg_object_ids).length) unresolvedWitnessContexts += 1;
  }

  return {
    total: citations.length,
    placed,
    unplaced: Math.max(0, citations.length - placed),
    unresolvedWitnessContexts
  };
}

export function groupCitationSentences(model) {
  return asArray(model?.answer?.citations).map((citation, index) => ({
    ...citation,
    label: `Sentence ${Number(citation?.sentence_index ?? index) + 1}`,
    witnessCount: asArray(citation?.eg_object_ids).length
  }));
}

export function buildViewBadges(model, conflictReviews = {}) {
  const citationHealth = answerCitationHealth(model);
  const findings = asArray(model?.answer?.findings);
  const weakFindings = findings.filter((finding) => {
    const strength = strengthOf(finding);
    return strength != null && strength < 0.55;
  }).length;
  const findingConflicts = findings.reduce((sum, finding) => sum + asArray(finding?.conflict_edge_ids).length, 0);
  const incompleteChains = asArray(model?.trace?.ranked_chains).filter((chain) => !chain?.witness_complete).length;
  const unresolvedConflicts = asArray(model?.conflicts?.edges).filter((edge) =>
    reviewForConflict(conflictReviews, edge.edge_id).review_label === 'UNRESOLVED'
  ).length;

  return {
    overview: {
      count: null,
      attention: asArray(model?.overview?.warnings).length + citationHealth.unplaced + unresolvedConflicts
    },
    answer: {
      count: asArray(model?.answer?.citations).length,
      attention: citationHealth.unplaced + citationHealth.unresolvedWitnessContexts + asArray(model?.overview?.warnings).length
    },
    explain: {
      count: asArray(model?.explanation?.tethers).length,
      attention: asArray(model?.explanation?.uncertainty_entries).length + asArray(model?.explanation?.tether_failures).length
    },
    align: {
      count: asArray(model?.alignment?.witnesses).length,
      attention: asArray(model?.alignment?.slot_bindings).filter((slot) => asArray(slot?.witnesses).length === 0).length
    },
    trace: {
      count: asArray(model?.trace?.ranked_chains).length,
      attention: incompleteChains
    },
    findings: {
      count: findings.length,
      attention: weakFindings + findingConflicts
    },
    conflicts: {
      count: asArray(model?.conflicts?.edges).length,
      attention: unresolvedConflicts || asArray(model?.conflicts?.edges).length
    },
    graphs: {
      count: objectCount(model?.trace?.eg_delta?.nodes) + objectCount(model?.trace?.rg_trace?.nodes) + objectCount(model?.answer?.graph?.nodes),
      attention: 0
    }
  };
}

function witnessText(witness) {
  return [
    witness?.witness_id,
    witness?.quality,
    witness?.mention?.surface,
    witness?.anchor?.artifact_id,
    witness?.anchor?.metadata?.artifact_name,
    witness?.justification
  ].filter(Boolean).join(' ');
}

function slotText(slot) {
  return [
    slot?.slot_id,
    slot?.slot_type,
    slot?.quality,
    slot?.description,
    ...(slot?.value || []).map((value) => value?.surface),
    ...asArray(slot?.witnesses).map(witnessText)
  ].filter(Boolean).join(' ');
}

export function slotFilterOptions(slots) {
  return Array.from(new Set(asArray(slots).map((slot) => String(slot?.slot_type || 'UNKNOWN').toUpperCase()))).sort();
}

export function filterSlotBindings(slots, { query = '', slotType = 'ALL', quality = 'ALL', minConfidence = 0 } = {}) {
  const q = lower(query).trim();
  const wantedSlot = String(slotType || 'ALL').toUpperCase();
  const wantedQuality = String(quality || 'ALL').toUpperCase();
  const threshold = Number(minConfidence) || 0;

  return asArray(slots)
    .filter((slot) => {
      if (wantedSlot !== 'ALL' && String(slot?.slot_type || '').toUpperCase() !== wantedSlot) return false;
      if (wantedQuality !== 'ALL' && String(slot?.quality || '').toUpperCase() !== wantedQuality) return false;
      if (Number(slot?.confidence || 0) < threshold) return false;
      if (q && !lower(slotText(slot)).includes(q)) return false;
      return true;
    })
    .map((slot) => ({
      ...slot,
      witnesses: asArray(slot?.witnesses).slice().sort((a, b) => Number(b?.score || 0) - Number(a?.score || 0))
    }));
}

export function traceChainHealth(chain) {
  return {
    chain_id: chain?.chain_id,
    score: chainScoreForDisplay(chain),
    slotCoverage: Number(chain?.slot_coverage || 0),
    witnessComplete: Boolean(chain?.witness_complete),
    premiseCount: asArray(chain?.nodes).length
  };
}

export function filterTraceChains(chains, { filter = 'all', sortKey = 'rank' } = {}) {
  return asArray(chains)
    .filter((chain) => {
      if (filter === 'complete') return Boolean(chain?.witness_complete);
      if (filter === 'incomplete') return !chain?.witness_complete;
      return true;
    })
    .slice()
    .sort((a, b) => {
      if (sortKey === 'score') return (chainScoreForDisplay(b) ?? -1) - (chainScoreForDisplay(a) ?? -1);
      if (sortKey === 'coverage') return Number(b?.slot_coverage || 0) - Number(a?.slot_coverage || 0);
      return Number(a?.rank || 0) - Number(b?.rank || 0);
    });
}

function conflictText(workup) {
  return [
    workup?.edge?.edge_id,
    workup?.edge?.stance,
    workup?.edge?.rule,
    workup?.edge?.description,
    workup?.assessment?.label,
    workup?.assessment?.rationale,
    workup?.source?.candidate?.surface,
    workup?.target?.candidate?.surface
  ].filter(Boolean).join(' ');
}

export function filterConflictWorkups(workups, conflictReviews = {}, { reviewLabel = 'ALL', relationFilter = 'all', sortKey = 'confidence', query = '' } = {}) {
  const q = lower(query).trim();
  return asArray(workups)
    .filter((workup) => {
      const edge = workup?.edge || {};
      const review = reviewForConflict(conflictReviews, edge.edge_id);
      const relation = classifyConflictRelation(edge);
      if (reviewLabel !== 'ALL' && review.review_label !== reviewLabel) return false;
      if (relationFilter === 'semantic' && !relation.isSemantic) return false;
      if (relationFilter === 'diagnostic' && relation.isSemantic) return false;
      if (q && !lower(conflictText(workup)).includes(q)) return false;
      return true;
    })
    .slice()
    .sort((a, b) => {
      if (sortKey === 'review') {
        const left = reviewForConflict(conflictReviews, a?.edge?.edge_id).review_label;
        const right = reviewForConflict(conflictReviews, b?.edge?.edge_id).review_label;
        return left.localeCompare(right);
      }
      return Number(b?.edge?.confidence || 0) - Number(a?.edge?.confidence || 0);
    });
}
