from __future__ import annotations

from storage import COLLECTIONS_PATH, load_json


def list_collections():
    return load_json(COLLECTIONS_PATH)


def preview_evidence_subset(request: dict) -> dict:
    selected = [item for item in list_collections() if item['collection_id'] in request.get('collection_ids', [])]
    base_count = sum(item.get('document_count', 0) for item in selected)
    predicate_discount = max(0.18, 1 - len(request.get('predicates', [])) * 0.22)
    estimated_document_count = round(base_count * predicate_discount)
    return {
        'estimated_document_count': estimated_document_count,
        'estimated_artifact_count': round(estimated_document_count * 1.06),
        'estimated_span_count': round(estimated_document_count * 4.8),
        'matched_collections': [
            {
                'collection_id': item['collection_id'],
                'estimated_document_count': round(item.get('document_count', 0) * predicate_discount)
            }
            for item in selected
        ],
        'warnings': [] if request.get('collection_ids') else [
            {'code': 'NO_COLLECTIONS', 'message': 'Select at least one collection.'}
        ]
    }
