function flattenSlotCandidates(slotCandidates) {
  return Object.values(slotCandidates || {}).flatMap((value) => Array.isArray(value) ? value : [value]).filter(Boolean);
}

export function buildTraceIndexes(trace) {
  const candidates = flattenSlotCandidates(trace.slot_candidates);
  const candidateById = Object.fromEntries(candidates.map((item) => [item.candidate_id, item]));
  const claimNodes = (trace.rg_trace?.nodes || []).filter((node) => node.labels?.includes('Claim') || node.properties?.domainType?.includes('SlotClaim'));
  const claimById = Object.fromEntries(claimNodes.map((node) => [node.properties?.uid, node]).filter(([id]) => id));
  const chainsByWitnessId = {};
  for (const chain of trace.ranked_chains || []) {
    for (const node of chain.nodes || []) {
      if (!node.object_id) continue;
      chainsByWitnessId[node.object_id] ||= [];
      chainsByWitnessId[node.object_id].push({
        chain_id: chain.chain_id,
        rank: chain.rank,
        confidence: chain.confidence,
        position: node.position,
        slot_type: node.slot_type,
        role: node.role
      });
    }
  }
  return { candidateById, claimById, chainsByWitnessId };
}

function slotAllowsMultiple(candidate, claim) {
  const slotType = candidate?.slot_type || claim?.properties?.domainMetadata?.slot_type || '';
  const description = [
    claim?.properties?.domainMetadata?.slot_description,
    claim?.properties?.statement
  ].filter(Boolean).join(' ').toLowerCase();
  if (['WHO', 'EVIDENCE'].includes(slotType)) return true;
  return /\b(employees|people|persons|actors|leaders|companies|documents|records|examples|sources)\b/.test(description);
}

export function classifyFusedConflict(edge, sourceCandidate, targetCandidate, sourceClaim, targetClaim) {
  if (edge.relationship_type || edge.semantic_relation) {
    return {
      status: 'semantic',
      label: edge.relationship_type || edge.semantic_relation,
      rationale: 'The conflict bundle provides an explicit semantic relationship.'
    };
  }

  if (edge.rule === 'SURFACE_MISMATCH') {
    const sameClaim = edge.claim_a_uid && edge.claim_a_uid === edge.claim_b_uid;
    const sameSlot = sourceCandidate?.slot_id && sourceCandidate.slot_id === targetCandidate?.slot_id;
    const allowsMultiple = slotAllowsMultiple(sourceCandidate, sourceClaim) || slotAllowsMultiple(targetCandidate, targetClaim);
    const sourceSurface = sourceCandidate?.surface || '';
    const targetSurface = targetCandidate?.surface || '';

    if (allowsMultiple && sameSlot) {
      return {
        status: 'plurality',
        label: 'Multiple candidate bindings / role distinction',
        rationale: [
          'This slot can plausibly admit more than one valid answer, so different WHO/EVIDENCE-style candidates are not automatically incompatible.',
          sameClaim ? 'Both sides map to the same TRACE claim id.' : null,
          `Both sides are candidates for ${sourceCandidate.slot_id} (${sourceCandidate.slot_type}).`,
          sourceSurface && targetSurface ? `Review whether "${sourceSurface}" and "${targetSurface}" are joint actors, different roles, or truly mutually exclusive.` : null
        ].filter(Boolean).join(' ')
      };
    }

    return {
      status: 'diagnostic',
      label: sameClaim || sameSlot ? 'Alternative bindings for same claim/slot' : 'Surface mismatch',
      rationale: [
        'This edge identifies competing extracted surfaces, not a proven logical contradiction.',
        sameClaim ? 'Both sides map to the same TRACE claim id.' : null,
        sameSlot ? `Both sides are candidates for ${sourceCandidate.slot_id} (${sourceCandidate.slot_type}).` : null,
        sourceSurface && targetSurface ? `Compare "${sourceSurface}" and "${targetSurface}" in their occurrence contexts before assigning REFUTES/SUPPORTS/QUALIFIES.` : null
      ].filter(Boolean).join(' ')
    };
  }

  return {
    status: 'unresolved',
    label: edge.stance || 'Claim relationship under review',
    rationale: 'The conflict edge needs ALIGN/TRACE context before assigning a semantic relationship.'
  };
}

