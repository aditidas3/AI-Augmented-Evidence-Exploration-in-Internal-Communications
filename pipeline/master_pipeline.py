"""
Question-first master runner for the evidence exploration pipeline.

Flow:
    question
      -> intent generation
      -> validation and correction
      -> ALIGN
      -> TRACE
      -> CONFLICT
      -> CONSTRUCT
      -> EXPLAIN

The helpers in this file call the existing operator APIs directly and write the
same bundle artifacts the UI/server contract expects.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = REPO_ROOT / "results" / "full_pipeline"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@dataclass(frozen=True)
class IntentPipelineArtifacts:
    raw_records: list[dict]
    raw_intent: dict
    validation_report: dict
    corrected_intent: dict
    correction_log: dict
    revalidated_report: dict


@dataclass(frozen=True)
class FullPipelineArtifacts:
    output_dir: Path
    corrected_intent: dict
    align_bundle: dict
    trace_bundle: dict
    conflict_bundle: dict
    construct_bundle: dict
    explain_bundle: dict
    result_index: dict


def run_intent_pipeline(
    question: str,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    *,
    model: str | None = None,
    temperature: float = 0.0,
) -> IntentPipelineArtifacts:
    """Generate, validate, correct, and re-validate intent for one question."""
    question = question.strip()
    if not question:
        raise ValueError("question must not be empty")

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    _load_dotenv()
    _normalize_openai_env()

    from pipeline.intent_analyzer.generator.intent_analysis import run_intent_analysis

    raw_records = run_intent_analysis(
        questions=[question],
        results_path=out / "intent_analysis_results.json",
        model=model or os.getenv("OPENAI_MODEL"),
        temperature=temperature,
        max_questions=1,
    )
    if not raw_records:
        raise RuntimeError("intent generator returned no records")

    raw_intent = _intent_from_generator_record(raw_records[0])
    corrected_intent, validation_report, correction_log, revalidated_report = (
        validate_and_correct_intent(question, raw_intent)
    )

    _write_json(out / "validation_report.json", validation_report)
    _write_json(out / "corrected_intent.json", _wrap_intent_record(question, corrected_intent))
    _write_json(out / "correction_log.json", correction_log)
    _write_json(out / "revalidated_report.json", revalidated_report)

    return IntentPipelineArtifacts(
        raw_records=raw_records,
        raw_intent=raw_intent,
        validation_report=validation_report,
        corrected_intent=corrected_intent,
        correction_log=correction_log,
        revalidated_report=revalidated_report,
    )


def validate_and_correct_intent(question: str, intent: dict) -> tuple[dict, dict, dict, dict]:
    """Run the validator and corrector modules around an intent dict."""
    validator_dir = REPO_ROOT / "pipeline" / "intent_analyzer" / "validator"
    with _prepend_sys_path(validator_dir):
        validator_main = _load_module(
            "master_pipeline_intent_validator_main",
            validator_dir / "main.py",
        )
        validator_corrector = _load_module(
            "master_pipeline_intent_corrector",
            validator_dir / "corrector.py",
        )

        validation_report = validator_main.run_validation(
            question=question,
            intent=intent,
            verbose=False,
        )
        corrector = validator_corrector.IntentCorrector()
        corrected_intent = corrector.correct(intent, validation_report)
        revalidated_report = validator_main.run_validation(
            question=question,
            intent=corrected_intent,
            verbose=False,
        )
        correction_log = validator_corrector.build_correction_summary(
            corrector.log,
            intent,
            corrected_intent,
            validation_report,
            revalidated_report,
        )

    return corrected_intent, validation_report, correction_log, revalidated_report


def run_align_pipeline(
    corrected_intent: dict,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    *,
    align_config: Any | None = None,
) -> dict:
    """Run ALIGN and write align_bundle.json."""
    from pipeline.align import generate_align_bundle

    config = align_config or build_default_align_config()
    align_bundle = generate_align_bundle(
        corrected_intent,
        config=config,
        source_uri=str(getattr(config.neo4j, "uri", "")),
    )
    _write_json(Path(output_dir) / "align_bundle.json", align_bundle)
    return align_bundle


def run_trace_pipeline(
    align_bundle: dict,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    *,
    trace_config: Any | None = None,
) -> dict:
    """Run TRACE and write trace_bundle.json."""
    from pipeline.trace import TraceConfig, generate_trace_bundle

    _load_dotenv()
    config = trace_config or TraceConfig()
    if "EVIDENCE_EXPLORER_MAP_TRANSFORM_BACKEND" not in os.environ:
        config.map_transform_classifier_backend = "heuristic"
    else:
        config.map_transform_classifier_backend = os.environ["EVIDENCE_EXPLORER_MAP_TRANSFORM_BACKEND"]
    if os.getenv("EVIDENCE_EXPLORER_DISABLE_MAP_TRANSFORM") == "1":
        config.map_transform_enabled = False
    trace_bundle = generate_trace_bundle(align_bundle, cfg=config, validate=True)
    _write_json(Path(output_dir) / "trace_bundle.json", trace_bundle)
    return trace_bundle


def run_conflict_pipeline(
    trace_bundle: dict,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
) -> dict:
    """Run CONFLICT and write conflict_bundle.json."""
    from pipeline.conflict.save_conflict_output import build_conflict_bundle

    conflict_bundle = build_conflict_bundle(trace_bundle)
    _write_json(Path(output_dir) / "conflict_bundle.json", conflict_bundle)
    return conflict_bundle


def run_construct_pipeline(
    conflict_bundle: dict,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
) -> dict:
    """Run CONSTRUCT and write construct_bundle.json."""
    from pipeline.construct.construct import Construct

    result = Construct(conflict_bundle).execute()
    construct_bundle = package_construct_result(conflict_bundle, result)
    _write_json(Path(output_dir) / "construct_bundle.json", construct_bundle)
    return construct_bundle


def run_explain_pipeline(
    construct_bundle: dict,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
) -> dict:
    """Run EXPLAIN and write explain_bundle.json plus explain_output.txt."""
    from pipeline.explain.explain import Explain

    out = Path(output_dir)
    result = Explain(construct_bundle).execute()
    explain_bundle = package_explain_result(
        construct_bundle,
        result,
        plain_text_path=out / "explain_output.txt",
    )
    _write_json(out / "explain_bundle.json", explain_bundle)
    (out / "explain_output.txt").write_text(_plain_text_answer(result), encoding="utf-8")
    return explain_bundle


def run_full_pipeline(
    question: str,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    *,
    run_id: str | None = None,
    model: str | None = None,
    temperature: float = 0.0,
    align_config: Any | None = None,
    trace_config: Any | None = None,
) -> FullPipelineArtifacts:
    """Run the complete question-first pipeline and write all artifacts."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    intent_artifacts = run_intent_pipeline(
        question,
        out,
        model=model,
        temperature=temperature,
    )
    align_bundle = run_align_pipeline(
        intent_artifacts.corrected_intent,
        out,
        align_config=align_config,
    )
    trace_bundle = run_trace_pipeline(align_bundle, out, trace_config=trace_config)
    conflict_bundle = run_conflict_pipeline(trace_bundle, out)
    construct_bundle = run_construct_pipeline(conflict_bundle, out)
    explain_bundle = run_explain_pipeline(construct_bundle, out)

    final_run_id = run_id or _intent_id(intent_artifacts.corrected_intent) or "pipeline-run"
    result_index = build_result_index(final_run_id, explain_bundle, construct_bundle)
    _write_json(out / "result_index.json", result_index)

    return FullPipelineArtifacts(
        output_dir=out,
        corrected_intent=intent_artifacts.corrected_intent,
        align_bundle=align_bundle,
        trace_bundle=trace_bundle,
        conflict_bundle=conflict_bundle,
        construct_bundle=construct_bundle,
        explain_bundle=explain_bundle,
        result_index=result_index,
    )


