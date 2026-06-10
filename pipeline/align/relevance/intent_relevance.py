from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from ..core_types import CandidateArtifact, EntityHint, GraphVar, IntentObject


_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
_DATE_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
_SHORT_NUMERIC_DATE_RE = re.compile(
    r"\b(?:0?[1-9]|1[0-2])[/.-](?:0?[1-9]|[12]\d|3[01])[/.-](\d{2})\b"
)
_SPLIT_RE = re.compile(r"[,;/]|\s+\b(?:and|or)\b\s+", re.IGNORECASE)

_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "did",
    "for",
    "from",
    "had",
    "has",
    "have",
    "how",
    "in",
    "inside",
    "internally",
    "is",
    "it",
    "of",
    "on",
    "or",
    "the",
    "to",
    "was",
    "were",
    "what",
    "which",
    "who",
    "with",
}

_ENTITY_HINT_CATEGORIES = {
    "ENTITY_ORGANIZATION",
    "ENTITY_DRUG",
    "ENTITY_PRODUCT",
    "ENTITY_LOCATION",
    "ENTITY_POLICY",
    "ENTITY_REGULATION",
    "ENTITY_EVENT",
}

_LOGISTICS_DRIFT_TERMS = {
        "center",
        "distribution",
        "dock",
        "inventory",
        "logistics",
        "manufacturing",
        "receiving",
        "shipping",
        "supply",
        "warehouse",
}

_ANALYTICS_DRIFT_TERMS = {
        "analytics",
        "chart",
        "charts",
        "counts",
        "data",
        "dataset",
        "dosage",
        "quarterly",
        "rates",
        "reporting",
        "source",
        "statistical",
        "statistics",
        "table",
        "total",
        "totals",
        "trends",
        "units",
    }

_MARKETING_DRIFT_TERMS = {
        "advertising",
        "brand",
        "campaign",
        "fund",
        "funded",
        "marketing",
        "promotion",
        "promotional",
        "sales",
        "sponsor",
        "sponsored",
}

_LEGAL_SETTLEMENT_DRIFT_TERMS = {
        "attorney",
        "attorneys",
        "claim",
        "claims",
        "complaint",
        "confidentiality",
        "defendant",
        "dismissal",
        "employment",
        "fees",
        "lawsuit",
        "plaintiff",
        "release",
        "settlement",
        "taxes",
        "waiver",
}

_SCOPE_DRIFT_GROUPS = (
    (_LOGISTICS_DRIFT_TERMS, 3),
    (_ANALYTICS_DRIFT_TERMS, 4),
    (_MARKETING_DRIFT_TERMS, 3),
    (_LEGAL_SETTLEMENT_DRIFT_TERMS, 3),
)


@dataclass(frozen=True)
class IntentRelevanceScore:
    total: float
    hard_entity_coverage: float = 0.0
    required_hard_entity_coverage: float = 0.0
    topic_coverage: float = 0.0
    focus_coverage: float = 0.0
    time_score: float = 0.0
    family_score: float = 0.0
    missing_hard_entity_penalty: float = 0.0
    scope_drift_penalty: float = 0.0
    has_text: bool = False

    @property
    def selection_bonus(self) -> float:
        return self.total


def score_candidate_relevance(
    candidate: CandidateArtifact,
    intent: IntentObject,
    *,
    required_families_override: Iterable[str] | None = None,
) -> IntentRelevanceScore:
    """Score a retrieved artifact against the current intent without domain constants.

    This is intentionally lexical and transparent. It uses only the current
    intent's entity hints, query text, expansions, slot descriptions, graph
    variable hints, time filter, and allowed artifact families.
    """

    text = _candidate_text(candidate)
    tokens = _tokens(text)
    has_text = bool(tokens)

    hard_text = _candidate_hard_text(candidate)
    normalized_hard_text = _normalized_text(hard_text)

    hard_phrases = _hard_phrase_alternatives(intent)
    hard_coverage = _phrase_group_coverage(normalized_hard_text, hard_phrases)
    required_hard_phrases = _required_hard_phrase_alternatives(intent)
    required_hard_coverage = _phrase_group_coverage(
        normalized_hard_text,
        required_hard_phrases,
    )

    topic_terms = _topic_terms(intent)
    topic_coverage = _token_coverage(tokens, topic_terms)
    focus_terms = _focus_terms(intent, hard_phrases)
    focus_coverage = _token_coverage(tokens, focus_terms)
    scope_drift_penalty = _scope_drift_penalty(tokens, topic_terms | focus_terms)

    time_score = _time_score(candidate, text, intent)
    family_score = _family_score(
        candidate,
        intent,
        required_families_override=required_families_override,
    )

    missing_hard_penalty = 0.0
    if has_text and hard_phrases and hard_coverage <= 0.01:
        missing_hard_penalty = 2.0
    if (
        has_text
        and required_hard_phrases
        and required_hard_coverage <= 0.01
    ):
        missing_hard_penalty = max(missing_hard_penalty, 2.5)

    generic_only_penalty = 0.0
    if has_text and hard_phrases and hard_coverage <= 0.01 and topic_coverage < 0.18:
        generic_only_penalty = 0.75

    total = (
        4.0 * hard_coverage
        + 2.2 * topic_coverage
        + 1.2 * focus_coverage
        + time_score
        + family_score
        - missing_hard_penalty
        - generic_only_penalty
        - scope_drift_penalty
    )
    return IntentRelevanceScore(
        total=total,
        hard_entity_coverage=hard_coverage,
        required_hard_entity_coverage=required_hard_coverage,
        topic_coverage=topic_coverage,
        focus_coverage=focus_coverage,
        time_score=time_score,
        family_score=family_score,
        missing_hard_entity_penalty=missing_hard_penalty,
        scope_drift_penalty=scope_drift_penalty,
        has_text=has_text,
    )


