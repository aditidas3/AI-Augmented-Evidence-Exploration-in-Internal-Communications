export function classifyConflictRelation(conflict) {
  const rule = conflict?.rule || '';
  const stance = conflict?.stance || '';
  const relationship = conflict?.relationship_type || conflict?.relation_type || conflict?.semantic_relation;

  if (relationship) {
    return {
      label: relationship,
      severity: relationship === 'SUPPORTS' || relationship === 'CORROBORATES' ? 'support' : 'semantic',
      isSemantic: true,
      explanation: 'This edge carries an explicit semantic relationship emitted by the conflict/corroboration process.'
    };
  }

  if (rule === 'SURFACE_MISMATCH') {
    return {
      label: 'Surface disagreement diagnostic',
      severity: 'diagnostic',
      isSemantic: false,
      explanation: 'Different extracted surfaces were detected for the same slot or claim. This is not automatically a logical contradiction; the surrounding claim contexts must be compared.'
    };
  }

  if (stance === 'SUPPORTS' || stance === 'CORROBORATES') {
    return {
      label: 'Support / corroboration',
      severity: 'support',
      isSemantic: true,
      explanation: 'This edge is marked as supporting or corroborating another claim.'
    };
  }

  if (stance === 'REFUTES') {
    return {
      label: 'Potential refutation',
      severity: 'semantic',
      isSemantic: true,
      explanation: 'This edge is marked as refuting, but it should still be read against the full claim contexts and qualifiers.'
    };
  }

  return {
    label: 'Claim relationship',
    severity: 'unknown',
    isSemantic: false,
    explanation: 'The relationship type is underspecified. Inspect both claim contexts before treating this as contradiction or support.'
  };
}

export function relationTone(relation) {
  if (relation.severity === 'support') return 'bg-emerald-100 text-emerald-800';
  if (relation.severity === 'diagnostic') return 'bg-violet-100 text-violet-800';
  if (relation.severity === 'semantic') return 'bg-rose-100 text-rose-800';
  if (relation.severity === 'plurality') return 'bg-cyan-100 text-cyan-800';
  return 'bg-slate-100 text-slate-700';
}
