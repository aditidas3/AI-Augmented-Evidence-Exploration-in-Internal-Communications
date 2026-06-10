from __future__ import annotations

import time

from questions import find_or_create_question
from scopes import create_scope
from storage import DEFAULT_WORKSPACE_ID, RUNS_DIR, RUNS_PATH, load_json, now_iso, remove_child_dir, save_json


def list_runs():
    items = load_json(RUNS_PATH)
    return items if isinstance(items, list) else []


def save_runs(items):
    save_json(RUNS_PATH, items)


def get_run(run_id: str):
    return next((item for item in list_runs() if item['run_id'] == run_id), None)


def delete_run(run_id: str) -> dict | None:
    items = list_runs()
    next_items = [item for item in items if item.get('run_id') != run_id]
    if len(next_items) == len(items):
        return None
    remove_child_dir(RUNS_DIR, run_id)
    save_runs(next_items)
    return {'run_id': run_id, 'deleted': True}


def update_run(run_id: str, updates: dict) -> dict | None:
    items = list_runs()
    for item in items:
        if item['run_id'] == run_id:
            item.update(updates)
            item['updated_at'] = now_iso()
            save_runs(items)
            return item
    return None


def create_run(request: dict) -> dict:
    items = list_runs()
    workspace_id = request.get('workspace_id') or request.get('exploration', {}).get('workspace_id') or DEFAULT_WORKSPACE_ID
    question = find_or_create_question(request.get('question', {}), workspace_id)
    scope_payload = request.get('scope', {})
    scope_payload['workspace_id'] = scope_payload.get('workspace_id') or workspace_id
    scope_id = scope_payload.get('scope_id')
    if request.get('options', {}).get('save_scope') and not scope_id:
        scope = create_scope(scope_payload)
        scope_id = scope['scope_id']
    run = {
        'run_id': f'run-{int(time.time() * 1000)}',
        'workspace_id': workspace_id,
        'parent_run_id': request.get('exploration', {}).get('parent_run_id'),
        'lineage_kind': request.get('exploration', {}).get('lineage_kind') or 'new_run',
        'comparison_group_id': request.get('exploration', {}).get('comparison_group_id'),
        'status': 'queued',
        'submitted_at': now_iso(),
        'question_id': question['question_id'],
        'scope_id': scope_id,
        'request': request
    }
    items.append(run)
    save_runs(items)
    if request.get('options', {}).get('execution_mode') != 'manual':
        from pipeline_jobs import start_pipeline_run
        start_pipeline_run(run['run_id'])
    return run