def build_default_align_config() -> Any:
    """Build AlignConfig from environment variables and existing defaults."""
    from pipeline.operators.configs import AlignConfig, Neo4jConfig

    _load_dotenv()
    config = AlignConfig()
    config.neo4j = _neo4j_config(Neo4jConfig)
    _configure_solr_from_env(config)
    _configure_qdrant_from_env(config)
    return config


def _neo4j_config(config_type: Any) -> Any:
    base = config_type()
    try:
        from pipeline_test.align.neo4j.gen_align_bundle import SOURCE

        base = SOURCE
    except Exception:
        pass

    return config_type(
        uri=os.getenv("EVIDENCE_EXPLORER_NEO4J_URI", os.getenv("NEO4J_URI", base.uri)),
        user=os.getenv("EVIDENCE_EXPLORER_NEO4J_USER", os.getenv("NEO4J_USER", base.user)),
        password=os.getenv(
            "EVIDENCE_EXPLORER_NEO4J_PASSWORD",
            os.getenv("NEO4J_PASSWORD", base.password),
        ),
        database=os.getenv(
            "EVIDENCE_EXPLORER_NEO4J_DATABASE",
            os.getenv("NEO4J_DATABASE", base.database),
        ),
        max_connection_pool_size=base.max_connection_pool_size,
    )


