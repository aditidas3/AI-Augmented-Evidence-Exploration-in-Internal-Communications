"""
Intent Corrector
================
Reads a validation report + original intent object, then automatically
applies fixes for:
  - All HIGH priority issues
  - All issues in layers scoring below 0.65

Each fix is a targeted, surgical mutation of the intent dict.
A correction log is emitted alongside the corrected intent.

Usage:
    python corrector.py --intent path/to/intent.json \
                        --report path/to/validation_report.json \
                        --output corrected_intent.json \
                        --log    correction_log.json

    python corrector.py --pipeline   # uses bundled JUUL data end-to-end
"""

import argparse
import copy
import json
import sys
import re
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"


def _safe_console(text) -> str:
    return str(text).encode("ascii", "backslashreplace").decode("ascii")


def _write_json_output(output_path: str | Path, payload) -> None:
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with output_file.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

# -----------------------------------------------------------------------
# Threshold — layers below this score get ALL their issues fixed
# -----------------------------------------------------------------------
SCORE_THRESHOLD = 0.65

# -----------------------------------------------------------------------
# Canonical node-type mapping for corrector
# (intent category  →  canonical node type used in corrected hint)
# -----------------------------------------------------------------------
# Fallback map: ENTITY_* category → canonical node type.
# Used only when intent_category is absent from the report (older reports).
# Primary path: read intent_category directly from ground_truth_used.entities.
# Must stay in sync with NODE_TYPE_TO_CATEGORY in question_parser.py.
CATEGORY_TO_NODE = {
    "ENTITY_ORGANIZATION":         "organization",
    "ENTITY_STRATEGY":             "claim",        # also covers legalFramework rules
    "ENTITY_DOCUMENT":             "assessment",
    "ENTITY_CONCEPT":              "topics",        # also covers abbreviations, location, metric
    "ENTITY_FRAMING":              "healthMention", # primary framing node type
    "ENTITY_ACTION_ITEM":          "procedure",     # also covers finance
    "ENTITY_PERSON":               "person",
    "ENTITY_EVENT":                "event",
    "ENTITY_TEMPORAL_UNCERTAINTY": "date",
    "ENTITY_TIME_RANGE":           "date",
    "ENTITY_RISK_FINDING":         "risk",
    "ENTITY_APPROVAL":             "decision",
    "ENTITY_PRODUCT":              "product",       # also covers drug
    "ENTITY_VISIBILITY_LEVEL":     "state",
    "ENTITY_INTENT":               "claim",
    "ENTITY_AWARENESS":            "topics",
    "ENTITY_BEHAVIORAL_PATTERN":   "claim",
    "ENTITY_QUALIFIER":            "requirement",
}

# -----------------------------------------------------------------------
# Correction log entry
# -----------------------------------------------------------------------

class CorrectionEntry:
    def __init__(self, issue_id: str, priority: str, layer: str,
                 field: str, action: str, before, after, reason: str):
        self.issue_id = issue_id
        self.priority = priority
        self.layer = layer
        self.field = field
        self.action = action      # ADDED | MODIFIED | REMOVED | REPLACED
        self.before = before
        self.after = after
        self.reason = reason

    def to_dict(self):
        return {
            "issue_id": self.issue_id,
            "priority": self.priority,
            "layer":    self.layer,
            "field":    self.field,
            "action":   self.action,
            "before":   self.before,
            "after":    self.after,
            "reason":   self.reason,
        }


# -----------------------------------------------------------------------
# Individual fix functions
# One function per fix class, each mutates intent in-place
# and returns a CorrectionEntry (or None if already correct)
# -----------------------------------------------------------------------

