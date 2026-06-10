from __future__ import annotations

import time

from storage import SCOPES_PATH, load_json, now_iso, save_json


def list_scopes():
    items = load_json(SCOPES_PATH)
    return items if isinstance(items, list) else []


def save_scopes(items):
    save_json(SCOPES_PATH, items)


def get_scope(scope_id: str):
    return next((item for item in list_scopes() if item.get('scope_id') == scope_id), None)


def delete_scope(scope_id: str):
    items = list_scopes()
    next_items = [item for item in items if item.get('scope_id') != scope_id]
    if len(next_items) == len(items):
        return None
    save_scopes(next_items)
    return {'scope_id': scope_id, 'deleted': True}


def create_scope(scope_payload: dict) -> dict:
    items = list_scopes()
    scope = {
        'scope_id': scope_payload.get('scope_id') or f"scope-{int(time.time() * 1000)}",
        'workspace_id': scope_payload.get('workspace_id') or 'workspace-default',
        'name': scope_payload.get('name') or 'Untitled scope',
        'description': scope_payload.get('description'),
        'collection_ids': scope_payload.get('collection_ids', []),
        'predicates': scope_payload.get('predicates', []),
        'parent_scope_id': scope_payload.get('parent_scope_id'),
        'relationship_to_parent': scope_payload.get('relationship_to_parent'),
        'derived_from': scope_payload.get('derived_from'),
        'scope_intent': scope_payload.get('scope_intent') or 'manual',
        'created_at': now_iso(),
        'updated_at': now_iso()
    }
    items.append(scope)
    save_scopes(items)
    return scope
