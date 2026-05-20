"""
Step 2 — Validation Engine
Six independent validation layers. Each returns a list of Issue objects.
Priority: HIGH | MEDIUM | LOW  (replaces ERROR/WARN/INFO)
"""

from dataclasses import dataclass, field
from typing import Optional
from question_parser import (
    QuestionGroundTruth, VALID_NODE_TYPES, ExtractedEntity, ALL_INTENT_CATEGORIES
)

# -----------------------------------------------------------------------
# Issue data structure
# -----------------------------------------------------------------------

@dataclass
class Issue:
    issue_id: str
    layer: str
    priority: str          # HIGH | MEDIUM | LOW
    field: str
    message: str
    fix: str
    evidence: str = ""     # what in the intent triggered this


@dataclass
class MinimalityFinding:
    field: str
    finding: str
    recommendation: str
    bloat_type: str        # REDUNDANT | OVER_SPECIFIED | UNUSED | GENERIC


@dataclass
class LayerResult:
    layer_id: str
    layer_name: str
    score: float           # 0.0 – 1.0
    issues: list = field(default_factory=list)
    minimality_findings: list = field(default_factory=list)
    passed_checks: list = field(default_factory=list)


# -----------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------

VALID_SLOT_TYPES = {
    "WHO", "WHAT", "WHEN", "HOW", "WHY",
    "EVIDENCE", "OUTCOME", "AWARENESS"
}

VALID_ARTIFACT_TYPES = {
    "ARTIFACT_DOCUMENT", "ARTIFACT_PDF", "ARTIFACT_EMAIL",
    "ARTIFACT_THREAD", "ARTIFACT_PRESENTATION"
}

VALID_SCOPE_MODES = {"PREFER", "REQUIRE", "EXCLUDE"}

VALID_EDGE_RELATIONS = {
    "REPRESENTS", "ABOUT", "INFLUENCED", "RESULTED_IN", "ASSOCIATED_WITH",
    "FRAMED_AS", "COORDINATED_WITH", "AWARE_OF", "INFORMED", "DERIVED_FROM",
    "CONSTRUCTED", "ORCHESTRATED", "LINKED_TO", "SUPPORTS", "ENABLED",
    "PUBLIC_FACING", "INTERNAL_ONLY", "HAS_TIME", "ATTACHED_TO",
    "EVIDENCED_BY", "RAN_PARALLEL_TO"
}

# Relations that assert causation — only valid if question asserts it
CAUSAL_RELATIONS = {"RESULTED_IN", "CAUSED", "LED_TO", "DERIVED_FROM"}

# Relations that are actually attributes, not edges
ATTRIBUTE_AS_EDGE = {"PUBLIC_FACING", "INTERNAL_ONLY"}

VALID_TEMPORAL_KINDS = {"ORDER", "CONCURRENT", "RANGE"}

GENERIC_EXPANSIONS = {
    "regulatory affairs", "compliance", "internal comms",
    "sales data", "documentation", "records"
}


# -----------------------------------------------------------------------
# Layer 1 — Entity Completeness
# -----------------------------------------------------------------------