class IntentCorrector:

    def __init__(self, score_threshold: float = SCORE_THRESHOLD):
        self.threshold = score_threshold
        self.log: list[CorrectionEntry] = []
        self._entity_id_counter = 0

    # -------------------------------------------------------------------
    # Top-level: decide which issues to fix
    # -------------------------------------------------------------------

    def correct(self, intent: dict, report: dict) -> dict:
        corrected = copy.deepcopy(intent)
        vr = report["validation_report"]

        # Determine which issues to fix
        layer_scores = {lid: v["score"] for lid, v in vr["layer_scores"].items()}
        below_threshold_layers = {
            lid for lid, score in layer_scores.items() if score < self.threshold
        }

        issues_to_fix = []
        for issue in vr["issues"]:
            if issue["priority"] == "HIGH":
                issues_to_fix.append(issue)
            elif issue["layer"] in below_threshold_layers:
                issues_to_fix.append(issue)

        # Deduplicate by issue_id
        seen = set()
        unique_issues = []
        for issue in issues_to_fix:
            if issue["issue_id"] not in seen:
                seen.add(issue["issue_id"])
                unique_issues.append(issue)

        print(f"\n  Issues selected for correction: {len(unique_issues)}")
        print(f"  (HIGH priority: {sum(1 for i in unique_issues if i['priority'] == 'HIGH')} | "
              f"below-threshold layers: {below_threshold_layers})")

        # Apply fixes in dependency order
        # (scope before slots, entities before graph, etc.)
        ordered = self._order_issues(unique_issues)

        for issue in ordered:
            self._apply_fix(corrected, issue, report)

        return corrected

    # -------------------------------------------------------------------
    # Order issues so dependencies resolve correctly
    # e.g. add entity hints before graph vars that reference them
    # -------------------------------------------------------------------

    def _order_issues(self, issues: list) -> list:
        order_map = {
            "entity_completeness":   0,
            "scope_correctness":     1,
            "retrieval_quality":     2,
            "slot_completeness":     3,
            "graph_spec_correctness":4,
            "internal_consistency":  5,
        }
        return sorted(issues, key=lambda i: (order_map.get(i["layer"], 9), i["issue_id"]))

    # -------------------------------------------------------------------
    # Dispatch to the right fix function per issue_id prefix
    # -------------------------------------------------------------------

    def _apply_fix(self, intent: dict, issue: dict, report: dict):
        iid = issue["issue_id"]
        layer = issue["layer"]
        field = issue["field"]
        msg = issue["message"]

        print(f"    Fixing [{iid}] ({issue['priority']}) {_safe_console(field[:60])}")

        if iid.startswith("EC-"):
            self._fix_entity_completeness(intent, issue, report)

        elif iid.startswith("SC-"):
            self._fix_scope(intent, issue)

        elif iid.startswith("RQ-"):
            self._fix_retrieval(intent, issue, report)

        elif iid.startswith("SL-"):
            self._fix_slot(intent, issue)

        elif iid.startswith("GS-"):
            self._fix_graph(intent, issue, report)

        elif iid.startswith("IC-"):
            self._fix_consistency(intent, issue)

    # -------------------------------------------------------------------
    # EC fixes — Entity Completeness
    # -------------------------------------------------------------------

    def _fix_entity_completeness(self, intent: dict, issue: dict, report: dict):
        msg = issue["message"]
        field = issue["field"]

        # Missing entity hint
        if "absent from EntityHints" in msg:
            # Extract entity info from ground truth used
            gt_entities = report["validation_report"]["ground_truth_used"]["entities"]
            # find the entity mentioned in msg
            match = re.search(r"Entity '([^']+)' \(type: ([^)]+)\)", msg)
            if not match:
                return
            surface, node_type = match.group(1), match.group(2)

            # Check not already present
            hints = intent.get("EntityHints", [])
            existing_surfaces = [h.get("surface", "").lower() for h in hints]
            if surface.lower() in existing_surfaces:
                return

            # Find qualifiers from ground truth
            gt_ent = next(
                (e for e in gt_entities if e["surface"].lower() == surface.lower()), None
            )
            qualifiers = gt_ent["qualifiers"] if gt_ent else {}

            # Generate new entity_id
            self._entity_id_counter += 1
            new_id = f"SE-C{self._entity_id_counter:02d}"

            # Use intent_category from ground truth (authoritative — set in question_parser.py)
            # Fall back to CATEGORY_TO_NODE reverse map only if not present (older reports)
            category = gt_ent.get("intent_category", "") if gt_ent else ""
            if not category:
                reverse_map = {v: k for k, v in CATEGORY_TO_NODE.items()}
                category = reverse_map.get(node_type, f"ENTITY_{node_type.upper()}")

            new_hint = {
                "entity_id":  new_id,
                "surface":    surface,
                "category":   category,
                "normalized": qualifiers.get("normalized", ""),
                "qualifiers": {k: v for k, v in qualifiers.items() if k != "normalized"},
            }

            before = len(hints)
            intent["EntityHints"].append(new_hint)

            self.log.append(CorrectionEntry(
                issue_id=issue["issue_id"],
                priority=issue["priority"],
                layer=issue["layer"],
                field="EntityHints",
                action="ADDED",
                before=f"{before} hints",
                after=new_hint,
                reason=f"Entity '{surface}' (type: {node_type}) found in question ground truth but missing from hints",
            ))

        # Wrong category on date entity
        elif "appears to be a date range" in msg or "ENTITY_EVENT" in msg:
            surface_match = re.search(r"'([^']+)' appears", msg) or re.search(r"\[\'([^']+)\'\]", field)
            if not surface_match:
                return
            surface = surface_match.group(1)
            hints = intent.get("EntityHints", [])
            for hint in hints:
                if hint.get("surface", "").lower() == surface.lower():
                    before = hint.get("category")
                    hint["category"] = "ENTITY_TIME_RANGE"
                    self.log.append(CorrectionEntry(
                        issue_id=issue["issue_id"],
                        priority=issue["priority"],
                        layer=issue["layer"],
                        field=f"EntityHints['{surface}'].category",
                        action="MODIFIED",
                        before=before,
                        after="ENTITY_TIME_RANGE",
                        reason="Date range typed as ENTITY_EVENT — corrected to ENTITY_TIME_RANGE",
                    ))
                    break

    # -------------------------------------------------------------------
    # SC fixes — Scope Correctness
    # -------------------------------------------------------------------

    def _fix_scope(self, intent: dict, issue: dict):
        msg = issue["message"]
        field = issue["field"]

        # Missing artifact type
        if "ARTIFACT_THREAD" in msg and "artifact_types" in field:
            scope = intent.setdefault("ScopeSpec", {})
            art_types = scope.setdefault("artifact_types", [])
            if "ARTIFACT_THREAD" not in art_types:
                before = list(art_types)
                art_types.append("ARTIFACT_THREAD")
                self.log.append(CorrectionEntry(
                    issue_id=issue["issue_id"],
                    priority=issue["priority"],
                    layer=issue["layer"],
                    field="ScopeSpec.artifact_types",
                    action="ADDED",
                    before=before,
                    after=art_types,
                    reason="ARTIFACT_THREAD used in slots but absent from ScopeSpec",
                ))

        # Time filter mismatch
        elif "time_filter" in field and "does not match" in msg:
            # Extract expected range from message
            match = re.search(r"\[(\d{4})–(\d{4})\]$", msg)
            if match:
                start_y, end_y = match.group(1), match.group(2)
                tf = intent.setdefault("ScopeSpec", {}).setdefault("time_filter", {})
                before = dict(tf)
                tf["start"] = f"{start_y}-01-01T00:00:00Z"
                tf["end"] = f"{end_y}-12-31T23:59:59Z"
                self.log.append(CorrectionEntry(
                    issue_id=issue["issue_id"],
                    priority=issue["priority"],
                    layer=issue["layer"],
                    field="ScopeSpec.time_filter",
                    action="MODIFIED",
                    before=before,
                    after=dict(tf),
                    reason=f"Time filter corrected to match question date range {start_y}–{end_y}",
                ))

        # mode=REQUIRE on investigative question
        elif "mode='REQUIRE'" in msg:
            scope = intent.setdefault("ScopeSpec", {})
            before = scope.get("mode")
            scope["mode"] = "PREFER"
            self.log.append(CorrectionEntry(
                issue_id=issue["issue_id"],
                priority=issue["priority"],
                layer=issue["layer"],
                field="ScopeSpec.mode",
                action="MODIFIED",
                before=before,
                after="PREFER",
                reason="REQUIRE too strict for investigative question — changed to PREFER",
            ))

    # -------------------------------------------------------------------
    # RQ fixes — Retrieval Quality
    # -------------------------------------------------------------------

    def _fix_retrieval(self, intent: dict, issue: dict, report: dict):
        msg = issue["message"]
        field = issue["field"]
        spec = intent.setdefault("RetrievalSpec", {})
        expansions = spec.setdefault("query_expansions", [])
        before_exp = list(expansions)

        # Missing entity coverage in query
        if "has no coverage in query_text or query_expansions" in msg:
            match = re.search(r"Entity '([^']+)'", msg)
            if not match:
                return
            surface = match.group(1)
            # Don't add raw years — they clutter expansions
            if re.fullmatch(r"\d{4}", surface.strip()):
                return
            # Don't add very short fragments
            if len(surface) < 5:
                return
            if surface.lower() not in [e.lower() for e in expansions]:
                expansions.append(surface)
                self.log.append(CorrectionEntry(
                    issue_id=issue["issue_id"],
                    priority=issue["priority"],
                    layer=issue["layer"],
                    field="RetrievalSpec.query_expansions",
                    action="ADDED",
                    before=before_exp,
                    after=surface,
                    reason=f"Entity '{surface}' has no retrieval coverage",
                ))

        # Cross-track awareness missing
        elif "Cross-track awareness" in msg:
            awareness_terms = [
                "silo", "need to know", "compartmentalized",
                "parallel workstream", "cross-functional awareness",
                "knowledge of each other"
            ]
            added = []
            for term in awareness_terms:
                if term.lower() not in [e.lower() for e in expansions]:
                    expansions.append(term)
                    added.append(term)
            if added:
                self.log.append(CorrectionEntry(
                    issue_id=issue["issue_id"],
                    priority=issue["priority"],
                    layer=issue["layer"],
                    field="RetrievalSpec.query_expansions",
                    action="ADDED",
                    before=before_exp,
                    after=added,
                    reason="Cross-track awareness (sub-question 2) had zero retrieval coverage",
                ))

        # Intentionality missing
        elif "Intentionality signal" in msg:
            intent_terms = [
                "strategic plan", "coordinated approach",
                "dual track", "deliberate", "orchestrated"
            ]
            added = []
            for term in intent_terms:
                if term.lower() not in [e.lower() for e in expansions]:
                    expansions.append(term)
                    added.append(term)
            if added:
                self.log.append(CorrectionEntry(
                    issue_id=issue["issue_id"],
                    priority=issue["priority"],
                    layer=issue["layer"],
                    field="RetrievalSpec.query_expansions",
                    action="ADDED",
                    before=before_exp,
                    after=added,
                    reason="Intentionality ('systematically') not reflected in retrieval",
                ))

        # Generic expansions
        elif "Generic expansions" in msg:
            generic = {
                "regulatory affairs", "compliance", "internal comms",
                "sales data", "documentation", "records"
            }
            replacements = [
                "PMTA", "premarket tobacco application",
                "Youth Forward", "age verification",
                "point of sale data", "purchase tracking"
            ]
            before_exp2 = list(expansions)
            expansions[:] = [e for e in expansions if e.lower() not in generic]
            for r in replacements:
                if r.lower() not in [e.lower() for e in expansions]:
                    expansions.append(r)
            self.log.append(CorrectionEntry(
                issue_id=issue["issue_id"],
                priority=issue["priority"],
                layer=issue["layer"],
                field="RetrievalSpec.query_expansions",
                action="REPLACED",
                before=before_exp2,
                after=list(expansions),
                reason="Generic expansions replaced with domain-specific terms",
            ))

        # Third-party missing
        elif "third-party" in msg.lower() and "absent from retrieval" in msg:
            third_party_terms = [
                "third party consultant", "external advisor", "outside counsel"
            ]
            added = []
            for term in third_party_terms:
                if term.lower() not in [e.lower() for e in expansions]:
                    expansions.append(term)
                    added.append(term)
            if added:
                self.log.append(CorrectionEntry(
                    issue_id=issue["issue_id"],
                    priority=issue["priority"],
                    layer=issue["layer"],
                    field="RetrievalSpec.query_expansions",
                    action="ADDED",
                    before=before_exp,
                    after=added,
                    reason="third-party qualifier absent from retrieval — external vendor docs unreachable",
                ))

    # -------------------------------------------------------------------
    # SL fixes — Slot Completeness
    # -------------------------------------------------------------------

    def _fix_slot(self, intent: dict, issue: dict):
        msg = issue["message"]
        slot_spec = intent.setdefault("SlotSpec", {})
        slots = slot_spec.setdefault("slots", [])

        # Check not already added
        existing_types = {s.get("slot_type") for s in slots}

        if "AWARENESS" in msg and "AWARENESS" not in existing_types:
            new_slot = {
                "slot_id":   "S-AWARENESS-001",
                "slot_type": "AWARENESS",
                "description": (
                    "Identify evidence that individuals on separate operational tracks "
                    "(regulatory proposals track, assessment track, surveillance track) "
                    "had knowledge of each other's activities — look for shared briefings, "
                    "cross-referenced emails, meeting minutes, or forwarded reports."
                ),
                "allowed_artifact_types": [
                    "ARTIFACT_EMAIL",
                    "ARTIFACT_THREAD",
                    "ARTIFACT_DOCUMENT",
                ],
                "target_schema_id": "schema:CROSS_TRACK_KNOWLEDGE",
            }
            before = len(slots)
            slots.append(new_slot)
            self.log.append(CorrectionEntry(
                issue_id=issue["issue_id"],
                priority=issue["priority"],
                layer=issue["layer"],
                field="SlotSpec.slots",
                action="ADDED",
                before=f"{before} slots",
                after=new_slot,
                reason="AWARENESS slot required for sub-question: 'did individuals have knowledge of each other's activities?'",
            ))

        elif "Required slot type" in msg:
            match = re.search(r"slot type '([^']+)'", msg)
            if not match:
                return
            slot_type = match.group(1)
            if slot_type not in existing_types:
                new_slot = {
                    "slot_id":   f"S-{slot_type}-AUTO",
                    "slot_type": slot_type,
                    "description": f"Auto-generated slot for {slot_type} — review and refine description.",
                    "allowed_artifact_types": [
                        "ARTIFACT_DOCUMENT", "ARTIFACT_EMAIL", "ARTIFACT_PDF"
                    ],
                    "target_schema_id": f"schema:{slot_type}",
                }
                slots.append(new_slot)
                self.log.append(CorrectionEntry(
                    issue_id=issue["issue_id"],
                    priority=issue["priority"],
                    layer=issue["layer"],
                    field="SlotSpec.slots",
                    action="ADDED",
                    before=f"{len(slots)-1} slots",
                    after=new_slot,
                    reason=f"Required slot type '{slot_type}' was missing",
                ))

    # -------------------------------------------------------------------
    # GS fixes — Graph Spec Correctness
    # -------------------------------------------------------------------

    def _fix_graph(self, intent: dict, issue: dict, report: dict):
        msg = issue["message"]
        field = issue["field"]
        graph = intent.get("SlotSpec", {}).get("graph_spec", {})
        if not graph:
            return

        vars_list = graph.setdefault("vars", [])
        edges = graph.setdefault("edges", [])
        temporal = graph.setdefault("temporal_constraints", [])
        var_names = {v.get("var") for v in vars_list}

        # ---- GS: Self-referential edge (P_EXEC → P_EXEC)
        if "Self-referential edge" in msg:
            # Find the self-referential edges
            self_ref = [e for e in edges if e.get("src") == e.get("dst")]
            for bad_edge in self_ref:
                bad_var = bad_edge.get("src")
                # Remove self-referential edge
                before_edges = [dict(e) for e in edges]
                edges[:] = [e for e in edges if not (e.get("src") == e.get("dst") == bad_var)]

                # Add two distinct person vars if not already present
                track_a = f"{bad_var}_TRACK_A"
                track_b = f"{bad_var}_TRACK_B"

                # Find original var definition to clone
                orig_var = next((v for v in vars_list if v.get("var") == bad_var), {})

                for track_var in [track_a, track_b]:
                    if track_var not in var_names:
                        new_var = {
                            "var":  track_var,
                            "type": orig_var.get("type", "ENTITY_PERSON"),
                            "role": f"executive_staff_{track_var.split('_')[-1].lower()}",
                            "hint": f"{orig_var.get('hint', 'executive')} ({track_var.split('_')[-1]})",
                            "hard": False,
                        }
                        vars_list.append(new_var)
                        var_names.add(track_var)

                # Add AWARE_OF edges between the two tracks
                for src, dst in [(track_a, track_b), (track_b, track_a)]:
                    edges.append({
                        "src":   src,
                        "rel":   "AWARE_OF",
                        "dst":   dst,
                        "hard":  False,
                        "notes": f"Cross-track awareness: {src} has knowledge of {dst}'s activities",
                    })

                self.log.append(CorrectionEntry(
                    issue_id=issue["issue_id"],
                    priority=issue["priority"],
                    layer=issue["layer"],
                    field=f"graph_spec.edges + vars",
                    action="REPLACED",
                    before={"removed_self_ref_edge": bad_edge},
                    after={
                        "added_vars": [track_a, track_b],
                        "added_edges": [
                            f"{track_a} → AWARE_OF → {track_b}",
                            f"{track_b} → AWARE_OF → {track_a}",
                        ],
                    },
                    reason="Self-referential edge replaced with two distinct person vars and bidirectional AWARE_OF edges",
                ))

        # ---- GS: Missing CONCURRENT temporal constraint
        elif "CONCURRENT" in msg and "temporal_constraints" in field:
            existing_kinds = {t.get("kind") for t in temporal}
            if "CONCURRENT" not in existing_kinds:
                # Identify operational track vars from graph
                track_vars = []
                for v in vars_list:
                    role = v.get("role", "")
                    if any(kw in role for kw in ["proposal", "assessment", "surveillance",
                                                  "operational", "distribution"]):
                        track_vars.append(v.get("var"))
                if not track_vars:
                    track_vars = ["D_PROPOSAL", "D_ASSESSMENT", "C_SURVEILLANCE", "A_DIST"]

                new_constraint = {
                    "kind":  "CONCURRENT",
                    "vars":  track_vars,
                    "notes": "All operational tracks must be active within the same time window — driven by 'simultaneously' and 'parallel tracks' in question",
                }
                before = list(temporal)
                temporal.append(new_constraint)
                self.log.append(CorrectionEntry(
                    issue_id=issue["issue_id"],
                    priority=issue["priority"],
                    layer=issue["layer"],
                    field="graph_spec.temporal_constraints",
                    action="ADDED",
                    before=before,
                    after=new_constraint,
                    reason="'simultaneously' and 'parallel tracks' require CONCURRENT constraint — was missing",
                ))

        # ---- GS: Cross-track awareness not modeled
        elif "Cross-track awareness" in msg and "AWARE_OF" in issue["fix"]:
            # Only add if not already fixed by the self-ref fix above
            existing_aware = [e for e in edges
                              if e.get("rel") == "AWARE_OF" and e.get("src") != e.get("dst")]
            if not existing_aware:
                track_a = "P_EXEC_TRACK_A"
                track_b = "P_EXEC_TRACK_B"
                for track_var in [track_a, track_b]:
                    if track_var not in var_names:
                        vars_list.append({
                            "var":  track_var,
                            "type": "ENTITY_PERSON",
                            "role": f"executive_staff_{track_var.split('_')[-1].lower()}",
                            "hint": f"individuals on {track_var.split('_')[-1]} track",
                            "hard": False,
                        })
                        var_names.add(track_var)

                for src, dst in [(track_a, track_b), (track_b, track_a)]:
                    edges.append({
                        "src":   src,
                        "rel":   "AWARE_OF",
                        "dst":   dst,
                        "hard":  False,
                        "notes": "Cross-track knowledge awareness",
                    })

                self.log.append(CorrectionEntry(
                    issue_id=issue["issue_id"],
                    priority=issue["priority"],
                    layer=issue["layer"],
                    field="graph_spec.vars + edges",
                    action="ADDED",
                    before="No cross-track awareness modeled",
                    after=f"Added {track_a} ↔ AWARE_OF ↔ {track_b}",
                    reason="Second sub-question (cross-track awareness) was entirely unmodeled in graph",
                ))

        # ---- GS: Temporal constraints using raw datetimes
        elif "raw datetime" in msg or ("ORDER" in msg and "datetime" in msg):
            before_tc = [dict(t) for t in temporal]
            # Remove ORDER constraints that have raw datetime strings in before/after/start/end
            cleaned = []
            removed = []
            for tc in temporal:
                has_raw = any(
                    re.match(r"\d{4}-\d{2}-\d{2}", str(tc.get(f, "")))
                    for f in ["before", "after", "start", "end"]
                )
                if has_raw and tc.get("kind") == "ORDER":
                    removed.append(tc)
                else:
                    cleaned.append(tc)
            temporal[:] = cleaned
            if removed:
                self.log.append(CorrectionEntry(
                    issue_id=issue["issue_id"],
                    priority=issue["priority"],
                    layer=issue["layer"],
                    field="graph_spec.temporal_constraints",
                    action="REMOVED",
                    before=removed,
                    after="Removed — time range is enforced by ScopeSpec.time_filter",
                    reason="ORDER constraints used raw datetime strings instead of var references — removed; ScopeSpec.time_filter handles the range",
                ))

        # ---- GS: Attribute-as-edge (PUBLIC_FACING, INTERNAL_ONLY)
        elif "attribute" in msg.lower() and ("PUBLIC_FACING" in msg or "INTERNAL_ONLY" in msg):
            attribute_rels = {"PUBLIC_FACING", "INTERNAL_ONLY"}
            before_edges = [dict(e) for e in edges]
            removed_edges = []
            property_additions = {}

            for edge in list(edges):
                rel = edge.get("rel", "")
                if rel in attribute_rels:
                    src_var = edge.get("src")
                    prop = "public" if rel == "PUBLIC_FACING" else "internal"
                    property_additions[src_var] = {"visibility": prop}
                    removed_edges.append(dict(edge))

            # Remove those edges
            edges[:] = [e for e in edges if e.get("rel") not in attribute_rels]

            # Add visibility property to the source vars
            for v in vars_list:
                if v.get("var") in property_additions:
                    v["properties"] = v.get("properties", {})
                    v["properties"].update(property_additions[v["var"]])

            if removed_edges:
                self.log.append(CorrectionEntry(
                    issue_id=issue["issue_id"],
                    priority=issue["priority"],
                    layer=issue["layer"],
                    field="graph_spec.edges + vars",
                    action="REPLACED",
                    before={"removed_edges": removed_edges},
                    after={"moved_to_node_properties": property_additions},
                    reason="PUBLIC_FACING and INTERNAL_ONLY are node attributes, not graph edges — moved to var properties",
                ))

        # ---- GS: Weak relation label (REPRESENTS → CONSTRUCTED)
        elif "REPRESENTS" in msg and "weak" in msg.lower():
            for edge in edges:
                if edge.get("rel") == "REPRESENTS":
                    src_hint = next(
                        (v.get("hint", "") for v in vars_list if v.get("var") == edge.get("src")), ""
                    )
                    dst_hint = next(
                        (v.get("hint", "") for v in vars_list if v.get("var") == edge.get("dst")), ""
                    )
                    # Only rename if it's JUUL → strategy
                    if "JUUL" in src_hint.upper() or "strategy" in dst_hint.lower():
                        before_rel = edge["rel"]
                        edge["rel"] = "CONSTRUCTED"
                        self.log.append(CorrectionEntry(
                            issue_id=issue["issue_id"],
                            priority=issue["priority"],
                            layer=issue["layer"],
                            field=f"graph_spec.edges[{edge.get('src')}→{edge.get('dst')}].rel",
                            action="MODIFIED",
                            before=before_rel,
                            after="CONSTRUCTED",
                            reason="REPRESENTS is semantically weak for 'systematically construct'",
                        ))
                        break

        # ---- GS: hard=true on investigated claim
        elif "hard=true" in msg and "investigative" in msg:
            match = re.search(r"var '([^']+)'", msg)
            if match:
                var_name = match.group(1)
                for v in vars_list:
                    if v.get("var") == var_name and v.get("hard"):
                        before_hard = v["hard"]
                        v["hard"] = False
                        self.log.append(CorrectionEntry(
                            issue_id=issue["issue_id"],
                            priority=issue["priority"],
                            layer=issue["layer"],
                            field=f"graph_spec.vars[{var_name}].hard",
                            action="MODIFIED",
                            before=before_hard,
                            after=False,
                            reason="Var represents investigated claim — hard=true would exclude exculpatory documents",
                        ))
                        break

        # ---- GS: Reversed edge direction (DERIVED_FROM → INFORMED)
        elif "reversed" in msg.lower() or "Direction" in msg:
            for edge in edges:
                if edge.get("rel") == "DERIVED_FROM":
                    # Check if src is surveillance and dst is distribution
                    src_hint = next(
                        (v.get("hint","") for v in vars_list if v.get("var") == edge.get("src")), ""
                    ).lower()
                    dst_hint = next(
                        (v.get("hint","") for v in vars_list if v.get("var") == edge.get("dst")), ""
                    ).lower()
                    if "surveillance" in src_hint or "distribution" in dst_hint:
                        before_rel = edge["rel"]
                        edge["rel"] = "INFORMED"
                        self.log.append(CorrectionEntry(
                            issue_id=issue["issue_id"],
                            priority=issue["priority"],
                            layer=issue["layer"],
                            field=f"graph_spec.edges[{edge.get('src')}→{edge.get('dst')}].rel",
                            action="MODIFIED",
                            before=before_rel,
                            after="INFORMED",
                            reason="Surveillance INFORMED distribution decisions — direction was reversed",
                        ))
                        break

    # -------------------------------------------------------------------
    # IC fixes — Internal Consistency
    # -------------------------------------------------------------------

    def _fix_consistency(self, intent: dict, issue: dict):
        msg = issue["message"]
        field = issue["field"]

        # Epoch timestamp
        if "1970-01-01" in msg or "epoch" in msg.lower():
            header = intent.setdefault("Header", {})
            before = header.get("created_at")
            header["created_at"] = datetime.now(timezone.utc).isoformat()
            self.log.append(CorrectionEntry(
                issue_id=issue["issue_id"],
                priority=issue["priority"],
                layer=issue["layer"],
                field="Header.created_at",
                action="MODIFIED",
                before=before,
                after=header["created_at"],
                reason="Epoch placeholder replaced with actual timestamp",
            ))

        # Slot artifact type not in ScopeSpec
        elif "ScopeSpec.artifact_types" in msg and "not declared" in msg:
            art_match = re.search(r"allows '([^']+)'", msg)
            if art_match:
                art = art_match.group(1)
                scope = intent.setdefault("ScopeSpec", {})
                art_types = scope.setdefault("artifact_types", [])
                if art not in art_types:
                    before = list(art_types)
                    art_types.append(art)
                    self.log.append(CorrectionEntry(
                        issue_id=issue["issue_id"],
                        priority=issue["priority"],
                        layer=issue["layer"],
                        field="ScopeSpec.artifact_types",
                        action="ADDED",
                        before=before,
                        after=art,
                        reason=f"'{art}' used in slot but missing from ScopeSpec.artifact_types",
                    ))

        # Type mismatch between EntityHint and graph var
        elif "type mismatch" in msg.lower() or "ENTITY_EVENT" in msg:
            # Find surface from field
            surface_match = re.search(r"surface='([^']+)'", issue.get("evidence", ""))
            if surface_match:
                surface = surface_match.group(1)
                for hint in intent.get("EntityHints", []):
                    if hint.get("surface", "").lower() == surface.lower():
                        if hint.get("category") == "ENTITY_EVENT":
                            before = hint["category"]
                            hint["category"] = "ENTITY_TIME_RANGE"
                            self.log.append(CorrectionEntry(
                                issue_id=issue["issue_id"],
                                priority=issue["priority"],
                                layer=issue["layer"],
                                field=f"EntityHints['{surface}'].category",
                                action="MODIFIED",
                                before=before,
                                after="ENTITY_TIME_RANGE",
                                reason="Type mismatch: ENTITY_EVENT corrected to ENTITY_TIME_RANGE to align with graph var",
                            ))
                            break

        # Orphaned graph var (no EntityHint)
        elif "orphaned var" in msg.lower() or "no matching EntityHint" in msg:
            var_match = re.search(r"Var '([^']+)'", msg)
            hint_match = re.search(r"hint='([^']+)'", msg)
            if var_match and hint_match:
                var_name = var_match.group(1)
                hint_val = hint_match.group(1)
                hints = intent.get("EntityHints", [])
                existing = [h.get("surface", "").lower() for h in hints]
                if hint_val.lower() not in existing:
                    self._entity_id_counter += 1
                    new_hint = {
                        "entity_id":  f"SE-IC{self._entity_id_counter:02d}",
                        "surface":    hint_val,
                        "category":   "ENTITY_CONCEPT",
                        "normalized": "",
                        "qualifiers": {"auto_added": True, "for_var": var_name},
                    }
                    hints.append(new_hint)
                    self.log.append(CorrectionEntry(
                        issue_id=issue["issue_id"],
                        priority=issue["priority"],
                        layer=issue["layer"],
                        field="EntityHints",
                        action="ADDED",
                        before=f"{len(hints)-1} hints",
                        after=new_hint,
                        reason=f"Graph var '{var_name}' had no EntityHint — added stub hint for '{hint_val}'",
                    ))


