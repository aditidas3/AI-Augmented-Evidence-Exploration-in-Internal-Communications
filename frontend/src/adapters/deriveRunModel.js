import { buildTraceIndexes, classifyFusedConflict } from '../domain/conflictFusion.js';
import { buildWitnessContextIndex } from '../domain/witnessContext.js';

// Converts raw operator bundles for one run into the UI inspection model.
//
// This is intentionally run-local: it fuses ALIGN, TRACE, CONFLICT, CONSTRUCT,
// and EXPLAIN outputs for a single execution. Exploration-level concerns
// (workspace, related runs, comparisons, composite graphs) are fetched through
// /api/runs/:run_id/exploration and should not be mixed into this adapter.

function asEntries(mapLike) {
  return Array.isArray(mapLike) ? mapLike : Object.values(mapLike || {});
}

function buildConflictWorkups(conflictEdges, traceIndexes, witnessContexts) {
  return conflictEdges.map((edge) => {
    const sourceCandidate = traceIndexes.candidateById[edge.source_candidate_id];
    const targetCandidate = traceIndexes.candidateById[edge.target_candidate_id];
    const sourceClaim = traceIndexes.claimById[edge.claim_a_uid || sourceCandidate?.claim_uid];
    const targetClaim = traceIndexes.claimById[edge.claim_b_uid || targetCandidate?.claim_uid];

    return {
      edge,
      source: {
        witnessContext: witnessContexts[edge.source_object_id],
        candidate: sourceCandidate,
        claim: sourceClaim,
        chains: traceIndexes.chainsByWitnessId[edge.source_object_id] || []
      },
      target: {
        witnessContext: witnessContexts[edge.target_object_id],
        candidate: targetCandidate,
        claim: targetClaim,
        chains: traceIndexes.chainsByWitnessId[edge.target_object_id] || []
      },
      assessment: classifyFusedConflict(edge, sourceCandidate, targetCandidate, sourceClaim, targetClaim)
    };
  });
}

function buildLineage(align, trace, conflict, construct, explain) {
  const nodes = [
    ['ALIGN', align.result?.intent_id ? 'align-output' : null],
    ['TRACE', trace.trace_bundle_id],
    ['CONFLICT', conflict.conflict_bundle_id],
    ['CONSTRUCT', construct.construct_bundle_id],
    ['EXPLAIN', explain.explain_bundle_id]
  ].filter(([, id]) => id).map(([operator, bundle_id]) => ({ operator, bundle_id }));

  const edges = [];
  for (let index = 0; index < nodes.length - 1; index += 1) {
    edges.push({ from_bundle_id: nodes[index].bundle_id, to_bundle_id: nodes[index + 1].bundle_id });
  }
  return { nodes, edges };
}

export function deriveRunModel(raw) {
  const question = raw.question;
  const align = raw.outputs.align || {};
  const trace = raw.outputs.trace || {};
  const conflict = raw.outputs.conflict || {};
  const construct = raw.outputs.construct || {};
  const explain = raw.outputs.explain || {};
  const answer = construct.ans_bundle || {};
  const explanation = explain.explain_bundle || {};
  const explainCitationMap = asEntries(explanation.citation_map);
  const answerCitations = explainCitationMap.length
    ? explainCitationMap
    : (answer.citation_map || construct.citation_map || []);
  const slotBindings = align.result?.slot_bindings || [];
  const traceIndexes = buildTraceIndexes(trace);
  const witnessContexts = buildWitnessContextIndex(slotBindings);
  const conflictEdges = asEntries(conflict.conflict_structure?.edges);

  return {
    run_id: explain.explain_bundle_id || construct.construct_bundle_id || trace.trace_bundle_id || 'run-unknown',
    question,
    outputs: raw.outputs,
    lineage: buildLineage(align, trace, conflict, construct, explain),
    overview: {
      question_text: question.text,
      answer_text: explain.answer_text || explanation.answer_text || answer.answer_text || '',
      confidence_score: explain.confidence_score ?? explanation.confidence_score ?? answer.confidence ?? null,
      confidence_label: explain.confidence_label || null,
      selected_chain_id: answer.selected_chain_id || construct.selected_chain_id || explanation.selected_chain_id || null,
      warnings: explain.warnings || [],
      stats: explain.stats || {}
    },
    alignment: {
      slot_bindings: slotBindings,
      subgraphs: align.result?.subgraphs || [],
      witnesses: align.result?.all_witnesses || [],
      anchors: align.result?.all_anchors || [],
      mentions: align.result?.all_mentions || [],
      suppressed_mentions: align.result?.suppressed_mentions || []
    },
    witnesses: {
      by_id: witnessContexts
    },
    trace: {
      ranked_chains: trace.ranked_chains || [],
      slot_candidates: trace.slot_candidates || {},
      candidate_by_id: traceIndexes.candidateById,
      claim_by_id: traceIndexes.claimById,
      chains_by_witness_id: traceIndexes.chainsByWitnessId,
      eg_delta: trace.eg_delta || { nodes: [], edges: [] },
      rg_trace: trace.rg_trace || { nodes: [], edges: [] }
    },
    conflicts: {
      edges: conflictEdges,
      workups: buildConflictWorkups(conflictEdges, traceIndexes, witnessContexts),
      clusters: asEntries(conflict.conflict_structure?.clusters),
      stats: conflict.conflict_result?.stats || {},
      contested_claims: conflict.conflict_result?.claims_contested || [],
      explanations: explanation.conflict_explanations || []
    },
    answer: {
      findings: answer.findings || [],
      citations: answerCitations,
      construct_answer_text: explanation.construct_answer_text || answer.answer_text || '',
      construct_citation_map: explanation.construct_citation_map || answer.citation_map || construct.citation_map || [],
      limitations: answer.limitations || construct.limitations || [],
      graph: construct.g_ans || answer.g_ans || { nodes: [], edges: [] }
    },
    explanation: {
      summary: explanation.summary || '',
      provenance_narratives: explanation.provenance_narratives || [],
      conflict_explanations: explanation.conflict_explanations || [],
      decision_explanations: explanation.decision_explanations || [],
      uncertainty_entries: explanation.uncertainty_map || [],
      tethers: explanation.tether_map || [],
      tether_failures: explanation.tether_failures || [],
      evidence_chain: explain.evidence_chain || [],
      citations: explain.citations || []
    }
  };
}