class EntityCompletenessValidator:
    """
    Checks that every entity extracted from the question exists in EntityHints,
    with the correct canonical node type.
    """

    CATEGORY_ALIASES = {
        # intent category -> acceptable canonical types
        "ENTITY_ORGANIZATION":        ["organization"],
        "ENTITY_STRATEGY":            ["claim", "legalFramework", "topics"],
        "ENTITY_DOCUMENT":            ["assessment", "citation", "procedure"],
        "ENTITY_CONCEPT":             ["topics", "procedure", "claim"],
        "ENTITY_FRAMING":             ["claim", "topics", "healthMention"],
        "ENTITY_ACTION_ITEM":         ["procedure", "finance", "topics"],
        "ENTITY_PERSON":              ["person"],
        "ENTITY_EVENT":               ["event", "date"],
        "ENTITY_TEMPORAL_UNCERTAINTY":["date"],
        "ENTITY_TIME_RANGE":          ["date"],
        "ENTITY_RISK_FINDING":        ["risk"],
        "ENTITY_APPROVAL":            ["decision", "legalFramework"],
        "ENTITY_PRODUCT":             ["product", "drug"],
        "ENTITY_VISIBILITY_LEVEL":    ["topics", "state"],
        "ENTITY_INTENT":              ["claim", "topics"],
        "ENTITY_AWARENESS":           ["topics", "claim"],
        "ENTITY_BEHAVIORAL_PATTERN":  ["claim", "topics"],
        "ENTITY_QUALIFIER":           ["requirement", "topics"],
    }

    def validate(self, intent: dict, gt: QuestionGroundTruth) -> LayerResult:
        issues = []
        minimality = []
        passed = []
        hints = intent.get("EntityHints", [])
        hint_surfaces = [h.get("surface", "").lower() for h in hints]
        hint_categories = {h.get("surface", "").lower(): h.get("category", "") for h in hints}

        issue_idx = 1

        # --- Check 1: every ground-truth entity has a corresponding hint
        for gt_entity in gt.entities:
            matched = self._find_match(gt_entity.surface, hint_surfaces)
            if not matched:
                priority = "HIGH" if not gt_entity.implicit else "MEDIUM"
                issues.append(Issue(
                    issue_id=f"EC-{issue_idx:03d}",
                    layer="entity_completeness",
                    priority=priority,
                    field="EntityHints",
                    message=(
                        f"Entity '{gt_entity.surface}' (type: {gt_entity.canonical_type}) "
                        f"extracted from question but absent from EntityHints"
                        + (" [implicit constraint]" if gt_entity.implicit else "")
                    ),
                    fix=(
                        f"Add EntityHint with surface='{gt_entity.surface}', "
                        f"category='{gt_entity.intent_category}'"
                    ),
                    evidence="Derived from question parser ground truth",
                ))
                issue_idx += 1
            else:
                passed.append(f"Entity '{gt_entity.surface}' found in EntityHints")

        # --- Check 2: every hint has a valid canonical category
        for hint in hints:
            cat = hint.get("category", "")
            surface = hint.get("surface", "")
            acceptable = self.CATEGORY_ALIASES.get(cat, [])
            if cat not in self.CATEGORY_ALIASES:
                issues.append(Issue(
                    issue_id=f"EC-{issue_idx:03d}",
                    layer="entity_completeness",
                    priority="MEDIUM",
                    field=f"EntityHints['{surface}'].category",
                    message=f"Category '{cat}' is not a recognized EntityHint category",
                    fix=f"Replace with one of: {', '.join(sorted(ALL_INTENT_CATEGORIES))}",
                    evidence=f"hint surface='{surface}'",
                ))
                issue_idx += 1
            else:
                passed.append(f"Category '{cat}' on '{surface}' is recognized")

        # --- Check 3: time range typed correctly
        for hint in hints:
            surface = hint.get("surface", "")
            cat = hint.get("category", "")
            if re.search(r"\d{4}", surface) and cat not in [
                "ENTITY_TEMPORAL_UNCERTAINTY", "ENTITY_TIME_RANGE", "ENTITY_EVENT"
            ]:
                # only flag if it looks like ONLY a date
                if re.fullmatch(r"[\d\s\-–—andAND]+", surface.strip()):
                    issues.append(Issue(
                        issue_id=f"EC-{issue_idx:03d}",
                        layer="entity_completeness",
                        priority="LOW",
                        field=f"EntityHints['{surface}'].category",
                        message=f"'{surface}' appears to be a date range but is typed '{cat}'",
                        fix="Change category to ENTITY_TIME_RANGE or use canonical type 'date'",
                        evidence=f"surface='{surface}', category='{cat}'",
                    ))
                    issue_idx += 1

        # --- Check 4: confidence scores — minimality
        for hint in hints:
            conf = hint.get("confidence")
            surface = hint.get("surface", "")
            if conf is not None:
                minimality.append(MinimalityFinding(
                    field=f"EntityHints['{surface}'].confidence",
                    finding=f"Confidence score {conf} present on unambiguous entity",
                    recommendation="Remove confidence scores for deterministic extractions; use only for genuinely ambiguous surfaces",
                    bloat_type="OVER_SPECIFIED",
                ))

        # score: penalise per issue weighted by priority
        score = self._compute_score(issues, len(gt.entities) + len(hints))
        return LayerResult("entity_completeness", "Entity Completeness", score, issues, minimality, passed)

    def _find_match(self, surface: str, hint_surfaces: list) -> bool:
        sl = surface.lower()
        return any(sl in h or h in sl for h in hint_surfaces)

    def _compute_score(self, issues, total_checks):
        if total_checks == 0:
            return 1.0
        penalty = sum({"HIGH": 3, "MEDIUM": 2, "LOW": 1}[i.priority] for i in issues)
        max_penalty = total_checks * 3
        return max(0.0, round(1.0 - (penalty / max_penalty), 2))


import re


# -----------------------------------------------------------------------
# Layer 2 — Scope Correctness
# -----------------------------------------------------------------------