# -----------------------------------------------------------------------
# Report diff — what changed
# -----------------------------------------------------------------------

def build_correction_summary(log: list, original: dict,
                              corrected: dict, report: dict,
                              corrected_report: dict = None) -> dict:
    vr = report["validation_report"]

    original_score   = vr["overall"]["score"]
    original_verdict = vr["overall"]["verdict"]

    # New score — from re-validation report if available
    if corrected_report:
        cvr          = corrected_report["validation_report"]
        new_score    = cvr["overall"]["score"]
        new_verdict  = cvr["overall"]["verdict"]
        new_minimality       = cvr["overall"]["minimality_verdict"]
        new_issue_counts     = cvr["overall"]["issue_counts"]
        new_layer_scores     = {
            lid: v["score"] for lid, v in cvr["layer_scores"].items()
        }
        score_delta  = round(new_score - original_score, 3)
        score_note   = "Re-validation complete."
    else:
        new_score        = None
        new_verdict      = None
        new_minimality   = None
        new_issue_counts = None
        new_layer_scores = None
        score_delta      = None
        score_note       = "Re-run validator on corrected intent to get updated scores."

    return {
        "correction_summary": {
            "corrected_at":      datetime.now(timezone.utc).isoformat(),
            "intent_id":         original.get("Header", {}).get("intent_id", "unknown"),

            # ---- before
            "before": {
                "score":         original_score,
                "verdict":       original_verdict,
                "issue_counts":  vr["overall"]["issue_counts"],
                "minimality":    vr["overall"]["minimality_verdict"],
                "layer_scores":  {
                    lid: v["score"] for lid, v in vr["layer_scores"].items()
                },
            },

            # ---- after
            "after": {
                "score":         new_score,
                "verdict":       new_verdict,
                "minimality":    new_minimality,
                "issue_counts":  new_issue_counts,
                "layer_scores":  new_layer_scores,
            },

            # ---- delta
            "delta": {
                "score_change":  score_delta,
                "verdict_change": (
                    f"{original_verdict} → {new_verdict}"
                    if new_verdict else None
                ),
                "note": score_note,
            },

            # ---- what was done
            "corrections_applied": len(log),
            "issues_targeted":     vr["overall"]["issue_counts"]["HIGH"],
            "fields_changed":      sorted(set(e.field for e in log)),
            "actions_taken": {
                action: sum(1 for e in log if e.action == action)
                for action in ["ADDED", "MODIFIED", "REMOVED", "REPLACED"]
            },
            "correction_log": [entry.to_dict() for entry in log],
        }
    }


