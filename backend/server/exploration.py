from __future__ import annotations

import time

from questions import get_question, list_questions
from runs import get_run, list_runs
from scopes import get_scope, list_scopes
from storage import COMPARISONS_PATH, COMPOSITES_PATH, DEFAULT_WORKSPACE_ID, WORKSPACES_PATH, load_json, now_iso, save_json


"""Exploration-level repository helpers.

The local prototype originally centered on a single completed run. Analyst
exploration is broader: a workspace can contain many questions, scopes, runs,
comparisons, and composite graph records. This module is the persistence seam
for that exploration spine. It still uses JSON files locally, but production
storage can replace these functions with database-backed repositories.
"""


def _as_list(path):
    payload = load_json(path)
    return payload if isinstance(payload, list) else []


def list_workspaces():
    workspaces = _as_list(WORKSPACES_PATH)
    if workspaces:
        return workspaces
    default = {
        'workspace_id': DEFAULT_WORKSPACE_ID,
        'name': 'Default exploration workspace',
        'description': 'Local prototype workspace for iterative questions, scopes, runs, comparisons, and composites.',
        'created_at': now_iso(),
        'updated_at': now_iso()
    }
    save_json(WORKSPACES_PATH, [default])
    return [default]


def get_workspace(workspace_id: str):
    return next((item for item in list_workspaces() if item['workspace_id'] == workspace_id), None)


def list_comparisons():
    return _as_list(COMPARISONS_PATH)


def save_comparisons(items):
    save_json(COMPARISONS_PATH, items)


def get_comparison(comparison_id: str):
    return next((item for item in list_comparisons() if item['comparison_id'] == comparison_id), None)


def create_comparison(payload: dict) -> dict:
    """Create a durable comparison record without computing comparison analytics.

    The record lets the UI and future backend agree that comparisons are
    first-class exploration objects. The expensive finding/graph delta work can
    be implemented later behind this stable object shape.
    """
    items = list_comparisons()
    comparison = {
        'comparison_id': payload.get('comparison_id') or f"comparison-{int(time.time() * 1000)}",
        'workspace_id': payload.get('workspace_id') or DEFAULT_WORKSPACE_ID,
        'name': payload.get('name') or 'Untitled comparison',
        'run_ids': payload.get('run_ids', []),
        'comparison_type': payload.get('comparison_type') or 'runs',
        'status': 'placeholder',
        'summary': payload.get('summary') or 'Comparison record created; comparison analytics are not computed in the local adapter yet.',
        'created_at': now_iso(),
        'updated_at': now_iso()
    }
    items.append(comparison)
    save_comparisons(items)
    return comparison


def list_composites():
    return _as_list(COMPOSITES_PATH)


def save_composites(items):
    save_json(COMPOSITES_PATH, items)


def get_composite(composite_id: str):
    return next((item for item in list_composites() if item['composite_id'] == composite_id), None)


def create_composite(payload: dict) -> dict:
    """Create a durable composite record without materializing merged graphs.

    Composite EG/RG/answer graphs will likely require secondary indexes or a
    graph/object store. This placeholder record is the contract where that
    materialized artifact will attach.
    """
    items = list_composites()
    composite = {
        'composite_id': payload.get('composite_id') or f"composite-{int(time.time() * 1000)}",
        'workspace_id': payload.get('workspace_id') or DEFAULT_WORKSPACE_ID,
        'name': payload.get('name') or 'Untitled composite',
        'source_run_ids': payload.get('source_run_ids', []),
        'source_finding_ids': payload.get('source_finding_ids', []),
        'graph_kinds': payload.get('graph_kinds', ['evidence', 'reasoning', 'answer']),
        'status': 'placeholder',
        'summary': payload.get('summary') or 'Composite record created; graph merge/materialization is not computed in the local adapter yet.',
        'created_at': now_iso(),
        'updated_at': now_iso()
    }
    items.append(composite)
    save_composites(items)
    return composite


def workspace_context(workspace_id: str = DEFAULT_WORKSPACE_ID) -> dict | None:
    workspace = get_workspace(workspace_id)
    if not workspace:
        return None

    runs = [run for run in list_runs() if run.get('workspace_id', DEFAULT_WORKSPACE_ID) == workspace_id]
    scopes = [scope for scope in list_scopes() if scope.get('workspace_id', DEFAULT_WORKSPACE_ID) == workspace_id]
    questions = [question for question in list_questions() if question.get('workspace_id', DEFAULT_WORKSPACE_ID) == workspace_id]
    comparisons = [item for item in list_comparisons() if item.get('workspace_id', DEFAULT_WORKSPACE_ID) == workspace_id]
    composites = [item for item in list_composites() if item.get('workspace_id', DEFAULT_WORKSPACE_ID) == workspace_id]

    return {
        'workspace': workspace,
        'questions': questions,
        'scopes': scopes,
        'runs': runs,
        'comparisons': comparisons,
        'composites': composites
    }


def run_exploration_context(run_id: str) -> dict | None:
    """Return the exploration neighborhood for a run.

    The result UI uses this to avoid treating a run as an isolated bundle. It
    includes the current workspace/question/scope plus nearby runs and any
    comparison/composite records that reference the run.
    """
    run = get_run(run_id)
    if not run:
        return None

    workspace_id = run.get('workspace_id') or DEFAULT_WORKSPACE_ID
    question_id = run.get('question_id')
    scope_id = run.get('scope_id')
    question = get_question(question_id) if question_id else None
    if not question and run.get('request', {}).get('question', {}).get('text'):
        question = {
            'question_id': question_id,
            'workspace_id': workspace_id,
            'text': run['request']['question']['text'],
            'source': 'embedded_run_request'
        }
    scope = get_scope(scope_id) if scope_id else None
    if not scope and run.get('request', {}).get('scope'):
        scope = {
            'scope_id': scope_id,
            'workspace_id': workspace_id,
            'source': 'embedded_run_request',
            **run['request']['scope']
        }
    all_runs = [item for item in list_runs() if item.get('workspace_id', DEFAULT_WORKSPACE_ID) == workspace_id]

    related_runs = [
        item for item in all_runs
        if item.get('run_id') != run_id
        and (
            (question_id and item.get('question_id') == question_id)
            or (scope_id and item.get('scope_id') == scope_id)
            or item.get('parent_run_id') == run_id
            or run.get('parent_run_id') == item.get('run_id')
        )
    ]

    comparisons = [
        item for item in list_comparisons()
        if item.get('workspace_id', DEFAULT_WORKSPACE_ID) == workspace_id and run_id in item.get('run_ids', [])
    ]
    composites = [
        item for item in list_composites()
        if item.get('workspace_id', DEFAULT_WORKSPACE_ID) == workspace_id and run_id in item.get('source_run_ids', [])
    ]

    return {
        'workspace': get_workspace(workspace_id),
        'question': question,
        'scope': scope,
        'run': run,
        'related_runs': related_runs,
        'comparisons': comparisons,
        'composites': composites,
        'exploration_affordances': [
            {
                'kind': 'same_question_new_scope',
                'label': 'Run same question on a changed scope',
                'enabled': True
            },
            {
                'kind': 'new_question_same_scope',
                'label': 'Ask a different question on this scope',
                'enabled': bool(scope_id)
            },
            {
                'kind': 'compare_runs',
                'label': 'Compare this run with related runs',
                'enabled': bool(related_runs)
            },
            {
                'kind': 'create_composite_graph',
                'label': 'Create a composite graph from selected runs/findings',
                'enabled': True
            }
        ]
    }
