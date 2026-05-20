"""
Step 3 — Scoring Engine & Report Generator
Aggregates layer results into a structured validation report JSON.
Priority: HIGH | MEDIUM | LOW
Includes minimality analysis as a dedicated section.
"""

import json

from datetime import datetime, timezone
from dataclasses import asdict
from validation_layers import (
    LayerResult, Issue, MinimalityFinding,
    EntityCompletenessValidator,
    ScopeCorrectnessValidator,
    RetrievalQualityValidator,
    SlotCompletenessValidator,
    GraphSpecValidator,
    InternalConsistencyValidator,
    MinimalityAuditor,
)
from question_parser import QuestionGroundTruth


# -----------------------------------------------------------------------
# Scoring weights per layer
# -----------------------------------------------------------------------
LAYER_WEIGHTS = {
    "entity_completeness":   0.20,
    "scope_correctness":     0.12,
    "retrieval_quality":     0.18,
    "slot_completeness":     0.15,
    "graph_spec_correctness":0.25,
    "internal_consistency":  0.10,
}

# Verdict thresholds
VERDICT_THRESHOLDS = {
    "PASS":         0.85,
    "PARTIAL_PASS": 0.60,
    "FAIL":         0.00,
}

# Priority ordering for fix grouping
PRIORITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}

# Priority → fix urgency label
PRIORITY_LABELS = {
    "HIGH":   "P0 — Fix immediately (breaks retrieval or leaves sub-question unaddressed)",
    "MEDIUM": "P1 — Fix before production (degrades result quality)",
    "LOW":    "P2 — Fix when convenient (hygiene and minimality)",
}


# -----------------------------------------------------------------------
# Scoring engine
# -----------------------------------------------------------------------

class ScoringEngine:

    def compute_overall(self, layer_results: list) -> float:
        total = 0.0
        for lr in layer_results:
            weight = LAYER_WEIGHTS.get(lr.layer_id, 0.0)
            total += lr.score * weight
        return round(total, 3)

    def verdict(self, score: float) -> str:
        if score >= VERDICT_THRESHOLDS["PASS"]:
            return "PASS"
        elif score >= VERDICT_THRESHOLDS["PARTIAL_PASS"]:
            return "PARTIAL_PASS"
        return "FAIL"

    def count_by_priority(self, all_issues: list) -> dict:
        return {
            "HIGH":   sum(1 for i in all_issues if i.priority == "HIGH"),
            "MEDIUM": sum(1 for i in all_issues if i.priority == "MEDIUM"),
            "LOW":    sum(1 for i in all_issues if i.priority == "LOW"),
        }

    def priority_fix_groups(self, all_issues: list) -> list:
        """Group issues by priority into actionable fix groups."""
        groups = {"HIGH": [], "MEDIUM": [], "LOW": []}
        for issue in all_issues:
            groups[issue.priority].append(issue.issue_id)

        result = []
        for priority in ["HIGH", "MEDIUM", "LOW"]:
            ids = groups[priority]
            if ids:
                result.append({
                    "priority": priority,
                    "urgency": PRIORITY_LABELS[priority],
                    "issue_ids": ids,
                    "count": len(ids),
                })
        return result


# -----------------------------------------------------------------------
# Report assembler
# -----------------------------------------------------------------------

