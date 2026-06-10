export const CONFLICT_REVIEW_OPTIONS = [
  ['AGREEMENT', 'Agreement / accept fused assessment', 'Default if the analyst takes no explicit exception.'],
  ['ROLE_DISTINCTION', 'Multiple valid actors / role distinction', 'Use when candidates may jointly or differently satisfy the claim.'],
  ['REFUTES', 'Semantic contradiction / refutes', 'Use only when the two contextualized claims cannot both hold.'],
  ['SUPPORTS', 'Supports / corroborates', 'Use when one claim strengthens or corroborates the other.'],
  ['QUALIFIES', 'Qualifies / reconciles', 'Use when qualifiers, time, or scope reconcile the apparent disagreement.'],
  ['UNRESOLVED', 'Flag unresolved', 'Use when expert review cannot decide from current evidence.']
];

export const CONFLICT_REVIEW_FILTERS = [
  ['ALL', 'All'],
  ['AGREEMENT', 'Agreement'],
  ['ROLE_DISTINCTION', 'Role distinction'],
  ['REFUTES', 'Refutes'],
  ['SUPPORTS', 'Supports'],
  ['QUALIFIES', 'Qualifies'],
  ['UNRESOLVED', 'Unresolved']
];

export function defaultConflictReview() {
  return {
    review_label: 'AGREEMENT',
    review_status: 'default'
  };
}

export function reviewForConflict(conflictReviews, edgeId) {
  return conflictReviews?.[edgeId] || defaultConflictReview();
}

export function reviewStatusForLabel(label) {
  if (label === 'UNRESOLVED') return 'unresolved';
  return 'reviewed';
}

