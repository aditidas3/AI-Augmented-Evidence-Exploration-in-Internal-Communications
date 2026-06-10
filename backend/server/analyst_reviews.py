from __future__ import annotations

from storage import ANALYST_REVIEWS_PATH, load_json, now_iso, save_json


def review_key(object_type: str, object_id: str) -> str:
    return f'{object_type}:{object_id}'


def list_analyst_reviews():
    return load_json(ANALYST_REVIEWS_PATH) or {}


def save_analyst_reviews(payload):
    save_json(ANALYST_REVIEWS_PATH, payload)


def reviews_for_run(run_id: str):
    return list_analyst_reviews().get(run_id, {})


def filtered_analyst_reviews(query: dict[str, list[str]]) -> dict:
    run_id = (query.get('run_id') or [''])[0]
    object_type = (query.get('object_type') or [''])[0]
    review_label = (query.get('review_label') or [''])[0]
    review_status = (query.get('review_status') or [''])[0]

    source = reviews_for_run(run_id) if run_id else {
        key: review
        for run_reviews in list_analyst_reviews().values()
        for key, review in run_reviews.items()
    }

    filtered = {}
    for key, review in source.items():
        if object_type and review.get('object_type') != object_type:
            continue
        if review_label and review.get('review_label') != review_label:
            continue
        if review_status and review.get('review_status') != review_status:
            continue
        filtered[key] = review
    return filtered


def save_analyst_review(run_id: str, object_type: str, object_id: str, review_payload: dict) -> dict:
    all_reviews = list_analyst_reviews()
    run_reviews = all_reviews.setdefault(run_id, {})
    key = review_key(object_type, object_id)
    existing = run_reviews.get(key, {})
    review = {
        **existing,
        'object_key': key,
        'run_id': run_id,
        'object_type': object_type,
        'object_id': object_id,
        'review_label': review_payload.get('review_label', 'AGREE'),
        'review_status': review_payload.get('review_status', 'default'),
        'notes': review_payload.get('notes', ''),
        'context': review_payload.get('context', {}),
        'reviewed_by': review_payload.get('reviewed_by', 'analyst'),
        'updated_at': now_iso()
    }
    if 'created_at' not in review:
        review['created_at'] = review['updated_at']
    run_reviews[key] = review
    save_analyst_reviews(all_reviews)
    return review
