from __future__ import annotations

import json
import sys
import threading
import traceback
from pathlib import Path
from typing import Any

from storage import ROOT, RUNS_DIR, now_iso, save_json

REPO_ROOT = ROOT.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_RUNNING: set[str] = set()
_LOCK = threading.Lock()


def start_pipeline_run(run_id: str) -> None:
    with _LOCK:
        if run_id in _RUNNING:
            return
        _RUNNING.add(run_id)

    thread = threading.Thread(
        target=_run_pipeline_job,
        args=(run_id,),
        name=f"evidence-pipeline-{run_id}",
        daemon=True,
    )
    thread.start()


def _run_pipeline_job(run_id: str) -> None:
    try:
        _execute_pipeline(run_id)
    except Exception as exc:  # noqa: BLE001 - preserve pipeline failure details for the UI.
        _update_run(run_id, {
            "status": "failed",
            "failed_at": now_iso(),
            "error": str(exc),
            "traceback": traceback.format_exc(),
        })
    finally:
        with _LOCK:
            _RUNNING.discard(run_id)


def _execute_pipeline(run_id: str) -> None:
    from runs import get_run

    run = get_run(run_id)
    if not run:
        raise RuntimeError(f"Run not found: {run_id}")

    request = run.get("request", {}) or {}
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    _update_run(run_id, {"status": "aligning", "stage": "intent"})
    intent = _intent_from_request(request, run_dir)
    _write_json(run_dir / "corrected_intent.json", _wrap_corrected_intent(intent, request))

    _update_run(run_id, {"status": "aligning", "stage": "align"})
    align_bundle = _pipeline().run_align_pipeline(intent, run_dir)

    _update_run(run_id, {"status": "tracing", "stage": "trace"})
    trace_bundle = _pipeline().run_trace_pipeline(align_bundle, run_dir)

    _update_run(run_id, {"status": "conflict_checking", "stage": "conflict"})
    conflict_bundle = _pipeline().run_conflict_pipeline(trace_bundle, run_dir)

    _update_run(run_id, {"status": "constructing", "stage": "construct"})
    construct_bundle = _pipeline().run_construct_pipeline(conflict_bundle, run_dir)

    _update_run(run_id, {"status": "explaining", "stage": "explain"})
    explain_bundle = _pipeline().run_explain_pipeline(construct_bundle, run_dir)

    result_index = _build_result_index(run_id, explain_bundle, construct_bundle)
    _write_json(run_dir / "result_index.json", result_index)

    _update_run(run_id, {
        "status": "completed",
        "stage": "completed",
        "completed_at": now_iso(),
        "result_index_ref": _run_ref(run_id, "result_index.json"),
        "artifact_refs": {
            "corrected_intent": _run_ref(run_id, "corrected_intent.json"),
            "align": _run_ref(run_id, "align_bundle.json"),
            "trace": _run_ref(run_id, "trace_bundle.json"),
            "conflict": _run_ref(run_id, "conflict_bundle.json"),
            "construct": _run_ref(run_id, "construct_bundle.json"),
            "explain": _run_ref(run_id, "explain_bundle.json"),
        },
    })


def _intent_from_request(request: dict, run_dir: Path) -> dict:
    direct = (
        request.get("corrected_intent")
        or request.get("intent")
        or request.get("response")
    )
    if direct:
        return _unwrap_intent(direct)

    intent_path = (
        request.get("intent_path")
        or (request.get("options", {}) or {}).get("intent_path")
    )
    if intent_path:
        path = _resolve_request_path(intent_path)
        return _unwrap_intent(json.loads(path.read_text(encoding="utf-8-sig")))

    question = str((request.get("question", {}) or {}).get("text", "")).strip()
    if not question:
        raise ValueError("Run request must include question.text or an intent payload")
    options = request.get("options", {}) or {}
    artifacts = _pipeline().run_intent_pipeline(
        question,
        run_dir,
        model=options.get("model"),
        temperature=float(options.get("temperature", 0.0) or 0.0),
    )
    return artifacts.corrected_intent


def _build_result_index(run_id: str, explain_bundle: dict, construct_bundle: dict) -> dict:
    result_index = _pipeline().build_result_index(run_id, explain_bundle, construct_bundle)
    for key, filename in list(result_index.items()):
        if key.endswith("_ref") and isinstance(filename, str):
            result_index[key] = _run_ref(run_id, filename)
    return result_index


def _wrap_corrected_intent(intent: dict, request: dict) -> Any:
    question = (request.get("question", {}) or {}).get("text", "")
    if not question:
        return intent
    return [{"index": 1, "question": question, "response": intent}]


def _unwrap_intent(raw: Any) -> dict:
    if isinstance(raw, list):
        if not raw:
            raise ValueError("Intent list is empty")
        return _unwrap_intent(raw[0])
    if isinstance(raw, dict) and isinstance(raw.get("response"), dict):
        return raw["response"]
    if isinstance(raw, dict):
        return raw
    raise ValueError("Intent payload must be a JSON object or a non-empty list")


def _resolve_request_path(path: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = REPO_ROOT / path
    resolved = candidate.resolve()
    try:
        resolved.relative_to(REPO_ROOT.resolve())
    except ValueError as exc:
        raise ValueError(f"Intent path must be inside the repository: {path}") from exc
    if not resolved.exists():
        raise FileNotFoundError(f"Intent path not found: {path}")
    return resolved


def _run_ref(run_id: str, filename: str) -> str:
    return f"/runs/{run_id}/{filename}"


def _write_json(path: Path, payload: Any) -> None:
    save_json(path, payload)


def _update_run(run_id: str, updates: dict) -> None:
    from runs import update_run

    update_run(run_id, updates)


def _pipeline() -> Any:
    from pipeline import master_pipeline

    return master_pipeline