class ScopeCorrectnessValidator:

    def validate(self, intent: dict, gt: QuestionGroundTruth) -> LayerResult:
        issues = []
        minimality = []
        passed = []
        scope = intent.get("ScopeSpec", {})
        issue_idx = 1

        # --- Check 1: time filter matches question
        tf = scope.get("time_filter", {})
        if gt.time_range:
            expected_start, expected_end = gt.time_range
            actual_start = tf.get("start", "")
            actual_end = tf.get("end", "")
            if actual_start[:4] != expected_start[:4] or actual_end[:4] != expected_end[:4]:
                issues.append(Issue(
                    issue_id=f"SC-{issue_idx:03d}",
                    layer="scope_correctness",
                    priority="HIGH",
                    field="ScopeSpec.time_filter",
                    message=(
                        f"Time filter [{actual_start[:4]}–{actual_end[:4]}] "
                        f"does not match question time range [{expected_start[:4]}–{expected_end[:4]}]"
                    ),
                    fix=f"Set start='{expected_start}', end='{expected_end}'",
                    evidence=f"Question contains years: {expected_start[:4]}, {expected_end[:4]}",
                ))
                issue_idx += 1
            else:
                passed.append("time_filter matches question date range")
        elif tf:
            minimality.append(MinimalityFinding(
                field="ScopeSpec.time_filter",
                finding="Time filter present but no date range found in question",
                recommendation="Remove time_filter or justify its source",
                bloat_type="REDUNDANT",
            ))

        # --- Check 2: artifact_types declared cover slot usage
        scope_artifacts = set(scope.get("artifact_types", []))
        slots = intent.get("SlotSpec", {}).get("slots", [])
        slot_artifacts = set()
        for slot in slots:
            for a in slot.get("allowed_artifact_types", []):
                slot_artifacts.add(a)

        undeclared = slot_artifacts - scope_artifacts
        for ua in undeclared:
            issues.append(Issue(
                issue_id=f"SC-{issue_idx:03d}",
                layer="scope_correctness",
                priority="HIGH",
                field="ScopeSpec.artifact_types",
                message=f"'{ua}' used in slots but not declared in ScopeSpec.artifact_types",
                fix=f"Add '{ua}' to ScopeSpec.artifact_types",
                evidence=f"Found in slot allowed_artifact_types",
            ))
            issue_idx += 1

        # --- Check 3: mode appropriateness
        mode = scope.get("mode", "")
        if mode not in VALID_SCOPE_MODES:
            issues.append(Issue(
                issue_id=f"SC-{issue_idx:03d}",
                layer="scope_correctness",
                priority="MEDIUM",
                field="ScopeSpec.mode",
                message=f"mode='{mode}' is not a recognized value",
                fix=f"Use one of: {VALID_SCOPE_MODES}",
                evidence=f"mode='{mode}'",
            ))
            issue_idx += 1
        elif mode == "REQUIRE" and gt.question_type in ["investigative_binary", "compound"]:
            issues.append(Issue(
                issue_id=f"SC-{issue_idx:03d}",
                layer="scope_correctness",
                priority="MEDIUM",
                field="ScopeSpec.mode",
                message="mode='REQUIRE' is too strict for an investigative yes/no question — may exclude exculpatory documents",
                fix="Change to mode='PREFER'",
                evidence=f"question_type={gt.question_type}",
            ))
            issue_idx += 1
        else:
            passed.append(f"mode='{mode}' appropriate for question type")

        # --- Check 4: unknown artifact types
        for art in scope_artifacts:
            if art not in VALID_ARTIFACT_TYPES:
                issues.append(Issue(
                    issue_id=f"SC-{issue_idx:03d}",
                    layer="scope_correctness",
                    priority="LOW",
                    field="ScopeSpec.artifact_types",
                    message=f"Artifact type '{art}' is not a recognized type",
                    fix=f"Use one of: {VALID_ARTIFACT_TYPES}",
                    evidence=f"Found in ScopeSpec.artifact_types",
                ))
                issue_idx += 1

        # --- Minimality: empty collections
        collections = scope.get("collections", {})
        if not collections.get("include") and not collections.get("exclude"):
            minimality.append(MinimalityFinding(
                field="ScopeSpec.collections",
                finding="Collections include/exclude are both empty",
                recommendation="Either populate with known document collection IDs or remove the field entirely",
                bloat_type="UNUSED",
            ))

        # --- Minimality: metadata_filters empty
        if "metadata_filters" in scope and not scope["metadata_filters"]:
            minimality.append(MinimalityFinding(
                field="ScopeSpec.metadata_filters",
                finding="metadata_filters is an empty list",
                recommendation="Remove if unused to reduce noise",
                bloat_type="REDUNDANT",
            ))

        score = self._compute_score(issues, 6)
        return LayerResult("scope_correctness", "Scope Correctness", score, issues, minimality, passed)

    def _compute_score(self, issues, total_checks):
        penalty = sum({"HIGH": 3, "MEDIUM": 2, "LOW": 1}[i.priority] for i in issues)
        return max(0.0, round(1.0 - (penalty / (total_checks * 3)), 2))


# -----------------------------------------------------------------------
# Layer 3 — Retrieval Quality
# -----------------------------------------------------------------------

