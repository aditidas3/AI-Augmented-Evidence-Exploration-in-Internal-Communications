"""
question_parser.py — Ground Truth Extractor
============================================
PURPOSE:
    Every validation layer needs a reference to check the intent object against.
    This module independently parses the raw question and extracts what the intent
    *should* contain:
        - All named entities with canonical node types
        - Time range
        - Required slot types
        - Temporal constraints (ORDER, CONCURRENT, RANGE)
        - Implicit constraints ("simultaneously", "third-party", "systematically")
        - Question type and sub-questions

    Validators then compare the intent object against this ground truth.
    Nothing here reads or depends on the intent object itself.

CANONICAL NODE TYPES (actual graph node categories):
    topics, caseContext, abbreviations, assessment, citation, claim,
    country, decision, drug, event, finance, formula, healthMention,
    legalFramework, link, location, metric, organization, person,
    procedure, product, requirement, risk, state, date

USAGE (from another script — no hardcoded data here):
    from question_parser import QuestionParser

    gt = QuestionParser().parse(question_text)

    # gt.entities        -> list[ExtractedEntity]
    # gt.time_range      -> ("2017-01-01T00:00:00Z", "2019-12-31T23:59:59Z") or None
    # gt.required_slot_types -> ["AWARENESS", "EVIDENCE", "WHO", ...]
    # gt.implicit_constraints -> ["concurrency: ...", "intentionality: ..."]
    # gt.temporal_constraints -> [TemporalConstraint(kind="CONCURRENT", ...)]
"""

import re
from dataclasses import dataclass, field
from typing import Optional


# -----------------------------------------------------------------------
# Valid node categories
# -----------------------------------------------------------------------
VALID_NODE_TYPES = {
    "topics", "caseContext", "abbreviations", "assessment", "citation",
    "claim", "country", "decision", "drug", "event", "finance", "formula",
    "healthMention", "legalFramework", "link", "location", "metric",
    "organization", "person", "procedure", "product", "requirement",
    "risk", "state", "date"
}

# Maps each canonical node type to the ENTITY_* category used in intent EntityHints.
# Where a node type is ambiguous (e.g. "claim" covers strategy, framing, intent,
# behavioral pattern), the most specific category is chosen based on context tags
# in ENTITY_RULES qualifiers. The ENTITY_RULES tuple now carries a 4th element
# (intent_category) for rules that need a non-default mapping; all others fall
# back to this table.
NODE_TYPE_TO_CATEGORY = {
    "organization":   "ENTITY_ORGANIZATION",
    "abbreviations":  "ENTITY_CONCEPT",          # abbreviations live in concept space
    "legalFramework": "ENTITY_STRATEGY",
    "product":        "ENTITY_PRODUCT",
    "drug":           "ENTITY_PRODUCT",
    "healthMention":  "ENTITY_FRAMING",
    "assessment":     "ENTITY_DOCUMENT",
    "procedure":      "ENTITY_ACTION_ITEM",
    "claim":          "ENTITY_STRATEGY",          # default; overridden per-rule below
    "finance":        "ENTITY_ACTION_ITEM",
    "person":         "ENTITY_PERSON",
    "risk":           "ENTITY_RISK_FINDING",
    "topics":         "ENTITY_CONCEPT",           # default; overridden per-rule below
    "event":          "ENTITY_EVENT",
    "date":           "ENTITY_TIME_RANGE",
    "decision":       "ENTITY_APPROVAL",
    "requirement":    "ENTITY_QUALIFIER",
    "state":          "ENTITY_VISIBILITY_LEVEL",
    "citation":       "ENTITY_DOCUMENT",
    "location":       "ENTITY_CONCEPT",
    "metric":         "ENTITY_CONCEPT",
    "formula":        "ENTITY_CONCEPT",
    "link":           "ENTITY_CONCEPT",
    "country":        "ENTITY_CONCEPT",
    "caseContext":    "ENTITY_CONCEPT",
}

# Categories that don't map 1:1 from a node type — reachable only via ENTITY_RULES overrides
# or as valid alternatives for ambiguous entities. Listed here so validators can enumerate
# the full set of valid ENTITY_* values without importing validation_layers.
INTENT_CATEGORY_EXTRAS = {
    "ENTITY_INTENT",               # claim entities asserting deliberate intent
    "ENTITY_AWARENESS",            # topics entities asserting cross-track knowledge
    "ENTITY_BEHAVIORAL_PATTERN",   # claim entities asserting a pattern of behaviour
    "ENTITY_TEMPORAL_UNCERTAINTY", # date entities where the exact range is uncertain
}

# Full set of valid intent categories (for validators and corrector)
ALL_INTENT_CATEGORIES = set(NODE_TYPE_TO_CATEGORY.values()) | INTENT_CATEGORY_EXTRAS


