from __future__ import annotations

from storage import CONFLICT_REVIEWS_PATH, load_json, now_iso, save_json


def list_conflict_reviews():
    return load_json(CONFLICT_REVIEWS_PATH) or {}


def save_conflict_reviews(payload):
    save_json(CONFLICT_REVIEWS_PATH, payload)


def reviews_for_run(run_id: str):
    return list_conflict_reviews().get(run_id, {})


def filtered_conflict_reviews(query: dict[str, list[str]]) -> dict:
    run_id = (query.get('run_id') or [''])[0]
    review_label = (query.get('review_label') or [''])[0]
    review_status = (query.get('review_status') or [''])[0]

    source = reviews_for_run(run_id) if run_id else {
        edge_id: review
        for run_reviews in list_conflict_reviews().values()
        for edge_id, review in run_reviews.items()
    }

    filtered = {}
    for edge_id, review in source.items():
        if review_label and review.get('review_label') != review_label:
            continue
        if review_status and review.get('review_status') != review_status:
            continue
        filtered[edge_id] = review
    return filtered


def save_conflict_review(run_id: str, edge_id: str, review_payload: dict) -> dict:
    all_reviews = list_conflict_reviews()
    run_reviews = all_reviews.setdefault(run_id, {})
    existing = run_reviews.get(edge_id, {})
    review = {
        **existing,
        'run_id': run_id,
        'edge_id': edge_id,
        'review_label': review_payload.get('review_label', 'AGREEMENT'),
        'review_status': review_payload.get('review_status', 'reviewed'),
        'notes': review_payload.get('notes', ''),
        'reviewed_by': review_payload.get('reviewed_by', 'analyst'),
        'updated_at': now_iso()
    }
    if 'created_at' not in review:
        review['created_at'] = review['updated_at']
    run_reviews[edge_id] = review
    save_conflict_reviews(all_reviews)
    return review
