"""
align/infrastructure/adapters.py
Family-specific artifact adapters for anchor extraction and mention extraction.
"""

from __future__ import annotations
import re
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..core_types import (
    Anchor, Mention, IntentObject, AnchorPathComponent,
    PageComponent, ParagraphComponent, FigureComponent,
    TableComponent, RowComponent, ColComponent,
    SlideComponent, TextFrameComponent, BulletComponent,
    MessageComponent, HeaderComponent, BodyComponent,
    SpeakerNotesComponent,
)
from ...operators.configs import AlignConfig

logger = logging.getLogger(__name__)


def _coerce_text(value: Any) -> str:
    """Best-effort conversion of heterogeneous payload values into plain text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts = [_coerce_text(item).strip() for item in value]
        return " ".join(part for part in parts if part)
    if isinstance(value, dict):
        preferred_fields = (
            "text",
            "body",
            "subject",
            "title",
            "name",
            "summary",
            "description",
            "caption",
            "notes",
            "fullForm",
            "abbvName",
        )
        parts = [_coerce_text(value.get(field)).strip() for field in preferred_fields]
        joined = " ".join(part for part in parts if part)
        if joined:
            return joined
        return str(value)
    return str(value)


def _join_text_parts(parts: List[Any], *, separator: str = " ") -> str:
    return separator.join(
        text for text in (_coerce_text(part).strip() for part in parts) if text
    )


def _as_part_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


# ============================================================
# Mention Extraction Engine (shared across adapters)
# ============================================================

class MentionExtractor:
    """
    Extracts all semantic elements from a text unit.
    
    This is query-independent: it extracts every recognizable
    entity, concept, strategy reference, risk finding, etc.
    """

    # Category patterns — in production these would be NER model calls;
    # here we define rule-based extractors that can be replaced.
    CATEGORY_PRIORITY: Dict[str, int] = {
        "ENTITY_DRUG": 100,
        "ENTITY_ORGANIZATION": 98,
        "ENTITY_LOCATION": 97,
        "ENTITY_ROLE": 96,
        "ENTITY_POPULATION": 94,
        "ENTITY_PERSON": 90,
        "ENTITY_POLICY": 80,
        "ENTITY_EVENT": 75,
        "ENTITY_ACTION_ITEM": 72,
        "ENTITY_CLAIM": 70,
        "ENTITY_RISK_FINDING": 65,
        "ENTITY_STRATEGY": 60,
        "ENTITY_DOCUMENT": 58,
        "ENTITY_CONCEPT": 55,
        "ENTITY_FRAMING": 50,
    }

    PERSON_DISALLOWED_TOKENS = {
        "all",
        "combined",
        "clinical",
        "conclusion",
        "concussion",
        "company",
        "companies",
        "background",
        "activity",
        "behaviors",
        "group",
        "team",
        "employees",
        "committee",
        "board",
        "center",
        "centers",
        "clinic",
        "cohort",
        "condition",
        "conditions",
        "county",
        "disease",
        "disorders",
        "discussion",
        "division",
        "department",
        "disorder",
        "document",
        "documents",
        "operations",
        "health",
        "hospital",
        "index",
        "indices",
        "injury",
        "injuries",
        "institute",
        "institutes",
        "introduction",
        "inventory",
        "inventories",
        "intensity",
        "medical",
        "measure",
        "measures",
        "methods",
        "military",
        "leadership",
        "program",
        "initiative",
        "meeting",
        "eligibility",
        "criteria",
        "sample",
        "size",
        "calculation",
        "oversight",
        "structure",
        "continuous",
        "positive",
        "airway",
        "pressure",
        "manual",
        "validity",
        "symbol",
        "digit",
        "modalities",
        "premorbid",
        "function",
        "behaviour",
        "behavior",
        "duration",
        "disturbances",
        "physical",
        "incident",
        "assessing",
        "aged",
        "component",
        "personnel",
        "focus",
        "profit",
        "compliance",
        "regulatory",
        "law",
        "working",
        "follow",
        "fitness",
        "acquisition",
        "close",
        "proposal",
        "proposed",
        "executive",
        "framework",
        "outcome",
        "outcomes",
        "population",
        "populations",
        "post",
        "protocol",
        "questionnaire",
        "questionnaires",
        "research",
        "senior",
        "results",
        "scale",
        "scales",
        "severity",
        "state",
        "stress",
        "study",
        "studies",
        "syndrome",
        "syndromes",
        "attorney",
        "the",
        "therapy",
        "this",
        "treatment",
        "trial",
        "university",
        "veteran",
        "veterans",
        "road",
        "street",
        "drive",
        "avenue",
        "boulevard",
        "blvd",
        "lane",
        "court",
        "industry",
        "naval",
    }

    ORGANIZATION_KEYWORDS = {
        "inc",
        "corp",
        "corporation",
        "llc",
        "ltd",
        "co",
        "company",
        "pharma",
        "pharmaceuticals",
        "group",
        "partners",
        "associates",
        "association",
        "board",
        "committee",
        "center",
        "centers",
        "clinic",
        "team",
        "employees",
        "employee",
        "patient",
        "patients",
        "customer",
        "customers",
        "consumer",
        "consumers",
        "caregiver",
        "caregivers",
        "member",
        "members",
        "staff",
        "leader",
        "leaders",
        "pharmacist",
        "pharmacists",
        "hospital",
        "institute",
        "institutes",
        "operations",
        "medical",
        "research",
        "university",
    }

    PERSON_DISALLOWED_PHRASES = (
        "bedside delivery patients",
        "behavioral therapy",
        "clinical trial",
        "delivery patients",
        "medical center",
        "post-concussion",
        "post traumatic",
        "research center",
        "severity index",
        "stress disorder",
        "systematic review",
        "team members",
        "veteran populations",
    )

    EXPANDABLE_NOMINAL_CATEGORIES = {
        "ENTITY_ORGANIZATION",
        "ENTITY_ROLE",
        "ENTITY_POLICY",
        "ENTITY_EVENT",
        "ENTITY_ACTION_ITEM",
        "ENTITY_DOCUMENT",
        "ENTITY_CONCEPT",
        "ENTITY_STRATEGY",
    }

    DOCUMENT_FILE_PATTERN = re.compile(
        r'\b[A-Za-z0-9][A-Za-z0-9&()/_ .-]{2,80}\.(?:docx?|pdf|pptx?|xlsx?|xls|txt)\b',
        re.I,
    )

    NOMINAL_LEFT_STOPWORDS = {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "but",
        "by",
        "for",
        "from",
        "have",
        "has",
        "had",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "my",
        "of",
        "on",
        "or",
        "our",
        "re",
        "re:",
        "regarding",
        "regards",
        "since",
        "that",
        "the",
        "their",
        "there",
        "these",
        "this",
        "those",
        "to",
        "was",
        "we",
        "were",
        "will",
        "would",
        "can",
        "could",
        "may",
        "might",
        "must",
        "should",
        "with",
        "you",
        "your",
        "consider",
        "develop",
        "discuss",
        "draft",
        "enter",
        "gather",
        "help",
        "implement",
        "made",
        "make",
        "reduce",
        "review",
        "send",
        "show",
        "support",
        "working",
    }

    NOMINAL_LEFT_TRIM_WORDS_POST = {
        "additional",
        "any",
        "few",
        "many",
        "more",
        "multiple",
        "other",
        "our",
        "own",
        "several",
        "such",
        "various",
    }

    GENERIC_CONCEPT_SINGLETONS = {
        "attachment",
        "attachments",
        "document",
        "documents",
        "form",
        "forms",
        "materials",
        "report",
        "reports",
        "summary",
    }

    ROLE_NOISE_TOKENS = {
        "announcing",
        "announcement",
        "final",
        "next",
        "proposed",
    }

    CATEGORY_PATTERNS: Dict[str, List[re.Pattern]] = {
        "ENTITY_PERSON": [
            re.compile(r'\b(?:Dr\.?|Mr\.?|Ms\.?|Mrs\.?)\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b'),
            re.compile(
                r'\b[A-Z][a-z]+(?:\s+[A-Z]\.)\s+[A-Z][a-z]+\b'
            ),
            re.compile(
                r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){2,3}\b'
            ),
            re.compile(r'\b[A-Z][a-z]+\s+[A-Z][a-z]+\b'),  # Two-word proper nouns
        ],
        "ENTITY_ORGANIZATION": [
            re.compile(r'\b(?:FDA|CDC|WHO|DEA|DOJ|CMS|HHS)\b'),
            re.compile(
                r'\b[A-Z][A-Za-z&.\'-]+(?:\s+[A-Z][A-Za-z&.\'-]+){0,3}\s+'
                r'(?:Inc\.?|Corp\.?|LLC|Ltd\.?|Pharma|Pharmaceuticals|'
                r'Company|Co\.?|Corporation|Group|Committee|Board|Association|Partners)\b'
            ),
        ],
        "ENTITY_RISK_FINDING": [
            re.compile(r'\b(?:abuse\s+potential|adverse\s+event|side\s+effect|'
                       r'safety\s+concern|health\s+risk|addiction|dependence|'
                       r'withdrawal|overdose|diversion|toxicity|mortality|death|'
                       r'risk|abuse|red-?flag|pharmacy-?shopping|doctor-?shopping|'
                       r'inappropriate\s+prescriptions?|suspicious\s+prescriptions?)\b', re.I),
        ],
        "ENTITY_STRATEGY": [
            re.compile(r'\b(?:messaging|strategy|initiative|program|campaign|outreach|'
                       r'framing|reframing|positioning|talking\s+points|counter-?messaging|'
                       r'downplay|minimize|suppress|withhold|avoid|delay|omit)\b', re.I),
            re.compile(
                r'\b(?:[Ff]ocus\s+on\s+(?:[A-Z][a-z-]*|[a-z][a-z-]*)(?:\s+(?:[A-Z][a-z-]*|[a-z][a-z-]*))?'
                r'|[Gg]ood\s+[Ff]aith\s+(?:[A-Z][a-z-]*|[a-z][a-z-]*)(?:\s+(?:[A-Z][a-z-]*|[a-z][a-z-]*))?)\b'
            ),
        ],
        "ENTITY_DRUG": [
            re.compile(r'\b(?:OxyContin|oxycodone|fentanyl|Actiq|Fentora|morphine|'
                       r'hydrocodone|Vicodin|Percocet|methadone|buprenorphine|'
                       r'Suboxone|naloxone|Narcan)\b', re.I),
        ],
        "ENTITY_LOCATION": [
            re.compile(r'\b(?:Vietnam|United\s+States|China|India|'
                       r'[A-Z][a-z]+\s+County|[A-Z][a-z]+\s+State)\b'),
            re.compile(
                r'\b[A-Z][A-Za-z.\'-]+(?:\s+[A-Z][A-Za-z.\'-]+)?\s+'
                r'(?:Road|Street|Drive|Avenue|Boulevard|Blvd|Lane|Court|Way)\b'
            ),
        ],
        "ENTITY_POLICY": [
            re.compile(r'\b(?:REMS|labeling|label\s+change|warning|black\s+box|'
                       r'guidance|regulation|resolution|policy|requirement|'
                       r'compliance|retention)\b', re.I),
        ],
        "ENTITY_EVENT": [
            re.compile(r'\b(?:meeting|conference|hearing|trial|launch|recall|'
                       r'approval|submission|audit|investigation|spike)\b', re.I),
        ],
        "ENTITY_ACTION_ITEM": [
            re.compile(
                r'\b(?:operational\s+response|next\s+steps|implementation|'
                r'move\s+forward|follow(?:-?\s*up)|share\s+results|'
                r'forward\s+comments?|review\s+the\s+attachments|'
                r'collect(?:ed|ing)?\s+and\s+analy(?:ze|zed|zing|sis)|'
                r'left\s+unresolved|unresolved|silence)\b',
                re.I,
            ),
            re.compile(
                r'\b(?:review|implement|implementation|respond|response|'
                r'address(?:ed|ing)?|escalat(?:e|ed|ing)|collect|analy(?:ze|sis|zed|zing)|'
                r'recirculate|follow(?:-?\s*up)?)\b',
                re.I,
            ),
        ],
        "ENTITY_ROLE": [
            re.compile(
                r'\b(?:Senior|Associate|Assistant|Deputy|Chief|Executive)\s+'
                r'(?:Attorney|Counsel|Officer|Director|Manager|Analyst|Pharmacist)\b',
                re.I,
            ),
            re.compile(r'\b(?:physician|doctor|prescriber|pharmacist|pharmacists|nurse|'
                       r'sales\s+rep|marketing|legal|compliance|executive|attorney|attorneys|'
                       r'VP|director|directors|manager|managers|analyst|analysts|'
                       r'counsel|officer|officers|supervisor|supervisors)\b', re.I),
        ],
        "ENTITY_DOCUMENT": [
            DOCUMENT_FILE_PATTERN,
            re.compile(
                r'\b(?:web\s+form|web-form|questionnaire|template|templates|'
                r'attachment|attachments|document|documents|memo|memos|'
                r'report|reports|draft|summary|agenda|form|forms)\b',
                re.I,
            ),
        ],
        "ENTITY_CONCEPT": [
            re.compile(
                r'\b(?:PTSD|CPTSD|post-?traumatic\s+stress\s+disorder|'
                r'complex\s+post-?traumatic\s+stress\s+disorder|'
                r'traumatic\s+brain\s+injury|mild\s+traumatic\s+brain\s+injury|'
                r'TBI|brain\s+injury|insomnia|depression|major\s+depression|'
                r'major\s+depressive\s+disorder|anxiety|anxiety\s+disorder|'
                r'generalized\s+anxiety\s+disorder|obesity|short\s+sleep|'
                r'post-?concussive\s+symptoms?|concussion|comorbidit(?:y|ies)|'
                r'complication(?:s)?|symptom(?:s)?|disease(?:s)?|disorder(?:s)?)\b',
                re.I,
            ),
            re.compile(r'\b(?:abuse-?deterrent|off-?label|breakthrough\s+pain|'
                       r'quality\s+of\s+life|market\s+share|prescribing\s+lift|'
                       r'responsible\s+use|noncompliance|tolerance|discomfort)\b', re.I),
            re.compile(
                r'\b(?:training\s+materials?|comments?/questions?|'
                r'plan|plans|materials?)\b',
                re.I,
            ),
        ],
        "ENTITY_FRAMING": [
            re.compile(r'\b(?:misuse|abuse|noncompliance|tolerance|discomfort|'
                       r'pseudo-?addiction|attack|controversy)\b', re.I),
        ],
        "ENTITY_CLAIM": [
            re.compile(r'(?:we\s+cannot\s+say|do\s+not\s+put\s+in\s+writing|'
                       r'low\s+abuse\s+potential|safe\s+and\s+effective|'
                       r'minimal\s+risk|not\s+addictive|lower\s+withdrawal)', re.I),
            re.compile(
                r'\b(?:significant|substantial|marked|sharp)\s+'
                r'(?:increase|decrease|reduction|change)\b',
                re.I,
            ),
            re.compile(
                r'\b(?:potential|possible|likely)\s+'
                r'(?:noncompliance|misuse|diversion|abuse|risk)\b',
                re.I,
            ),
            re.compile(
                r"(?:does(?:\s+not|n't)\s+(?:seem\s+to\s+)?(?:coincide|align)|"
                r"incompatib(?:le|ility)|"
                r"conflict(?:s|ed)?\s+with|"
                r"contradict(?:s|ed)?|"
                r"arguably\s+imply|"
                r"perhaps\s+we\s+should\s+consider)",
                re.I,
            ),
        ],
    }

    @classmethod
    def _clean_token(cls, token: str) -> str:
        return re.sub(r"^[^A-Za-z]+|[^A-Za-z.]+$", "", token).rstrip(".").lower()

    @classmethod
    def _is_hyphen_adjacent(
        cls,
        text: str,
        span_start: int,
        span_end: int,
    ) -> bool:
        if not text:
            return False
        if span_start > 0 and text[span_start - 1] == "-":
            return True
        if span_end < len(text) and text[span_end : span_end + 1] == "-":
            return True
        return False

    @classmethod
    def _is_titlecase_prefix(
        cls,
        text: str,
        span_end: int,
    ) -> bool:
        if not text or span_end >= len(text):
            return False
        trailing = text[span_end:]
        return bool(re.match(r"^\s+[A-Z][a-z]+(?:\b|[.-])", trailing))

    @classmethod
    def _contains_non_person_signal(cls, surface: str) -> bool:
        lowered = str(surface or "").strip().lower()
        if not lowered:
            return False
        return any(phrase in lowered for phrase in cls.PERSON_DISALLOWED_PHRASES)

    @classmethod
    def _is_plausible_person(cls, surface: str) -> bool:
        tokens = [cls._clean_token(token) for token in surface.split()]
        tokens = [token for token in tokens if token]
        if len(tokens) < 2:
            return False
        if len(tokens) > 4:
            return False
        if cls._contains_non_person_signal(surface):
            return False
        if any(token in cls.PERSON_DISALLOWED_TOKENS for token in tokens):
            return False
        if any(token in cls.ORGANIZATION_KEYWORDS for token in tokens):
            return False
        return True

    @classmethod
    def _accept_candidate(cls, category: str, surface: str) -> bool:
        if category == "ENTITY_PERSON":
            return cls._is_plausible_person(surface)
        if category == "ENTITY_ROLE":
            tokens = [cls._clean_token(token) for token in surface.split()]
            tokens = [token for token in tokens if token]
            if not tokens:
                return False
            if len(tokens) >= 3 and any(
                token in cls.ROLE_NOISE_TOKENS for token in tokens
            ):
                return False
            head = tokens[-1]
            if len(tokens) >= 3 and head not in {
                "attorney",
                "attorneys",
                "counsel",
                "officer",
                "officers",
                "director",
                "directors",
                "manager",
                "managers",
                "supervisor",
                "supervisors",
                "pharmacist",
                "pharmacists",
                "reviewer",
                "reviewers",
                "employee",
                "employees",
            }:
                return False
        if category == "ENTITY_DOCUMENT":
            if cls.DOCUMENT_FILE_PATTERN.search(surface):
                return True
            tokens = [cls._clean_token(token) for token in surface.split()]
            tokens = [token for token in tokens if token]
            return len(tokens) >= 2
        if category == "ENTITY_CONCEPT":
            tokens = [cls._clean_token(token) for token in surface.split()]
            tokens = [token for token in tokens if token]
            if len(tokens) == 1 and tokens[0] in cls.GENERIC_CONCEPT_SINGLETONS:
                return False
        return True

    @classmethod
    def _is_expanded_nominal_candidate(cls, candidate: Dict[str, Any]) -> bool:
        qualifiers = candidate.get("qualifiers", {}) or {}
        extraction = qualifiers.get("extraction", {}) or {}
        matched = extraction.get("matched_surface", "")
        expanded = extraction.get("expanded_surface", "")
        return (
            candidate.get("category") in cls.EXPANDABLE_NOMINAL_CATEGORIES
            and bool(matched)
            and bool(expanded)
            and matched.strip().lower() != expanded.strip().lower()
        )

    @classmethod
    def _allow_overlap_pair(
        cls,
        candidate: Dict[str, Any],
        existing: Dict[str, Any],
    ) -> bool:
        categories = {candidate.get("category"), existing.get("category")}
        if "ENTITY_CLAIM" in categories:
            claim = candidate if candidate.get("category") == "ENTITY_CLAIM" else existing
            other = existing if claim is candidate else candidate
            contains_other = (
                claim["span_start"] <= other["span_start"]
                and claim["span_end"] >= other["span_end"]
            )
            if contains_other:
                return True

        if candidate.get("category") == existing.get("category"):
            return False

        expanded = None
        other = None
        if cls._is_expanded_nominal_candidate(candidate):
            expanded = candidate
            other = existing
        elif cls._is_expanded_nominal_candidate(existing):
            expanded = existing
            other = candidate
        if expanded is None or other is None:
            return False

        contains_other = (
            expanded["span_start"] <= other["span_start"]
            and expanded["span_end"] >= other["span_end"]
            and (
                expanded["span_start"] != other["span_start"]
                or expanded["span_end"] != other["span_end"]
            )
        )
        return contains_other

    @classmethod
    def _candidate_token_set(cls, candidate: Dict[str, Any]) -> set[str]:
        tokens = set()
        for token in re.findall(
            r"[A-Za-z0-9]+",
            str(candidate.get("surface", "")).lower(),
        ):
            cleaned = cls._clean_token(token)
            if len(cleaned) <= 1:
                continue
            tokens.add(cleaned)
        return tokens

    @classmethod
    def _is_redundant_nested_nominal(
        cls,
        candidate: Dict[str, Any],
        existing: Dict[str, Any],
    ) -> bool:
        if "ENTITY_CLAIM" in {candidate.get("category"), existing.get("category")}:
            return False

        existing_contains_candidate = (
            existing["span_start"] <= candidate["span_start"]
            and existing["span_end"] >= candidate["span_end"]
            and (
                existing["span_start"] != candidate["span_start"]
                or existing["span_end"] != candidate["span_end"]
            )
        )
        if not existing_contains_candidate:
            return False

        existing_tokens = cls._candidate_token_set(existing)
        candidate_tokens = cls._candidate_token_set(candidate)
        if not existing_tokens or not candidate_tokens:
            return False

        overlap_ratio = len(candidate_tokens & existing_tokens) / max(
            1, len(candidate_tokens)
        )
        if not (candidate_tokens <= existing_tokens or overlap_ratio >= 0.75):
            return False

        existing_priority = cls.CATEGORY_PRIORITY.get(existing.get("category", ""), 0)
        candidate_priority = cls.CATEGORY_PRIORITY.get(candidate.get("category", ""), 0)
        same_category = existing.get("category") == candidate.get("category")

        if same_category and len(existing_tokens) > len(candidate_tokens):
            return True

        if (
            existing_priority > candidate_priority
            and (
                cls._is_expanded_nominal_candidate(existing)
                or existing.get("category") in cls.EXPANDABLE_NOMINAL_CATEGORIES
            )
        ):
            return True

        return False

    @classmethod
    def _expand_nominal_surface(
        cls,
        text: str,
        category: str,
        start: int,
        end: int,
    ) -> Tuple[str, int, int, str]:
        if category not in cls.EXPANDABLE_NOMINAL_CATEGORIES:
            surface = text[start:end].strip()
            return surface, start, end, surface

        token_pattern = re.compile(r"[A-Za-z0-9][A-Za-z0-9&./'-]*")
        tokens = list(token_pattern.finditer(text))
        if not tokens:
            surface = text[start:end].strip()
            return surface, start, end, surface

        head_index = None
        for idx, token in enumerate(tokens):
            if token.start() <= start and token.end() >= end:
                head_index = idx
                break
            if token.start() == start and token.end() == end:
                head_index = idx
                break
        if head_index is None:
            surface = text[start:end].strip()
            return surface, start, end, surface

        expanded_start = tokens[head_index].start()
        expansions = 0
        idx = head_index - 1
        while idx >= 0 and expansions < 6:
            token = tokens[idx]
            gap = text[token.end():expanded_start]
            if gap.strip() and not all(ch in {" ", "\t", "\n", "\r"} for ch in gap):
                break
            token_lower = cls._clean_token(token.group(0))
            if not token_lower or token_lower in cls.NOMINAL_LEFT_STOPWORDS:
                break
            expanded_start = token.start()
            expansions += 1
            idx -= 1

        surface = text[expanded_start:end].strip(" \t\r\n|:;,-")
        surface_start = expanded_start
        if category in cls.EXPANDABLE_NOMINAL_CATEGORIES and surface:
            trim_pattern = re.compile(r"[A-Za-z0-9][A-Za-z0-9&./'-]*")
            trim_tokens = list(trim_pattern.finditer(surface))
            while trim_tokens:
                lead = trim_tokens[0]
                lead_token = cls._clean_token(lead.group(0))
                if lead_token not in cls.NOMINAL_LEFT_TRIM_WORDS_POST:
                    break
                surface_start += lead.end()
                surface = surface[lead.end():].lstrip(" \t\r\n|:;,-")
                trim_tokens = list(trim_pattern.finditer(surface))

        expanded_end = surface_start + len(surface)
        matched_surface = text[start:end].strip()
        return surface, surface_start, expanded_end, matched_surface

    @classmethod
    def _expand_claim_surface(
        cls,
        text: str,
        start: int,
        end: int,
    ) -> Tuple[str, int, int, str]:
        if not text:
            return "", start, end, ""

        left_boundary = max(
            text.rfind(".", 0, start),
            text.rfind("!", 0, start),
            text.rfind("?", 0, start),
            text.rfind("\n", 0, start),
            text.rfind(";", 0, start),
        )
        right_candidates = [
            pos
            for pos in (
                text.find(".", end),
                text.find("!", end),
                text.find("?", end),
                text.find("\n", end),
                text.find(";", end),
            )
            if pos != -1
        ]
        right_boundary = min(right_candidates) if right_candidates else len(text)
        claim_start = max(0, left_boundary + 1)
        claim_end = right_boundary

        raw_surface = text[claim_start:claim_end].strip(" \t\r\n|:;,-")
        if not raw_surface:
            matched_surface = text[start:end].strip()
            return matched_surface, start, end, matched_surface

        raw_surface = re.sub(
            r"^(?:and|but|or|so|because|however|therefore|thus|please|thanks|thank you)\b[:,]?\s*",
            "",
            raw_surface,
            flags=re.I,
        ).strip()
        words = raw_surface.split()
        if len(words) > 24:
            raw_surface = " ".join(words[:24]).rstrip(",;:-")

        surface_start = text.find(raw_surface, claim_start, claim_end)
        if surface_start == -1:
            surface_start = claim_start
        surface_end = surface_start + len(raw_surface)
        matched_surface = text[start:end].strip()
        return raw_surface, surface_start, surface_end, matched_surface

    @classmethod
    def extract_candidate_mentions(
        cls,
        text: str,
    ) -> List[Dict[str, Any]]:
        """
        Extract candidate mention spans, resolving exact-span conflicts by
        preferring the more specific category. This prevents phrases like
        "Combined Company" from surviving as both PERSON and ORGANIZATION.
        """
        if not text or not text.strip():
            return []

        candidates: List[Dict[str, Any]] = []
        seen_candidates = set()

        for category, patterns in cls.CATEGORY_PATTERNS.items():
            for pattern in patterns:
                for match in pattern.finditer(text):
                    if category == "ENTITY_CLAIM":
                        surface, span_start, span_end, matched_surface = cls._expand_claim_surface(
                            text,
                            match.start(),
                            match.end(),
                        )
                    else:
                        surface, span_start, span_end, matched_surface = cls._expand_nominal_surface(
                            text,
                            category,
                            match.start(),
                            match.end(),
                        )
                    if (
                        category == "ENTITY_PERSON"
                        and (
                            cls._is_hyphen_adjacent(text, span_start, span_end)
                            or cls._is_titlecase_prefix(text, span_end)
                        )
                    ):
                        continue
                    if len(surface) < 2 or not cls._accept_candidate(category, surface):
                        continue
                    key = (span_start, span_end, category, surface.lower())
                    if key in seen_candidates:
                        continue
                    seen_candidates.add(key)
                    qualifiers = {}
                    if surface != matched_surface:
                        qualifiers["extraction"] = {
                            "matched_surface": matched_surface,
                            "expanded_surface": surface,
                        }
                    candidates.append(
                        {
                            "surface": surface,
                            "category": category,
                            "span_start": span_start,
                            "span_end": span_end,
                            "confidence": 0.7,
                            "qualifiers": qualifiers,
                        }
                    )

        best_by_span: Dict[Tuple[int, int], Dict[str, Any]] = {}
        for candidate in candidates:
            key = (candidate["span_start"], candidate["span_end"])
            existing = best_by_span.get(key)
            if existing is None:
                best_by_span[key] = candidate
                continue
            existing_priority = cls.CATEGORY_PRIORITY.get(existing["category"], 0)
            candidate_priority = cls.CATEGORY_PRIORITY.get(candidate["category"], 0)
            if candidate_priority > existing_priority:
                best_by_span[key] = candidate

        def spans_overlap(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
            return not (
                a["span_end"] <= b["span_start"]
                or b["span_end"] <= a["span_start"]
            )

        prioritized = sorted(
            best_by_span.values(),
            key=lambda item: (
                -cls.CATEGORY_PRIORITY.get(item["category"], 0),
                -(item["span_end"] - item["span_start"]),
                item["span_start"],
            ),
        )

        selected: List[Dict[str, Any]] = []
        for candidate in prioritized:
            suppressed = False
            for existing in selected:
                if not spans_overlap(candidate, existing):
                    continue
                if cls._allow_overlap_pair(candidate, existing):
                    if cls._is_redundant_nested_nominal(candidate, existing):
                        suppressed = True
                        break
                    continue
                existing_priority = cls.CATEGORY_PRIORITY.get(existing["category"], 0)
                candidate_priority = cls.CATEGORY_PRIORITY.get(candidate["category"], 0)
                if existing_priority < candidate_priority:
                    continue
                suppressed = True
                break
            if not suppressed:
                selected.append(candidate)

        return sorted(
            selected,
            key=lambda item: (item["span_start"], item["span_end"], item["category"]),
        )

    @classmethod
    def extract_mentions(
        cls,
        anchor: Anchor,
        text: Optional[str] = None,
    ) -> List[Mention]:
        """
        Extract all semantic elements from the text at this anchor.
        Query-independent: extracts everything recognizable.
        """
        text = text or anchor.raw_text
        if not text or not text.strip():
            return []

        mentions: List[Mention] = []
        for candidate in cls.extract_candidate_mentions(text):
            surface = candidate["surface"]
            mentions.append(
                Mention(
                    mention_id=Mention.generate_id(),
                    anchor_id=anchor.anchor_id,
                    surface=surface,
                    category=candidate["category"],
                    normalized=surface.lower().strip(),
                    confidence=candidate["confidence"],
                    span_start=candidate["span_start"],
                    span_end=candidate["span_end"],
                    qualifiers=candidate.get("qualifiers", {}),
                )
            )

        return mentions


# ============================================================
# Abstract Adapter Interface
# ============================================================

class ArtifactAdapter(ABC):
    """
    Family-specific processing module for anchor and mention extraction.
    
    Contract:
        - Anchors(artifact, intent, config) → List[Anchor]
            Produces structural anchors. Anchors must reference valid
            regions. Total bounded by K_anchors_per_artifact.
        
        - Mentions(artifact, anchors) → Dict[anchor_id, List[Mention]]
            Extracts ALL semantic elements at each anchor, regardless
            of query relevance. Query filtering happens at witness
            construction, not here.
        
        - Text(artifact_data) → str
            Returns full text for indexing.
        
        - Metadata(artifact_data) → dict
            Returns structured metadata for scope filtering.
        
        - Structure(artifact_data) → list
            Returns structural decomposition for anchor addressing.
    """

    def __init__(self, config: AlignConfig):
        self.config = config
        self.extractor = MentionExtractor()

    @abstractmethod
    def extract_anchors(
        self,
        artifact_id: str,
        artifact_data: Dict[str, Any],
        intent: IntentObject,
    ) -> List[Anchor]:
        """Extract structural anchors from this artifact."""
        ...

    def extract_mentions(
        self,
        artifact_id: str,
        anchors: List[Anchor],
    ) -> Dict[str, List[Mention]]:
        """
        Extract all mentions at each anchor.
        Default implementation uses MentionExtractor.
        """
        result = {}
        for anchor in anchors:
            mentions = self.extractor.extract_mentions(anchor)
            result[anchor.anchor_id] = mentions
        return result

    @abstractmethod
    def get_full_text(self, artifact_data: Dict[str, Any]) -> str:
        """Extract full text for Solr indexing."""
        ...

    @abstractmethod
    def get_metadata(self, artifact_data: Dict[str, Any]) -> Dict[str, Any]:
        """Extract structured metadata."""
        ...

    def _score_paragraph_relevance(
        self,
        text: str,
        intent: IntentObject,
    ) -> float:
        """
        Score a text unit's relevance to the intent.
        Uses trigger terms and entity hint surfaces.
        """
        if not text:
            return 0.0

        text_lower = text.lower()
        score = 0.0

        # Trigger term matching
        trigger_terms = intent.slot_spec.global_trigger.terms
        for term in trigger_terms:
            if term.lower() in text_lower:
                score += 1.0

        # Entity hint surface matching
        for hint in intent.entity_hints:
            if hint.surface.lower() in text_lower:
                score += hint.confidence

        # Query expansion matching
        for expansion in intent.retrieval_spec.query_expansions:
            if expansion.lower() in text_lower:
                score += 0.5

        # Normalize by number of matching features
        max_possible = len(trigger_terms) + len(intent.entity_hints) + len(
            intent.retrieval_spec.query_expansions
        )
        if max_possible > 0:
            score = score / max_possible

        return min(score, 1.0)

    def _split_into_paragraphs(self, text: str) -> List[Tuple[int, str]]:
        """Split text into paragraphs, returning (index, text) pairs."""
        paragraphs = []
        current = []
        idx = 0
        for line in text.split("\n"):
            stripped = line.strip()
            if not stripped and current:
                para_text = " ".join(current)
                if len(para_text.strip()) > 10:
                    paragraphs.append((idx, para_text.strip()))
                    idx += 1
                current = []
            elif stripped:
                current.append(stripped)
        if current:
            para_text = " ".join(current)
            if len(para_text.strip()) > 10:
                paragraphs.append((idx, para_text.strip()))
        return paragraphs


# ============================================================
# Email Adapter
# ============================================================

class EmailAdapter(ArtifactAdapter):
    """Adapter for ARTIFACT_EMAIL."""

    def extract_anchors(
        self,
        artifact_id: str,
        artifact_data: Dict[str, Any],
        intent: IntentObject,
    ) -> List[Anchor]:
        anchors = []

        # Header anchor (subject, sender, recipients)
        header_parts = [artifact_data.get("subject", "")]
        header_parts.extend(_as_part_list(artifact_data.get("participants", "")))
        header_text = _join_text_parts(header_parts, separator=" | ")
        if header_text.strip():
            header_anchor = Anchor(
                anchor_id=Anchor.generate_id(),
                artifact_id=artifact_id,
                path=[HeaderComponent()],
                raw_text=header_text.strip(),
                relevance_score=self._score_paragraph_relevance(
                    header_text, intent
                ),
            )
            anchors.append(header_anchor)

        # Body paragraphs
        body = artifact_data.get("body", "")
        for para_idx, para_text in self._split_into_paragraphs(body):
            relevance = self._score_paragraph_relevance(para_text, intent)
            if relevance >= self.config.min_anchor_relevance:
                anchor = Anchor(
                    anchor_id=Anchor.generate_id(),
                    artifact_id=artifact_id,
                    path=[BodyComponent(), ParagraphComponent(index=para_idx)],
                    raw_text=para_text,
                    relevance_score=relevance,
                )
                anchors.append(anchor)

        # Sort by relevance, take top K
        anchors.sort(key=lambda a: a.relevance_score, reverse=True)
        return anchors[: self.config.k_anchors_per_artifact]

    def get_full_text(self, artifact_data: Dict[str, Any]) -> str:
        return _join_text_parts([
            artifact_data.get("subject", ""),
            artifact_data.get("body", ""),
            artifact_data.get("participants", ""),
        ])

    def get_metadata(self, artifact_data: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "subject": artifact_data.get("subject", ""),
            "sender": artifact_data.get("sender", ""),
            "recipients": artifact_data.get("recipients", []),
            "date": artifact_data.get("date", ""),
            "family": "EMAIL",
        }


# ============================================================
# Thread Adapter
# ============================================================

class ThreadAdapter(ArtifactAdapter):
    """Adapter for ARTIFACT_THREAD (sequence of emails)."""

    def extract_anchors(
        self,
        artifact_id: str,
        artifact_data: Dict[str, Any],
        intent: IntentObject,
    ) -> List[Anchor]:
        anchors = []
        messages = artifact_data.get("messages", [])

        for msg_idx, message in enumerate(messages):
            # Message header
            header_parts = [
                message.get("subject", ""),
                message.get("sender", ""),
                *_as_part_list(message.get("recipients", "")),
            ]
            header_text = _join_text_parts(header_parts, separator=" | ")
            if header_text.strip():
                anchors.append(Anchor(
                    anchor_id=Anchor.generate_id(),
                    artifact_id=artifact_id,
                    path=[
                        MessageComponent(index=msg_idx),
                        HeaderComponent(),
                    ],
                    raw_text=header_text.strip(),
                    relevance_score=self._score_paragraph_relevance(
                        header_text, intent
                    ),
                ))

            # Message body paragraphs
            body = message.get("body", "")
            for para_idx, para_text in self._split_into_paragraphs(body):
                relevance = self._score_paragraph_relevance(para_text, intent)
                if relevance >= self.config.min_anchor_relevance:
                    anchors.append(Anchor(
                        anchor_id=Anchor.generate_id(),
                        artifact_id=artifact_id,
                        path=[
                            MessageComponent(index=msg_idx),
                            BodyComponent(),
                            ParagraphComponent(index=para_idx),
                        ],
                        raw_text=para_text,
                        relevance_score=relevance,
                    ))

        anchors.sort(key=lambda a: a.relevance_score, reverse=True)
        return anchors[: self.config.k_anchors_per_artifact]

    def get_full_text(self, artifact_data: Dict[str, Any]) -> str:
        parts = []
        for msg in artifact_data.get("messages", []):
            parts.append(msg.get("subject", ""))
            parts.append(msg.get("sender", ""))
            parts.append(msg.get("recipients", ""))
            parts.append(msg.get("body", ""))
        return _join_text_parts(parts)

    def get_metadata(self, artifact_data: Dict[str, Any]) -> Dict[str, Any]:
        messages = artifact_data.get("messages", [])
        return {
            "message_count": len(messages),
            "participants": list({
                m.get("sender", "") for m in messages if m.get("sender")
            }),
            "date_range": {
                "first": messages[0].get("date", "") if messages else "",
                "last": messages[-1].get("date", "") if messages else "",
            },
            "family": "THREAD",
        }


# ============================================================
# Document Adapter
# ============================================================

class DocumentAdapter(ArtifactAdapter):
    """Adapter for ARTIFACT_DOCUMENT (generic documents, memos, reports)."""

    def extract_anchors(
        self,
        artifact_id: str,
        artifact_data: Dict[str, Any],
        intent: IntentObject,
    ) -> List[Anchor]:
        anchors = []

        # Title anchor
        title = artifact_data.get("title", "")
        if title.strip():
            anchors.append(Anchor(
                anchor_id=Anchor.generate_id(),
                artifact_id=artifact_id,
                path=[PageComponent(index=0), ParagraphComponent(index=0)],
                raw_text=title.strip(),
                relevance_score=self._score_paragraph_relevance(title, intent),
                metadata={"is_title": True},
            ))

        author_text = (artifact_data.get("author", "") or "").strip()
        if author_text:
            anchors.append(Anchor(
                anchor_id=Anchor.generate_id(),
                artifact_id=artifact_id,
                path=[PageComponent(index=0), ParagraphComponent(index=0)],
                raw_text=f"Authors: {author_text}",
                relevance_score=max(self.config.min_anchor_relevance, 0.75),
                metadata={"component_type": "author"},
            ))

        # Body paragraphs
        body = artifact_data.get("body", "")
        for para_idx, para_text in self._split_into_paragraphs(body):
            relevance = self._score_paragraph_relevance(para_text, intent)
            if relevance >= self.config.min_anchor_relevance:
                # Estimate page number (rough: ~3000 chars per page)
                char_offset = body.find(para_text)
                page_num = max(0, char_offset // 3000) if char_offset >= 0 else 0
                anchors.append(Anchor(
                    anchor_id=Anchor.generate_id(),
                    artifact_id=artifact_id,
                    path=[
                        PageComponent(index=page_num),
                        ParagraphComponent(index=para_idx),
                    ],
                    raw_text=para_text,
                    relevance_score=relevance,
                ))

        anchors.sort(key=lambda a: a.relevance_score, reverse=True)
        return anchors[: self.config.k_anchors_per_artifact]

    def get_full_text(self, artifact_data: Dict[str, Any]) -> str:
        return _join_text_parts([
            artifact_data.get("title", ""),
            artifact_data.get("body", ""),
        ])

    def get_metadata(self, artifact_data: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "title": artifact_data.get("title", ""),
            "author": artifact_data.get("author", ""),
            "date": artifact_data.get("date", ""),
            "family": "DOCUMENT",
        }


# ============================================================
# PDF Adapter
# ============================================================

class PDFAdapter(ArtifactAdapter):
    """Adapter for ARTIFACT_PDF (page-structured documents)."""

    def extract_anchors(
        self,
        artifact_id: str,
        artifact_data: Dict[str, Any],
        intent: IntentObject,
    ) -> List[Anchor]:
        anchors = []

        pages = artifact_data.get("pages", [])
        if not pages:
            # Fall back to body-as-single-page
            body = artifact_data.get("body", "")
            pages = [{"page_num": 0, "text": body}]

        author_text = (artifact_data.get("author", "") or "").strip()
        if author_text:
            anchors.append(Anchor(
                anchor_id=Anchor.generate_id(),
                artifact_id=artifact_id,
                path=[PageComponent(index=0), ParagraphComponent(index=0)],
                raw_text=f"Authors: {author_text}",
                relevance_score=max(self.config.min_anchor_relevance, 0.75),
                metadata={"component_type": "author"},
            ))

        for page in pages:
            page_num = page.get("page_num", 0)
            page_text = page.get("text", "")

            # Paragraphs within the page
            for para_idx, para_text in self._split_into_paragraphs(page_text):
                relevance = self._score_paragraph_relevance(para_text, intent)
                if relevance >= self.config.min_anchor_relevance:
                    anchors.append(Anchor(
                        anchor_id=Anchor.generate_id(),
                        artifact_id=artifact_id,
                        path=[
                            PageComponent(index=page_num),
                            ParagraphComponent(index=para_idx),
                        ],
                        raw_text=para_text,
                        relevance_score=relevance,
                    ))

            # Tables within the page
            for table_idx, table in enumerate(page.get("tables", [])):
                table_text = str(table)
                relevance = self._score_paragraph_relevance(table_text, intent)
                if relevance >= self.config.min_anchor_relevance:
                    anchors.append(Anchor(
                        anchor_id=Anchor.generate_id(),
                        artifact_id=artifact_id,
                        path=[
                            PageComponent(index=page_num),
                            TableComponent(index=table_idx),
                        ],
                        raw_text=table_text,
                        relevance_score=relevance,
                        metadata={"is_table": True},
                    ))

            # Figures within the page
            for fig_idx, figure in enumerate(page.get("figures", [])):
                caption = figure.get("caption", "")
                if caption:
                    relevance = self._score_paragraph_relevance(caption, intent)
                    if relevance >= self.config.min_anchor_relevance:
                        anchors.append(Anchor(
                            anchor_id=Anchor.generate_id(),
                            artifact_id=artifact_id,
                            path=[
                                PageComponent(index=page_num),
                                FigureComponent(index=fig_idx),
                            ],
                            raw_text=caption,
                            relevance_score=relevance,
                            metadata={"is_figure": True},
                        ))

        anchors.sort(key=lambda a: a.relevance_score, reverse=True)
        return anchors[: self.config.k_anchors_per_artifact]

    def get_full_text(self, artifact_data: Dict[str, Any]) -> str:
        pages = artifact_data.get("pages", [])
        if pages:
            return _join_text_parts([p.get("text", "") for p in pages])
        return _coerce_text(artifact_data.get("body", ""))

    def get_metadata(self, artifact_data: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "title": artifact_data.get("title", ""),
            "page_count": len(artifact_data.get("pages", [])),
            "date": artifact_data.get("date", ""),
            "family": "PDF",
        }


# ============================================================
# Presentation Adapter
# ============================================================

class PresentationAdapter(ArtifactAdapter):
    """Adapter for ARTIFACT_PRESENTATION and ARTIFACT_PRESENTATION_SLIDE."""

    def extract_anchors(
        self,
        artifact_id: str,
        artifact_data: Dict[str, Any],
        intent: IntentObject,
    ) -> List[Anchor]:
        anchors = []

        slides = artifact_data.get("slides", [])
        if not slides:
            # Fall back to body
            body = artifact_data.get("body", "")
            slides = [{"slide_num": 0, "text_frames": [{"text": body}]}]

        for slide in slides:
            slide_num = slide.get("slide_num", 0)

            # Title of slide
            slide_title = slide.get("title", "")
            if slide_title.strip():
                anchors.append(Anchor(
                    anchor_id=Anchor.generate_id(),
                    artifact_id=artifact_id,
                    path=[
                        SlideComponent(index=slide_num),
                        TextFrameComponent(index=0),
                    ],
                    raw_text=slide_title.strip(),
                    relevance_score=self._score_paragraph_relevance(
                        slide_title, intent
                    ),
                    metadata={"is_slide_title": True},
                ))

            # Text frames / bullets
            for tf_idx, text_frame in enumerate(slide.get("text_frames", [])):
                tf_text = text_frame.get("text", "") if isinstance(text_frame, dict) else str(text_frame)
                bullets = tf_text.split("\n") if tf_text else []

                for bullet_idx, bullet in enumerate(bullets):
                    bullet = bullet.strip()
                    if len(bullet) < 5:
                        continue
                    relevance = self._score_paragraph_relevance(bullet, intent)
                    if relevance >= self.config.min_anchor_relevance:
                        anchors.append(Anchor(
                            anchor_id=Anchor.generate_id(),
                            artifact_id=artifact_id,
                            path=[
                                SlideComponent(index=slide_num),
                                TextFrameComponent(index=tf_idx),
                                BulletComponent(index=bullet_idx),
                            ],
                            raw_text=bullet,
                            relevance_score=relevance,
                        ))

            # Speaker notes
            notes = slide.get("speaker_notes", "")
            if notes.strip():
                relevance = self._score_paragraph_relevance(notes, intent)
                if relevance >= self.config.min_anchor_relevance:
                    anchors.append(Anchor(
                        anchor_id=Anchor.generate_id(),
                        artifact_id=artifact_id,
                        path=[
                            SlideComponent(index=slide_num),
                            SpeakerNotesComponent(),
                        ],
                        raw_text=notes.strip(),
                        relevance_score=relevance,
                        metadata={"is_speaker_notes": True},
                    ))

        anchors.sort(key=lambda a: a.relevance_score, reverse=True)
        return anchors[: self.config.k_anchors_per_artifact]

    def get_full_text(self, artifact_data: Dict[str, Any]) -> str:
        parts = []
        for slide in artifact_data.get("slides", []):
            parts.append(slide.get("title", ""))
            for tf in slide.get("text_frames", []):
                if isinstance(tf, dict):
                    parts.append(tf.get("text", ""))
                else:
                    parts.append(str(tf))
            parts.append(slide.get("speaker_notes", ""))
        if not parts:
            parts.append(artifact_data.get("body", ""))
        return _join_text_parts(parts)

    def get_metadata(self, artifact_data: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "title": artifact_data.get("title", ""),
            "slide_count": len(artifact_data.get("slides", [])),
            "date": artifact_data.get("date", ""),
            "family": "PRESENTATION",
        }


# ============================================================
# Adapter Registry
# ============================================================

# ============================================================
# KG0-native adapter
#
# This adapter reads anchors + mentions directly from the
# Collection → Document → Page → Entity shape produced by
# pipeline/kg0/kg0_from_db.py, rather than parsing free text
# pulled from Solr. One anchor per :Page, one mention per
# Page→Entity edge. Witness text (stored on the edge since
# the KG0 rebuild) becomes the anchor's raw_text.
#
# This is the "right path" from the agentic evidence paper:
# anchors are genuine bounded regions (pages) with grounded
# witness pointers, and mentions carry a direct KG0 entity id
# so Phase 4/5 can traverse the graph without another lookup.
# ============================================================


# Map KG0 top_category → ALIGN mention category. The values on the
# right match the rest of ALIGN's category vocabulary (ENTITY_PERSON
# etc.), so Phase 4 clustering and Phase 6 slot binding don't need
# special-case handling for KG0-derived mentions.
# ============================================================
# KG0 PascalCase label → ALIGN mention category
# ------------------------------------------------------------
# Both `top_category` (free text) and the per-node `labels` array are
# produced by the LLM extractor — so any rule that does substring
# matching on those strings is brittle across re-extractions.
#
# This resolver does the simplest thing that survives LLM drift:
# walk the `labels` list in order (KG0 puts the broadest type first),
# return the first label that exact-matches the closed mapping table
# below. Anything that does not match is `ENTITY_OTHER` — explicit
# "I don't know" rather than a wrong guess.
#
# When a future LLM run invents new top labels, extend the table.
# No keyword rules, no specific-category sniffing.
# ============================================================

_KG0_LABEL_TO_MENTION_CATEGORY: Dict[str, str] = {
    # People & roles
    "Person":            "ENTITY_PERSON",
    "Role":              "ENTITY_ROLE",
    "Occupation":        "ENTITY_ROLE",
    "Population":        "ENTITY_POPULATION",
    "PatientGroup":      "ENTITY_POPULATION",
    "AuthorizedPerson":  "ENTITY_POPULATION",
    "GeneralPopulation": "ENTITY_POPULATION",
    # Organizations
    "Organization":      "ENTITY_ORGANIZATION",
    # Drugs
    "Drug":              "ENTITY_DRUG",
    # Products / technology
    "Product":           "ENTITY_PRODUCT",
    "Software":          "ENTITY_PRODUCT",
    "System":            "ENTITY_PRODUCT",
    "TechnicalEntity":   "ENTITY_PRODUCT",
    # Locations
    "Location":          "ENTITY_LOCATION",
    # Events
    "Event":             "ENTITY_EVENT",
    # Identifiers
    "Identifier":        "ENTITY_IDENTIFIER",
    # Metrics
    "Metric":            "ENTITY_METRIC",
    "FinancialMetric":   "ENTITY_METRIC",
    # Policy / legal / regulation
    "Policy":            "ENTITY_POLICY",
    "Regulation":        "ENTITY_REGULATION",
    "LegalEntity":       "ENTITY_LEGAL",
    "LegalConcept":      "ENTITY_LEGAL",
    # Concepts
    "Concept":           "ENTITY_CONCEPT",
    "BusinessConcept":   "ENTITY_CONCEPT",
    "TechnicalConcept":  "ENTITY_CONCEPT",
    "FinancialConcept":  "ENTITY_CONCEPT",
    "DomainEntity":      "ENTITY_CONCEPT",
    "StudyEntity":       "ENTITY_CONCEPT",
    "DataField":         "ENTITY_CONCEPT",
    "Data":              "ENTITY_CONCEPT",
    # Conditions / clinical
    "Condition":         "ENTITY_CONDITION",
    "ClinicalEntity":    "ENTITY_CLINICAL",
    "AdverseEvent":      "ENTITY_RISK_FINDING",
    "Warning":           "ENTITY_RISK_FINDING",
    # Processes
    "Process":           "ENTITY_PROCESS",
    # Documents / files
    "Document":          "ENTITY_DOCUMENT",
    "DocumentSection":   "ENTITY_DOCUMENT",
    "Record":            "ENTITY_DOCUMENT",
    "DocumentEntity":    "ENTITY_DOCUMENT_REF",
    "DocumentType":      "ENTITY_DOCUMENT_REF",
    "File":              "ENTITY_FILE",
    # Other
    "Claim":             "ENTITY_CLAIM",
    "Topic":             "ENTITY_TOPIC",
}

_KG0_ROLE_PRIORITY_LABELS: Set[str] = {
    "Role",
    "JobTitle",
    "Occupation",
    "EmployeeGroup",
}

_KG0_POPULATION_PRIORITY_LABELS: Set[str] = {
    "Population",
    "PatientGroup",
    "AuthorizedPerson",
    "GeneralPopulation",
}

_KG0_POPULATION_SURFACE_RE = re.compile(
    r"\b(?:"
    r"patients?|"
    r"customers?|"
    r"consumers?|"
    r"caregivers?|"
    r"subjects?|"
    r"participants?|"
    r"population(?:s)?"
    r")\b",
    re.I,
)

_KG0_ROLE_SURFACE_RE = re.compile(
    r"\b(?:"
    r"team\s+members?|"
    r"district\s+leaders?|"
    r"staff|"
    r"employees?|"
    r"supervisors?|"
    r"managers?|"
    r"leaders?|"
    r"pharmacists?|"
    r"prescribers?|"
    r"attorneys?|"
    r"counsels?"
    r")\b",
    re.I,
)


# CamelCase / snake_case / kebab-case / spaced splitter. Splits on:
#   - lower→upper boundary  (`BusinessProcess` -> `Business`, `Process`)
#   - upper→Upper+lower     (`HTTPServer`     -> `HTTP`, `Server`)
#   - whitespace, _, -      (`business_process`, `Business-Process`)
_LABEL_SPLIT_RE = __import__("re").compile(
    r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|[\s_\-]+"
)


def _split_label_tokens(label: str) -> List[str]:
    return [tok for tok in _LABEL_SPLIT_RE.split(label) if tok]


def _kg0_labels_to_mention_category(labels: Sequence[str]) -> str:
    """Resolve an ALIGN mention category from a KG0 node's `labels` list.

    Two-pass exact-match resolution:

    1. **Whole-label exact match.** Walk `labels` in order and return
       the first one that exact-matches `_KG0_LABEL_TO_MENTION_CATEGORY`.
       This is the high-precision path for known canonical labels.
    2. **CamelCase token fallback.** If no whole label matches, walk
       each label, split it into CamelCase tokens, and return the first
       token that exact-matches the table. This catches LLM-invented
       compound labels like `BusinessProcess`, `MedicalCondition`,
       `DrugClass` without ever doing substring matching.

    Returns `ENTITY_OTHER` if neither pass finds a hit. No substring
    matching, no `top_category`/`specific_category` sniffing — both are
    LLM-generated free-text fields and would drift between re-runs.
    """
    if not labels:
        return "ENTITY_OTHER"

    # KG0 may label group/population entities as both Person and Population.
    # ALIGN's ENTITY_PERSON is reserved for named people; population wins.
    if any(label in _KG0_POPULATION_PRIORITY_LABELS for label in labels):
        return "ENTITY_POPULATION"

    # KG0 may label role-like entities as both Person and a more specific
    # role/group label, e.g. ["Person", "JobTitle"] for "Pharmacy
    # Supervisors" or ["Person", "EmployeeGroup"] for "team members".
    # ALIGN's ENTITY_PERSON is reserved for named people; role labels win.
    if any(label in _KG0_ROLE_PRIORITY_LABELS for label in labels):
        return "ENTITY_ROLE"

    # Pass 1: whole-label exact match
    for label in labels:
        mapped = _KG0_LABEL_TO_MENTION_CATEGORY.get(label)
        if mapped is not None:
            return mapped

    # Pass 2: CamelCase token fallback
    for label in labels:
        for token in _split_label_tokens(label):
            mapped = _KG0_LABEL_TO_MENTION_CATEGORY.get(token)
            if mapped is not None:
                return mapped

    return "ENTITY_OTHER"


def _kg0_labels_and_surface_to_mention_category(
    labels: Sequence[str],
    surface: str,
) -> str:
    category = _kg0_labels_to_mention_category(labels)
    if category != "ENTITY_PERSON":
        return category

    normalized_surface = str(surface or "").strip()
    if _KG0_POPULATION_SURFACE_RE.search(normalized_surface):
        return "ENTITY_POPULATION"
    if _KG0_ROLE_SURFACE_RE.search(normalized_surface):
        return "ENTITY_ROLE"
    return category


def _coerce_witness_value(value: Any) -> str:
    """Coerce a Neo4j witness property (string, list, or None) to a
    stripped string. Memgraph occasionally returns list-typed edge
    properties when the same key was set from multiple sources."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        parts = [str(v).strip() for v in value if v is not None and str(v).strip()]
        return " | ".join(dict.fromkeys(parts))
    return str(value).strip()


