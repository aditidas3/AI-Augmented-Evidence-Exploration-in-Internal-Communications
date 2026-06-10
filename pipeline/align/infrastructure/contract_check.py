"""Startup contract check for ALIGN.

ALIGN's phases make several assumptions about KG0's shape that, when
violated, produce silently-degraded output rather than visible errors.
Concrete examples we have hit in production debugging:

* ``top_category`` is stored lowercase in Postgres and propagated to
  Neo4j unchanged, but the adapter mapping table was PascalCase, so
  every mention silently became ``ENTITY_OTHER``.
* ``kg_id`` was implicitly assumed to be a scalar string, but a
  merged KG0 rebuild produced list-valued ids, breaking
  ``find_paths`` and ``get_neighbors`` Cypher equality matches.
* ``length(r)`` was used on a variable-length relationship binding,
  which only erred once a real WHERE clause matched.

Each of those bugs cost meaningful debugging time because the
pipeline kept running and produced a "result" — just an empty or
incorrect one.

This module runs a small set of cheap probes against the live index
once per :class:`AlignEngine` and raises :class:`AlignContractError`
the moment something is off, with a diagnostic message that names
the failed assertion and includes the offending evidence.

The contract is intentionally narrow: it checks the things ALIGN
relies on, not the full KG0 schema. Adding a new probe is fine when
a new silent-degradation bug surfaces — keep them cheap so the whole
suite stays well under one second.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Categories in ``adapters._KG0_TOP_TO_MENTION_CATEGORY``. Imported
# lazily inside :func:`_check_top_category_mapping` so this module
# does not pull the adapter package at import time.


class AlignContractError(RuntimeError):
    """Raised when ALIGN's KG0 contract is violated.

    The exception carries a ``failed_check`` attribute with the name
    of the assertion that failed, plus an ``evidence`` payload with
    the offending sample so callers can render an actionable error.
    """

    def __init__(
        self,
        failed_check: str,
        message: str,
        evidence: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.failed_check = failed_check
        self.evidence = evidence or {}
        suffix = ""
        if self.evidence:
            suffix = "\nEvidence:\n  " + "\n  ".join(
                f"{k}: {v}" for k, v in self.evidence.items()
            )
        super().__init__(
            f"[ALIGN contract: {failed_check}] {message}{suffix}"
        )


# ----------------------------------------------------------------------
# Public entrypoint
# ----------------------------------------------------------------------


def run_contract_check(
    index: Any, *, sample_size: int = 5
) -> "ContractReport":
    """Probe the live index and raise :class:`AlignContractError` on drift.

    Parameters
    ----------
    index:
        An :class:`IndexFacade` whose ``neo4j`` attribute exposes
        ``execute_cypher`` and ``find_paths``.
    sample_size:
        How many entity nodes to sample. Five is enough to detect a
        consistent property-shape regression while keeping the probe
        well under 100 ms on a small KG.

    Returns
    -------
    ContractReport
        Cached snapshot of KG0 facts the engine can reuse for cheap
        per-query checks (e.g. intent satisfiability), so it doesn't
        have to re-query Neo4j on every ``execute``.
    """
    if index is None or getattr(index, "neo4j", None) is None:
        raise AlignContractError(
            "no_graph_store",
            "IndexFacade has no Neo4j-compatible graph store; cannot "
            "run ALIGN against this configuration.",
        )
    graph = index.neo4j

    _check_documents_present(graph)
    _check_pages_and_mentions(graph)
    sampled = _sample_entity_nodes(graph, sample_size)
    _check_entity_id_properties(sampled)
    _check_top_category_mapping(sampled)
    _check_find_paths_callable(graph, sampled)
    _check_adapter_traversal(graph)
    label_set = _collect_label_set(graph)
    logger.info(
        "ALIGN contract check passed (sampled %d entity nodes, "
        "%d distinct labels in KG0)",
        len(sampled),
        len(label_set),
    )
    return ContractReport(label_set=label_set)


# ----------------------------------------------------------------------
# Cached snapshot consumed by per-query checks
# ----------------------------------------------------------------------


class ContractReport:
    """Frozen view of KG0 facts the contract check observed.

    The engine stores one of these on first ``execute`` and reuses it
    on subsequent calls so per-query satisfiability probes are O(1)
    set lookups instead of round-tripping Neo4j.
    """

    __slots__ = ("label_set", "label_set_lower")

    def __init__(self, *, label_set: List[str]) -> None:
        self.label_set: List[str] = list(label_set)
        self.label_set_lower: set = {
            (lbl or "").strip().lower() for lbl in label_set if lbl
        }

    def has_label(self, label: str) -> bool:
        return (label or "").strip().lower() in self.label_set_lower


# ----------------------------------------------------------------------
# Individual probes
# ----------------------------------------------------------------------


def _check_documents_present(graph: Any) -> None:
    rows = graph.execute_cypher(
        "MATCH (d:Document) RETURN count(d) AS total LIMIT 1", {}
    )
    total = int((rows[0] if rows else {}).get("total") or 0)
    if total == 0:
        raise AlignContractError(
            "documents_present",
            "KG0 contains zero :Document nodes. Has the KG0 rebuild "
            "completed for this Neo4j database?",
        )
    sample = graph.execute_cypher(
        "MATCH (d:Document) WHERE d.kg_id IS NULL RETURN d.document_id AS doc_id LIMIT 1",
        {},
    )
    if sample:
        raise AlignContractError(
            "document_kg_id_present",
            "Found a :Document node without a kg_id property.",
            {"document": sample[0]},
        )


def _check_pages_and_mentions(graph: Any) -> None:
    rows = graph.execute_cypher(
        "MATCH (p:Page) WITH count(p) AS pages "
        "OPTIONAL MATCH (:Page)-[r]->() WITH pages, count(r) AS mention_edges "
        "RETURN pages, mention_edges",
        {},
    )
    if not rows:
        raise AlignContractError(
            "pages_present",
            "Graph store returned no rows for the :Page count probe.",
        )
    pages = int(rows[0].get("pages") or 0)
    mention_edges = int(rows[0].get("mention_edges") or 0)
    if pages == 0:
        raise AlignContractError(
            "pages_present",
            "KG0 contains :Document nodes but zero :Page nodes. "
            "Phase 3 anchor extraction will produce nothing.",
        )
    if mention_edges == 0:
        raise AlignContractError(
            "page_to_entity_edges_present",
            "Pages have no outgoing mention edges. Phase 3 will "
            "extract anchors but no mentions, leaving Phase 4 with "
            "an empty hypothesis pool.",
            {"pages": pages},
        )


def _sample_entity_nodes(
    graph: Any, sample_size: int
) -> List[Dict[str, Any]]:
    """Pull a small set of entity nodes (non-Page, non-Document, non-Collection)."""
    rows = graph.execute_cypher(
        "MATCH (e) "
        "WHERE NOT 'Page' IN labels(e) "
        "  AND NOT 'Document' IN labels(e) "
        "  AND NOT 'Collection' IN labels(e) "
        "  AND e.name IS NOT NULL "
        "RETURN e.kg_id AS kg_id, e.id AS id, e.name AS name, "
        "       e.top_category AS top_category, labels(e) AS labels "
        "LIMIT $limit",
        {"limit": int(sample_size)},
    )
    if not rows:
        raise AlignContractError(
            "entity_sample_nonempty",
            "Could not sample any entity nodes from KG0 "
            "(non-Page, non-Document, non-Collection with a name). "
            "Phase 3 will have no mentions to attach.",
        )
    return rows


def _check_entity_id_properties(sample: List[Dict[str, Any]]) -> None:
    missing_kg_id: List[Dict[str, Any]] = []
    missing_id: List[Dict[str, Any]] = []
    for row in sample:
        if not row.get("kg_id"):
            missing_kg_id.append(
                {
                    "name": row.get("name"),
                    "labels": row.get("labels"),
                }
            )
        if not row.get("id"):
            missing_id.append(
                {
                    "name": row.get("name"),
                    "labels": row.get("labels"),
                }
            )
    if missing_kg_id:
        raise AlignContractError(
            "entity_kg_id_present",
            "Sampled entity nodes are missing the kg_id property "
            "that ALIGN's adapters and Phase 4 path search rely on.",
            {"missing_kg_id_examples": missing_kg_id[:3]},
        )
    if missing_id:
        # ``id`` is the legacy alias; it's optional now that the
        # graph queries match on either ``kg_id`` or ``id``, but a
        # warning helps catch a half-completed rebuild.
        logger.warning(
            "ALIGN contract: %d/%d sampled entity nodes are missing "
            "the legacy `id` property; kg_id-only matching is in use.",
            len(missing_id),
            len(sample),
        )


def _check_top_category_mapping(sample: List[Dict[str, Any]]) -> None:
    """Verify the sampled entities carry KG0 ``labels`` that ALIGN can map.

    Phase 3's mention categorization keys off the per-node ``labels``
    list (PascalCase) via ``_KG0_LABEL_TO_MENTION_CATEGORY``. We sample
    a few entities and check that:

    * the sample actually has labels populated (catches a rebuild that
      stripped them);
    * a reasonable fraction match the closed mapping table — if the
      majority drift outside it, downstream Phase 3 will collapse most
      mentions to ``ENTITY_OTHER``.
    """
    from .adapters import _KG0_LABEL_TO_MENTION_CATEGORY

    populated = [row for row in sample if (row.get("labels") or [])]
    if not populated:
        raise AlignContractError(
            "entity_labels_populated",
            "None of the sampled entity nodes have any labels. "
            "Phase 3 mention categorization will collapse every "
            "entity to ENTITY_OTHER.",
            {"sampled": [r.get("name") for r in sample]},
        )

    unmapped: List[str] = []
    for row in populated:
        labels = [str(lbl).strip() for lbl in (row.get("labels") or []) if lbl]
        if not any(lbl in _KG0_LABEL_TO_MENTION_CATEGORY for lbl in labels):
            # Record the broadest label (KG0 puts the top type first)
            unmapped.append(labels[0] if labels else "")
    if unmapped:
        unmapped_unique = sorted(set(unmapped))
        if len(unmapped) >= max(2, len(populated) // 2):
            raise AlignContractError(
                "entity_labels_in_mapping_table",
                "Most sampled entity nodes have no label that matches "
                "the ALIGN mapping table. Phase 3 will mark these as "
                "ENTITY_OTHER and downstream slot binding will miss "
                "them. Add the missing labels to "
                "adapters._KG0_LABEL_TO_MENTION_CATEGORY.",
                {
                    "unmapped_top_labels": unmapped_unique[:6],
                    "sample_size": len(populated),
                },
            )
        logger.info(
            "ALIGN contract: %d/%d sampled entity nodes have no label "
            "in the mapping table (top labels: %s); within tolerance.",
            len(unmapped),
            len(populated),
            unmapped_unique[:4],
        )


def _collect_label_set(graph: Any) -> List[str]:
    """Snapshot every node label in KG0 via ``CALL db.labels()``.

    The result is cached on the engine so per-query intent
    satisfiability probes can answer "does KG0 have an :Organization
    node?" with an O(1) set lookup. Falls back to an empty list if
    the procedure is unavailable (Memgraph or stub envs); per-query
    checks degrade to no-ops in that case rather than crashing.
    """
    try:
        rows = graph.execute_cypher(
            "CALL db.labels() YIELD label RETURN label", {}
        )
    except Exception as exc:  # pragma: no cover - exercised only in stub envs
        logger.debug(
            "_collect_label_set: db.labels() unavailable (%s); "
            "intent satisfiability probes will no-op.",
            type(exc).__name__,
        )
        return []
    return [row.get("label", "") for row in rows if row.get("label")]


def check_intent_satisfiability(
    intent: Any,
    report: Optional["ContractReport"],
    config: Any,
) -> List[str]:
    """Per-query probe: do the intent's entity hint categories exist
    in KG0 at all?

    Returns a list of human-readable warnings for categories with
    zero KG0 representation. The engine logs them as warnings rather
    than raising because operators sometimes legitimately explore an
    empty corpus during bootstrap. An empty list means every hint
    category resolves to at least one label.

    Cheap: every check is an O(1) set lookup against the cached
    label snapshot from :func:`run_contract_check`, so it's safe to
    run on every ``execute`` call.
    """
    if report is None or not report.label_set_lower:
        return []
    hints = getattr(intent, "entity_hints", None) or []
    warnings: List[str] = []
    seen: set = set()
    for hint in hints:
        category = (getattr(hint, "category", "") or "").strip()
        if not category or category.upper() == "ENTITY_COLLECTION":
            continue
        candidates = _candidate_labels_for_category(category, config)
        if not candidates:
            continue
        if any(c in report.label_set_lower for c in candidates):
            continue
        key = category.upper()
        if key in seen:
            continue
        seen.add(key)
        warnings.append(
            f"intent EntityHint category {category!r} has zero "
            f"matching node labels in KG0 "
            f"(checked: {sorted(candidates)})"
        )
    return warnings


def _candidate_labels_for_category(category: str, config: Any) -> set:
    """Return the lowercase label names that could match a category.

    Tries the ontology mapping first, then a few obvious casings so
    a hint of ``ORGANIZATION`` matches a Neo4j ``:Organization`` node
    even when the ontology has no explicit entry.
    """
    candidates: set = set()
    raw = (category or "").strip()
    if not raw:
        return candidates
    upper = raw.upper()
    stripped = upper
    for prefix in ("ENTITY_", "ARTIFACT_"):
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix):]
            break
    candidates.add(raw.lower())
    candidates.add(stripped.lower())
    candidates.add(stripped.title().lower())
    if config is not None and hasattr(config, "kg0_labels_for_category"):
        try:
            for lbl in config.kg0_labels_for_category(category) or []:
                candidates.add((lbl or "").strip().lower())
        except Exception:  # pragma: no cover - ontology stubs
            pass
    candidates.discard("")
    return candidates


def _check_adapter_traversal(graph: Any) -> None:
    """Verify the Document→Page→Entity path the Phase 3 adapter uses.

    Phase 3's KG0-native adapter walks
    ``(:Document)-[:HAS_PAGE]->(:Page)-[r]->(e)`` and expects ``e.name``
    to be populated. If a KG0 rebuild drops the ``HAS_PAGE`` edge, or
    if entities stop carrying ``name``, Phase 3 silently extracts zero
    mentions and the whole pipeline starves. This probe runs the same
    traversal against one Document and fails loud if no named entity
    comes back.
    """
    rows = graph.execute_cypher(
        "MATCH (d:Document)-[:HAS_PAGE]->(p:Page)-[r]->(e) "
        "WHERE e.name IS NOT NULL AND NOT 'Abbreviation' IN labels(e) "
        "RETURN d.kg_id AS doc_id, count(DISTINCT e) AS entity_count "
        "LIMIT 1",
        {},
    )
    if not rows:
        raise AlignContractError(
            "adapter_traversal",
            "Traversing (:Document)-[:HAS_PAGE]->(:Page)-[]->(e) "
            "returns no named entities. Phase 3's KG0-native adapter "
            "will extract anchors but zero mentions, leaving Phase 4 "
            "with an empty hypothesis pool.",
        )
    row = rows[0]
    if int(row.get("entity_count") or 0) == 0:
        raise AlignContractError(
            "adapter_traversal",
            "Adapter-shaped traversal matched a Document but found "
            "no entities with a populated ``name`` property.",
            {"doc_id": row.get("doc_id")},
        )


def _check_find_paths_callable(
    graph: Any, sample: List[Dict[str, Any]]
) -> None:
    """Run real ``find_paths`` / ``get_neighbors`` queries so latent
    Cypher syntax errors surface here.

    The bug this guards against: ``find_paths`` and ``get_neighbors``
    historically used ``length(r)`` on a variable-length relationship
    binding, which is a Cypher type error. It only fired when the
    WHERE clause actually matched a node, so an empty or wrong-id
    graph produced zero results and looked fine. This probe forces
    both queries to execute against real ids so any latent syntax
    error in the query shape surfaces at startup instead of during
    the first "real" investigation.
    """
    if not sample:
        return

    def _first_id(row: Dict[str, Any]) -> Optional[str]:
        raw = row.get("kg_id")
        if isinstance(raw, list):
            raw = raw[0] if raw else None
        return str(raw) if raw else None

    ids = [i for i in (_first_id(row) for row in sample) if i]
    if not ids:
        return

    # 1. get_neighbors against a real id catches the ``length(r)``
    #    vs ``size(r)`` type mismatch we hit in this corpus.
    if hasattr(graph, "get_neighbors"):
        try:
            graph.get_neighbors(ids[0], max_hops=1)
        except Exception as exc:
            raise AlignContractError(
                "get_neighbors_callable",
                f"get_neighbors failed against a real node id with "
                f"{type(exc).__name__}: {exc}",
                {"sample_id": ids[0]},
            ) from exc

    # 2. find_paths needs two distinct node ids because
    #    ``shortestPath`` rejects identical endpoints. Fall back to a
    #    same-id call only when the sample collapsed to one id, in
    #    which case we skip the probe rather than fabricate a second
    #    node.
    if hasattr(graph, "find_paths") and len(ids) >= 2:
        start, end = ids[0], ids[1]
        try:
            graph.find_paths(start, end, max_hops=2)
        except Exception as exc:
            raise AlignContractError(
                "find_paths_callable",
                f"find_paths failed against real node ids with "
                f"{type(exc).__name__}: {exc}",
                {"start_id": start, "end_id": end},
            ) from exc