def audit_candidates(
    candidates: Iterable[CandidateArtifact],
    intent: IntentObject,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        score = score_candidate_relevance(candidate, intent)
        rows.append(
            {
                "artifact_id": candidate.artifact_id,
                "artifact_name": candidate.artifact_name,
                "family": candidate.family,
                "fused_score": candidate.fused_score,
                "intent_relevance_total": round(score.total, 4),
                "hard_entity_coverage": round(score.hard_entity_coverage, 4),
                "required_hard_entity_coverage": round(
                    score.required_hard_entity_coverage, 4
                ),
                "topic_coverage": round(score.topic_coverage, 4),
                "focus_coverage": round(score.focus_coverage, 4),
                "time_score": round(score.time_score, 4),
                "family_score": round(score.family_score, 4),
                "missing_hard_entity_penalty": round(
                    score.missing_hard_entity_penalty, 4
                ),
                "scope_drift_penalty": round(score.scope_drift_penalty, 4),
                "has_text": score.has_text,
            }
        )
    rows.sort(
        key=lambda row: (
            row["intent_relevance_total"],
            row["hard_entity_coverage"],
            row["topic_coverage"],
            row["fused_score"],
        ),
        reverse=True,
    )
    return rows


def _candidate_text(candidate: CandidateArtifact) -> str:
    metadata = candidate.metadata or {}
    parts: list[str] = [candidate.artifact_name or "", candidate.family or ""]
    for key in (
        "text",
        "document_text",
        "full_text",
        "ocr_text",
        "body",
        "title",
        "subject",
        "summary",
        "description",
        "document_name",
        "collection",
        "participants",
        "matched_entity_names",
        "matched_required_entity_names",
        "artifact_name",
    ):
        value = metadata.get(key)
        if value is None:
            continue
        if isinstance(value, (list, tuple, set)):
            parts.extend(str(item) for item in value if item is not None)
        else:
            parts.append(str(value))
    local_ocr = _candidate_local_ocr_text(candidate)
    if local_ocr:
        parts.append(local_ocr)
    return " ".join(part for part in parts if part)


def _candidate_hard_text(candidate: CandidateArtifact) -> str:
    metadata = candidate.metadata or {}
    parts: list[str] = [candidate.artifact_name or ""]
    for key in (
        "text",
        "document_text",
        "full_text",
        "ocr_text",
        "body",
        "title",
        "subject",
        "summary",
        "description",
        "document_name",
        "artifact_name",
        "participants",
        "matched_entity_names",
        "matched_required_entity_names",
    ):
        value = metadata.get(key)
        if value is None:
            continue
        if isinstance(value, (list, tuple, set)):
            parts.extend(str(item) for item in value if item is not None)
        else:
            parts.append(str(value))
    local_ocr = _candidate_local_ocr_text(candidate)
    if local_ocr:
        parts.append(local_ocr)
    return " ".join(part for part in parts if part)


def _candidate_local_ocr_text(candidate: CandidateArtifact) -> str:
    ids = _candidate_document_ids(candidate)
    if not ids:
        return ""

    roots: list[Path] = []
    env_root = os.environ.get("ALIGN_OCR_ROOT")
    if env_root:
        roots.append(Path(env_root))
    roots.append(Path(__file__).resolve().parents[2] / "docs")

    for root in roots:
        for doc_id in ids:
            text = _read_local_ocr_text(str(root), doc_id)
            if text:
                return text
    return ""


def _candidate_document_ids(candidate: CandidateArtifact) -> list[str]:
    metadata = candidate.metadata or {}
    values = [
        metadata.get("document_id"),
        metadata.get("document_name"),
        metadata.get("artifact_name"),
        candidate.artifact_name,
    ]
    ids: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value is None:
            continue
        if isinstance(value, (list, tuple, set)):
            raw_values = value
        else:
            raw_values = [value]
        for raw in raw_values:
            stem = Path(str(raw or "").strip()).stem
            if not stem or stem in seen:
                continue
            seen.add(stem)
            ids.append(stem)
    return ids


@lru_cache(maxsize=512)
def _read_local_ocr_text(root: str, doc_id: str) -> str:
    safe_doc_id = Path(str(doc_id)).name
    if not safe_doc_id:
        return ""
    path = Path(root) / safe_doc_id / f"{safe_doc_id}.txt"
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _tokens(text: str) -> set[str]:
    return {
        token.lower()
        for token in _TOKEN_RE.findall(str(text or ""))
        if len(token) >= 2 and token.lower() not in _STOPWORDS
    }


def _normalized_text(text: str) -> str:
    tokens = [
        token.lower()
        for token in _TOKEN_RE.findall(str(text or ""))
        if len(token) >= 2 and token.lower() not in _STOPWORDS
    ]
    return f" {' '.join(tokens)} " if tokens else ""


def _normalized_phrase(text: str) -> str:
    return _normalized_text(text).strip()


def _hard_phrase_alternatives(intent: IntentObject) -> list[list[str]]:
    groups: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()

    for hint in intent.entity_hints:
        if not _entity_hint_is_core(hint):
            continue
        alternatives = _alternatives_for_texts(hint.surface, hint.normalized)
        if alternatives:
            key = tuple(sorted(alternatives))
            if key not in seen:
                seen.add(key)
                groups.append(alternatives)

    gs = intent.graph_spec
    if gs is not None:
        for var in gs.vars:
            if not var.hard:
                continue
            alternatives = _graph_var_phrase_alternatives(intent, var)
            if alternatives:
                key = tuple(sorted(alternatives))
                if key not in seen:
                    seen.add(key)
                    groups.append(alternatives)

    return groups


def _required_hard_phrase_alternatives(intent: IntentObject) -> list[list[str]]:
    groups: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()

    gs = intent.graph_spec
    if gs is None:
        return groups

    for var in gs.vars:
        if not var.hard:
            continue
        alternatives = _graph_var_phrase_alternatives(intent, var)
        if alternatives:
            key = tuple(sorted(alternatives))
            if key not in seen:
                seen.add(key)
                groups.append(alternatives)
    return groups


def _entity_hint_is_core(hint: EntityHint) -> bool:
    category = str(hint.category or "").strip().upper()
    if category in _ENTITY_HINT_CATEGORIES and float(hint.confidence or 0.0) >= 0.80:
        return True
    if category == "ENTITY_PERSON":
        return _looks_like_name(hint.surface) or _looks_like_name(hint.normalized)
    return False


def _looks_like_name(value: str) -> bool:
    tokens = re.findall(r"[A-Za-z][A-Za-z'.-]*", str(value or ""))
    if len(tokens) < 2 or len(tokens) > 5:
        return False
    return sum(1 for token in tokens if token[:1].isupper()) >= 2


def _alternatives_for_texts(*values: str) -> list[str]:
    alternatives: list[str] = []
    seen: set[str] = set()
    for value in values:
        for part in _split_alias_parts(value):
            phrase = _normalized_phrase(part)
            if not phrase:
                continue
            candidates = {phrase}
            acronym = _phrase_acronym(phrase)
            if acronym:
                candidates.add(acronym)
            candidates.add(_singularized_phrase(phrase))
            for candidate in sorted(candidates):
                candidate = _normalized_phrase(candidate)
                if not candidate or candidate in seen:
                    continue
                seen.add(candidate)
                alternatives.append(candidate)
    return alternatives


def _graph_var_phrase_alternatives(
    intent: IntentObject,
    var: GraphVar,
) -> list[str]:
    values: list[str] = [var.hint]
    for hint in intent.entity_hints:
        if _entity_hint_matches_graph_var(hint, var):
            values.extend([hint.surface, hint.normalized])
            aliases = (hint.qualifiers or {}).get("aliases")
            if isinstance(aliases, str):
                values.append(aliases)
            elif isinstance(aliases, (list, tuple, set)):
                values.extend(str(alias) for alias in aliases)
    return _alternatives_for_texts(*values)


def _entity_hint_matches_graph_var(hint: EntityHint, var: GraphVar) -> bool:
    if not (_category_aliases(hint.category) & _category_aliases(var.type)):
        return False

    hint_values = _alternatives_for_texts(hint.surface, hint.normalized)
    var_values = _alternatives_for_texts(var.hint, var.role)
    if not hint_values or not var_values:
        return False

    for hint_value in hint_values:
        for var_value in var_values:
            if hint_value == var_value:
                return True
            if _phrase_acronym(hint_value) == var_value:
                return True
            if _phrase_acronym(var_value) == hint_value:
                return True
            if _token_overlap_ratio(hint_value, var_value) >= 0.67:
                return True
    return False


def _category_aliases(category: str) -> set[str]:
    normalized = str(category or "").strip().upper()
    if not normalized:
        return set()
    aliases = {normalized}
    if normalized.startswith("ENTITY_"):
        aliases.add(normalized[len("ENTITY_"):])
    elif normalized.startswith("ARTIFACT_"):
        bare = normalized[len("ARTIFACT_"):]
        aliases.add(bare)
        aliases.add(f"ENTITY_{bare}")
    else:
        aliases.add(f"ENTITY_{normalized}")
    return aliases


def _split_alias_parts(value: str) -> list[str]:
    text = str(value or "")
    if not text.strip():
        return []
    parts = [text]
    split_parts = [part.strip() for part in _SPLIT_RE.split(text) if part.strip()]
    if len(split_parts) > 1:
        parts.extend(split_parts)
    return parts


def _phrase_acronym(phrase: str) -> str:
    tokens = [
        token
        for token in _TOKEN_RE.findall(str(phrase or ""))
        if token and token.lower() not in _STOPWORDS
    ]
    if len(tokens) < 2:
        return ""
    acronym = "".join(token[0].lower() for token in tokens if token[0].isalpha())
    return acronym if len(acronym) >= 2 else ""


def _singularized_phrase(phrase: str) -> str:
    tokens = [_match_token(token) for token in str(phrase or "").split()]
    return " ".join(token for token in tokens if token)


def _match_token(token: str) -> str:
    token = str(token or "").lower()
    if token.endswith("s") and len(token) > 4:
        return token[:-1]
    return token


def _token_overlap_ratio(left: str, right: str) -> float:
    left_tokens = {_match_token(token) for token in str(left or "").split()}
    right_tokens = {_match_token(token) for token in str(right or "").split()}
    left_tokens.discard("")
    right_tokens.discard("")
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / max(1, min(len(left_tokens), len(right_tokens)))


def _phrase_group_coverage(
    candidate_text: str,
    groups: list[list[str]],
) -> float:
    if not groups:
        return 0.0
    candidate_tokens = {
        _match_token(token)
        for token in str(candidate_text or "").split()
        if token
    }
    hits = 0
    for alternatives in groups:
        if any(
            _phrase_alternative_matches(candidate_text, candidate_tokens, alt)
            for alt in alternatives
        ):
            hits += 1
    return hits / len(groups)


def _phrase_alternative_matches(
    candidate_text: str,
    candidate_tokens: set[str],
    alternative: str,
) -> bool:
    alt = _normalized_phrase(alternative)
    if not alt:
        return False
    if f" {alt} " in candidate_text:
        return True
    alt_tokens = {
        _match_token(token)
        for token in alt.split()
        if token
    }
    if not alt_tokens:
        return False
    if len(alt_tokens) == 1:
        return next(iter(alt_tokens)) in candidate_tokens
    return False


def _topic_terms(intent: IntentObject) -> set[str]:
    parts: list[str] = [
        intent.header.question_text,
        intent.retrieval_spec.query_text,
        " ".join(intent.retrieval_spec.query_expansions),
        intent.scope_spec.scope_notes,
    ]
    if intent.slot_spec.global_trigger.terms:
        parts.append(" ".join(intent.slot_spec.global_trigger.terms))
    for slot in intent.slot_spec.slots:
        parts.append(slot.description)
    gs = intent.graph_spec
    if gs is not None:
        for var in gs.vars:
            parts.extend([var.type, var.role, var.hint])
        for edge in gs.edges:
            parts.extend([edge.rel, edge.notes])
    for hint in intent.entity_hints:
        if not _entity_hint_is_core(hint):
            parts.extend([hint.surface, hint.normalized])
    return _tokens(" ".join(part for part in parts if part))


def _focus_terms(intent: IntentObject, hard_phrases: list[list[str]]) -> set[str]:
    parts: list[str] = [
        intent.header.question_text,
        intent.scope_spec.scope_notes,
    ]
    for hint in intent.entity_hints:
        if not _entity_hint_is_core(hint):
            parts.extend([hint.surface, hint.normalized])
    for expansion in intent.retrieval_spec.query_expansions:
        parts.append(expansion)
    for slot in intent.slot_spec.slots:
        if slot.slot_type in {"WHAT", "WHO", "HOW", "EVIDENCE"}:
            parts.append(slot.description)
    gs = intent.graph_spec
    if gs is not None:
        for var in gs.vars:
            if not var.hard:
                parts.extend([var.role, var.hint])
        for edge in gs.edges:
            parts.extend([edge.rel, edge.notes])

    terms = _tokens(" ".join(part for part in parts if part))
    hard_terms = {
        token
        for alternatives in hard_phrases
        for phrase in alternatives
        for token in phrase.split()
    }
    return terms - hard_terms


def _scope_drift_penalty(
    candidate_tokens: set[str],
    expected_terms: set[str],
) -> float:
    if not candidate_tokens:
        return 0.0

    penalty = 0.0
    for group, min_hits in _SCOPE_DRIFT_GROUPS:
        if expected_terms & group:
            continue
        hits = candidate_tokens & group
        if len(hits) >= min_hits:
            penalty += 1.35
    return min(2.0, penalty)


def _token_coverage(candidate_tokens: set[str], expected_tokens: set[str]) -> float:
    if not candidate_tokens or not expected_tokens:
        return 0.0
    hits = len(candidate_tokens & expected_tokens)
    return min(1.0, hits / max(4, min(len(expected_tokens), 20)))


def _family_score(
    candidate: CandidateArtifact,
    intent: IntentObject,
    *,
    required_families_override: Iterable[str] | None = None,
) -> float:
    allowed = {
        _canonical_family(value)
        for value in list(intent.scope_spec.artifact_types or [])
        + [slot_type for slot in intent.slot_spec.slots for slot_type in slot.allowed_artifact_types]
    }
    required_families = (
        list(required_families_override)
        if required_families_override is not None
        else list(intent.required_families or [])
    )
    if required_families:
        allowed |= {_canonical_family(value) for value in required_families}
    if not allowed:
        return 0.0
    return 0.45 if _canonical_family(candidate.family) in allowed else -0.75


def _canonical_family(value: str) -> str:
    raw = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    if raw.startswith("ARTIFACT_"):
        raw = raw[len("ARTIFACT_") :]
    return raw


def _time_score(candidate: CandidateArtifact, text: str, intent: IntentObject) -> float:
    tf = intent.scope_spec.time_filter
    if not tf or str(tf.op or "").lower() == "none":
        return 0.0

    start_year = _year_from_value(tf.start)
    end_year = _year_from_value(tf.end)
    if start_year is None and end_year is None:
        return 0.0

    metadata_years = set()
    text_years = set()
    metadata = candidate.metadata or {}
    for key in ("date", "created_at", "sent_at", "artifact_date", "year"):
        year = _year_from_value(metadata.get(key))
        if year is not None:
            metadata_years.add(year)
    text_years |= {int(year) for year in _DATE_YEAR_RE.findall(text or "")}
    text_years |= {
        _expand_two_digit_year(year)
        for year in _SHORT_NUMERIC_DATE_RE.findall(text or "")
    }
    text_years = {year for year in text_years if year is not None}
    candidate_years = metadata_years | text_years

    if not candidate_years:
        return 0.0

    def in_range(year: int) -> bool:
        if start_year is not None and year < start_year:
            return False
        if end_year is not None and year > end_year:
            return False
        return True

    if text_years and not any(in_range(year) for year in text_years):
        return -1.25
    if any(in_range(year) for year in candidate_years):
        return 0.75
    return -1.25


def _expand_two_digit_year(value: Any) -> int | None:
    try:
        year = int(value)
    except (TypeError, ValueError):
        return None
    return 2000 + year if year <= 30 else 1900 + year


def _year_from_value(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value if 1000 <= value <= 9999 else None
    text = str(value)
    match = _DATE_YEAR_RE.search(text)
    if match:
        return int(match.group(1))
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).year
    except ValueError:
        return None