def _coerce_kg0_scalar(value: Any) -> str:
    """Coerce any KG0 property (string, list, number, None) to a plain
    stripped string. Used for top_category, specific_category, rel_type,
    entity_id, name — all of which can come back list-typed from
    Memgraph when multiple sources set the same key."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        for v in value:
            if v is None:
                continue
            s = str(v).strip()
            if s:
                return s
        return ""
    return str(value).strip()


class KG0NativeAdapter(ArtifactAdapter):
    """
    KG0-native family-agnostic adapter.

    Contract reminder (from ArtifactAdapter):
        - extract_anchors: one Anchor per :Page under this Document.
          ``path`` = [PageComponent(index=page_index)]. ``raw_text`` is
          the ' | '-joined set of distinct witness strings carried by
          that page's outgoing MENTIONS_*/HAS_*/CITES edges. Family
          dispatch (email vs memo vs spreadsheet) is carried in
          ``metadata['page_label']``.

        - extract_mentions: one Mention per Page→Entity edge observed
          during extract_anchors. ``kg0_entity_id`` is populated so
          Phase 4 can skip any re-lookup into KG0.

        - get_full_text / get_metadata: stubs — this adapter is not
          Solr-backed and therefore has no "full text" to hand out.
    """

    def __init__(self, config: AlignConfig, neo4j_store: Any):
        super().__init__(config)
        self.neo4j = neo4j_store
        # Per-run cache keyed on document kg_id so extract_anchors and
        # extract_mentions don't each round-trip to Neo4j.
        self._fetch_cache: Dict[str, List[Dict[str, Any]]] = {}

    # ------------------------------------------------------------

    # Cypher template shared by the single-id and batched fetch
    # paths. Both return rows in the same shape so the cleanup loop
    # below can handle either.
    _PAGE_FETCH_PROJECTION = (
        "       p.kg_id AS page_kg_id, "
        "       p.page_index AS page_index, "
        "       coalesce(p.label, 'unknown') AS page_label, "
        "       coalesce(p.image_path, '') AS page_image, "
        "       raw_mentions "
    )

    _PAGE_FETCH_RAW_MENTIONS_PROJECTION = (
        "collect({"
        "  entity_id: e.kg_id, "
        "  name: e.name, "
        "  top_category: coalesce(e.top_category, ''), "
        "  specific_category: coalesce(e.specific_category, ''), "
        "  labels: labels(e), "
        "  rel_type: type(r), "
        "  witness: coalesce(r.witness, ''), "
        "  confidence: coalesce(r.confidence, '') "
        "}) AS raw_mentions "
    )

    def _fetch_document_pages(self, artifact_id: str) -> List[Dict[str, Any]]:
        """Return one record per :Page under the Document, each with its
        full outgoing-mention list. Witnesses are read from the edges.
        """
        if artifact_id in self._fetch_cache:
            return self._fetch_cache[artifact_id]

        cypher = (
            "MATCH (d:Document {kg_id: $id})-[:HAS_PAGE]->(p:Page) "
            "OPTIONAL MATCH (p)-[r]->(e) "
            "WHERE e.name IS NOT NULL AND NOT 'Abbreviation' IN labels(e) "
            "WITH p, "
            + self._PAGE_FETCH_RAW_MENTIONS_PROJECTION
            + "RETURN "
            + self._PAGE_FETCH_PROJECTION
            + "ORDER BY page_index ASC"
        )
        rows = self.neo4j.execute_cypher(cypher, {"id": artifact_id})
        cleaned = self._normalize_page_rows(rows)
        self._fetch_cache[artifact_id] = cleaned
        return cleaned

    def prefetch_documents(self, artifact_ids: Sequence[str]) -> None:
        """Populate the page cache for many documents in one round-trip.

        Phase 3 calls this before its per-artifact loop. Without it,
        each ``extract_anchors`` call drives a separate Cypher query
        and Phase 3's wall-clock cost grows linearly with the number
        of selected artifacts. The batched query collapses N
        round-trips into 1, which dominated Phase 3's runtime once
        the lexical backend started feeding it 27+ documents.

        Already-cached ids are skipped. Failures fall back to the
        per-artifact path so the pipeline keeps working even if the
        batched query shape ever drifts.
        """
        ids_to_fetch = [
            aid for aid in artifact_ids
            if aid and aid not in self._fetch_cache
        ]
        if not ids_to_fetch:
            return

        cypher = (
            "MATCH (d:Document)-[:HAS_PAGE]->(p:Page) "
            "WHERE d.kg_id IN $ids "
            "OPTIONAL MATCH (p)-[r]->(e) "
            "WHERE e.name IS NOT NULL AND NOT 'Abbreviation' IN labels(e) "
            "WITH d.kg_id AS doc_id, p, "
            + self._PAGE_FETCH_RAW_MENTIONS_PROJECTION
            + "RETURN doc_id, "
            + self._PAGE_FETCH_PROJECTION
            + "ORDER BY doc_id, page_index ASC"
        )
        try:
            rows = self.neo4j.execute_cypher(cypher, {"ids": list(ids_to_fetch)})
        except Exception as exc:
            logger.warning(
                "KG0NativeAdapter.prefetch_documents failed (%s): %s; "
                "falling back to per-artifact fetches.",
                type(exc).__name__,
                exc,
            )
            return

        rows_by_doc: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            doc_id = row.get("doc_id")
            if not doc_id:
                continue
            rows_by_doc.setdefault(doc_id, []).append(row)

        for aid in ids_to_fetch:
            doc_rows = rows_by_doc.get(aid, [])
            self._fetch_cache[aid] = self._normalize_page_rows(doc_rows)

    @staticmethod
    def _normalize_page_rows(
        rows: Sequence[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Drop null-entity placeholders and keep page shells.

        OPTIONAL MATCH yields one row with ``entity_id=null`` when a
        page has no outgoing mention edges. Strip the null but keep
        the shell so ``extract_anchors`` still emits an Anchor for
        the page.
        """
        cleaned: List[Dict[str, Any]] = []
        for rec in rows:
            mentions = [
                m for m in (rec.get("raw_mentions") or [])
                if m and m.get("entity_id")
            ]
            cleaned.append({
                "page_kg_id": rec.get("page_kg_id"),
                "page_index": rec.get("page_index"),
                "page_label": rec.get("page_label") or "unknown",
                "page_image": rec.get("page_image") or "",
                "mentions": mentions,
            })
        return cleaned

    # ------------------------------------------------------------

    def extract_anchors(
        self,
        artifact_id: str,
        artifact_data: Dict[str, Any],
        intent: IntentObject,
    ) -> List[Anchor]:
        rows = self._fetch_document_pages(artifact_id)
        anchors: List[Anchor] = []

        for rec in rows:
            page_index_raw = rec.get("page_index")
            try:
                page_index = int(page_index_raw) if page_index_raw is not None else 0
            except (TypeError, ValueError):
                page_index = 0
            page_label = rec.get("page_label") or "unknown"
            mentions_raw = rec.get("mentions") or []

            # Synthesize anchor text from distinct edge witnesses. Falls
            # back to a list of entity names if every edge on this page
            # had an empty witness string (e.g. pages that existed only
            # in labels.jsonl with no matching entity rows).
            seen_witnesses: List[str] = []
            for m in mentions_raw:
                w = _coerce_witness_value(m.get("witness"))
                if w and w not in seen_witnesses:
                    seen_witnesses.append(w)
            if seen_witnesses:
                raw_text = " | ".join(seen_witnesses)
            else:
                raw_text = " ".join(
                    _coerce_kg0_scalar(m.get("name"))
                    for m in mentions_raw
                    if _coerce_kg0_scalar(m.get("name"))
                )

            relevance = self._score_paragraph_relevance(raw_text, intent)

            # Keep empty-text pages only when the page label itself is
            # relevant (e.g. a slide with no extracted entities but on a
            # presentation the user cares about).
            if not raw_text and relevance == 0.0:
                continue

            anchor = Anchor(
                anchor_id=Anchor.generate_id(),
                artifact_id=artifact_id,
                path=[PageComponent(index=page_index)],
                raw_text=raw_text,
                relevance_score=relevance,
                metadata={
                    "page_label": page_label,
                    "page_kg_id": rec.get("page_kg_id"),
                    "page_image": rec.get("page_image"),
                    "artifact_family": page_label,
                    # Stashed so extract_mentions doesn't re-query Neo4j.
                    "_kg0_mentions": mentions_raw,
                },
            )
            anchors.append(anchor)

        anchors.sort(key=lambda a: (a.relevance_score, a.address), reverse=True)
        return anchors[: self.config.k_anchors_per_artifact]

    # ------------------------------------------------------------

    def extract_mentions(
        self,
        artifact_id: str,
        anchors: List[Anchor],
    ) -> Dict[str, List[Mention]]:
        result: Dict[str, List[Mention]] = {}
        for anchor in anchors:
            stash = anchor.metadata.get("_kg0_mentions") or []
            mentions: List[Mention] = []
            seen_keys: set = set()
            for m in stash:
                name = _coerce_kg0_scalar(m.get("name"))
                entity_id_str = _coerce_kg0_scalar(m.get("entity_id"))
                if not name or not entity_id_str:
                    continue
                # Dedupe on (entity_id, rel_type) within one anchor so the
                # same entity doesn't appear twice if the edge was re-read.
                rel_type = _coerce_kg0_scalar(m.get("rel_type"))
                key = (entity_id_str, rel_type)
                if key in seen_keys:
                    continue
                seen_keys.add(key)

                top_category_str = _coerce_kg0_scalar(m.get("top_category"))
                specific_category_str = _coerce_kg0_scalar(m.get("specific_category"))
                kg0_labels_raw = m.get("labels") or []
                kg0_labels = [
                    str(lbl).strip() for lbl in kg0_labels_raw if str(lbl).strip()
                ]
                category = _kg0_labels_and_surface_to_mention_category(
                    kg0_labels,
                    name,
                )
                _CONF_MAP = {"high": 0.9, "medium": 0.6, "low": 0.3}
                conf_raw = m.get("confidence")
                if isinstance(conf_raw, (list, tuple)):
                    conf_raw = next((v for v in conf_raw if v not in (None, "")), None)
                if isinstance(conf_raw, str) and conf_raw.strip().lower() in _CONF_MAP:
                    confidence = _CONF_MAP[conf_raw.strip().lower()]
                elif conf_raw not in (None, ""):
                    try:
                        confidence = float(conf_raw)
                    except (TypeError, ValueError):
                        confidence = 0.6
                else:
                    confidence = 0.6

                mentions.append(
                    Mention(
                        mention_id=Mention.generate_id(),
                        anchor_id=anchor.anchor_id,
                        surface=name,
                        category=category,
                        normalized=name,
                        confidence=confidence,
                        span_start=0,
                        span_end=len(name),
                        qualifiers={
                            "kg0_rel_type": rel_type,
                            "kg0_top_category": top_category_str,
                            "kg0_specific_category": specific_category_str,
                            "kg0_labels": kg0_labels,
                            "page_label": anchor.metadata.get("page_label"),
                            "page_kg_id": anchor.metadata.get("page_kg_id"),
                        },
                        kg0_entity_id=entity_id_str,
                    )
                )
            result[anchor.anchor_id] = mentions
        return result

    # ------------------------------------------------------------

    def get_full_text(self, artifact_data: Dict[str, Any]) -> str:
        # Not used: KG0-native adapter has no Solr-style full text.
        return ""

    def get_metadata(self, artifact_data: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "family": artifact_data.get("family", "DOCUMENT"),
            "kg_id": artifact_data.get("kg_id") or artifact_data.get("_id"),
        }


