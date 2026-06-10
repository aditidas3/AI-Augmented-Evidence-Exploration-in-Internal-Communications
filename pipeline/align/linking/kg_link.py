from __future__ import annotations

from difflib import SequenceMatcher

from ..utils.text_normalization import normalize_phrase, tokenize_phrase


def organization_subset_link_bonus(
    surface_tokens: set[str],
    candidate_tokens: set[str],
    *,
    legal_entity_tokens: set[str],
    modifier_tokens: set[str],
) -> float:
    if not surface_tokens or not candidate_tokens:
        return 0.0
    if not candidate_tokens.issubset(surface_tokens):
        return 0.0

    extra_tokens = surface_tokens - candidate_tokens
    if not extra_tokens:
        return 0.0
    if not candidate_tokens & legal_entity_tokens:
        return 0.0
    if not all(token in modifier_tokens for token in extra_tokens):
        return 0.0

    bonus = 0.18 + (0.03 * min(3, len(extra_tokens)))
    if len(candidate_tokens) >= 2:
        bonus += 0.02
    return min(0.30, bonus)


def score_kg_link_candidate(
    surface_norm: str,
    candidate_text: str,
    category: str = "",
    *,
    legal_entity_tokens: set[str] | None = None,
    modifier_tokens: set[str] | None = None,
) -> float:
    candidate_norm = normalize_phrase(candidate_text)
    if not candidate_norm:
        return 0.0
    if candidate_norm == surface_norm:
        return 1.0

    surface_tokens = set(tokenize_phrase(surface_norm))
    candidate_tokens = set(tokenize_phrase(candidate_norm))
    overlap = 0.0
    if surface_tokens and candidate_tokens:
        overlap = len(surface_tokens & candidate_tokens) / len(surface_tokens | candidate_tokens)

    ratio = SequenceMatcher(None, surface_norm, candidate_norm).ratio()
    score = 0.45 * ratio + 0.35 * overlap

    if candidate_norm.startswith(surface_norm) or surface_norm.startswith(candidate_norm):
        score += 0.20
    elif surface_norm in candidate_norm or candidate_norm in surface_norm:
        score += 0.10

    significant_tokens = [token for token in surface_tokens if len(token) >= 4]
    if significant_tokens and all(token in candidate_tokens for token in significant_tokens):
        score += 0.10

    category_key = str(category or "").strip().upper()
    if category_key in {"ENTITY_ORGANIZATION", "ORGANIZATION"}:
        score += organization_subset_link_bonus(
            surface_tokens,
            candidate_tokens,
            legal_entity_tokens=legal_entity_tokens or set(),
            modifier_tokens=modifier_tokens or set(),
        )

    return round(min(1.0, score), 4)
