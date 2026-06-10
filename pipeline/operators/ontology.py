"""
operators/ontology.py

KG0Ontology — per-collection domain type system.

All domain-specific type knowledge lives here.  Phase logic is
ontology-agnostic: it asks the ontology questions and acts on
the answers.  Two deployments will typically have completely
different ontology definitions.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# Slot-type mapping
# ═══════════════════════════════════════════════════════════════

@dataclass
class SlotTypeMapping:
    """
    Declares which KG0 entity categories and variable role keywords
    are compatible with a single slot question-type.

    Populated from the collection's ontology definition, never from
    code.  Two collections will typically have completely different
    entries here.

    Example (tobacco litigation)::

        slot_type:                "WHO"
        compatible_categories:    {"PERSON", "ORGANIZATION", "ROLE"}
        compatible_role_keywords: {"speaker", "author", "participant"}

    Example (pharmaceutical patents)::

        slot_type:                "WHO"
        compatible_categories:    {"INVENTOR", "ASSIGNEE", "EXAMINER"}
        compatible_role_keywords: {"applicant", "filer", "agent"}
    """
    slot_type: str
    compatible_categories: Set[str] = field(default_factory=set)
    compatible_role_keywords: Set[str] = field(default_factory=set)


# ═══════════════════════════════════════════════════════════════
# KG0 Ontology
# ═══════════════════════════════════════════════════════════════

@dataclass
class KG0Ontology:
    """
    The domain-specific type system for one ALIGN deployment.

    This is the single source of truth that phases consult when they
    need to know whether a KG0 category can fill a slot, which Neo4j
    labels correspond to an entity type, or how artifact families map
    to adapter names.

    In production this is loaded at startup from a schema file that
    ships with the collection (see ``load_ontology``).  The empty
    default lets ALIGN run without an ontology — it falls back to
    explicit ``target_schema_id`` mappings on each SlotDef and direct
    text overlap, both of which are domain-agnostic.

    Attributes
    ----------
    entity_categories :
        All entity category names defined in this KG0.
    edge_types :
        All relationship type names defined in this KG0.
    category_to_kg0_labels :
        Maps each entity category to the Neo4j node labels it
        corresponds to.
    slot_type_mappings :
        Maps each slot question-type string to its compatibility
        declaration.
    category_hierarchy :
        Optional parent → {children} for type subsumption.
    edge_type_aliases :
        Maps abstract intent edge names to concrete KG0 relationship
        types that may realize them.
    artifact_families :
        Maps artifact type strings to adapter family names.
    """
    entity_categories: Set[str] = field(default_factory=set)
    edge_types: Set[str] = field(default_factory=set)
    category_to_kg0_labels: Dict[str, List[str]] = field(
        default_factory=dict,
    )
    edge_type_aliases: Dict[str, List[str]] = field(
        default_factory=dict,
    )
    slot_type_mappings: Dict[str, SlotTypeMapping] = field(
        default_factory=dict,
    )
    category_hierarchy: Dict[str, Set[str]] = field(default_factory=dict)
    artifact_families: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Inject default category hierarchy when no ontology is loaded.

        Intent-analysis commonly produces abstract variable types
        (ENTITY_EVENT, ENTITY_ACTION_ITEM, ENTITY_TEXT_SPAN,
        ENTITY_VISIBILITY_LEVEL) that the extraction pipeline maps to
        concrete categories (ENTITY_LEGAL, ENTITY_OTHER,
        ENTITY_DOCUMENT_REF, etc.).  Without a hierarchy bridge the
        Phase 5 beam search finds zero compatible entity hypotheses for
        these variables, leaving them permanently unbound.

        The defaults below are non-destructive: an explicit ontology
        file that provides its own hierarchy takes precedence because
        ``load_ontology`` overwrites these fields.
        """
        _DEFAULT_HIERARCHY: Dict[str, Set[str]] = {
            # Abstract intent categories → extractable categories
            "ENTITY_EVENT": {
                "ENTITY_LEGAL", "ENTITY_ACTION_ITEM",
            },
            "ENTITY_ACTION_ITEM": {
                "ENTITY_LEGAL", "ENTITY_OTHER",
            },
            "ENTITY_DOCUMENT": {
                "ENTITY_DOCUMENT_REF", "ARTIFACT_DOCUMENT",
            },
            "ENTITY_TEXT_SPAN": {
                "ENTITY_OTHER", "ENTITY_DOCUMENT_REF",
            },
            "ENTITY_VISIBILITY_LEVEL": {
                "ENTITY_OTHER",
            },
            "ENTITY_COLLECTION": {
                "ENTITY_OTHER",
            },
            "ENTITY_CONCEPT": {
                "ENTITY_OTHER", "ENTITY_LEGAL",
            },
            "ENTITY_CLAIM": {
                "ENTITY_OTHER", "ENTITY_LEGAL",
            },
            "ENTITY_RISK_FINDING": {
                "ENTITY_OTHER", "ENTITY_LEGAL",
            },
            "ENTITY_POLICY": {
                "ENTITY_LEGAL", "ENTITY_OTHER",
            },
        }

        for parent, children in _DEFAULT_HIERARCHY.items():
            if parent not in self.category_hierarchy:
                self.category_hierarchy[parent] = children

        # Default edge type aliases: map abstract intent edge names to
        # the concrete relationship types that KG0 actually stores.
        _DEFAULT_EDGE_ALIASES: Dict[str, List[str]] = {
            "AFFILIATED_WITH": ["WORKS_FOR", "OWNS", "MENTIONS_ORG"],
            "REPRESENTS": ["WORKS_FOR", "MENTIONS_PERSON"],
            "AUTHORED_BY": ["MENTIONS_PERSON", "SENT_TO", "FORWARDED_TO"],
            "ABOUT": [
                "HAS_EVENT", "HAS_CLAIM", "HAS_RISK", "CONCERNS",
                "ASKED_ABOUT",
            ],
            "INVOLVED_IN": [
                "MENTIONS_PERSON", "HAS_EVENT", "HAS_CLAIM",
            ],
            "COORDINATED_RESPONSE_TO": [
                "HAS_EVENT", "CONCERNS", "HAS_CLAIM",
            ],
            "EVIDENCED_BY": [
                "HAS_PAGE", "CONTAINS_DOCUMENTS", "CITES",
            ],
            "QUOTES_SPAN": ["HAS_CLAIM", "HAS_RISK", "MENTIONS_PERSON"],
            "INTERNAL_ONLY": [],  # no KG0 equivalent; structural only
        }

        for edge, aliases in _DEFAULT_EDGE_ALIASES.items():
            if edge not in self.edge_type_aliases:
                self.edge_type_aliases[edge] = aliases

    # ----------------------------------------------------------
    # Query interface used by phase logic
    # ----------------------------------------------------------

    def is_slot_compatible(self, slot_type: str, category: str) -> bool:
        """
        Is *category* allowed to fill a slot of *slot_type*?

        Checks direct membership first, then walks the category
        hierarchy so that parent types subsume their children.
        """
        mapping = self.slot_type_mappings.get(slot_type)
        if mapping is None:
            return False

        cat_upper = category.upper()

        if cat_upper in mapping.compatible_categories:
            return True

        for parent_cat in mapping.compatible_categories:
            if cat_upper in self.category_hierarchy.get(parent_cat, set()):
                return True

        return False

    def role_matches_slot(self, slot_type: str, role: str) -> bool:
        """
        Does the free-text *role* string on a GraphVar match any of
        the declared role keywords for *slot_type*?
        """
        mapping = self.slot_type_mappings.get(slot_type)
        if mapping is None or not role:
            return False
        role_lower = role.lower()
        return any(kw in role_lower for kw in mapping.compatible_role_keywords)

    def expand_category(self, category: str) -> Set[str]:
        """
        Return *category* plus all its transitive children in the
        hierarchy.
        """
        cat_upper = category.upper()
        result = {cat_upper}
        children = self.category_hierarchy.get(cat_upper, set())
        for child in children:
            result |= self.expand_category(child)
        return result

    def kg0_labels_for(self, category: str) -> List[str]:
        """
        Neo4j node labels that correspond to *category*.

        Falls back to treating the category name itself as a label
        when the ontology has no explicit mapping.
        """
        category_upper = category.upper()
        if category_upper in self.category_to_kg0_labels:
            return list(self.category_to_kg0_labels[category_upper])

        stripped = category_upper
        for prefix in ("ENTITY_", "ARTIFACT_"):
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix):]
                break

        if stripped in self.category_to_kg0_labels:
            return list(self.category_to_kg0_labels[stripped])

        return [category]

    def family_for_artifact_type(self, artifact_type: str) -> str:
        """
        Adapter family name for an artifact type string.

        Falls back to ``"DOCUMENT"`` when the ontology has no mapping.
        """
        return self.artifact_families.get(artifact_type, "DOCUMENT")

    def resolve_edge_types(self, edge_type: str) -> List[str]:
        """
        Concrete KG0 relationship types that can satisfy *edge_type*.

        When the ontology has an alias mapping, return that expanded
        list. Otherwise, preserve exact KG0 edge names or the original
        value as a strict fallback.
        """
        edge_upper = edge_type.upper()
        if edge_upper in self.edge_type_aliases:
            return list(self.edge_type_aliases[edge_upper])
        if edge_upper in self.edge_types:
            return [edge_upper]
        return [edge_type]

    @property
    def is_loaded(self) -> bool:
        """True if a real ontology has been loaded (not the empty default)."""
        return bool(self.entity_categories) or bool(self.slot_type_mappings)