class RetrievalQualityValidator:

    def validate(self, intent: dict, gt: QuestionGroundTruth) -> LayerResult:
        issues = []
        minimality = []
        passed = []
        spec = intent.get("RetrievalSpec", {})
        query_text = spec.get("query_text", "").lower()
        expansions = [e.lower() for e in spec.get("query_expansions", [])]
        all_retrieval_text = query_text + " " + " ".join(expansions)
        issue_idx = 1

        # --- Check 1: key entities covered in query
        for entity in gt.entities:
            surface_words = entity.surface.lower().split()
            key_word = max(surface_words, key=len)  # use longest word as proxy
            if key_word not in all_retrieval_text and not entity.implicit:
                issues.append(Issue(
                    issue_id=f"RQ-{issue_idx:03d}",
                    layer="retrieval_quality",
                    priority="HIGH",
                    field="RetrievalSpec.query_text + query_expansions",
                    message=f"Entity '{entity.surface}' (type: {entity.canonical_type}) has no coverage in query_text or query_expansions",
                    fix=f"Add '{entity.surface}' or a synonym to query_expansions",
                    evidence=f"Entity found in ground truth, absent from retrieval spec",
                ))
                issue_idx += 1
            else:
                passed.append(f"Entity '{entity.surface}' covered in retrieval")

        # --- Check 2: implicit constraints covered
        if gt.cross_track_awareness_required:
            awareness_terms = ["silo", "compartment", "parallel workstream", "need to know",
                               "cross-track", "cross track", "knowledge of each other"]
            if not any(t in all_retrieval_text for t in awareness_terms):
                issues.append(Issue(
                    issue_id=f"RQ-{issue_idx:03d}",
                    layer="retrieval_quality",
                    priority="HIGH",
                    field="RetrievalSpec.query_expansions",
                    message="Cross-track awareness (second sub-question) has no retrieval coverage",
                    fix="Add expansions: 'silo', 'need to know', 'compartmentalized', 'parallel workstream', 'cross-functional awareness'",
                    evidence="gt.cross_track_awareness_required=True",
                ))
                issue_idx += 1

        if gt.intentionality_required:
            intent_terms = ["strategic plan", "coordinated", "dual track", "deliberate", "orchestrat"]
            if not any(t in all_retrieval_text for t in intent_terms):
                issues.append(Issue(
                    issue_id=f"RQ-{issue_idx:03d}",
                    layer="retrieval_quality",
                    priority="HIGH",
                    field="RetrievalSpec.query_expansions",
                    message="Intentionality signal ('systematically') in question not reflected in retrieval — will miss deliberate planning documents",
                    fix="Add expansions: 'strategic plan', 'coordinated approach', 'dual track', 'deliberate'",
                    evidence="gt.intentionality_required=True",
                ))
                issue_idx += 1

        # --- Check 3: generic expansions
        generic_found = [e for e in expansions if e in GENERIC_EXPANSIONS]
        if generic_found:
            issues.append(Issue(
                issue_id=f"RQ-{issue_idx:03d}",
                layer="retrieval_quality",
                priority="MEDIUM",
                field="RetrievalSpec.query_expansions",
                message=f"Generic expansions will over-retrieve noise: {generic_found}",
                fix="Replace with domain-specific terms: 'PMTA', 'premarket tobacco application', 'Youth Forward', 'age verification'",
                evidence=f"Found in expansions: {generic_found}",
            ))
            issue_idx += 1
        else:
            passed.append("No generic expansions detected")

        # --- Check 4: third-party qualifier coverage
        if any("third" in e.qualifiers.get("role","") or "third_party" in str(e.qualifiers)
               for e in gt.entities):
            if "third party" not in all_retrieval_text and "third-party" not in all_retrieval_text:
                issues.append(Issue(
                    issue_id=f"RQ-{issue_idx:03d}",
                    layer="retrieval_quality",
                    priority="HIGH",
                    field="RetrievalSpec.query_expansions",
                    message="'third-party' qualifier present in ground truth entities but absent from retrieval — will miss external consultant/vendor documents",
                    fix="Add: 'third party consultant', 'external advisor', 'outside counsel'",
                    evidence="third-party entity in ground truth",
                ))
                issue_idx += 1

        # --- Minimality: field_boosts
        field_boosts = spec.get("field_boosts", {})
        if field_boosts:
            minimality.append(MinimalityFinding(
                field="RetrievalSpec.field_boosts",
                finding=f"field_boosts {field_boosts} are hardcoded defaults not derived from this question",
                recommendation="Move to system-level retrieval config; only include in intent if question requires non-default boosting",
                bloat_type="OVER_SPECIFIED",
            ))

        # --- Minimality: top_k values
        top_k_lex = spec.get("top_k_lex", 0)
        top_k_sem = spec.get("top_k_sem", 0)
        if top_k_lex == top_k_sem and top_k_lex > 0:
            minimality.append(MinimalityFinding(
                field="RetrievalSpec.top_k_lex / top_k_sem",
                finding=f"Both top_k values are identical ({top_k_lex}) — suggests copy-paste defaults",
                recommendation="Tune per question complexity or rely on system defaults",
                bloat_type="OVER_SPECIFIED",
            ))

        score = self._compute_score(issues, len(gt.entities) + 4)
        return LayerResult("retrieval_quality", "Retrieval Quality", score, issues, minimality, passed)

    def _compute_score(self, issues, total_checks):
        if total_checks == 0:
            return 1.0
        penalty = sum({"HIGH": 3, "MEDIUM": 2, "LOW": 1}[i.priority] for i in issues)
        return max(0.0, round(1.0 - (penalty / (total_checks * 3)), 2))


# -----------------------------------------------------------------------
# Layer 4 — Slot Completeness
# -----------------------------------------------------------------------

