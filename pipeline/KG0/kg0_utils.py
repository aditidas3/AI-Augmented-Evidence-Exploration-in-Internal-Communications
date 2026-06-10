"""
Shared utilities for the KG0 pipeline.

Provides stable ID generation, Neo4j label resolution, and the
entity-type → relationship-type mapping used by both ``kg0_from_db``
and ``load_pages``.
"""

from __future__ import annotations

import hashlib
import re


# ──────────────────────────────────────────────────────────────────
# Stable ID generation
# ──────────────────────────────────────────────────────────────────

def sha_id(namespace: str, *parts: str) -> str:
    """Deterministic 12-char hex ID from a namespace + key parts."""
    raw = namespace + "|" + "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


# ──────────────────────────────────────────────────────────────────
# Neo4j label resolution
# ──────────────────────────────────────────────────────────────────

# Labels owned by infrastructure nodes — entity categories that would
# produce one of these are renamed to avoid collision.
INFRA_LABELS: frozenset[str] = frozenset({
    "Document", "Collection", "Abbreviation",
})


def to_label(text: str | None) -> str | None:
    """Convert a free-text category to a PascalCase Neo4j label.

    Returns None for empty input. Appends ``Entity`` if the result
    would collide with an infrastructure label. Prepends ``N`` if the
    result starts with a digit (invalid in Neo4j).
    """
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", s) if p]
    if not parts:
        return None
    label = "".join(p[:1].upper() + p[1:].lower() for p in parts)
    if label and label[0].isdigit():
        label = "N" + label
    if label in INFRA_LABELS:
        label = f"{label}Entity"
    return label


def resolve_labels(
    top_category: str | None,
    specific_category: str | None,
) -> list[str]:
    """Return ordered Neo4j labels ``[TopLabel, SpecificLabel]`` for an entity.

    Returns an empty list when neither category is usable.
    """
    labels: list[str] = []
    top = to_label(top_category)
    if top:
        labels.append(top)
    spec = to_label(specific_category)
    if spec and spec not in labels:
        labels.append(spec)
    return labels


# ──────────────────────────────────────────────────────────────────
# Label → relationship-type mapping (Page→Entity edges)
# ──────────────────────────────────────────────────────────────────

LABEL_TO_REL: dict[str, str] = {
    "Person":             "MENTIONS_PERSON",
    "Organization":       "MENTIONS_ORG",
    "Drug":               "MENTIONS_DRUG",
    "Product":            "MENTIONS_PRODUCT",
    "Location":           "MENTIONS_LOCATION_IN_TEXT",
    "Gpe":                "MENTIONS_LOCATION_IN_TEXT",
    "HealthMention":      "MENTIONS_HEALTH",
    "Health":             "MENTIONS_HEALTH",
    "MedicalCondition":   "MENTIONS_HEALTH",
    "DateMention":        "MENTIONS_DATE",
    "Date":               "MENTIONS_DATE",
    "Event":              "HAS_EVENT",
    "Risk":               "HAS_RISK",
    "Decision":           "HAS_DECISION",
    "Requirement":        "HAS_REQUIREMENT",
    "LegalFramework":     "HAS_REQUIREMENT",
    "Regulation":         "HAS_REQUIREMENT",
    "Claim":              "HAS_CLAIM",
    "Topic":              "HAS_CLAIM",
    "DocumentEntity":     "CITES",
    "AbbreviationEntity": "HAS_ABBREVIATION",
    "Identifier":         "HAS_IDENTIFIER",
    "Procedure":          "HAS_PROCEDURE",
    "Assessment":         "HAS_RISK",
    "Finance":            "HAS_FINANCE",
    "Metric":             "HAS_METRIC",
}

DEFAULT_REL = "HAS_CLAIM"


def slugify_rel(text: str) -> str:
    """Turn a free-text relationship string into a Neo4j relationship type."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", text.strip()).strip("_").upper()
    return slug or "RELATED_TO"