# ═══════════════════════════════════════════════════════════════
# Ontology loaders
# ═══════════════════════════════════════════════════════════════

def load_ontology(source: Any) -> KG0Ontology:
    """
    Load a KG0Ontology from a file path, a parsed dict, or a JSON
    string.

    Parameters
    ----------
    source : str | Path | dict
        - A ``pathlib.Path`` or string ending in ``.json`` / ``.yaml``
          / ``.yml`` is treated as a file path.
        - A ``dict`` is treated as already-parsed content.
        - A ``str`` that looks like JSON is parsed first.

    Returns
    -------
    KG0Ontology
    """
    if isinstance(source, dict):
        return _load_ontology_from_dict(source)

    if isinstance(source, (str, Path)):
        path = Path(source)
        if path.exists() and path.is_file():
            return _load_ontology_from_file(path)
        if isinstance(source, str) and source.strip().startswith("{"):
            return _load_ontology_from_dict(json.loads(source))

    raise ValueError(
        f"Cannot load ontology from {type(source).__name__}: {source!r}"
    )


def _load_ontology_from_file(path: Path) -> KG0Ontology:
    """Load ontology from a JSON or YAML file."""
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")

    if suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
            raw = yaml.safe_load(text)
        except ImportError:
            raise ImportError(
                "PyYAML is required to load .yaml ontology files.  "
                "Install it with: pip install pyyaml"
            )
    elif suffix == ".json":
        raw = json.loads(text)
    else:
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            try:
                import yaml  # type: ignore
                raw = yaml.safe_load(text)
            except ImportError:
                raise ValueError(
                    f"Could not parse {path} as JSON and PyYAML is not "
                    f"installed for YAML fallback."
                )

    logger.info("Loaded ontology from %s", path)
    return _load_ontology_from_dict(raw)