class SlotCompletenessValidator:

    def validate(self, intent: dict, gt: QuestionGroundTruth) -> LayerResult:
        issues = []
        minimality = []
        passed = []
        slot_spec = intent.get("SlotSpec", {})
        slots = slot_spec.get("slots", [])
        declared_slot_types = {s.get("slot_type", "") for s in slots}
        issue_idx = 1

        # --- Check 1: required slots present
        for required_slot in gt.required_slot_types:
            if required_slot not in declared_slot_types:
                priority = "HIGH" if required_slot in ["EVIDENCE", "AWARENESS"] else "MEDIUM"
                issues.append(Issue(
                    issue_id=f"SL-{issue_idx:03d}",
                    layer="slot_completeness",
                    priority=priority,
                    field="SlotSpec.slots",
                    message=f"Required slot type '{required_slot}' missing — inferred from question content",
                    fix=f"Add slot with slot_type='{required_slot}' and appropriate description and artifact_types",
                    evidence=f"gt.required_slot_types={gt.required_slot_types}",
                ))
                issue_idx += 1
            else:
                passed.append(f"Slot '{required_slot}' present")

        # --- Check 2: AWARENESS slot specifically for cross-track questions
        if gt.cross_track_awareness_required and "AWARENESS" not in declared_slot_types:
            issues.append(Issue(
                issue_id=f"SL-{issue_idx:03d}",
                layer="slot_completeness",
                priority="HIGH",
                field="SlotSpec.slots",
                message="AWARENESS slot missing — question explicitly asks 'did individuals have knowledge of each other's activities'",
                fix="Add: { slot_id: 'S-AWARENESS-001', slot_type: 'AWARENESS', description: 'Evidence of cross-track knowledge between individuals', allowed_artifact_types: ['ARTIFACT_EMAIL', 'ARTIFACT_THREAD', 'ARTIFACT_DOCUMENT'], target_schema_id: 'schema:CROSS_TRACK_KNOWLEDGE' }",
                evidence="gt.cross_track_awareness_required=True",
            ))
            issue_idx += 1

        # --- Check 3: slot artifact types are valid
        for slot in slots:
            slot_id = slot.get("slot_id", "?")
            for art in slot.get("allowed_artifact_types", []):
                if art not in VALID_ARTIFACT_TYPES:
                    issues.append(Issue(
                        issue_id=f"SL-{issue_idx:03d}",
                        layer="slot_completeness",
                        priority="LOW",
                        field=f"SlotSpec.slots[{slot_id}].allowed_artifact_types",
                        message=f"Artifact type '{art}' not recognized",
                        fix=f"Use one of: {VALID_ARTIFACT_TYPES}",
                        evidence=f"slot_id={slot_id}",
                    ))
                    issue_idx += 1

        # --- Check 4: slot descriptions match question context
        for slot in slots:
            desc = slot.get("description", "")
            if len(desc) < 20:
                issues.append(Issue(
                    issue_id=f"SL-{issue_idx:03d}",
                    layer="slot_completeness",
                    priority="LOW",
                    field=f"SlotSpec.slots[{slot.get('slot_id')}].description",
                    message="Slot description is too brief to guide retrieval meaningfully",
                    fix="Expand description to reference specific question context (e.g., which individuals, which tracks)",
                    evidence=f"description='{desc}'",
                ))
                issue_idx += 1

        # --- Minimality: global_trigger terms
        global_trigger = slot_spec.get("global_trigger", {})
        gt_terms = global_trigger.get("terms", [])
        if gt_terms:
            # check for terms that are just duplicates of entity surfaces
            entity_surfaces = [e.surface.lower() for e in gt.entities]
            redundant = [t for t in gt_terms if t.lower() in entity_surfaces]
            if len(redundant) > len(gt_terms) * 0.7:
                minimality.append(MinimalityFinding(
                    field="SlotSpec.global_trigger.terms",
                    finding=f"{len(redundant)}/{len(gt_terms)} trigger terms duplicate EntityHints surfaces",
                    recommendation="global_trigger terms should add value beyond entity surfaces; consider removing duplicates",
                    bloat_type="REDUNDANT",
                ))

        score = self._compute_score(issues, len(gt.required_slot_types) + 3)
        return LayerResult("slot_completeness", "Slot Completeness", score, issues, minimality, passed)

    def _compute_score(self, issues, total_checks):
        if total_checks == 0:
            return 1.0
        penalty = sum({"HIGH": 3, "MEDIUM": 2, "LOW": 1}[i.priority] for i in issues)
        return max(0.0, round(1.0 - (penalty / (total_checks * 3)), 2))


# -----------------------------------------------------------------------
# Layer 5 — Graph Spec Correctness
# -----------------------------------------------------------------------