# -----------------------------------------------------------------------
# Batch helpers
# -----------------------------------------------------------------------

def _normalize_report_records(raw) -> tuple[list, bool]:
    """
    Normalize supported report shapes into a list of report records.

    Supported shapes:
      - single validation report dict
      - wrapper dict with {"report": {...}}
      - batch wrapper dict with {"batch_validation_report": {"reports": [...]}}
      - list of single report dicts
    """
    if isinstance(raw, dict) and "batch_validation_report" in raw:
        items = raw["batch_validation_report"].get("reports", [])
        records = []
        for pos, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"Batch report item {pos} must be a JSON object")
            report = item.get("report", item)
            if not isinstance(report, dict) or "validation_report" not in report:
                raise ValueError(f"Batch report item {pos} is missing a validation_report payload")
            records.append({
                "position": pos - 1,
                "record_index": item.get("record_index", pos),
                "question": item.get("question", ""),
                "report": report,
            })
        return records, True

    if isinstance(raw, list):
        records = []
        for pos, item in enumerate(raw, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"Report record {pos} must be a JSON object")
            report = item.get("report", item)
            if not isinstance(report, dict) or "validation_report" not in report:
                raise ValueError(f"Report record {pos} is missing a validation_report payload")
            records.append({
                "position": pos - 1,
                "record_index": item.get("record_index", pos),
                "question": item.get("question", ""),
                "report": report,
            })
        return records, True

    if isinstance(raw, dict):
        report = raw.get("report", raw)
        if not isinstance(report, dict) or "validation_report" not in report:
            raise ValueError("Report input must contain a validation_report object")
        return [{
            "position": 0,
            "record_index": raw.get("record_index"),
            "question": raw.get("question", ""),
            "report": report,
        }], False

    raise ValueError("Report input must be a JSON object or an array of JSON objects")


