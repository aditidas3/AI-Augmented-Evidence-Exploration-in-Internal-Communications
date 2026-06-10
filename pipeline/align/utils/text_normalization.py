from __future__ import annotations

import re
from typing import Any


def surface_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for token in re.findall(r"[A-Za-z0-9]+", str(text or "").lower()):
        if len(token) <= 1:
            continue
        tokens.add(token)
        if token.endswith("ies") and len(token) > 4:
            tokens.add(token[:-3] + "y")
        elif token.endswith("s") and len(token) > 4:
            tokens.add(token[:-1])
    return tokens


def normalize_condition_surface(surface: str) -> str:
    text = " ".join(str(surface or "").strip().lower().split())
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = text.replace("-", " ")
    text = text.replace("posttraumatic", "post traumatic")
    return " ".join(text.split())


def normalize_phrase(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def tokenize_phrase(value: Any) -> list[str]:
    normalized = normalize_phrase(value)
    return [token for token in normalized.split() if token]


def person_name_variants(value: Any) -> set[str]:
    raw = str(value or "").strip()
    if not raw:
        return set()
    variants = {normalize_phrase(raw)}
    if "," in raw:
        last, first = [part.strip() for part in raw.split(",", 1)]
        if first and last:
            variants.add(normalize_phrase(f"{first} {last}"))
    return {variant for variant in variants if variant}


def document_name_variants(value: Any) -> set[str]:
    raw = str(value or "").strip()
    if not raw:
        return set()
    variants = {normalize_phrase(raw)}
    stem = re.sub(r"\.(?:docx?|pdf|pptx?|xlsx?|xls|txt)$", "", raw, flags=re.I).strip()
    if stem and stem != raw:
        variants.add(normalize_phrase(stem))
    stem = re.sub(r"\bv(?:er(?:sion)?)?\s*\d+\b", "", stem or raw, flags=re.I).strip(" -_()")
    if stem:
        variants.add(normalize_phrase(stem))
    return {variant for variant in variants if variant}


def looks_like_filename(surface: str) -> bool:
    return bool(
        re.search(
            r"\.(?:docx?|pdf|pptx?|xlsx?|xls|txt)\b",
            str(surface or ""),
            flags=re.I,
        )
    )


def context_window(text: str, start: int, end: int, width: int = 48) -> str:
    raw = str(text or "")
    return raw[max(0, start - width) : min(len(raw), end + width)].lower()


def matches_metadata_value(surface_norm: str, values: set[str]) -> bool:
    if not surface_norm or not values:
        return False
    if surface_norm in values:
        return True
    return any(
        surface_norm in value or value in surface_norm
        for value in values
        if value and len(value) >= 4
    )


def link_hint_tokens(
    surface_norm: str,
    category: str = "",
    *,
    stopwords: set[str] | None = None,
    limit: int = 8,
) -> list[str]:
    stopword_set = stopwords or set()
    tokens = [
        token
        for token in tokenize_phrase(surface_norm)
        if len(token) >= 2 and token not in stopword_set
    ]
    significant = [token for token in tokens if len(token) >= 3]

    hints: list[str] = []
    if surface_norm:
        hints.append(surface_norm)
    if significant:
        joined = " ".join(significant)
        if joined and joined != surface_norm:
            hints.append(joined)

    for width in (3, 2):
        if len(significant) < width:
            continue
        for start in range(0, len(significant) - width + 1):
            hints.append(" ".join(significant[start : start + width]))

    for token in significant:
        hints.append(token)
        if token.endswith("s") and len(token) >= 5:
            hints.append(token[:-1])

    category_key = str(category or "").strip().upper()
    if category_key in {"ENTITY_ROLE", "ROLE"}:
        abbreviation_tokens = [
            token for token in tokens if len(token) <= 3 and token.isalpha()
        ]
        if len(abbreviation_tokens) >= 2:
            hints.append(" ".join(abbreviation_tokens))
            hints.append(".".join(abbreviation_tokens))
            hints.append("".join(abbreviation_tokens))
        for token in abbreviation_tokens:
            hints.append(token)

    deduped: list[str] = []
    seen: set[str] = set()
    for hint in hints:
        normalized = normalize_phrase(hint)
        if not normalized or normalized in seen:
            continue
        deduped.append(normalized)
        seen.add(normalized)
    deduped.sort(key=len, reverse=True)
    return deduped[:limit]