class GraphSpecValidator:

    def validate(self, intent: dict, gt: QuestionGroundTruth) -> LayerResult:
        issues = []
        minimality = []
        passed = []
        graph = intent.get("SlotSpec", {}).get("graph_spec", {})
        if not graph:
            issues.append(Issue(
                issue_id="GS-000",
                layer="graph_spec_correctness",
                priority="HIGH",
                field="SlotSpec.graph_spec",
                message="graph_spec section is missing entirely",
                fix="Add graph_spec with vars, edges, temporal_constraints",
                evidence="graph_spec key absent",
            ))
            return LayerResult("graph_spec_correctness", "Graph Spec Correctness",
                               0.0, issues, minimality, passed)

        vars_list = graph.get("vars", [])
        edges = graph.get("edges", [])
        temporal = graph.get("temporal_constraints", [])
        entity_hints = intent.get("EntityHints", [])
        hint_ids = {h.get("entity_id", "") for h in entity_hints}
        var_names = {v.get("var", "") for v in vars_list}
        issue_idx = 1

        # --- Check 1: hard flag correctness
        for v in vars_list:
            var = v.get("var", "")
            hard = v.get("hard", False)
            hint = v.get("hint", "").lower()
            # If the var represents the investigated claim itself, hard must be False
            if hard and gt.question_type in ["investigative_binary", "compound"]:
                # heuristic: if the hint matches a claim-type entity, it should not be hard
                matching_gt = [e for e in gt.entities
                               if e.surface.lower() in hint or hint in e.surface.lower()]
                for me in matching_gt:
                    if me.canonical_type in ["claim", "legalFramework", "topics"] and hard:
                        issues.append(Issue(
                            issue_id=f"GS-{issue_idx:03d}",
                            layer="graph_spec_correctness",
                            priority="HIGH",
                            field=f"graph_spec.vars[{var}].hard",
                            message=f"var '{var}' (hint='{v.get('hint')}') is hard=true but its existence is what the investigative question is trying to verify",
                            fix=f"Set hard=false for '{var}'",
                            evidence=f"question_type={gt.question_type}, entity_type={me.canonical_type}",
                        ))
                        issue_idx += 1

        # --- Check 2: self-referential edges
        for edge in edges:
            src, dst = edge.get("src", ""), edge.get("dst", "")
            if src == dst:
                issues.append(Issue(
                    issue_id=f"GS-{issue_idx:03d}",
                    layer="graph_spec_correctness",
                    priority="HIGH",
                    field=f"graph_spec.edges[{src}→{dst}]",
                    message=f"Self-referential edge '{src} → {dst}' models nothing — a node cannot relate to itself",
                    fix=f"Split into two distinct vars (e.g., {src}_TRACK_A and {src}_TRACK_B) with AWARE_OF edges in both directions",
                    evidence=f"src='{src}', dst='{dst}'",
                ))
                issue_idx += 1

        # --- Check 3: attribute-as-edge
        for edge in edges:
            rel = edge.get("rel", "")
            src, dst = edge.get("src", ""), edge.get("dst", "")
            if rel in ATTRIBUTE_AS_EDGE:
                issues.append(Issue(
                    issue_id=f"GS-{issue_idx:03d}",
                    layer="graph_spec_correctness",
                    priority="MEDIUM",
                    field=f"graph_spec.edges[{src}→{dst}].rel",
                    message=f"Relation '{rel}' is a node attribute, not a graph edge — using it as rel is a schema misuse",
                    fix=f"Remove edge; add visibility property directly to the '{src}' node var",
                    evidence=f"rel='{rel}'",
                ))
                issue_idx += 1

        # --- Check 4: causal edges on investigative questions
        for edge in edges:
            rel = edge.get("rel", "")
            src, dst = edge.get("src", ""), edge.get("dst", "")
            if rel in CAUSAL_RELATIONS and gt.question_type in ["investigative_binary", "compound"]:
                issues.append(Issue(
                    issue_id=f"GS-{issue_idx:03d}",
                    layer="graph_spec_correctness",
                    priority="MEDIUM",
                    field=f"graph_spec.edges[{src}→{dst}].rel",
                    message=f"Causal relation '{rel}' asserts causation as fact, but the question is investigative — causation is what needs to be found",
                    fix=f"Replace '{rel}' with 'ASSOCIATED_WITH' or 'POTENTIALLY_CAUSED'",
                    evidence=f"question_type={gt.question_type}",
                ))
                issue_idx += 1

        # --- Check 5: concurrent constraints
        needs_concurrent = any(
            c.kind == "CONCURRENT" for c in gt.temporal_constraints
        )
        has_concurrent = any(
            t.get("kind") == "CONCURRENT" for t in temporal
        )
        if needs_concurrent and not has_concurrent:
            issues.append(Issue(
                issue_id=f"GS-{issue_idx:03d}",
                layer="graph_spec_correctness",
                priority="HIGH",
                field="graph_spec.temporal_constraints",
                message="Question contains concurrency signals ('simultaneously', 'parallel tracks') but no CONCURRENT temporal constraint exists",
                fix="Add: { kind: 'CONCURRENT', vars: ['D_PROPOSAL', 'D_ASSESSMENT', 'C_SURVEILLANCE', 'A_DIST'] }",
                evidence=f"gt temporal_constraints: {[c.kind for c in gt.temporal_constraints]}",
            ))
            issue_idx += 1
        elif has_concurrent:
            passed.append("CONCURRENT temporal constraint present")

        # --- Check 6: temporal constraints reference vars not raw datetimes
        for tc in temporal:
            for field_name in ["before", "after", "start", "end"]:
                val = tc.get(field_name, "")
                if val and re.match(r"\d{4}-\d{2}-\d{2}", str(val)):
                    issues.append(Issue(
                        issue_id=f"GS-{issue_idx:03d}",
                        layer="graph_spec_correctness",
                        priority="MEDIUM",
                        field=f"graph_spec.temporal_constraints[{field_name}]",
                        message=f"Temporal constraint uses raw datetime '{val}' — should reference a graph var; time range is already in ScopeSpec",
                        fix="Replace hardcoded datetime with a var reference (e.g., T_TIME) or remove and rely on ScopeSpec.time_filter",
                        evidence=f"field='{field_name}', value='{val}'",
                    ))
                    issue_idx += 1

        # --- Check 7: cross-track awareness modeled
        if gt.cross_track_awareness_required:
            # Look for AWARE_OF or similar edges between distinct person vars
            awareness_edges = [e for e in edges if e.get("rel") in ["AWARE_OF", "COORDINATED_WITH", "INFORMED"]]
            self_ref_awareness = [e for e in awareness_edges if e.get("src") == e.get("dst")]
            valid_awareness = [e for e in awareness_edges if e.get("src") != e.get("dst")]

            if not valid_awareness:
                issues.append(Issue(
                    issue_id=f"GS-{issue_idx:03d}",
                    layer="graph_spec_correctness",
                    priority="HIGH",
                    field="graph_spec.edges",
                    message="Cross-track awareness (second sub-question) not modeled — no valid AWARE_OF edge between distinct person vars",
                    fix="Add vars P_EXEC_TRACK_A and P_EXEC_TRACK_B; add edges P_EXEC_TRACK_A → AWARE_OF → P_EXEC_TRACK_B and reverse",
                    evidence="gt.cross_track_awareness_required=True; no non-self-referential awareness edge found",
                ))
                issue_idx += 1
            else:
                passed.append("Cross-track awareness edge modeled")

        # --- Check 8: vars with no EntityHint grounding
        hint_surfaces = [h.get("surface", "").lower() for h in entity_hints]
        for v in vars_list:
            hint = v.get("hint", "").lower()
            var = v.get("var", "")
            if hint and not any(hint in hs or hs in hint for hs in hint_surfaces):
                issues.append(Issue(
                    issue_id=f"GS-{issue_idx:03d}",
                    layer="graph_spec_correctness",
                    priority="LOW",
                    field=f"graph_spec.vars[{var}].hint",
                    message=f"Var '{var}' (hint='{v.get('hint')}') has no matching EntityHint — orphaned var",
                    fix="Either add a corresponding EntityHint or remove the var if not needed",
                    evidence=f"hint='{hint}' not found in EntityHints surfaces",
                ))
                issue_idx += 1

        # --- Check 9: edges reference declared vars
        for edge in edges:
            for end in ["src", "dst"]:
                val = edge.get(end, "")
                if val and val not in var_names:
                    issues.append(Issue(
                        issue_id=f"GS-{issue_idx:03d}",
                        layer="graph_spec_correctness",
                        priority="HIGH",
                        field=f"graph_spec.edges[{edge.get('src')}→{edge.get('dst')}].{end}",
                        message=f"Edge {end}='{val}' references undeclared var",
                        fix=f"Add var '{val}' to graph_spec.vars or correct the edge reference",
                        evidence=f"declared vars: {sorted(var_names)}",
                    ))
                    issue_idx += 1

        # --- Minimality
        obj = graph.get("objective", {})
        k_alt = obj.get("return_top_k_alternatives", 1)
        if k_alt > 1:
            minimality.append(MinimalityFinding(
                field="graph_spec.objective.return_top_k_alternatives",
                finding=f"return_top_k_alternatives={k_alt} adds complexity without justification for a binary investigative question",
                recommendation="Set to 1 unless multiple traversal strategies are explicitly needed",
                bloat_type="OVER_SPECIFIED",
            ))

        score = self._compute_score(issues, len(vars_list) + len(edges) + 4)
        return LayerResult("graph_spec_correctness", "Graph Spec Correctness", score, issues, minimality, passed)

    def _compute_score(self, issues, total_checks):
        if total_checks == 0:
            return 1.0
        penalty = sum({"HIGH": 3, "MEDIUM": 2, "LOW": 1}[i.priority] for i in issues)
        return max(0.0, round(1.0 - (penalty / (total_checks * 3)), 2))


