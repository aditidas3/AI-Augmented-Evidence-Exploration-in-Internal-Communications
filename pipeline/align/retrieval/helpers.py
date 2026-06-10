from __future__ import annotations

from typing import Any, Iterable, Mapping


def dominant_page_label(page_labels: Iterable[Any]) -> str:
    counts: dict[str, int] = {}
    first_seen: dict[str, int] = {}
    for raw_label in page_labels or []:
        if not raw_label:
            continue
        label = str(raw_label)
        if not label:
            continue
        if label not in first_seen:
            first_seen[label] = len(first_seen)
        counts[label] = counts.get(label, 0) + 1

    if not counts:
        return "unknown"
    return max(counts, key=lambda label: (counts[label], -first_seen[label]))


def build_solr_or_query(terms: Iterable[Any]) -> str:
    return " OR ".join(
        f'"{term}"' if " " in str(term) else str(term)
        for term in terms
        if term
    )


def expanded_retrieval_terms(
    base_terms: Iterable[Any],
    graph_vars: Iterable[Any],
    var_labels: Mapping[str, Iterable[Any]],
) -> list[Any]:
    terms = list(base_terms)
    seen = {
        str(term).lower()
        for term in terms
        if term
    }
    for var in graph_vars:
        hint = getattr(var, "hint", "")
        if hint:
            hint_lower = str(hint).lower()
            if hint_lower not in seen:
                seen.add(hint_lower)
                terms.append(hint)
        for label in var_labels.get(getattr(var, "var", ""), []):
            if not label:
                continue
            label_lower = str(label).lower()
            if label_lower not in seen:
                seen.add(label_lower)
                terms.append(label)
    return terms


def lower_unique_terms(values: Iterable[Any]) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        term = str(value).strip().lower()
        if term and term not in seen:
            seen.add(term)
            terms.append(term)
    return terms


def entity_hint_terms(expanded_terms: Iterable[Any], hints: Iterable[Any]) -> list[str]:
    values: list[Any] = list(expanded_terms)
    for hint in hints:
        values.append(getattr(hint, "surface", ""))
        values.append(getattr(hint, "normalized", ""))
    return lower_unique_terms(values)


def required_hint_terms(
    hints: Iterable[Any],
    *,
    min_confidence: float = 0.9,
) -> list[str]:
    values: list[Any] = []
    for hint in hints:
        if getattr(hint, "category", "") not in ("ENTITY_ORGANIZATION", "ENTITY_PERSON"):
            continue
        if float(getattr(hint, "confidence", 0.0) or 0.0) < min_confidence:
            continue
        values.append(getattr(hint, "surface", ""))
        values.append(getattr(hint, "normalized", ""))
    return lower_unique_terms(values)