def package_construct_result(conflict_bundle: dict, result: Any) -> dict:
    """Package ConstructResult into the chain-first ConstructBundle shape."""
    return {
        "schema_version": "construct-bundle.chain.v1",
        "construct_bundle_id": result.ans_bundle.get("bundle_id", result.synthesis_uid),
        "trace_bundle_id": conflict_bundle.get("trace_bundle_id", ""),
        "conflict_bundle_id": conflict_bundle.get("conflict_bundle_id", ""),
        "status": result.stats.get("status", "ANSWER_CONSTRUCTED"),
        "selected_chain_id": result.selected_chain_id,
        "trace_bundle": conflict_bundle.get("trace_bundle", {}),
        "conflict_bundle": conflict_bundle,
        "ans_bundle": result.ans_bundle,
        "g_ans": result.g_ans,
        "citation_map": result.citation_map,
        "limitations": result.limitations,
        "construct_result": {
            "status": result.stats.get("status", "ANSWER_CONSTRUCTED"),
            "selected_chain_id": result.selected_chain_id,
            "synthesis_uid": result.synthesis_uid,
            "synthesis_confidence": result.synthesis_conf,
            "synthesis_type": result.synthesis_type,
            "stats": result.stats,
            "diagnostics": result.diagnostics,
        },
        "rg_delta": {
            "nodes": result.new_nodes,
            "edges": result.new_edges,
        },
        "provenance_manifest_delta": {
            "operator": "CONSTRUCT",
            "input_conflict_bundle_id": conflict_bundle.get("conflict_bundle_id", ""),
            "output_construct_bundle_id": result.ans_bundle.get("bundle_id", result.synthesis_uid),
        },
    }


def package_explain_result(
    construct_bundle: dict,
    result: Any,
    *,
    plain_text_path: Path,
) -> dict:
    """Package ExplainResult into the chain-first ExplainBundle shape."""
    return {
        "schema_version": "explain-bundle.chain.v1",
        "explain_bundle_id": result.explain_bundle.get("bundle_id", result.explain_node_uid),
        "construct_bundle_id": construct_bundle.get("construct_bundle_id", ""),
        "trace_bundle_id": construct_bundle.get("trace_bundle_id", ""),
        "conflict_bundle_id": construct_bundle.get("conflict_bundle_id", ""),
        "status": result.stats.get(
            "status",
            construct_bundle.get("status", "ANSWER_CONSTRUCTED"),
        ),
        "answer_text": result.answer_text,
        "confidence_score": result.confidence_score,
        "confidence_label": result.confidence_label,
        "citations": result.citations,
        "evidence_chain": result.evidence_chain,
        "warnings": result.warnings,
        "stats": result.stats,
        "explain_bundle": result.explain_bundle,
        "construct_bundle": construct_bundle,
        "trace_bundle": construct_bundle.get("trace_bundle", {}),
        "conflict_bundle": construct_bundle.get("conflict_bundle", {}),
        "plain_text_path": str(plain_text_path),
        "provenance_manifest_delta": {
            "operator": "EXPLAIN",
            "input_construct_bundle_id": construct_bundle.get("construct_bundle_id", ""),
            "output_explain_bundle_id": result.explain_bundle.get("bundle_id", result.explain_node_uid),
        },
    }


def build_result_index(run_id: str, explain_bundle: dict, construct_bundle: dict) -> dict:
    """Build a local result index for the artifact files in one output dir."""
    return {
        "run_id": run_id,
        "corrected_intent_ref": "corrected_intent.json",
        "align_bundle_ref": "align_bundle.json",
        "trace_bundle_ref": "trace_bundle.json",
        "conflict_bundle_ref": "conflict_bundle.json",
        "construct_bundle_ref": "construct_bundle.json",
        "explain_bundle_ref": "explain_bundle.json",
        "summary": {
            "status": construct_bundle.get("status", "ANSWER_CONSTRUCTED"),
            "answer_text": explain_bundle.get("answer_text", ""),
            "confidence_score": explain_bundle.get("confidence_score", 0.0),
            "confidence_label": explain_bundle.get("confidence_label", ""),
            "selected_chain_id": construct_bundle.get("selected_chain_id", ""),
        },
    }


def load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(REPO_ROOT / ".env", override=False)
    load_dotenv(REPO_ROOT / "db" / ".env", override=False)


def _configure_solr_from_env(config: Any) -> None:
    solr_url = _env_first("EVIDENCE_EXPLORER_SOLR_URL", "SOLR_URL")
    solr_port = _env_first("EVIDENCE_EXPLORER_SOLR_PORT", "SOLR_PORT")
    solr_collection = _env_first(
        "EVIDENCE_EXPLORER_SOLR_COLLECTION",
        "SOLR_COLLECTION",
        "EVIDENCE_EXPLORER_SOLR_CORE_NAME",
        "SOLR_CORE_NAME",
    )

    if solr_collection:
        config.solr.collection = solr_collection
    if solr_url or solr_port:
        config.solr.url = _normalize_solr_base_url(
            solr_url or config.solr.url,
            solr_port,
            config.solr.collection,
        )