# -----------------------------------------------------------------------
# Layer 6 — Internal Consistency
# -----------------------------------------------------------------------

class InternalConsistencyValidator:

    def validate(self, intent: dict, gt: QuestionGroundTruth) -> LayerResult:
        issues = []
        minimality = []
        passed = []
        issue_idx = 1

        hints = intent.get("EntityHints", [])
        scope = intent.get("ScopeSpec", {})
        retrieval = intent.get("RetrievalSpec", {})
        slot_spec = intent.get("SlotSpec", {})
        graph = slot_spec.get("graph_spec", {})
        header = intent.get("Header", {})

        hint_ids = {h.get("entity_id", "") for h in hints}
        hint_categories = {h.get("entity_id", ""): h.get("category", "") for h in hints}
        vars_list = graph.get("vars", [])
        var_names = {v.get("var", "") for v in vars_list}
        scope_artifacts = set(scope.get("artifact_types", []))
        slots = slot_spec.get("slots", [])

        # --- Check 1: epoch timestamp
        created_at = header.get("created_at", "")
        if created_at.startswith("1970-01-01"):
            issues.append(Issue(
                issue_id=f"IC-{issue_idx:03d}",
                layer="internal_consistency",
                priority="LOW",
                field="Header.created_at",
                message="created_at is Unix epoch (1970-01-01) — placeholder never replaced",
                fix="Populate with actual creation timestamp",
                evidence=f"created_at='{created_at}'",
            ))
            issue_idx += 1
        else:
            passed.append("Header.created_at is set")

        # --- Check 2: EntityHint ↔ graph var type alignment
        for v in vars_list:
            hint_ref = v.get("hint", "")
            var_type = v.get("type", "")
            # find matching hint
            for h in hints:
                if hint_ref.lower() in h.get("surface", "").lower() or \
                   h.get("surface", "").lower() in hint_ref.lower():
                    hint_cat = h.get("category", "")
                    acceptable = EntityCompletenessValidator.CATEGORY_ALIASES.get(hint_cat, [])
                    # var type should be consistent with hint category
                    # we just flag obvious mismatches (ENTITY_EVENT used as temporal var)
                    if hint_cat == "ENTITY_EVENT" and "TIME" in var_type.upper():
                        issues.append(Issue(
                            issue_id=f"IC-{issue_idx:03d}",
                            layer="internal_consistency",
                            priority="MEDIUM",
                            field=f"EntityHints ↔ graph_spec.vars[{v.get('var')}]",
                            message=f"EntityHint category='{hint_cat}' but graph var type='{var_type}' — type mismatch across sections",
                            fix=f"Align hint category to ENTITY_TIME_RANGE and var type to ENTITY_TEMPORAL_UNCERTAINTY",
                            evidence=f"hint surface='{h.get('surface')}', var='{v.get('var')}'",
                        ))
                        issue_idx += 1

        # --- Check 3: slot artifact types ⊆ scope artifact types
        for slot in slots:
            slot_id = slot.get("slot_id", "?")
            for art in slot.get("allowed_artifact_types", []):
                if art not in scope_artifacts:
                    issues.append(Issue(
                        issue_id=f"IC-{issue_idx:03d}",
                        layer="internal_consistency",
                        priority="HIGH",
                        field=f"SlotSpec.slots[{slot_id}].allowed_artifact_types ↔ ScopeSpec.artifact_types",
                        message=f"Slot '{slot_id}' allows '{art}' but this type is not declared in ScopeSpec.artifact_types",
                        fix=f"Add '{art}' to ScopeSpec.artifact_types",
                        evidence=f"scope artifact_types={list(scope_artifacts)}",
                    ))
                    issue_idx += 1

        # --- Check 4: time filter ↔ temporal entity alignment
        tf_start = scope.get("time_filter", {}).get("start", "")[:4]
        tf_end = scope.get("time_filter", {}).get("end", "")[:4]
        for h in hints:
            surface = h.get("surface", "")
            if re.search(r"\b(20|19)\d{2}\b", surface):
                years_in_hint = re.findall(r"\b(20|19)(\d{2})\b", surface)
                for prefix, suffix in years_in_hint:
                    y = prefix + suffix
                    if tf_start and tf_end:
                        if not (tf_start <= y <= tf_end):
                            issues.append(Issue(
                                issue_id=f"IC-{issue_idx:03d}",
                                layer="internal_consistency",
                                priority="MEDIUM",
                                field=f"EntityHints['{surface}'] ↔ ScopeSpec.time_filter",
                                message=f"Year '{y}' in entity hint is outside declared time_filter [{tf_start}–{tf_end}]",
                                fix="Align time_filter range to include all years referenced in EntityHints",
                                evidence=f"hint='{surface}', time_filter=[{tf_start},{tf_end}]",
                            ))
                            issue_idx += 1

        # --- Check 5: graph vars reference hint surfaces (orphan check)
        for v in vars_list:
            hint = v.get("hint", "").lower()
            if hint:
                hint_surfaces = [h.get("surface", "").lower() for h in hints]
                if not any(hint in hs or hs in hint for hs in hint_surfaces):
                    issues.append(Issue(
                        issue_id=f"IC-{issue_idx:03d}",
                        layer="internal_consistency",
                        priority="MEDIUM",
                        field=f"graph_spec.vars[{v.get('var')}].hint ↔ EntityHints",
                        message=f"Graph var '{v.get('var')}' hint='{v.get('hint')}' has no matching EntityHint surface",
                        fix="Add corresponding EntityHint or correct the hint value",
                        evidence=f"EntityHint surfaces: {[h.get('surface') for h in hints]}",
                    ))
                    issue_idx += 1
                else:
                    passed.append(f"Graph var '{v.get('var')}' has matching EntityHint")

        score = self._compute_score(issues, len(vars_list) + len(slots) + 4)
        return LayerResult("internal_consistency", "Internal Consistency", score, issues, minimality, passed)

    def _compute_score(self, issues, total_checks):
        if total_checks == 0:
            return 1.0
        penalty = sum({"HIGH": 3, "MEDIUM": 2, "LOW": 1}[i.priority] for i in issues)
        return max(0.0, round(1.0 - (penalty / (total_checks * 3)), 2))


