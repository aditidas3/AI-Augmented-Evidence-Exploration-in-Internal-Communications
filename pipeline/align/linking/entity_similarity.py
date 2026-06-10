from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Iterable

from ..utils.text_normalization import surface_tokens


def normalize_entity_phrase(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def surface_similarity_ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def surface_similar(
    a: str,
    b: str,
    *,
    threshold: float,
    stopwords: Iterable[str],
) -> bool:
    if a == b:
        return True
    if not a or not b:
        return False
    if a in b or b in a:
        return True

    a_tokens_all = set(a.split())
    b_tokens_all = set(b.split())
    if not a_tokens_all or not b_tokens_all:
        return False

    stopword_set = set(stopwords)
    a_tokens = a_tokens_all - stopword_set
    b_tokens = b_tokens_all - stopword_set
    if not a_tokens or not b_tokens:
        a_tokens, b_tokens = a_tokens_all, b_tokens_all

    if a_tokens <= b_tokens or b_tokens <= a_tokens:
        return True

    union = a_tokens | b_tokens
    intersection = a_tokens & b_tokens
    if not union:
        return False
    ratio = len(intersection) / len(union)
    return ratio >= threshold


def artifact_context_overlap_score(
    source_texts: Iterable[str],
    target_phrases: Iterable[str],
) -> float:
    normalized_sources: list[str] = []
    for text in source_texts:
        normalized = normalize_entity_phrase(text)
        if normalized:
            normalized_sources.append(normalized)

    normalized_targets: list[str] = []
    for phrase in target_phrases:
        normalized = normalize_entity_phrase(phrase)
        if normalized:
            normalized_targets.append(normalized)

    best = 0.0
    for phrase in normalized_targets:
        phrase_tokens = surface_tokens(phrase)
        if len(phrase_tokens) < 2:
            continue
        for text in normalized_sources:
            if phrase in text or text in phrase:
                best = max(best, 1.0 if phrase in text else 0.7)
                continue
            overlap = len(phrase_tokens & surface_tokens(text))
            if overlap >= max(2, len(phrase_tokens) - 1):
                best = max(best, overlap / max(1, len(phrase_tokens)))
    return round(min(1.0, best), 4)