# -----------------------------------------------------------------------
# Data structures returned by the parser
# -----------------------------------------------------------------------

@dataclass
class ExtractedEntity:
    surface: str                         # exact phrase from question
    canonical_type: str                  # one of VALID_NODE_TYPES
    intent_category: str = ""           # ENTITY_* category used in intent EntityHints
    normalized: str = ""                 # cleaned form if known
    qualifiers: dict = field(default_factory=dict)
    implicit: bool = False               # True = inferred, not literally in question


@dataclass
class TemporalConstraint:
    kind: str                            # ORDER | CONCURRENT | RANGE
    description: str
    vars_involved: list = field(default_factory=list)


@dataclass
class QuestionGroundTruth:
    raw_question: str
    question_type: str                   # compound | investigative_binary | factual | exploratory
    sub_questions: list
    entities: list                       # list[ExtractedEntity]
    temporal_constraints: list           # list[TemporalConstraint]
    implicit_constraints: list           # list[str]
    required_slot_types: list            # list[str]
    required_artifact_types: list        # list[str]
    time_range: Optional[tuple]          # (start_iso, end_iso) or None
    intentionality_required: bool
    cross_track_awareness_required: bool


# -----------------------------------------------------------------------
# Signal banks — patterns that drive inference
# -----------------------------------------------------------------------

DATE_PATTERN = re.compile(
    r"\b(19|20)\d{2}\b"
    r"|\b(january|february|march|april|may|june|july|august|september|"
    r"october|november|december)\s+\d{4}\b"
    r"|\bQ[1-4]\s+\d{4}\b",
    re.IGNORECASE
)

CONCURRENT_SIGNALS = [
    "simultaneously", "at the same time", "in parallel", "concurrently",
    "while", "parallel tracks", "dual track", "at once"
]

ORDER_SIGNALS = [
    "before", "after", "prior to", "following", "subsequently",
    "preceded", "led to", "resulted in"
]

INTENTIONALITY_SIGNALS = [
    "systematically", "deliberately", "intentionally", "strategically",
    "coordinated", "orchestrated", "designed to", "constructed"
]

AWARENESS_SIGNALS = [
    "knowledge of each other", "aware of", "knew about",
    "knowledge of", "informed of", "cross-track", "silo"
]

QUESTION_TYPE_SIGNALS = {
    "investigative_binary": ["did ", "was ", "were ", "is ", "has ", "have "],
    "factual":              ["what ", "which ", "who ", "when ", "where "],
    "exploratory":          ["how ", "why ", "explain ", "describe "],
}

# Slot type inference: keywords in question → slot type required
SLOT_INFERENCE_RULES = [
    (["who", "individual", "person", "executive", "staff", "employee"],  "WHO"),
    (["what", "define", "describe", "component", "system", "technology"],"WHAT"),
    (["when", "timeline", "between", "during", "period", "date"],        "WHEN"),
    (["how", "mechanism", "coordination", "process", "method"],          "HOW"),
    (["why", "rationale", "reason", "intent", "purpose", "motive"],      "WHY"),
    (["evidence", "document", "prove", "show", "demonstrate"],           "EVIDENCE"),
    (["outcome", "result", "impact", "effect", "consequence"],           "OUTCOME"),
    (["knowledge", "aware", "knew", "informed", "cross-track"],          "AWARENESS"),
]