class ReportGenerator:

    def __init__(self):
        self.scoring = ScoringEngine()

    def generate(
        self,
        intent: dict,
        gt: QuestionGroundTruth,
        layer_results: list,
        minimality_findings: list,
    ) -> dict:

        all_issues = []
        for lr in layer_results:
            all_issues.extend(lr.issues)

        overall_score = self.scoring.compute_overall(layer_results)
        verdict = self.scoring.verdict(overall_score)
        priority_counts = self.scoring.count_by_priority(all_issues)

        # ---- Minimality verdict
        bloat_by_type = {}
        for mf in minimality_findings:
            bloat_by_type.setdefault(mf.bloat_type, []).append(mf.field)

        minimality_verdict = "MINIMAL"
        if len(minimality_findings) > 6:
            minimality_verdict = "BLOATED"
        elif len(minimality_findings) > 2:
            minimality_verdict = "PARTIALLY_MINIMAL"

        report = {
            "validation_report": {

                # ---- Meta
                "meta": {
                    "intent_id":       intent.get("Header", {}).get("intent_id", "unknown"),
                    "question_id":     intent.get("Header", {}).get("question_id", "unknown"),
                    "schema_version":  intent.get("Header", {}).get("schema_version", "unknown"),
                    "validated_at":    datetime.now(timezone.utc).isoformat(),
                    "question_type":   gt.question_type,
                    "sub_question_count": len(gt.sub_questions),
                },

                # ---- Overall verdict
                "overall": {
                    "score":   overall_score,
                    "verdict": verdict,
                    "issue_counts": priority_counts,
                    "total_issues": len(all_issues),
                    "minimality_verdict": minimality_verdict,
                    "minimality_finding_count": len(minimality_findings),
                },

                # ---- Layer scores
                "layer_scores": {
                    lr.layer_id: {
                        "name":           lr.layer_name,
                        "score":          lr.score,
                        "weight":         LAYER_WEIGHTS.get(lr.layer_id, 0.0),
                        "weighted_score": round(lr.score * LAYER_WEIGHTS.get(lr.layer_id, 0.0), 3),
                        "issue_count":    len(lr.issues),
                        "passed_checks":  len(lr.passed_checks),
                        "minimality_findings": len(lr.minimality_findings),
                    }
                    for lr in layer_results
                },

                # ---- All issues (flat, sortable)
                "issues": [
                    {
                        "issue_id": issue.issue_id,
                        "layer":    issue.layer,
                        "priority": issue.priority,
                        "field":    issue.field,
                        "message":  issue.message,
                        "fix":      issue.fix,
                        "evidence": issue.evidence,
                    }
                    for issue in sorted(all_issues, key=lambda i: (PRIORITY_ORDER[i.priority], i.issue_id))
                ],

                # ---- Minimality analysis
                "minimality_analysis": {
                    "verdict":    minimality_verdict,
                    "summary":    f"{len(minimality_findings)} minimality findings across {len(set(mf.bloat_type for mf in minimality_findings))} bloat categories",
                    "bloat_by_type": {
                        bt: fields for bt, fields in bloat_by_type.items()
                    },
                    "findings": [
                        {
                            "field":          mf.field,
                            "bloat_type":     mf.bloat_type,
                            "finding":        mf.finding,
                            "recommendation": mf.recommendation,
                        }
                        for mf in minimality_findings
                    ],
                },

                # ---- Priority fix groups
                "priority_fixes": self.scoring.priority_fix_groups(all_issues),

                # ---- Ground truth used for validation
                "ground_truth_used": {
                    "question_type":            gt.question_type,
                    "sub_questions":            gt.sub_questions,
                    "time_range":               gt.time_range,
                    "intentionality_required":  gt.intentionality_required,
                    "cross_track_awareness_required": gt.cross_track_awareness_required,
                    "implicit_constraints":     gt.implicit_constraints,
                    "required_slot_types":      gt.required_slot_types,
                    "required_artifact_types":  gt.required_artifact_types,
                    "temporal_constraint_kinds": [c.kind for c in gt.temporal_constraints],
                    "entity_count":             len(gt.entities),
                    "entities": [
                        {
                            "surface":         e.surface,
                            "canonical_type":  e.canonical_type,
                            "intent_category": e.intent_category,
                            "implicit":        e.implicit,
                            "qualifiers":      e.qualifiers,
                        }
                        for e in gt.entities
                    ],
                },
            }
        }

        return report


# -----------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------

def generate_report(intent: dict, gt: QuestionGroundTruth) -> dict:
    """Run all 6 validation layers + minimality audit and return report dict."""

    # Run layers
    layer_results = [
        EntityCompletenessValidator().validate(intent, gt),
        ScopeCorrectnessValidator().validate(intent, gt),
        RetrievalQualityValidator().validate(intent, gt),
        SlotCompletenessValidator().validate(intent, gt),
        GraphSpecValidator().validate(intent, gt),
        InternalConsistencyValidator().validate(intent, gt),
    ]

    # Collect per-layer minimality findings
    all_minimality = []
    for lr in layer_results:
        all_minimality.extend(lr.minimality_findings)

    # Cross-cutting minimality audit
    all_minimality.extend(MinimalityAuditor().audit(intent, gt))

    # Generate report
    reporter = ReportGenerator()
    return reporter.generate(intent, gt, layer_results, all_minimality)


if __name__ == "__main__":
    # Quick smoke test with dummy data
    from question_parser import QuestionParser

    dummy_intent = {
        "Header": {
            "intent_id": "TEST-001",
            "question_id": "Q-TEST-001",
            "schema_version": "v1",
            "created_at": "1970-01-01T00:00:00Z",
        },
        "EntityHints": [],
        "ScopeSpec": {"mode": "PREFER", "artifact_types": [], "time_filter": {}},
        "RetrievalSpec": {"query_text": "", "query_expansions": []},
        "SlotSpec": {"slots": [], "graph_spec": {}},
        "Diagnostics": {"rule_hits": [], "notes": ""},
    }

    q = "Did JUUL Labs do something between 2017 and 2019?"
    gt = QuestionParser().parse(q)
    report = generate_report(dummy_intent, gt)
    print(json.dumps(report, indent=2))
