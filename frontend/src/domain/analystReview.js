export const ANALYST_REVIEW_OPTIONS = [
  ['AGREE', 'Agree', 'Accept this object as currently represented.'],
  ['DISAGREE', 'Disagree', 'The object is wrong, misleading, or should not be used this way.'],
  ['UNRESOLVED', 'Leave unresolved', 'Keep this object open for later expert review.']
];

export function analystReviewStatusForLabel(label) {
  if (label === 'UNRESOLVED') return 'unresolved';
  if (label === 'DISAGREE') return 'disputed';
  return 'reviewed';
}

export function defaultAnalystReview() {
  return {
    review_label: 'AGREE',
    review_status: 'default'
  };
}

export function analystReviewKey(objectType, objectId) {
  return `${objectType}:${objectId}`;
}

export function reviewForObject(analystReviews, objectType, objectId) {
  return analystReviews?.[analystReviewKey(objectType, objectId)] || defaultAnalystReview();
}