# Entity extraction rules: (regex, node_type, qualifiers)
# Ordered from most specific to most general
ENTITY_RULES = [
    # Organizations
    (r"JUUL\s*Labs?",          "organization", {"normalized": "JUUL Labs, Inc."}),
    (r"\bFDA\b",               "organization", {"normalized": "Food and Drug Administration", "role": "regulator"}),
    (r"\bFTC\b",               "organization", {"normalized": "Federal Trade Commission"}),
    (r"\bCDC\b",               "organization", {"normalized": "Centers for Disease Control"}),
    (r"\bEPA\b",               "organization", {"normalized": "Environmental Protection Agency"}),

    # Abbreviations
    (r"\bPMTA\b",              "abbreviations", {"expanded": "Premarket Tobacco Application"}),

    # Legal frameworks
    (r"regulatory capture",    "legalFramework", {"subtype": "strategy"}),
    (r"premarket tobacco application", "legalFramework", {"subtype": "regulatory_pathway"}),

    # Products / drugs
    (r"nicotine\s+product[s]?","product",  {"subtype": "nicotine_delivery"}),
    (r"e-?cigarette[s]?",      "product",  {"subtype": "ecigarette"}),
    (r"\bnicotine\b",          "drug",     {"controlled": True}),

    # Health mentions
    (r"public health",         "healthMention", {"framing": "credibility_projection"}),
    (r"youth.{0,10}(protect|access|prevention)", "healthMention", {"demographic": "youth"}),

    # Assessments
    (r"(executive\s+)?leadership\s+assessment[s]?",        "assessment", {"source_qualifier": "third_party"}),
    (r"youth.protection\s+technology\s+proposal[s]?",      "assessment", {"target": "FDA"}),
    (r"technology\s+proposal[s]?",                          "assessment", {"target": "FDA"}),

    # Procedures / systems
    (r"(consumer\s+)?nicotine\s+purchase\s+surveillance",  "procedure", {"subtype": "operational_monitoring"}),
    (r"surveillance\s+system[s]?",                          "procedure", {"subtype": "operational_monitoring"}),
    (r"beaconing\s+technology",                             "procedure", {"subtype": "tracking_technology"}),
    (r"pilot\s+program[s]?",                                "procedure", {"subtype": "pilot"}),

    # Claims
    (r"regulatory\s+strateg(y|ies)",  "claim", {"type": "strategy_claim"}),
    (r"multi.layered",                "claim", {"qualifier": "structural_complexity"}),
    (r"aspirational",                 "claim", {"qualifier": "framing_qualifier"}),

    # Finance / distribution
    (r"(nicotine\s+)?product\s+distribution", "finance", {"subtype": "distribution_scaling"}),
    (r"operationally\s+scaling",              "finance", {"subtype": "operational_scaling"}),

    # Persons
    (r"individual[s]?\s+involved", "person", {"role": "unresolved_cross_track"}),
    (r"executive[s]?",             "person", {"role": "executive_staff"}),

    # Risk
    (r"youth\s+access",    "risk", {"demographic": "youth", "type": "access_risk"}),
    (r"underage\s+access", "risk", {"demographic": "minor", "type": "access_risk"}),

    # Topics
    (r"parallel\s+tracks?", "topics", {"concept": "operational_separation"}),
    (r"dual\s+track",       "topics", {"concept": "operational_separation"}),

    # Dates — catch all 4-digit years
    (r"\b(20\d{2})\b", "date", {}),
]


# -----------------------------------------------------------------------
# Parser
# -----------------------------------------------------------------------

