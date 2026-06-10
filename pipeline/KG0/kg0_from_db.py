"""
Build KG0 in Neo4j / Memgraph from PostgreSQL raw-data tables.

Usage::

    python pipeline/kg0/kg0_from_db.py \\
        --neo4j-uri bolt://127.0.0.1:7687 \\
        --neo4j-user neo4j --neo4j-password testtest \\
        --wipe --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pipeline.kg0.kg0_utils import (
    sha_id,
    resolve_labels,
    slugify_rel,
    LABEL_TO_REL,
    DEFAULT_REL,
)

log = logging.getLogger("kg0_from_db")

# ──────────────────────────────────────────────────────────────────
# Constants — PostgreSQL source tables
# ──────────────────────────────────────────────────────────────────

NODE_TABLE = "ucsf_opioid.node_raw_data_ucsf_50"
EDGE_TABLE = "ucsf_opioid.edges_raw_data_ucsf_50"
CATALOG_TABLE = "ucsf_opioid.master_collection_catalog"
DEFAULT_DOCS_DIR = Path(__file__).resolve().parents[2] / "docs"

# Page fallback when a document has no labels.jsonl or an entity row has
# no page_number. Every mention is still attached to a Page so that
# ALIGN can rely on a uniform (:Document)-[:HAS_PAGE]->(:Page)-[:MENTIONS_*]->(:Entity) shape.
UNKNOWN_PAGE_LABEL = "unknown"
UNKNOWN_PAGE_INDEX = 0


# ══════════════════════════════════════════════════════════════════
# 1. Abbreviation handling
#
#    The LLM extraction occasionally packs canonical name + abbreviation
#    into one term using "||" (e.g. "Office of Inspector General || OIG").
#    We split these so the canonical name becomes the main entity and
#    the abbreviation becomes its own :Abbreviation node.
# ══════════════════════════════════════════════════════════════════


def _split_term_abbrev(term: str) -> tuple[str, str | None]:
    """Split 'Foo || ABBR' into ('Foo', 'ABBR'). No-op if no '||'."""
    if "||" not in term:
        return term.strip(), None
    canon, _, abbr = term.partition("||")
    canon = canon.strip()
    abbr = abbr.strip()
    return (canon or term.strip(), abbr or None)


def _build_abbrev_map(
    entities: list[dict[str, Any]],
) -> dict[str, tuple[str, str]]:
    """Return ``abbrev_lower → (canonical_name, canonical_label)``.

    Skips ambiguous abbreviations (mapped to multiple canonicals).
    """
    raw: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for row in entities:
        term = (row.get("term") or "").strip()
        if "||" not in term:
            continue
        canon, abbr = _split_term_abbrev(term)
        if not abbr:
            continue
        labels = resolve_labels(row.get("top_category"), row.get("specific_category"))
        if not labels:
            continue
        raw[abbr.lower()].add((canon, labels[0]))
    return {abbr: next(iter(v)) for abbr, v in raw.items() if len(v) == 1}


def _canonical_for_row(
    row: dict[str, Any],
    abbrev_map: dict[str, tuple[str, str]],
) -> tuple[str, list[str], str | None]:
    """Return ``(canonical_name, labels, abbreviation_or_None)`` for a row.

    If the bare term is a known abbreviation, it is redirected to the
    canonical form (no new abbreviation node needed).
    """
    raw_term = (row.get("term") or "").strip()
    canon_part, abbr_part = _split_term_abbrev(raw_term)
    labels = resolve_labels(row.get("top_category"), row.get("specific_category"))

    if abbr_part:
        return canon_part, labels, abbr_part

    redirect = abbrev_map.get(canon_part.lower())
    if redirect:
        canon_name, canon_primary_label = redirect
        redirected: list[str] = [canon_primary_label]
        for lbl in labels[1:]:
            if lbl not in redirected:
                redirected.append(lbl)
        return canon_name, redirected, None

    return canon_part, labels, None


# ══════════════════════════════════════════════════════════════════
# 2. Fuzzy endpoint resolution (edge-table term → entity kg_id)
#
#    Four tiers in decreasing precision:
#      1. exact      — original lowercase
#      2. normalized — punctuation collapsed, leading articles stripped
#      3. token_set  — same words in any order, ignoring stopwords
#      4. token_sub  — query tokens ⊂ node tokens (unique match only)
# ══════════════════════════════════════════════════════════════════

_AMBIG = object()
_STOPWORDS = frozenset({
    "the", "a", "an", "of", "for", "in", "to", "and", "or", "by",
    "on", "at", "with", "from",
})
_LEADING_ARTICLES = ("the ", "a ", "an ")


def _norm_for_match(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9\s]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    for art in _LEADING_ARTICLES:
        if s.startswith(art):
            s = s[len(art):]
            break
    return s


def _token_key(s: str) -> tuple[str, ...]:
    norm = _norm_for_match(s)
    return tuple(sorted(t for t in norm.split() if t and t not in _STOPWORDS))


def _has_substantive_signal(tokens: frozenset[str]) -> bool:
    nondigit = [t for t in tokens if not t.isdigit()]
    if not nondigit:
        return False
    if len(nondigit) >= 2:
        return True
    return len(nondigit[0]) >= 5


class EndpointResolver:
    """Multi-tier fuzzy lookup from edge-table term → entity kg_id."""

    def __init__(self, entities: list[dict[str, Any]]) -> None:
        self.exact: dict[str, str] = {}
        self.norm: dict[str, Any] = {}
        self.tokens: dict[tuple[str, ...], Any] = {}
        self.abbrev_alias: dict[str, str] = {}
        self._by_kg: list[tuple[str, frozenset[str]]] = []

        abbrev_map = _build_abbrev_map(entities)

        seen_kg: set[str] = set()
        for row in entities:
            raw_term = (row.get("term") or "").strip()
            if not raw_term:
                continue
            canon_name, labels, abbrev = _canonical_for_row(row, abbrev_map)
            if not labels:
                continue
            canon_lower = canon_name.lower()
            kg_id = sha_id("entity_weak", labels[0], canon_lower)

            self.exact.setdefault(canon_lower, kg_id)
            if abbrev:
                self.abbrev_alias.setdefault(abbrev.lower(), kg_id)

            n = _norm_for_match(canon_name)
            if n:
                cur = self.norm.get(n)
                if cur is None:
                    self.norm[n] = kg_id
                elif cur is not _AMBIG and cur != kg_id:
                    self.norm[n] = _AMBIG

            tk = _token_key(canon_name)
            if tk:
                cur = self.tokens.get(tk)
                if cur is None:
                    self.tokens[tk] = kg_id
                elif cur is not _AMBIG and cur != kg_id:
                    self.tokens[tk] = _AMBIG

            if kg_id not in seen_kg:
                seen_kg.add(kg_id)
                self._by_kg.append((kg_id, frozenset(tk)))

    def resolve(self, query: str) -> tuple[str | None, str]:
        """Return (kg_id, tier_name). tier_name == 'miss' on failure."""
        canon_query, _ = _split_term_abbrev(query)
        ql = canon_query.strip().lower()
        if not ql:
            return None, "miss"

        kid = self.exact.get(ql)
        if kid:
            return kid, "exact"

        kid = self.abbrev_alias.get(ql)
        if kid:
            return kid, "abbrev_alias"

        n = _norm_for_match(canon_query)
        if n:
            v = self.norm.get(n)
            if v is not None and v is not _AMBIG:
                return v, "normalized"  # type: ignore[return-value]

        tk = _token_key(canon_query)
        if tk:
            v = self.tokens.get(tk)
            if v is not None and v is not _AMBIG:
                return v, "token_set"  # type: ignore[return-value]

        if tk and len(tk) >= 2:
            qset = frozenset(tk)
            unique: set[str] = set()
            for kg_id, nset in self._by_kg:
                if not nset:
                    continue
                if qset <= nset or nset <= qset:
                    smaller = qset if len(qset) <= len(nset) else nset
                    if not _has_substantive_signal(smaller):
                        continue
                    unique.add(kg_id)
                    if len(unique) > 1:
                        break
            if len(unique) == 1:
                return next(iter(unique)), "token_sub"

        return None, "miss"


# ══════════════════════════════════════════════════════════════════
# 3. Catalog metadata helpers
# ══════════════════════════════════════════════════════════════════

_DD_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}
_DD_RE = re.compile(r"(\d{4})\s+(\w+)\s+(\d{1,2})")


def _parse_dd_date(text: str) -> str | None:
    """Parse '2018 January 04' → '2018-01-04'."""
    m = _DD_RE.match(text.strip())
    if not m:
        return None
    year, month_str, day = m.group(1), m.group(2).lower(), m.group(3)
    month = _DD_MONTHS.get(month_str)
    if not month:
        return None
    return f"{year}-{month:02d}-{int(day):02d}"


def _extract_source_title(raw: str | None) -> str:
    if not raw or not raw.strip():
        return ""
    try:
        items = json.loads(raw)
        if isinstance(items, list) and items:
            return (items[0].get("title") or "").strip()
    except (json.JSONDecodeError, TypeError):
        pass
    return ""


def _pick_newest_dd(raw: str | None) -> str:
    if not raw or not raw.strip():
        return ""
    parts = [p.strip() for p in raw.split(";") if p.strip()]
    parsed = [_parse_dd_date(p) for p in parts]
    valid = [d for d in parsed if d]
    if not valid:
        return raw.strip()
    return max(valid)


# ══════════════════════════════════════════════════════════════════
# 4. PostgreSQL data loading
# ══════════════════════════════════════════════════════════════════

def _get_pg_client():
    try:
        from labeler.db.db_connect import get_postgres_client
    except ImportError:
        try:
            from ..db.db_connect import get_postgres_client  # type: ignore[import]
        except ImportError:
            import sys
            sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
            from labeler.db.db_connect import get_postgres_client
    return get_postgres_client()


@dataclass
class PGData:
    """All data read from PostgreSQL (+ on-disk page labels), ready for
    graph construction."""
    collections: set[str] = field(default_factory=set)
    documents: dict[str, str] = field(default_factory=dict)          # doc_id → collection
    catalog: dict[str, dict[str, Any]] = field(default_factory=dict) # doc_id → catalog row
    entities: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    # doc_id → page_index → {label, confidence, image_path}
    page_labels: dict[str, dict[int, dict[str, Any]]] = field(default_factory=dict)


def _load_page_labels_from_docs(
    docs_dir: Path,
    doc_ids: list[str],
) -> dict[str, dict[int, dict[str, Any]]]:
    """Load per-page family labels from ``{docs_dir}/{doc_id}/labels.jsonl``.

    Each line of labels.jsonl is a dict with at least ``page_index`` and
    ``label``. Missing files are tolerated — the caller will fall back to
    an UNKNOWN_PAGE for that document.
    """
    out: dict[str, dict[int, dict[str, Any]]] = {}
    missing = 0
    for doc_id in doc_ids:
        path = docs_dir / doc_id / "labels.jsonl"
        if not path.is_file():
            missing += 1
            continue
        pages: dict[int, dict[str, Any]] = {}
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    pi = row.get("page_index")
                    if pi is None:
                        continue
                    try:
                        pi_int = int(pi)
                    except (TypeError, ValueError):
                        continue
                    pages[pi_int] = {
                        "label": (row.get("label") or "").strip() or UNKNOWN_PAGE_LABEL,
                        "confidence": row.get("confidence"),
                        "image_path": (row.get("image_path") or "").strip(),
                    }
        except OSError as exc:
            log.warning("Could not read %s: %s", path, exc)
            continue
        if pages:
            out[doc_id] = pages
    log.info("  page labels loaded for %d/%d documents (%d missing)",
             len(out), len(doc_ids), missing)
    return out


def load_from_postgres(
    node_table: str = NODE_TABLE,
    edge_table: str = EDGE_TABLE,
    catalog_table: str = CATALOG_TABLE,
    docs_dir: Path | None = None,
) -> PGData:
    client = _get_pg_client()
    data = PGData()
    try:
        log.info("Reading nodes from %s …", node_table)
        node_rows = client.fetch_all(
            f"SELECT collection_name, document_id, page_number, term, "
            f"       top_category, specific_category, "
            f"       wikipedia_category, wikipedia_url, "
            f"       confidence, witness "
            f"FROM {node_table}"
        )
        for row in node_rows:
            coll = (row.get("collection_name") or "").strip()
            doc_id = (row.get("document_id") or "").strip()
            if coll:
                data.collections.add(coll)
            if doc_id and coll:
                data.documents[doc_id] = coll
            data.entities.append(row)
        log.info("  %d entity rows loaded", len(data.entities))

        log.info("Reading edges from %s …", edge_table)
        edge_rows = client.fetch_all(
            f"SELECT collection_name, document_id, "
            f"       term_1, semantic_category_1, "
            f"       term_2, semantic_category_2, "
            f"       relationship, relation_category, confidence "
            f"FROM {edge_table}"
        )
        data.edges = edge_rows
        log.info("  %d edge rows loaded", len(data.edges))

        doc_ids = list(data.documents.keys())
        if doc_ids:
            log.info("Reading catalog from %s …", catalog_table)
            catalog_rows = client.fetch_all(
                f'SELECT id, bn, "case", source, dd, industry, dt '
                f"FROM {catalog_table} "
                f"WHERE id = ANY(%s)",
                (doc_ids,),
            )
            for row in catalog_rows:
                data.catalog[row["id"]] = row
            log.info("  %d catalog rows matched", len(data.catalog))
    finally:
        client.close()

    # Load per-page family labels from on-disk labels.jsonl files.
    resolved_docs_dir = Path(docs_dir) if docs_dir else DEFAULT_DOCS_DIR
    if data.documents:
        log.info("Reading page labels from %s …", resolved_docs_dir)
        data.page_labels = _load_page_labels_from_docs(
            resolved_docs_dir, list(data.documents.keys())
        )
    return data


# ══════════════════════════════════════════════════════════════════
# 5. KG0 graph builder
# ══════════════════════════════════════════════════════════════════

class KG0Builder:
    """Translates PGData into Cypher statements for Neo4j / Memgraph."""

    def __init__(self, driver, *, dry_run: bool = False, batch_size: int = 500, database: str = ""):
        self.driver = driver
        self.dry_run = dry_run
        self.batch_size = batch_size
        self.database = database
        self.stats: dict[str, int] = defaultdict(int)
        self._constraint_cache: set[str] = set()

    # --- low-level helpers -------------------------------------------

    def _session_kwargs(self) -> dict[str, str]:
        return {"database": self.database} if self.database else {}

    def _run(self, cypher: str, **params):
        if self.dry_run:
            return
        with self.driver.session(**self._session_kwargs()) as session:
            session.run(cypher, **params)

    def _run_batch(self, cypher: str, rows: list[dict]):
        if self.dry_run or not rows:
            return
        with self.driver.session(**self._session_kwargs()) as session:
            for i in range(0, len(rows), self.batch_size):
                session.run(cypher, rows=rows[i : i + self.batch_size])

    def _ensure_constraint(self, label: str) -> None:
        if label in self._constraint_cache:
            return
        try:
            self._run(
                f"CREATE CONSTRAINT IF NOT EXISTS "
                f"FOR (n:{label}) REQUIRE n.kg_id IS UNIQUE"
            )
        except Exception as exc:
            try:
                self._run(
                    f"CREATE CONSTRAINT ON (n:{label}) "
                    f"ASSERT n.kg_id IS UNIQUE"
                )
            except Exception:
                log.warning("Could not create constraint for %s: %s", label, exc)
        self._constraint_cache.add(label)

    # --- node builders -----------------------------------------------

    def setup_constraints(self):
        for label in ("Collection", "Document", "Page", "Abbreviation"):
            self._ensure_constraint(label)
        log.info("Base constraints created")

    def create_collections(self, collections: set[str]):
        rows = []
        for name in sorted(collections):
            kg_id = sha_id("collection_strong", name.lower())
            rows.append({"kg_id": kg_id, "name": name})
        self._run_batch(
            "UNWIND $rows AS r "
            "MERGE (c:Collection {kg_id: r.kg_id}) "
            "SET c.id = r.kg_id, c.name = r.name",
            rows,
        )
        self.stats["collections"] = len(rows)
        log.info("  %d Collection nodes", len(rows))
        return {r["name"]: r["kg_id"] for r in rows}

    def create_documents(
        self,
        documents: dict[str, str],
        coll_kg_ids: dict[str, str],
        catalog: dict[str, dict[str, Any]],
    ):
        doc_rows = []
        link_rows = []
        for doc_id, coll_name in sorted(documents.items()):
            kg_id = sha_id("doc_strong", doc_id)
            cat = catalog.get(doc_id, {})
            doc_rows.append({
                "kg_id": kg_id,
                "document_id": doc_id,
                "name": doc_id,
                "collection": coll_name,
                "batesNumber": (cat.get("bn") or "").strip(),
                "case": (cat.get("case") or "").strip(),
                "source": _extract_source_title(cat.get("source")),
                "documentDate": _pick_newest_dd(cat.get("dd")),
                "industry": (cat.get("industry") or "").strip(),
                "documentType": (cat.get("dt") or "").strip(),
            })
            coll_kg_id = coll_kg_ids.get(coll_name)
            if coll_kg_id:
                link_rows.append({"coll_kg_id": coll_kg_id, "doc_kg_id": kg_id})

        self._run_batch(
            "UNWIND $rows AS r "
            "MERGE (d:Document {kg_id: r.kg_id}) "
            "SET d.id            = r.kg_id, "
            "    d.document_id   = r.document_id, "
            "    d.name          = r.name, "
            "    d.collection    = r.collection, "
            "    d.batesNumber   = r.batesNumber, "
            "    d.case          = r.case, "
            "    d.source        = r.source, "
            "    d.documentDate  = r.documentDate, "
            "    d.industry      = r.industry, "
            "    d.documentType  = r.documentType",
            doc_rows,
        )
        self._run_batch(
            "UNWIND $rows AS r "
            "MATCH (c:Collection {kg_id: r.coll_kg_id}) "
            "MATCH (d:Document   {kg_id: r.doc_kg_id}) "
            "MERGE (c)-[:CONTAINS_DOCUMENTS]->(d)",
            link_rows,
        )
        self.stats["documents"] = len(doc_rows)
        self.stats["collection_doc_edges"] = len(link_rows)
        log.info("  %d Document nodes, %d CONTAINS_DOCUMENTS edges",
                 len(doc_rows), len(link_rows))
        return {r["document_id"]: r["kg_id"] for r in doc_rows}

    def create_pages(
        self,
        doc_kg_ids: dict[str, str],
        page_labels: dict[str, dict[int, dict[str, Any]]],
        entities: list[dict[str, Any]],
    ) -> dict[tuple[str, int], str]:
        """Create Page nodes and Document→Page edges.

        For every document we materialize the union of pages referenced
        by (a) the on-disk labels.jsonl file and (b) page_number values
        observed on entity rows. Rows without a page_number — or docs
        without a labels file — still get an UNKNOWN_PAGE_INDEX placeholder
        so every mention can attach to a Page.

        Returns ``(doc_id, page_index) → page_kg_id`` for the link builder.
        """
        # 1. Collect the set of (doc_id, page_index) pairs we need.
        needed: dict[tuple[str, int], dict[str, Any]] = {}

        for doc_id, pages in page_labels.items():
            if doc_id not in doc_kg_ids:
                continue
            for page_idx, meta in pages.items():
                needed[(doc_id, page_idx)] = {
                    "label": meta.get("label") or UNKNOWN_PAGE_LABEL,
                    "confidence": meta.get("confidence"),
                    "image_path": meta.get("image_path") or "",
                }

        # Observed pages from entity rows — fill in any pages not covered
        # by labels.jsonl, plus the fallback unknown page when page_number
        # is NULL.
        for row in entities:
            doc_id = (row.get("document_id") or "").strip()
            if not doc_id or doc_id not in doc_kg_ids:
                continue
            raw_pn = row.get("page_number")
            try:
                page_idx = int(raw_pn) if raw_pn is not None else UNKNOWN_PAGE_INDEX
            except (TypeError, ValueError):
                page_idx = UNKNOWN_PAGE_INDEX
            key = (doc_id, page_idx)
            if key not in needed:
                needed[key] = {
                    "label": UNKNOWN_PAGE_LABEL,
                    "confidence": None,
                    "image_path": "",
                }

        # Ensure every document has at least one page node so downstream
        # queries never return a Document without a :Page child.
        for doc_id in doc_kg_ids:
            if not any(d == doc_id for (d, _) in needed):
                needed[(doc_id, UNKNOWN_PAGE_INDEX)] = {
                    "label": UNKNOWN_PAGE_LABEL,
                    "confidence": None,
                    "image_path": "",
                }

        # 2. Build rows and emit.
        page_kg_ids: dict[tuple[str, int], str] = {}
        page_rows: list[dict[str, Any]] = []
        hp_rows: list[dict[str, Any]] = []

        for (doc_id, page_idx), meta in sorted(needed.items()):
            kg_id = sha_id("page_strong", doc_id, str(page_idx))
            page_kg_ids[(doc_id, page_idx)] = kg_id
            page_rows.append({
                "kg_id": kg_id,
                "document_id": doc_id,
                "page_index": page_idx,
                "label": meta["label"],
                "confidence": meta.get("confidence") if meta.get("confidence") is not None else "",
                "image_path": meta.get("image_path") or "",
            })
            hp_rows.append({
                "doc_kg_id": doc_kg_ids[doc_id],
                "page_kg_id": kg_id,
            })

        self._run_batch(
            "UNWIND $rows AS r "
            "MERGE (p:Page {kg_id: r.kg_id}) "
            "SET p.id = r.kg_id, "
            "    p.document_id = r.document_id, "
            "    p.page_index = r.page_index, "
            "    p.label = r.label, "
            "    p.confidence = r.confidence, "
            "    p.image_path = r.image_path",
            page_rows,
        )
        self._run_batch(
            "UNWIND $rows AS r "
            "MATCH (d:Document {kg_id: r.doc_kg_id}) "
            "MATCH (p:Page     {kg_id: r.page_kg_id}) "
            "MERGE (d)-[:HAS_PAGE]->(p)",
            hp_rows,
        )
        self.stats["pages"] = len(page_rows)
        self.stats["document_page_edges"] = len(hp_rows)
        log.info("  %d Page nodes, %d HAS_PAGE edges", len(page_rows), len(hp_rows))
        return page_kg_ids

    def create_entities_and_page_links(
        self,
        entities: list[dict[str, Any]],
        doc_kg_ids: dict[str, str],
        page_kg_ids: dict[tuple[str, int], str],
    ):
        """Create entity nodes and Page→Entity edges.

        Each entity gets dual Neo4j labels from the DB (e.g. :Person:Employee).
        Witness text and confidence are stored on the edge so that the
        same canonical entity mentioned on multiple pages retains a
        distinct per-page witness while the Entity node itself stays
        deduped at corpus scope.
        """
        abbrev_map = _build_abbrev_map(entities)

        # Phase 1: deduplicate entities, collect page links
        entity_map: dict[tuple[str, str], dict[str, Any]] = {}
        # key: (page_kg_id, entity_kg_id, rel_type) → edge row (witnesses merged)
        page_link_map: dict[tuple[str, str, str], dict[str, Any]] = {}
        abbrev_nodes: dict[str, dict[str, Any]] = {}
        abbrev_edges: list[dict[str, Any]] = []

        skipped_no_label = 0
        skipped_no_page = 0
        for row in entities:
            raw_term = (row.get("term") or "").strip()
            if not raw_term:
                continue
            canon_name, labels, abbrev = _canonical_for_row(row, abbrev_map)
            if not labels:
                skipped_no_label += 1
                continue
            primary_label = labels[0]
            doc_id = (row.get("document_id") or "").strip()
            key = (primary_label, canon_name.lower())

            if key not in entity_map:
                kg_id = sha_id("entity_weak", primary_label, canon_name.lower())
                entity_map[key] = {
                    "kg_id": kg_id,
                    "labels": list(labels),
                    "name": canon_name,
                    "top_category": row.get("top_category") or "",
                    "specific_category": row.get("specific_category") or "",
                    "confidence": row.get("confidence") or "",
                    "wikipedia_url": row.get("wikipedia_url") or "",
                    "wikipedia_category": row.get("wikipedia_category") or "",
                }
            else:
                for lbl in labels:
                    if lbl not in entity_map[key]["labels"]:
                        entity_map[key]["labels"].append(lbl)

            entity_kg_id = entity_map[key]["kg_id"]

            # Abbreviation tracking
            if abbrev:
                abbr_lower = abbrev.lower()
                abbr_kg_id = sha_id("entity_weak", "Abbreviation", abbr_lower)
                if abbr_lower not in abbrev_nodes:
                    abbrev_nodes[abbr_lower] = {
                        "kg_id": abbr_kg_id,
                        "name": abbrev,
                        "expanded_form": canon_name,
                    }
                abbrev_edges.append({
                    "entity_kg_id": entity_kg_id,
                    "abbrev_kg_id": abbr_kg_id,
                })

            # Page→Entity link
            if doc_id not in doc_kg_ids:
                skipped_no_page += 1
                continue
            raw_pn = row.get("page_number")
            try:
                page_idx = int(raw_pn) if raw_pn is not None else UNKNOWN_PAGE_INDEX
            except (TypeError, ValueError):
                page_idx = UNKNOWN_PAGE_INDEX
            page_kg_id = page_kg_ids.get((doc_id, page_idx))
            if page_kg_id is None:
                # Fall back to the unknown page for this document.
                page_kg_id = page_kg_ids.get((doc_id, UNKNOWN_PAGE_INDEX))
            if page_kg_id is None:
                skipped_no_page += 1
                continue

            rel_type = LABEL_TO_REL.get(primary_label, DEFAULT_REL)
            edge_key = (page_kg_id, entity_kg_id, rel_type)
            row_witness = (row.get("witness") or "").strip()
            row_conf = row.get("confidence") or ""
            existing = page_link_map.get(edge_key)
            if existing is None:
                page_link_map[edge_key] = {
                    "page_kg_id": page_kg_id,
                    "entity_kg_id": entity_kg_id,
                    "rel_type": rel_type,
                    "label": primary_label,
                    "witness": row_witness,
                    "confidence": row_conf,
                }
            elif row_witness and row_witness not in existing["witness"]:
                # Same (page, entity, rel) seen again — concat distinct
                # witness spans so no evidence is lost.
                if existing["witness"]:
                    existing["witness"] = existing["witness"] + " | " + row_witness
                else:
                    existing["witness"] = row_witness

        # Phase 2: batch-create entity nodes grouped by label tuple
        by_label_tuple: dict[tuple[str, ...], list[dict]] = defaultdict(list)
        for ent in entity_map.values():
            by_label_tuple[tuple(ent["labels"])].append(ent)

        for label_tuple, rows in by_label_tuple.items():
            label_str = ":".join(label_tuple)
            primary_label = label_tuple[0]
            secondary_labels = label_tuple[1:]
            label_set_clause = ""
            if secondary_labels:
                label_set_clause = "SET e:" + ":".join(secondary_labels) + " "
            self._ensure_constraint(primary_label)
            self._run_batch(
                f"UNWIND $rows AS r "
                f"MERGE (e:{primary_label} {{kg_id: r.kg_id}}) "
                f"{label_set_clause}"
                f"SET e.id = r.kg_id, "
                f"    e.name = r.name, "
                f"    e.top_category = r.top_category, "
                f"    e.specific_category = r.specific_category, "
                f"    e.confidence = r.confidence, "
                f"    e.wikipedia_url = r.wikipedia_url, "
                f"    e.wikipedia_category = r.wikipedia_category",
                rows,
            )
            self.stats[f"entities_{label_str}"] += len(rows)
            log.info("    %d :%s nodes", len(rows), label_str)

        # Phase 3: batch-create Page→Entity edges grouped by rel type.
        # Witness text and confidence travel on the edge so that a single
        # shared Entity node can carry distinct per-page evidence.
        by_rel: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for link in page_link_map.values():
            by_rel[(link["rel_type"], link["label"])].append(link)

        for (rel_type, label), rows in by_rel.items():
            self._run_batch(
                f"UNWIND $rows AS r "
                f"MATCH (p:Page    {{kg_id: r.page_kg_id}}) "
                f"MATCH (e:{label} {{kg_id: r.entity_kg_id}}) "
                f"MERGE (p)-[rel:{rel_type}]->(e) "
                f"SET rel.witness = r.witness, "
                f"    rel.confidence = r.confidence",
                rows,
            )
            self.stats[f"page_edges_{rel_type}"] += len(rows)

        # Phase 4: Abbreviation nodes + edges
        if abbrev_nodes:
            self._run_batch(
                "UNWIND $rows AS r "
                "MERGE (a:Abbreviation {kg_id: r.kg_id}) "
                "SET a.id = r.kg_id, a.name = r.name, "
                "    a.expanded_form = r.expanded_form",
                list(abbrev_nodes.values()),
            )
            self.stats["entities_Abbreviation"] += len(abbrev_nodes)
            log.info("    %d :Abbreviation nodes", len(abbrev_nodes))

        if abbrev_edges:
            seen: set[tuple[str, str]] = set()
            unique_edges: list[dict[str, Any]] = []
            for e in abbrev_edges:
                k = (e["entity_kg_id"], e["abbrev_kg_id"])
                if k not in seen:
                    seen.add(k)
                    unique_edges.append(e)
            self._run_batch(
                "UNWIND $rows AS r "
                "MATCH (e {kg_id: r.entity_kg_id}) "
                "MATCH (a:Abbreviation {kg_id: r.abbrev_kg_id}) "
                "MERGE (e)-[:HAS_ABBREVIATION]->(a)",
                unique_edges,
            )
            self.stats["entity_abbreviation_edges"] = len(unique_edges)
            log.info("    %d HAS_ABBREVIATION edges", len(unique_edges))

        total_ent = sum(len(v) for v in by_label_tuple.values()) + len(abbrev_nodes)
        total_page_edges = sum(len(v) for v in by_rel.values())
        log.info("  %d entity nodes (incl. %d abbreviations), %d page→entity edges",
                 total_ent, len(abbrev_nodes), total_page_edges)
        if skipped_no_label:
            log.warning("  %d rows skipped (no usable top/specific category)",
                        skipped_no_label)
            self.stats["entities_skipped_no_label"] = skipped_no_label
        if skipped_no_page:
            log.warning("  %d rows skipped (no resolvable page)", skipped_no_page)
            self.stats["entities_skipped_no_page"] = skipped_no_page

    def create_entity_to_entity_edges(
        self,
        edges: list[dict[str, Any]],
        entities: list[dict[str, Any]],
    ):
        """Create entity↔entity edges from the edge table via fuzzy resolution."""
        resolver = EndpointResolver(entities)

        try:
            from pipeline.kg0.kg0_clean import normalize_rel, log_normalization_report
        except ModuleNotFoundError:
            from pipeline.kg0.temp.kg0_clean import normalize_rel, log_normalization_report

        by_rel: dict[str, list[dict]] = defaultdict(list)
        skipped = 0
        dropped = 0
        mapping_used: dict[str, str] = {}
        drop_counts: dict[str, int] = defaultdict(int)
        tier_counts: dict[str, int] = defaultdict(int)

        for row in edges:
            t1 = (row.get("term_1") or "").strip()
            t2 = (row.get("term_2") or "").strip()
            rel = (row.get("relationship") or "").strip()
            if not t1 or not t2 or not rel:
                skipped += 1
                continue
            kg_id_1, tier1 = resolver.resolve(t1)
            kg_id_2, tier2 = resolver.resolve(t2)
            if not kg_id_1 or not kg_id_2:
                skipped += 1
                continue
            tier_counts[tier1] += 1
            tier_counts[tier2] += 1
            slug = slugify_rel(rel)
            canonical = normalize_rel(slug)
            if canonical is None:
                drop_counts[slug] = drop_counts.get(slug, 0) + 1
                dropped += 1
                continue
            if canonical != slug:
                mapping_used[slug] = canonical
            by_rel[canonical].append({
                "kg_id_1": kg_id_1,
                "kg_id_2": kg_id_2,
                "confidence": row.get("confidence") or "",
                "relation_category": row.get("relation_category") or "",
            })

        log_normalization_report(mapping_used, dict(drop_counts))

        for rel_type, rows in by_rel.items():
            self._run_batch(
                f"UNWIND $rows AS r "
                f"MATCH (a {{kg_id: r.kg_id_1}}) "
                f"MATCH (b {{kg_id: r.kg_id_2}}) "
                f"MERGE (a)-[e:{rel_type}]->(b) "
                f"SET e.confidence = r.confidence, "
                f"    e.relation_category = r.relation_category",
                rows,
            )
            self.stats[f"e2e_{rel_type}"] += len(rows)

        total = sum(len(v) for v in by_rel.values())
        log.info("  %d entity→entity edges (%d rel types, %d skipped, %d dropped)",
                 total, len(by_rel), skipped, dropped)
        if tier_counts:
            log.info("  Endpoint resolution tiers:")
            for k in ("exact", "abbrev_alias", "normalized", "token_set", "token_sub", "miss"):
                if tier_counts.get(k):
                    log.info("    %-13s %d", k, tier_counts[k])
                    self.stats[f"resolver_{k}"] = tier_counts[k]

    # --- orchestrator ------------------------------------------------

    def build(self, data: PGData):
        log.info("=== KG0 build start ===")
        self.setup_constraints()
        coll_kg_ids = self.create_collections(data.collections)
        doc_kg_ids = self.create_documents(data.documents, coll_kg_ids, data.catalog)
        page_kg_ids = self.create_pages(doc_kg_ids, data.page_labels, data.entities)
        self.create_entities_and_page_links(data.entities, doc_kg_ids, page_kg_ids)
        self.create_entity_to_entity_edges(data.edges, data.entities)
        log.info("=== KG0 build complete ===")
        log.info("Stats: %s", dict(self.stats))


# ══════════════════════════════════════════════════════════════════
# 6. CLI
# ══════════════════════════════════════════════════════════════════

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Build KG0 in Neo4j/Memgraph from PostgreSQL raw-data tables."
    )
    p.add_argument("--neo4j-uri", default="bolt://127.0.0.1:7687")
    p.add_argument("--neo4j-user", default="")
    p.add_argument("--neo4j-password", default="")
    p.add_argument("--neo4j-database", default="")
    p.add_argument("--node-table", default=NODE_TABLE)
    p.add_argument("--edge-table", default=EDGE_TABLE)
    p.add_argument("--docs-dir", default=str(DEFAULT_DOCS_DIR),
                   help="Directory containing {doc_id}/labels.jsonl files "
                        "for per-page family labels.")
    p.add_argument("--batch-size", type=int, default=500)
    p.add_argument("--dry-run", action="store_true",
                    help="Load from PG but skip all Neo4j writes")
    p.add_argument("--wipe", action="store_true",
                    help="Delete all nodes/edges before building")
    p.add_argument("--log-level", default="INFO",
                    choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    data = load_from_postgres(
        node_table=args.node_table,
        edge_table=args.edge_table,
        docs_dir=Path(args.docs_dir),
    )
    log.info("Loaded %d collections, %d documents, %d entities, %d edges from PG",
             len(data.collections), len(data.documents),
             len(data.entities), len(data.edges))

    if args.dry_run:
        label_counts: dict[str, int] = defaultdict(int)
        skipped_no_label = 0
        for row in data.entities:
            labels = resolve_labels(row.get("top_category"), row.get("specific_category"))
            if not labels:
                skipped_no_label += 1
                continue
            label_counts[":".join(labels)] += 1
        log.info("Label distribution preview:")
        for label, count in sorted(label_counts.items(), key=lambda x: -x[1]):
            log.info("  %-40s %d", label, count)
        if skipped_no_label:
            log.warning("  %d rows would be skipped", skipped_no_label)
        log.info("Dry run complete.")
        return 0

    from neo4j import GraphDatabase
    auth = (args.neo4j_user, args.neo4j_password) if args.neo4j_user else None
    driver = GraphDatabase.driver(args.neo4j_uri, auth=auth)

    try:
        if args.wipe:
            log.warning("Wiping all existing graph data …")
            session_kwargs = {"database": args.neo4j_database} if args.neo4j_database else {}
            with driver.session(**session_kwargs) as session:
                session.run("MATCH (n) DETACH DELETE n")
            log.info("Graph wiped.")

        builder = KG0Builder(
            driver,
            dry_run=False,
            batch_size=args.batch_size,
            database=args.neo4j_database,
        )
        builder.build(data)
    finally:
        driver.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