def _configure_qdrant_from_env(config: Any) -> None:
    qdrant_host = _env_first("EVIDENCE_EXPLORER_QDRANT_HOST", "QDRANT_HOST")
    qdrant_port = _env_first("EVIDENCE_EXPLORER_QDRANT_PORT", "QDRANT_PORT")
    qdrant_grpc_port = _env_first("EVIDENCE_EXPLORER_QDRANT_GRPC_PORT", "QDRANT_GRPC_PORT")
    qdrant_collection = _env_first(
        "EVIDENCE_EXPLORER_QDRANT_COLLECTION",
        "QDRANT_COLLECTION",
    )
    qdrant_use_grpc = _env_first("EVIDENCE_EXPLORER_QDRANT_USE_GRPC", "QDRANT_USE_GRPC")

    if qdrant_host:
        config.qdrant.host = qdrant_host
    if qdrant_port:
        config.qdrant.port = int(qdrant_port)
    if qdrant_grpc_port:
        config.qdrant.grpc_port = int(qdrant_grpc_port)
    if qdrant_collection:
        config.qdrant.collection_name = qdrant_collection
    if qdrant_use_grpc:
        config.qdrant.use_grpc = qdrant_use_grpc.strip().lower() in {"1", "true", "yes", "on"}


def _env_first(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value.strip()
    return None


def _normalize_solr_base_url(raw_url: str, port: str | None, collection: str) -> str:
    if "://" not in raw_url:
        raw_url = f"http://{raw_url}"

    parsed = urlsplit(raw_url)
    netloc = parsed.netloc
    host_port = netloc.rsplit("@", 1)[-1]
    if port and ":" not in host_port:
        netloc = f"{netloc}:{port}"

    segments = [segment for segment in parsed.path.split("/") if segment]
    if not segments:
        segments = ["solr"]
    elif len(segments) >= 2 and segments[-2] == "solr" and segments[-1] == collection:
        segments = segments[:-1]
    elif segments[-1] != "solr":
        segments.append("solr")

    return urlunsplit((parsed.scheme, netloc, "/" + "/".join(segments), "", ""))


def _normalize_openai_env() -> None:
    if not os.getenv("OPENAI_API_KEY") and os.getenv("OPENAI_KEY"):
        os.environ["OPENAI_API_KEY"] = os.environ["OPENAI_KEY"]


def _intent_from_generator_record(record: dict) -> dict:
    response = record.get("response")
    if not isinstance(response, dict):
        raise RuntimeError(f"intent generator did not return a JSON object: {response!r}")
    return response


def _wrap_intent_record(question: str, intent: dict) -> list[dict]:
    return [{"index": 1, "question": question, "response": intent}]


def _intent_id(intent: dict) -> str:
    return str((intent.get("Header", {}) or {}).get("intent_id", "") or "")


def _plain_text_answer(result: Any) -> str:
    lines = [
        "INVESTIGATOR ANSWER",
        "=" * 60,
        "",
        result.answer_text,
        "",
        "CITATIONS",
        "-" * 60,
    ]
    lines.extend(f"  {citation}" for citation in result.citations)
    lines.extend(["", "EVIDENCE CHAIN (AUDIT TRAIL)", "-" * 60])
    lines.extend(f"  {step}" for step in result.evidence_chain)
    return "\n".join(lines) + "\n"


@contextmanager
def _prepend_sys_path(path: Path):
    text = str(path)
    inserted = False
    if text not in sys.path:
        sys.path.insert(0, text)
        inserted = True
    try:
        yield
    finally:
        if inserted:
            try:
                sys.path.remove(text)
            except ValueError:
                pass


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader:
        raise ImportError(f"Cannot import module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the full evidence pipeline from a raw question.")
    parser.add_argument("question", nargs="?", help="Raw natural-language research question.")
    parser.add_argument("--question-file", help="Path to a text file containing the question.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory for output artifacts.")
    parser.add_argument("--run-id", default=None, help="Optional run id for result_index.json.")
    parser.add_argument("--model", default=None, help="Intent generator model override.")
    parser.add_argument("--temperature", type=float, default=0.0, help="Intent generator temperature.")
    args = parser.parse_args(argv)

    question = args.question or ""
    if args.question_file:
        question = Path(args.question_file).read_text(encoding="utf-8").strip()
    if not question.strip():
        parser.error("provide a question argument or --question-file")

    artifacts = run_full_pipeline(
        question=question,
        output_dir=args.output_dir,
        run_id=args.run_id,
        model=args.model,
        temperature=args.temperature,
    )

    print(json.dumps({
        "ok": True,
        "output_dir": str(artifacts.output_dir),
        "run_id": artifacts.result_index.get("run_id"),
        "confidence_label": artifacts.explain_bundle.get("confidence_label"),
        "answer_chars": len(str(artifacts.explain_bundle.get("answer_text", ""))),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