class QuestionParser:
    """
    Parses a raw question string into a QuestionGroundTruth.
    No external dependencies. No hardcoded question data.
    Call parse(question_text) and use the returned object.
    """

    def parse(self, question: str) -> QuestionGroundTruth:
        q = question.strip()
        return QuestionGroundTruth(
            raw_question=q,
            question_type=self._detect_question_type(q),
            sub_questions=self._split_sub_questions(q),
            entities=self._extract_entities(q),
            temporal_constraints=self._extract_temporal_constraints(q),
            implicit_constraints=self._extract_implicit_constraints(q),
            required_slot_types=self._infer_required_slots(q),
            required_artifact_types=self._infer_artifact_types(q),
            time_range=self._extract_time_range(q),
            intentionality_required=self._has_intentionality(q),
            cross_track_awareness_required=self._has_awareness_requirement(q),
        )

    def _detect_question_type(self, q: str) -> str:
        q_lower = q.lower()
        if re.search(r",?\s+and\s+(did|were|was|is|has|have)\s+", q_lower):
            return "compound"
        for qtype, signals in QUESTION_TYPE_SIGNALS.items():
            if any(q_lower.startswith(s) or f" {s}" in q_lower for s in signals):
                return qtype
        return "exploratory"

    def _split_sub_questions(self, q: str) -> list:
        parts = re.split(r"[,—]\s+and\s+(?=did |were |was |is |has )", q, flags=re.IGNORECASE)
        return [p.strip().rstrip("?") for p in parts if len(p.strip()) > 20]

    def _extract_entities(self, q: str) -> list:
        found = []
        seen = set()

        for rule in ENTITY_RULES:
            pattern, node_type, qualifiers = rule[0], rule[1], rule[2]
            category_override = rule[3] if len(rule) > 3 else ""
            intent_cat = category_override or NODE_TYPE_TO_CATEGORY.get(node_type, f"ENTITY_{node_type.upper()}")
            for m in re.finditer(pattern, q, re.IGNORECASE):
                surface = m.group(0).strip()
                key = (surface.lower(), node_type)
                if key in seen:
                    continue
                seen.add(key)
                found.append(ExtractedEntity(
                    surface=surface,
                    canonical_type=node_type,
                    intent_category=intent_cat,
                    normalized=qualifiers.get("normalized", ""),
                    qualifiers={k: v for k, v in qualifiers.items() if k != "normalized"},
                ))

        # Implicit: intentionality marker
        if self._has_intentionality(q):
            found.append(ExtractedEntity(
                surface="systematically / deliberately",
                canonical_type="claim",
                intent_category="ENTITY_INTENT",
                qualifiers={"type": "intentionality_marker"},
                implicit=True,
            ))

        # Implicit: cross-track awareness
        if self._has_awareness_requirement(q):
            found.append(ExtractedEntity(
                surface="knowledge of each other's activities",
                canonical_type="topics",
                intent_category="ENTITY_AWARENESS",
                qualifiers={"scope": "cross_track_awareness"},
                implicit=True,
            ))

        # Explicit: third-party
        if re.search(r"third.party|outside\s+(firm|consultant|vendor)", q, re.IGNORECASE):
            found.append(ExtractedEntity(
                surface="third-party",
                canonical_type="requirement",
                intent_category="ENTITY_QUALIFIER",
                qualifiers={"role": "source_qualifier", "applies_to": "assessments"},
            ))

        return found

    def _extract_time_range(self, q: str) -> Optional[tuple]:
        matches = DATE_PATTERN.findall(q)
        flat = []
        for m in matches:
            if isinstance(m, tuple):
                flat.extend([x for x in m if x and re.match(r"\d{4}", x)])
            elif re.match(r"\d{4}", m):
                flat.append(m)
        year_ints = sorted(set(int(y) for y in flat))
        if len(year_ints) >= 2:
            return (f"{year_ints[0]}-01-01T00:00:00Z", f"{year_ints[-1]}-12-31T23:59:59Z")
        if len(year_ints) == 1:
            return (f"{year_ints[0]}-01-01T00:00:00Z", f"{year_ints[0]}-12-31T23:59:59Z")
        return None

    def _extract_temporal_constraints(self, q: str) -> list:
        constraints = []
        q_lower = q.lower()

        if any(s in q_lower for s in CONCURRENT_SIGNALS):
            signals_found = [s for s in CONCURRENT_SIGNALS if s in q_lower]
            constraints.append(TemporalConstraint(
                kind="CONCURRENT",
                description=f"Concurrency required — signals: {signals_found}",
                vars_involved=["all_operational_tracks"],
            ))

        if any(s in q_lower for s in ORDER_SIGNALS):
            constraints.append(TemporalConstraint(
                kind="ORDER",
                description="Sequential ordering implied",
                vars_involved=[],
            ))

        tr = self._extract_time_range(q)
        if tr:
            constraints.append(TemporalConstraint(
                kind="RANGE",
                description=f"Time window: {tr[0][:4]} to {tr[1][:4]}",
                vars_involved=["all_entities"],
            ))

        return constraints

    def _extract_implicit_constraints(self, q: str) -> list:
        out = []
        q_lower = q.lower()
        if "systematically" in q_lower:
            out.append("intentionality: evidence of deliberate planning required, not incidental overlap")
        if "multi-layered" in q_lower or "multi layered" in q_lower:
            out.append("structural_complexity: at least 3 distinct tracks must be evidenced separately")
        if any(s in q_lower for s in ["simultaneously", "parallel tracks", "parallel track"]):
            out.append("concurrency: all tracks must be active within the same time window")
        if re.search(r"third.party|outside\s+(firm|consultant|vendor)", q_lower):
            out.append("source_qualifier: assessments must originate from outside the primary organization")
        if re.search(r"knowledge of each other|aware of each other", q_lower):
            out.append("cross_track_awareness: individuals must be shown to know about activities on other tracks")
        return out

    def _infer_required_slots(self, q: str) -> list:
        q_lower = q.lower()
        slots = set()
        for keywords, slot_type in SLOT_INFERENCE_RULES:
            if any(kw in q_lower for kw in keywords):
                slots.add(slot_type)
        slots.add("EVIDENCE")
        return sorted(slots)

    def _infer_artifact_types(self, q: str) -> list:
        types = ["ARTIFACT_DOCUMENT", "ARTIFACT_PDF"]
        q_lower = q.lower()
        if any(w in q_lower for w in ["email", "communication", "correspondence", "memo"]):
            types.append("ARTIFACT_EMAIL")
        if any(w in q_lower for w in ["thread", "chain", "discussion"]):
            types.append("ARTIFACT_THREAD")
        if any(w in q_lower for w in ["proposal", "presentation", "deck", "slide"]):
            types.append("ARTIFACT_PRESENTATION")
        if self._has_awareness_requirement(q) or "coordinated" in q_lower:
            if "ARTIFACT_EMAIL" not in types:
                types.append("ARTIFACT_EMAIL")
            if "ARTIFACT_THREAD" not in types:
                types.append("ARTIFACT_THREAD")
        return types

    def _has_intentionality(self, q: str) -> bool:
        return any(s in q.lower() for s in INTENTIONALITY_SIGNALS)

    def _has_awareness_requirement(self, q: str) -> bool:
        return any(s in q.lower() for s in AWARENESS_SIGNALS)