def _load_report_input(report_path: str):
    with open(report_path, encoding="utf-8") as f:
        raw = json.load(f)
    records, is_batch = _normalize_report_records(raw)
    return raw, records, is_batch


def _rebuild_corrected_output(records: list, corrected_intents: list, is_batch: bool):
    if not is_batch:
        return corrected_intents[0]

    output = []
    for record, corrected_intent in zip(records, corrected_intents):
        if record.get("source_type") == "bundle_record":
            updated = copy.deepcopy(record["raw_record"])
            updated["response"] = corrected_intent
            output.append(updated)
        else:
            output.append(corrected_intent)
    return output


def _assemble_batch_validation_report(items: list) -> dict:
    verdict_counts = {"PASS": 0, "PARTIAL_PASS": 0, "FAIL": 0}
    issue_totals = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    total_score = 0.0

    for item in items:
        overall = item["report"]["validation_report"]["overall"]
        total_score += overall["score"]
        verdict_counts[overall["verdict"]] = verdict_counts.get(overall["verdict"], 0) + 1
        for priority, count in overall["issue_counts"].items():
            issue_totals[priority] = issue_totals.get(priority, 0) + count

    return {
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


def _assemble_batch_correction_log(records: list) -> dict:
    total_before = 0.0
    total_after = 0.0
    total_corrections = 0
    before_verdicts = {"PASS": 0, "PARTIAL_PASS": 0, "FAIL": 0}
    after_verdicts = {"PASS": 0, "PARTIAL_PASS": 0, "FAIL": 0}
    has_after = True

    for item in records:
        summary = item["correction_summary"]
        total_before += summary["before"]["score"]
        total_corrections += summary["corrections_applied"]
        before_verdicts[summary["before"]["verdict"]] = before_verdicts.get(summary["before"]["verdict"], 0) + 1

        after_score = summary["after"]["score"]
        after_verdict = summary["after"]["verdict"]
        if after_score is None or after_verdict is None:
            has_after = False
        else:
            total_after += after_score
            after_verdicts[after_verdict] = after_verdicts.get(after_verdict, 0) + 1

    count = len(records)
    return {
        "batch_correction_log": {
            "corrected_at": datetime.now(timezone.utc).isoformat(),
            "record_count": count,
            "summary": {
                "average_score_before": round(total_before / count, 3) if count else 0.0,
                "average_score_after": round(total_after / count, 3) if count and has_after else None,
                "verdict_counts_before": before_verdicts,
                "verdict_counts_after": after_verdicts if has_after else None,
                "total_corrections_applied": total_corrections,
            },
            "records": records,
        }
    }


def correct_batch(
    intent_records: list,
    report_records: list,
    is_batch_input: bool,
    question_override: str = None,
    question_file_text: str = None,
) -> tuple[object, dict, dict | None]:
    from main import resolve_question_for_record, run_validation

    if len(intent_records) != len(report_records):
        raise ValueError(
            f"Intent record count ({len(intent_records)}) does not match report record count ({len(report_records)})"
        )

    corrected_intents = []
    correction_log_records = []
    revalidated_items = []

    print("=" * 60)
    print("  Intent Corrector - Batch Mode")
    print("=" * 60)
    print(f"  Records: {len(intent_records)}")

    for idx, (record, report_record) in enumerate(zip(intent_records, report_records), start=1):
        intent = record["intent"]
        report = report_record["report"]
        question = resolve_question_for_record(
            question_str=question_override,
            question_file_text=question_file_text,
            record=record,
        )
        if not question:
            question = report_record.get("question", "").strip()

        intent_id = intent.get("Header", {}).get("intent_id", "unknown")
        print(
            f"\n[Record {idx}/{len(intent_records)}] "
            f"index={_safe_console(record.get('record_index'))} intent_id={_safe_console(intent_id)}"
        )

        corrector = IntentCorrector(score_threshold=SCORE_THRESHOLD)
        corrected_intent = corrector.correct(intent, report)

        if question:
            corrected_report = run_validation(question=question, intent=corrected_intent, verbose=False)
            revalidated_items.append({
                "record_index": record.get("record_index"),
                "question": question,
                "intent_id": corrected_report["validation_report"]["meta"]["intent_id"],
                "report": corrected_report,
            })
        else:
            corrected_report = None

        summary = build_correction_summary(
            corrector.log,
            intent,
            corrected_intent,
            report,
            corrected_report,
        )

        corrected_intents.append(corrected_intent)
        correction_log_records.append({
            "record_index": record.get("record_index"),
            "question": question,
            "intent_id": corrected_intent.get("Header", {}).get("intent_id", intent_id),
            "correction_summary": summary["correction_summary"],
        })

    corrected_output = _rebuild_corrected_output(intent_records, corrected_intents, is_batch_input)
    batch_log = _assemble_batch_correction_log(correction_log_records)
    batch_revalidated = _assemble_batch_validation_report(revalidated_items) if revalidated_items else None
    return corrected_output, batch_log, batch_revalidated


# -----------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------

def run_demo(intent_path: str = None, question: str = None):
    """
    Full pipeline runner: validate → correct → re-validate in one shot.
    Reads intent from an external JSON file — no hardcoded data here.

    Args:
        intent_path: Path to intent JSON file (required for demo)
        question:    Question string. If None, read from intent Header.question_text
    """
    from main import load_intent_input, resolve_question_for_record, run_batch_validation, run_validation

    if not intent_path:
        print("Error: --pipeline requires --intent <path_to_intent.json>")
        sys.exit(1)

    _raw, records, is_batch = load_intent_input(intent_path)

    print("=" * 60)
    print("  Intent Corrector - Demo Mode")
    print("=" * 60)

    if is_batch:
        # Phase 1: validate original bundle
        print("\n[Phase 1] Validating original intent bundle...")
        report = run_batch_validation(
            records=records,
            question_override=question,
            question_file_text=None,
            output_path=None,
            verbose=False,
        )

        # Phase 2 + 3: correct and re-validate bundle
        print("\n[Phase 2] Applying corrections...")
        corrected_output, batch_log, batch_revalidated = correct_batch(
            intent_records=records,
            report_records=report["batch_validation_report"]["reports"],
            is_batch_input=True,
            question_override=question,
            question_file_text=None,
        )

        corrected_path   = DEFAULT_OUTPUT_DIR / "corrected_intent.json"
        log_path         = DEFAULT_OUTPUT_DIR / "correction_log.json"
        revalidated_path = DEFAULT_OUTPUT_DIR / "revalidated_report.json"

        _write_json_output(corrected_path, corrected_output)
        _write_json_output(log_path, batch_log)
        if batch_revalidated:
            _write_json_output(revalidated_path, batch_revalidated)

        print(f"\n  Saved: {corrected_path}")
        print(f"  Saved: {log_path}")
        if batch_revalidated:
            print(f"  Saved: {revalidated_path}")
        print("=" * 60)
        return corrected_output, batch_log, batch_revalidated

    intent = records[0]["intent"]
    question = question or resolve_question_for_record(
        question_str=None,
        question_file_text=None,
        record=records[0],
    )
    if not question:
        print("Error: question not found. Pass --question or set Header.question_text")
        sys.exit(1)

    # Phase 1: validate original
    print("\n[Phase 1] Validating original intent...")
    report = run_validation(question=question, intent=intent, verbose=False)
    original_score = report["validation_report"]["overall"]["score"]
    print(f"  Original score: {original_score}  "
          f"Verdict: {report['validation_report']['overall']['verdict']}")

    # Phase 2: correct
    print("\n[Phase 2] Applying corrections...")
    corrector = IntentCorrector(score_threshold=SCORE_THRESHOLD)
    corrected_intent = corrector.correct(intent, report)

    # Phase 3: re-validate corrected intent
    print("\n[Phase 3] Re-validating corrected intent...")
    corrected_report = run_validation(question=question, intent=corrected_intent, verbose=False)
    new_score = corrected_report["validation_report"]["overall"]["score"]
    new_verdict = corrected_report["validation_report"]["overall"]["verdict"]
    print(f"  Corrected score: {new_score}  Verdict: {new_verdict}")
    print(f"  Score improvement: {original_score} -> {new_score} "
          f"(+{round(new_score - original_score, 3)})")

    # Build summary — pass corrected_report so new score appears in the log
    summary = build_correction_summary(corrector.log, intent, corrected_intent, report, corrected_report)

    # Save outputs
    out_dir = DEFAULT_OUTPUT_DIR
    corrected_path   = out_dir / "corrected_intent.json"
    log_path         = out_dir / "correction_log.json"
    revalidated_path = out_dir / "revalidated_report.json"

    _write_json_output(corrected_path, corrected_intent)
    _write_json_output(log_path, summary)
    _write_json_output(revalidated_path, corrected_report)

    print(f"\n  Saved: {corrected_path}")
    print(f"  Saved: {log_path}")
    print(f"  Saved: {revalidated_path}")
    print("=" * 60)

    return corrected_intent, summary, corrected_report


def main():
    parser = argparse.ArgumentParser(
        description="Intent Object Corrector",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Correct using a pre-generated validation report:
  python corrector.py --intent intent.json --report validation_report.json

  # Correct a batch file using a batch validation report:
  python corrector.py --intent intent_analysis_results.json --report batch_report.json

  # Full pipeline: validate + correct + re-validate in one step:
  python corrector.py --pipeline --intent intent.json

  # With explicit question:
  python corrector.py --pipeline --intent intent.json --question "Did JUUL Labs..."
        """,
    )
    parser.add_argument("--intent",        type=str, help="Path to intent JSON")
    parser.add_argument("--question",      type=str, help="Question string (inline)")
    parser.add_argument("--question-file", type=str, dest="question_file",
                        help="Path to a plain .txt file containing the question")
    parser.add_argument("--report",        type=str, help="Path to pre-generated validation report JSON")
    parser.add_argument(
        "--output",
        type=str,
        default=str(DEFAULT_OUTPUT_DIR / "corrected_intent.json"),
    )
    parser.add_argument(
        "--log",
        type=str,
        default=str(DEFAULT_OUTPUT_DIR / "correction_log.json"),
    )
    parser.add_argument(
        "--pipeline",
        action="store_true",
        help="Full pipeline: validate -> correct -> re-validate (requires --intent)",
    )
    args = parser.parse_args()

    question_file_text = None
    if args.question_file:
        with open(args.question_file, encoding="utf-8") as f:
            question_file_text = f.read().strip()

    if args.pipeline:
        run_demo(intent_path=args.intent, question=args.question or question_file_text)
        return

    if not args.intent or not args.report:
        print("Error: --intent and --report required (or use --pipeline --intent <file>)")
        sys.exit(1)

    from main import load_intent_input, resolve_question_for_record, run_validation

    try:
        _intent_raw, intent_records, is_batch_input = load_intent_input(args.intent)
        _report_raw, report_records, is_batch_report = _load_report_input(args.report)
    except ValueError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    if is_batch_input:
        try:
            corrected_output, batch_log, batch_revalidated = correct_batch(
                intent_records=intent_records,
                report_records=report_records,
                is_batch_input=True,
                question_override=args.question,
                question_file_text=question_file_text,
            )
        except ValueError as exc:
            print(f"Error: {exc}")
            sys.exit(1)

        _write_json_output(args.output, corrected_output)
        _write_json_output(args.log, batch_log)

        print(f"\n  Corrected intent -> {_safe_console(args.output)}")
        print(f"  Correction log   -> {_safe_console(args.log)}")
        if batch_revalidated:
            before_avg = batch_log["batch_correction_log"]["summary"]["average_score_before"]
            after_avg = batch_log["batch_correction_log"]["summary"]["average_score_after"]
            print(f"  Average score: {before_avg} -> {after_avg}")
        return

    if len(report_records) != 1:
        print("Error: single intent input requires a single validation report, not a batch report")
        sys.exit(1)

    intent = intent_records[0]["intent"]
    report = report_records[0]["report"]

    corrector = IntentCorrector(score_threshold=SCORE_THRESHOLD)
    corrected = corrector.correct(intent, report)

    # Re-validate corrected intent so the log contains the new score
    question = resolve_question_for_record(
        question_str=args.question,
        question_file_text=question_file_text,
        record=intent_records[0],
    )
    if question:
        corrected_report = run_validation(question=question, intent=corrected, verbose=False)
    else:
        corrected_report = None

    summary = build_correction_summary(corrector.log, intent, corrected, report, corrected_report)

    _write_json_output(args.output, corrected)
    _write_json_output(args.log, summary)

    print(f"\n  Corrected intent -> {_safe_console(args.output)}")
    print(f"  Correction log   -> {_safe_console(args.log)}")
    print(f"  Corrections applied: {len(corrector.log)}")
    if corrected_report:
        orig  = report["validation_report"]["overall"]["score"]
        new   = corrected_report["validation_report"]["overall"]["score"]
        print(f"  Score: {orig} -> {new} ({'+' if new >= orig else ''}{round(new - orig, 3)})")


if __name__ == "__main__":
    main()
