from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

SERVER_DIR = Path(__file__).resolve().parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from artifacts import artifact_provider
from analyst_reviews import filtered_analyst_reviews, reviews_for_run as analyst_reviews_for_run, save_analyst_review
from conflict_reviews import filtered_conflict_reviews, reviews_for_run, save_conflict_review
from evidence_collections import list_collections, preview_evidence_subset
from exploration import (
    create_comparison,
    create_composite,
    get_comparison,
    get_composite,
    list_comparisons,
    list_composites,
    list_workspaces,
    run_exploration_context,
    workspace_context,
)
from filter_config import resolve_filter_config
from questions import get_question, list_questions
from runs import create_run, delete_run, get_run, list_runs
from scopes import create_scope, delete_scope, get_scope, list_scopes
from storage import FRONTEND_DIR, RUNS_DIR, sanitize_for_strict_json


class StrictJSONResponse(JSONResponse):
    def render(self, content: Any) -> bytes:
        return super().render(sanitize_for_strict_json(content))


app = FastAPI(
    title='Evidence Explorer API',
    default_response_class=StrictJSONResponse,
)


@app.middleware('http')
async def no_store_cache(request: Request, call_next):
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    return response


def json_response(payload: Any, status_code: int = 200) -> StrictJSONResponse:
    return StrictJSONResponse(payload, status_code=status_code)


def not_found_response() -> StrictJSONResponse:
    return json_response({'error': 'not_found'}, 404)


def query_params_as_lists(request: Request) -> dict[str, list[str]]:
    return {
        key: request.query_params.getlist(key)
        for key in request.query_params.keys()
    }


@app.get('/')
def root():
    return RedirectResponse('/app/', status_code=302)


@app.get('/api', include_in_schema=False)
@app.get('/api/', include_in_schema=False)
def api_docs_redirect():
    return RedirectResponse('/docs', status_code=302)


@app.get('/api/collections')
def api_collections():
    return list_collections()


@app.get('/api/scopes')
def api_scopes():
    return list_scopes()


@app.post('/api/scopes', status_code=201)
def api_create_scope(payload: dict[str, Any] = Body(default_factory=dict)):
    return create_scope(payload)


@app.get('/api/scopes/{scope_id}')
def api_scope(scope_id: str):
    scope = get_scope(scope_id)
    return scope if scope else not_found_response()


@app.delete('/api/scopes/{scope_id}')
def api_delete_scope(scope_id: str):
    deleted = delete_scope(scope_id)
    return deleted if deleted else not_found_response()


@app.get('/api/runs')
def api_runs():
    return list_runs()


@app.post('/api/runs', status_code=201)
def api_create_run(payload: dict[str, Any] = Body(default_factory=dict)):
    return create_run(payload)


@app.get('/api/runs/{run_id}')
def api_run(run_id: str):
    run = get_run(run_id)
    return run if run else not_found_response()


@app.delete('/api/runs/{run_id}')
def api_delete_run(run_id: str):
    deleted = delete_run(run_id)
    return deleted if deleted else not_found_response()


@app.get('/api/runs/{run_id}/results')
def api_run_results(run_id: str):
    result_index = artifact_provider.result_index(run_id)
    return result_index if result_index else not_found_response()


@app.get('/api/runs/{run_id}/bundles')
def api_run_bundles(run_id: str):
    artifacts = artifact_provider.artifacts(run_id)
    return artifacts if artifacts else not_found_response()


@app.get('/api/runs/{run_id}/conflict-reviews')
def api_run_conflict_reviews(run_id: str):
    return reviews_for_run(run_id)


@app.post('/api/runs/{run_id}/conflict-reviews/{edge_id}')
def api_save_conflict_review(
    run_id: str,
    edge_id: str,
    payload: dict[str, Any] = Body(default_factory=dict),
):
    return save_conflict_review(run_id, edge_id, payload)


@app.get('/api/runs/{run_id}/analyst-reviews')
def api_run_analyst_reviews(run_id: str):
    return analyst_reviews_for_run(run_id)


@app.post('/api/runs/{run_id}/analyst-reviews/{object_type}/{object_id}')
def api_save_analyst_review(
    run_id: str,
    object_type: str,
    object_id: str,
    payload: dict[str, Any] = Body(default_factory=dict),
):
    return save_analyst_review(run_id, object_type, object_id, payload)


@app.get('/api/runs/{run_id}/exploration')
def api_run_exploration(run_id: str):
    context = run_exploration_context(run_id)
    return context if context else not_found_response()


@app.get('/api/workspaces')
def api_workspaces():
    return list_workspaces()


@app.get('/api/workspaces/{workspace_id}')
def api_workspace(workspace_id: str):
    context = workspace_context(workspace_id)
    return context if context else not_found_response()


@app.get('/api/questions')
def api_questions():
    return list_questions()


@app.get('/api/questions/{question_id}')
def api_question(question_id: str):
    question = get_question(question_id)
    return question if question else not_found_response()


@app.get('/api/comparisons')
def api_comparisons():
    return list_comparisons()


@app.post('/api/comparisons', status_code=201)
def api_create_comparison(payload: dict[str, Any] = Body(default_factory=dict)):
    return create_comparison(payload)


@app.get('/api/comparisons/{comparison_id}')
def api_comparison(comparison_id: str):
    comparison = get_comparison(comparison_id)
    return comparison if comparison else not_found_response()


@app.get('/api/composites')
def api_composites():
    return list_composites()


@app.post('/api/composites', status_code=201)
def api_create_composite(payload: dict[str, Any] = Body(default_factory=dict)):
    return create_composite(payload)


@app.get('/api/composites/{composite_id}')
def api_composite(composite_id: str):
    composite = get_composite(composite_id)
    return composite if composite else not_found_response()


@app.get('/api/conflict-reviews')
def api_conflict_reviews(request: Request):
    return filtered_conflict_reviews(query_params_as_lists(request))


@app.get('/api/analyst-reviews')
def api_analyst_reviews(request: Request):
    return filtered_analyst_reviews(query_params_as_lists(request))


@app.post('/api/filter-config/resolve')
def api_resolve_filter_config(payload: dict[str, Any] = Body(default_factory=dict)):
    return resolve_filter_config(payload['application_id'], payload.get('selected_collection_ids', []))


@app.post('/api/evidence/preview')
def api_evidence_preview(payload: dict[str, Any] = Body(default_factory=dict)):
    return preview_evidence_subset(payload)


@app.api_route(
    '/api/{path:path}',
    methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'],
    include_in_schema=False,
)
def api_not_found(path: str):
    return not_found_response()


@app.get('/app')
def app_redirect():
    return RedirectResponse('/app/', status_code=302)


app.mount('/app', StaticFiles(directory=FRONTEND_DIR, html=True), name='frontend')
app.mount('/runs', StaticFiles(directory=RUNS_DIR, check_dir=False), name='runs')


if __name__ == '__main__':
    import uvicorn

    port = int(os.environ.get('EVIDENCE_EXPLORER_PORT', '8002'))
    print(f'Serving Evidence Explorer on http://127.0.0.1:{port}')
    uvicorn.run(app, host='127.0.0.1', port=port)
