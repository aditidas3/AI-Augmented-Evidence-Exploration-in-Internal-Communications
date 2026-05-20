"""
main.py — Intent Validator Pipeline
=====================================
Orchestrates: parse → validate → report

No hardcoded questions or intent objects here.
All inputs come from external files or are passed programmatically.

CLI usage:
    python main.py --question "your question" --intent intent.json
    python main.py --intent intent.json        # reads question from Header.question_text
    python main.py --intent intent.json --output my_report.json

Programmatic usage (from another script):
    from main import run_validation

    report = run_validation(
        question="Did JUUL Labs ...",
        intent=my_intent_dict,
        output_path="report.json",   # optional
        verbose=True,                # optional
    )
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"

from question_parser import QuestionParser
from scoring_engine import generate_report


def _safe_console(text) -> str:
    return str(text).encode("ascii", "backslashreplace").decode("ascii")


def _write_json_output(output_path: str | Path, payload: dict) -> None:
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with output_file.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def _normalize_intent_records(raw) -> tuple[list, bool]:
    """
    Normalize supported input shapes into a list of records.

    Supported shapes:
      - single intent dict
      - single wrapper dict with {"question": ..., "response": {...}}
      - list of wrapper dicts like intent_analysis_results.json
      - list of plain intent dicts
    """
    if isinstance(raw, list):
        records = []
        for pos, item in enumerate(raw, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"Record {pos} must be a JSON object")

            if "response" in item:
                if not isinstance(item["response"], dict):
                    raise ValueError(f"Record {pos} has a non-object 'response' field")
                records.append({
                    "position": pos - 1,
                    "record_index": item.get("index", pos),
                    "question": item.get("question", ""),
                    "intent": item["response"],
                    "source_type": "bundle_record",
                    "raw_record": item,
                })
            else:
                records.append({
                    "position": pos - 1,
                    "record_index": item.get("index", pos),
                    "question": item.get("question", ""),
                    "intent": item,
                    "source_type": "intent",
                    "raw_record": item,
                })
        return records, True

    if isinstance(raw, dict):
        if "response" in raw and isinstance(raw.get("response"), dict):
            return [{
                "position": 0,
                "record_index": raw.get("index", 1),
                "question": raw.get("question", ""),
                "intent": raw["response"],
                "source_type": "bundle_record",
                "raw_record": raw,
            }], False

        return [{
            "position": 0,
            "record_index": raw.get("index"),
            "question": raw.get("question", ""),
            "intent": raw,
            "source_type": "intent",
            "raw_record": raw,
        }], False

    raise ValueError("Intent input must be a JSON object or an array of JSON objects")


def load_intent_input(intent_path: str):
    with open(intent_path, encoding="utf-8") as f:
        raw = json.load(f)
    records, is_batch = _normalize_intent_records(raw)
    return raw, records, is_batch


# -----------------------------------------------------------------------
# Core pipeline function — call this from any external script
# -----------------------------------------------------------------------

def run_validation(
    question: str,
    intent: dict,
    output_path: str = None,
    verbose: bool = True,
) -> dict:
    """
    Run the full validation pipeline.

    Args:
        question:    Raw question string (the ground truth source)
        intent:      Intent object dict (the thing being validated)
        output_path: If given, save report JSON here
        verbose:     Print progress to stdout

    Returns:
        Full validation report dict
    """

    if verbose:
        print("=" * 60)
        print("  Intent Validator")
        print("=" * 60)

    # Step 1: Parse question into ground truth
    if verbose:
        print("\n[Step 1] Parsing question into ground truth...")
    parser = QuestionParser()
    gt = parser.parse(question)

    if verbose:
        print(f"  question_type:             {gt.question_type}")
        print(f"  sub_questions:             {len(gt.sub_questions)}")
        print(f"  time_range:                {gt.time_range}")
        print(f"  intentionality_required:   {gt.intentionality_required}")
        print(f"  cross_track_awareness:     {gt.cross_track_awareness_required}")
        print(f"  implicit_constraints:      {len(gt.implicit_constraints)}")
        print(f"  required_slot_types:       {gt.required_slot_types}")
        print(f"  entities extracted ({len(gt.entities)}):")
        for e in gt.entities:
            impl = " [implicit]" if e.implicit else ""
            print(f"    {e.intent_category:<30}  canonical={e.canonical_type:<15}  surface={e.surface!r}{impl}")

    # Step 2: Run validation layers
    if verbose:
        print("\n[Step 2] Running validation layers...")

    from validation_layers import (
        EntityCompletenessValidator,
        ScopeCorrectnessValidator,
        RetrievalQualityValidator,
        SlotCompletenessValidator,
        GraphSpecValidator,
        InternalConsistencyValidator,
        MinimalityAuditor,
    )

    validators = [
        EntityCompletenessValidator(),
        ScopeCorrectnessValidator(),
        RetrievalQualityValidator(),
        SlotCompletenessValidator(),
        GraphSpecValidator(),
        InternalConsistencyValidator(),
    ]

    layer_results = []
    for v in validators:
        result = v.validate(intent, gt)
        layer_results.append(result)
        if verbose:
            hi = sum(1 for i in result.issues if i.priority == "HIGH")
            md = sum(1 for i in result.issues if i.priority == "MEDIUM")
            lo = sum(1 for i in result.issues if i.priority == "LOW")
            print(f"  [{result.layer_name:<26}]  score={result.score:.2f}  "
                  f"HIGH={hi}  MEDIUM={md}  LOW={lo}")

    # Step 3: Collect minimality findings and generate report
    all_minimality = []
    for lr in layer_results:
        all_minimality.extend(lr.minimality_findings)
    all_minimality.extend(MinimalityAuditor().audit(intent, gt))

    if verbose:
        print("\n[Step 3] Generating report...")

    from scoring_engine import ReportGenerator
    reporter = ReportGenerator()
    report = reporter.generate(intent, gt, layer_results, all_minimality)

    if verbose:
        vr = report["validation_report"]
        overall = vr["overall"]
        print(f"\n  Overall score:   {overall['score']}")
        print(f"  Verdict:         {overall['verdict']}")
        print(f"  Minimality:      {overall['minimality_verdict']}")
        print(f"  HIGH:            {overall['issue_counts']['HIGH']}")
        print(f"  MEDIUM:          {overall['issue_counts']['MEDIUM']}")
        print(f"  LOW:             {overall['issue_counts']['LOW']}")

    if output_path:
        _write_json_output(output_path, report)
        if verbose:
            print(f"\n  Report saved -> {_safe_console(output_path)}")

    if verbose:
        print("=" * 60)

    return report


def run_batch_validation(
    records: list,
    question_override: str = None,
    question_file_text: str = None,
    output_path: str = None,
    verbose: bool = True,
) -> dict:
    """
    Run validation over a list of normalized records.
    Returns a batch report wrapper containing one full report per record.
    """

    if verbose:
        print("=" * 60)
        print("  Intent Validator - Batch Mode")
        print("=" * 60)
        print(f"  Records: {len(records)}")

    items = []
    verdict_counts = {"PASS": 0, "PARTIAL_PASS": 0, "FAIL": 0}
    issue_totals = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    total_score = 0.0

    for idx, record in enumerate(records, start=1):
        intent = record["intent"]
        question = resolve_question_for_record(
            question_str=question_override,
            question_file_text=question_file_text,
            record=record,
        )
        if not question:
            record_label = record.get("record_index", idx)
            raise ValueError(
                f"Record {record_label} is missing a question. "
                "Provide --question, --question-file, top-level question, or Header.question_text."
            )

        if verbose:
            intent_id = intent.get("Header", {}).get("intent_id", "unknown")
            print(
                f"\n[Record {idx}/{len(records)}] "
                f"index={_safe_console(record.get('record_index'))} intent_id={_safe_console(intent_id)}"
            )

        report = run_validation(
            question=question,
            intent=intent,
            output_path=None,
            verbose=verbose,
        )

        overall = report["validation_report"]["overall"]
        total_score += overall["score"]
        verdict_counts[overall["verdict"]] = verdict_counts.get(overall["verdict"], 0) + 1
        for priority, count in overall["issue_counts"].items():
            issue_totals[priority] = issue_totals.get(priority, 0) + count

        items.append({
            "record_index": record.get("record_index"),
            "question": question,
            "intent_id": report["validation_report"]["meta"]["intent_id"],
            "report": report,
        })

    batch_report = {
        "batch_validation_report": {
            "validated_at": datetime.now(timezone.utc).isoformat(),
            "record_count": len(items),
            "summary": {
                "average_score": round(total_score / len(items), 3) if items else 0.0,
                "verdict_counts": verdict_counts,
                "issue_counts": issue_totals,
            },
            "reports": items,
        }
    }

    if output_path:
        _write_json_output(output_path, batch_report)
        if verbose:
            print(f"\n  Batch report saved -> {_safe_console(output_path)}")
            print("=" * 60)

    return batch_report


# -----------------------------------------------------------------------
# CLI entry point
# -----------------------------------------------------------------------

def resolve_question_for_record(
    question_str: str,
    question_file_text: str,
    record: dict,
) -> str:
    if question_str:
        return question_str.strip()
    if question_file_text:
        return question_file_text.strip()
    if record.get("question"):
        return record["question"].strip()
    return record.get("intent", {}).get("Header", {}).get("question_text", "").strip()


def _resolve_question(question_str: str, question_file: str, intent: dict) -> str:
    """
    Resolve question from three sources in priority order:
      1. --question  "direct string"
      2. --question-file  path/to/question.txt
      3. intent Header.question_text
    """
    if question_str:
        return question_str.strip()
    if question_file:
        with open(question_file, encoding="utf-8") as f:
            return f.read().strip()
    return intent.get("Header", {}).get("question_text", "")


def main():
    parser = argparse.ArgumentParser(
        description="Intent Object Validator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Question input — three ways (pick one):
  --question      "Did JUUL Labs..."          direct string
  --question-file question.txt                path to a plain text file
  (neither)                                   reads from Header.question_text inside intent JSON

Examples:
  python main.py --intent intent.json
  python main.py --intent intent.json --question "Did JUUL Labs..."
  python main.py --intent intent.json --question-file question.txt
  python main.py --intent intent.json --question-file question.txt --output intent_analyzer/output/report.json --quiet
  python main.py --intent intent_analysis_results.json --output intent_analyzer/output/batch_report.json
        """,
    )
    parser.add_argument("--question",      type=str,
                        help="Raw question string (inline)")
    parser.add_argument("--question-file", type=str, dest="question_file",
                        help="Path to a plain .txt file containing the question")
    parser.add_argument("--intent",        type=str, required=True,
                        help="Path to intent JSON file")
    parser.add_argument(
        "--output",
        type=str,
        default=str(DEFAULT_OUTPUT_DIR / "validation_report.json"),
        help="Output report path (default: intent_analyzer/output/validation_report.json)",
    )
    parser.add_argument("--quiet",         action="store_true",
                        help="Suppress progress output")
    args = parser.parse_args()

    try:
        _raw, records, is_batch = load_intent_input(args.intent)
    except ValueError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    question_file_text = None
    if args.question_file:
        with open(args.question_file, encoding="utf-8") as f:
            question_file_text = f.read().strip()

    if is_batch:
        try:
            run_batch_validation(
                records=records,
                question_override=args.question,
                question_file_text=question_file_text,
                output_path=args.output,
                verbose=not args.quiet,
            )
        except ValueError as exc:
            print(f"Error: {exc}")
            sys.exit(1)
        return

    intent = records[0]["intent"]

    # Resolve question — inline > file > top-level record question > Header
    question = resolve_question_for_record(
        question_str=args.question,
        question_file_text=question_file_text,
        record=records[0],
    )
    if not question:
        print("Error: provide --question, --question-file, top-level question, or set Header.question_text in intent JSON")
        sys.exit(1)

    run_validation(
        question=question,
        intent=intent,
        output_path=args.output,
        verbose=not args.quiet,
    )


if __name__ == "__main__":
    main()