# -----------------------------------------------------------------------
# Minimality Auditor (cross-cutting)
# -----------------------------------------------------------------------

class MinimalityAuditor:
    """
    Cross-cutting minimality checks not tied to a single layer.
    Checks for bloat, redundancy, and over-specification.
    """

    def audit(self, intent: dict, gt: QuestionGroundTruth) -> list:
        findings = []

        hints = intent.get("EntityHints", [])
        retrieval = intent.get("RetrievalSpec", {})
        graph = intent.get("SlotSpec", {}).get("graph_spec", {})
        slots = intent.get("SlotSpec", {}).get("slots", [])

        # 1. EntityHints with no corresponding graph var
        var_hints = {v.get("hint", "").lower()
                     for v in graph.get("vars", [])}
        for h in hints:
            surface = h.get("surface", "").lower()
            if not any(surface in vh or vh in surface for vh in var_hints):
                findings.append(MinimalityFinding(
                    field=f"EntityHints['{h.get('surface')}']",
                    finding="EntityHint has no corresponding graph var — it extracts an entity but never uses it in retrieval graph",
                    recommendation="Either add a graph var for this entity or remove the hint if not needed",
                    bloat_type="UNUSED",
                ))

        # 2. Slots with duplicate artifact types
        for slot in slots:
            arts = slot.get("allowed_artifact_types", [])
            if len(arts) != len(set(arts)):
                findings.append(MinimalityFinding(
                    field=f"SlotSpec.slots[{slot.get('slot_id')}].allowed_artifact_types",
                    finding=f"Duplicate artifact types: {arts}",
                    recommendation="Remove duplicate entries",
                    bloat_type="REDUNDANT",
                ))

        # 3. Diagnostics notes too vague
        diag = intent.get("Diagnostics", {})
        notes = diag.get("notes", "")
        if notes and len(notes) < 50:
            findings.append(MinimalityFinding(
                field="Diagnostics.notes",
                finding="Diagnostics notes are too brief to be actionable",
                recommendation="Expand with specific field names, uncertainty sources, and resolution strategies",
                bloat_type="OVER_SPECIFIED",
            ))

        # 4. rule_hits that don't correspond to any slot or check
        rule_hits = diag.get("rule_hits", [])
        slot_types = {s.get("slot_type", "") for s in slots}
        if len(rule_hits) > len(slots) * 2:
            findings.append(MinimalityFinding(
                field="Diagnostics.rule_hits",
                finding=f"{len(rule_hits)} rule_hits vs {len(slots)} slots — disproportionate",
                recommendation="rule_hits should map to actual validation rules; trim to those that fired meaningfully",
                bloat_type="OVER_SPECIFIED",
            ))

        # 5. graph objective secondary goals
        obj = graph.get("objective", {})
        secondary = obj.get("secondary", [])
        if len(secondary) > 2:
            findings.append(MinimalityFinding(
                field="graph_spec.objective.secondary",
                finding=f"{len(secondary)} secondary objectives declared — adds optimization complexity",
                recommendation="Limit to 1-2 secondary objectives most relevant to this question",
                bloat_type="OVER_SPECIFIED",
            ))

        return findings