class AdapterRegistry:
    """Maps artifact families to their adapters.

    When an IndexFacade (with a live Neo4jStore) is supplied, every
    family is routed to a single KG0NativeAdapter that reads anchors
    and mentions straight from the KG0 graph. The family-specific
    adapters below (EmailAdapter, DocumentAdapter, ThreadAdapter,
    PDFAdapter, PresentationAdapter) stay registered but inactive in
    that mode — they remain reachable for tests or a future Solr
    reactivation, but ``get_adapter`` short-circuits to the KG0
    adapter as long as it was constructed.
    """

    def __init__(self, config: AlignConfig, index: Any = None):
        self.config = config
        document_adapter = DocumentAdapter(config)
        self._adapters: Dict[str, ArtifactAdapter] = {
            "EMAIL": EmailAdapter(config),
            "THREAD": ThreadAdapter(config),
            "DOCUMENT": document_adapter,
            "TEXT": document_adapter,
            "SPREADSHEET": document_adapter,
            "PDF": PDFAdapter(config),
            "PRESENTATION": PresentationAdapter(config),
        }

        # KG0-native adapter (active whenever a Neo4j-backed index was
        # supplied). Falls back gracefully if the facade lacks a graph.
        self._kg0_adapter: Optional[KG0NativeAdapter] = None
        neo4j_store = getattr(index, "neo4j", None) if index is not None else None
        if neo4j_store is not None:
            self._kg0_adapter = KG0NativeAdapter(config, neo4j_store)

    def get_adapter(self, family: str) -> ArtifactAdapter:
        """Get the adapter for an artifact family."""
        # KG0-native path: every family resolves to the same adapter
        # because family-specific behavior now lives on :Page.label.
        if self._kg0_adapter is not None:
            return self._kg0_adapter

        # Fallback family-specific routing for tests and non-KG0 facades.
        # Normalize family name
        normalized = family.upper().replace("ARTIFACT_", "")
        if normalized == "PRESENTATION_SLIDE":
            normalized = "PRESENTATION"

        if normalized in self._adapters:
            return self._adapters[normalized]

        # Default to document adapter
        logger.warning(f"No adapter for family '{family}', falling back to DOCUMENT")
        return self._adapters["DOCUMENT"]

    def register(self, family: str, adapter: ArtifactAdapter):
        """Register a custom adapter."""
        self._adapters[family.upper()] = adapter