def _load_ontology_from_dict(raw: Dict[str, Any]) -> KG0Ontology:
    """
    Build a KG0Ontology from a parsed dictionary.

    Expected schema::

        entity_categories: [PERSON, ORGANIZATION, ...]
        edge_types: [AUTHORED, MENTIONS, ...]
        category_to_kg0_labels:
          PERSON: [Person]
          ORGANIZATION: [Organization, Company]
        edge_type_aliases:
          ABOUT: [HAS_CLAIM, HAS_DECISION, HAS_RISK]
          EVIDENCED_BY: [HAS_MESSAGE, HAS_PAGE, HAS_SLIDE]
        category_hierarchy:
          AGENT: [PERSON, ORGANIZATION]
        artifact_families:
          ARTIFACT_THREAD: THREAD
          ARTIFACT_EMAIL: EMAIL
        slot_type_mappings:
          WHO:
            compatible_categories: [PERSON, ORGANIZATION, ROLE]
            compatible_role_keywords: [speaker, author, participant]
          WHAT:
            compatible_categories: [STRATEGY, POLICY, CLAIM, CONCEPT]
            compatible_role_keywords: [strategy, plan, action]
          ...
    """
    mappings: Dict[str, SlotTypeMapping] = {}
    for slot_type, spec in raw.get("slot_type_mappings", {}).items():
        slot_type_upper = slot_type.upper()
        mappings[slot_type_upper] = SlotTypeMapping(
            slot_type=slot_type_upper,
            compatible_categories={
                c.upper() for c in spec.get("compatible_categories", [])
            },
            compatible_role_keywords={
                k.lower() for k in spec.get("compatible_role_keywords", [])
            },
        )

    hierarchy: Dict[str, Set[str]] = {}
    for parent, children in raw.get("category_hierarchy", {}).items():
        hierarchy[parent.upper()] = {c.upper() for c in children}

    cat_labels: Dict[str, List[str]] = {}
    for cat, labels in raw.get("category_to_kg0_labels", {}).items():
        cat_labels[cat.upper()] = list(labels)

    edge_aliases: Dict[str, List[str]] = {}
    for edge_type, rels in raw.get("edge_type_aliases", {}).items():
        edge_aliases[edge_type.upper()] = [str(rel).upper() for rel in rels]

    return KG0Ontology(
        entity_categories={
            c.upper() for c in raw.get("entity_categories", [])
        },
        edge_types={str(edge).upper() for edge in raw.get("edge_types", [])},
        category_to_kg0_labels=cat_labels,
        edge_type_aliases=edge_aliases,
        slot_type_mappings=mappings,
        category_hierarchy=hierarchy,
        artifact_families={
            k: v for k, v in raw.get("artifact_families", {}).items()
        },
    )
