"""
align/infrastructure/index_facade.py
Store facade for Neo4j graph retrieval and Qdrant semantic retrieval.

Each store interface is defined as an ABC.  IndexFacade accepts optional
implementations via constructor injection; when omitted it falls back to
the production implementations (QdrantIndex, Neo4jStore, EmbeddingService).
This allows tests to supply in-memory fakes without standing up external
infrastructure.
"""

from __future__ import annotations
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..core_types import (
    CandidateArtifact,
    CompiledRetrievalQuery,
    CompiledScopePredicate,
    FusionMethod,
    ScopeMode,
)
from ...operators.configs import AlignConfig

logger = logging.getLogger(__name__)


# ============================================================
# Abstract Index Interfaces
# ============================================================

class LexicalIndex(ABC):
    """Interface for lexical (BM25) retrieval."""

    @abstractmethod
    def search(
        self,
        query: str,
        boost_query: str,
        filter_queries: List[str],
        fields: List[str],
        top_k: int,
    ) -> List[Dict[str, Any]]:
        ...

    @abstractmethod
    def get_text(self, artifact_id: str, fields: List[str]) -> Dict[str, Any]:
        ...

    @abstractmethod
    def get_texts_batch(
        self, artifact_ids: List[str], fields: List[str]
    ) -> Dict[str, Dict[str, Any]]:
        ...


class SemanticIndex(ABC):
    """Interface for embedding-based retrieval."""

    @abstractmethod
    def search(
        self,
        vector: List[float],
        payload_filter: Dict[str, Any],
        top_k: int,
    ) -> List[Dict[str, Any]]:
        ...

    @abstractmethod
    def get_similar(
        self,
        point_id: str,
        payload_filter: Dict[str, Any],
        top_k: int,
    ) -> List[Dict[str, Any]]:
        ...


class GraphStore(ABC):
    """Interface for KG0 graph operations."""

    @abstractmethod
    def execute_cypher(
        self, query: str, parameters: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        ...

    @abstractmethod
    def get_node(self, node_id: str) -> Optional[Dict[str, Any]]:
        ...

    @abstractmethod
    def get_neighbors(
        self,
        node_id: str,
        relationship_types: Optional[List[str]] = None,
        direction: str = "BOTH",
        max_hops: int = 1,
    ) -> List[Dict[str, Any]]:
        ...

    @abstractmethod
    def find_paths(
        self,
        start_id: str,
        end_id: str,
        max_hops: int = 3,
        relationship_types: Optional[List[str]] = None,
    ) -> List[List[Dict[str, Any]]]:
        ...

    @abstractmethod
    def get_subgraph(
        self,
        node_ids: List[str],
        max_hops: int = 1,
    ) -> Dict[str, Any]:
        ...

    def close(self) -> None:
        """Release resources.  No-op by default so test fakes can
        ignore lifecycle management."""


class Embedder(ABC):
    """Interface for dense text embedding."""

    @abstractmethod
    def embed(self, text: str) -> List[float]:
        ...

    @abstractmethod
    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        ...


# ============================================================
# Solr Implementation
# ============================================================

class SolrIndex(LexicalIndex):
    """
    Solr-backed lexical retrieval.

    Schema expectations for the current ``document_ocr_text`` core:
        id: artifact element ID (matches Neo4j)
        document_id: source document ID
        title: document title
        body: full text content
        collection: collection name (facet field)
        family: artifact family (facet field)
        date: optional document date from master_collection_catalog.dd
        features: optional feature labels
        labels: per-page family labels
        page_count: page count
        indexed_at: Solr ingest timestamp
    """

    def __init__(self, config: AlignConfig):
        self.config = config
        self._solr = None

    @property
    def solr(self):
        if self._solr is None:
            import pysolr
            self._solr = pysolr.Solr(
                f"{self.config.solr.url}/{self.config.solr.collection}",
                timeout=self.config.solr.timeout,
                always_commit=False,
            )
        return self._solr

    def search(
        self,
        query: str,
        boost_query: str = "",
        filter_queries: List[str] = None,
        fields: List[str] = None,
        top_k: int = 250,
    ) -> List[Dict[str, Any]]:
        params = {
            "rows": top_k,
            "defType": "edismax",
            "qf": "title^3 body document_id collection",
        }
        if boost_query:
            params["bq"] = boost_query
        if filter_queries:
            params["fq"] = filter_queries
        if fields:
            params["fl"] = ",".join(fields)
        else:
            params["fl"] = "id,title,document_id,collection,family,date,labels,page_count,indexed_at,score"

        results = self.solr.search(query, **params)
        return [
            {
                "id": doc.get("id", ""),
                "score": getattr(doc, "score", 0.0) if hasattr(doc, "score") else doc.get("score", 0.0),
                **{k: v for k, v in doc.items() if k not in ("id", "score")},
            }
            for doc in results
        ]

    def get_text(self, artifact_id: str, fields: List[str] = None) -> Dict[str, Any]:
        if fields is None:
            fields = ["id", "body", "title", "document_id", "collection", "family", "date", "labels", "page_count", "indexed_at"]
        results = self.solr.search(
            f"id:{artifact_id}",
            fl=",".join(fields),
            rows=1,
        )
        if results:
            return dict(results.docs[0])
        return {}

    def get_texts_batch(
        self, artifact_ids: List[str], fields: List[str] = None
    ) -> Dict[str, Dict[str, Any]]:
        if not artifact_ids:
            return {}
        if fields is None:
            fields = ["id", "body", "title", "document_id", "collection", "family", "date", "labels", "page_count", "indexed_at"]

        batch_results = {}
        chunk_size = self.config.solr.batch_size
        for i in range(0, len(artifact_ids), chunk_size):
            chunk = artifact_ids[i : i + chunk_size]
            id_query = " OR ".join(f'id:"{aid}"' for aid in chunk)
            results = self.solr.search(
                id_query,
                fl=",".join(fields),
                rows=len(chunk),
            )
            for doc in results:
                batch_results[doc.get("id", "")] = dict(doc)
        return batch_results


# ============================================================
# Qdrant Implementation
# ============================================================

class QdrantIndex(SemanticIndex):
    """
    Qdrant-backed semantic retrieval.

    Point structure:
        id: artifact element ID (matches Neo4j and Solr)
        vector: dense embedding (768-dim default)
        payload:
            collection: str
            family: str
            artifact_id: str
            document_id: str
            document_name: str
            page_index: int
            text: str
    """

    def __init__(self, config: AlignConfig):
        self.config = config
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from qdrant_client import QdrantClient
            self._client = QdrantClient(
                host=self.config.qdrant.host,
                port=self.config.qdrant.port,
                grpc_port=self.config.qdrant.grpc_port,
                prefer_grpc=self.config.qdrant.use_grpc,
            )
        return self._client

    def search(
        self,
        vector: List[float],
        payload_filter: Dict[str, Any] = None,
        top_k: int = 250,
    ) -> List[Dict[str, Any]]:
        from qdrant_client.models import Filter, FieldCondition, MatchValue, MatchAny, Range

        qdrant_filter = None
        if payload_filter:
            qdrant_filter = self._build_filter(payload_filter)

        if hasattr(self.client, "search"):
            results = self.client.search(
                collection_name=self.config.qdrant.collection_name,
                query_vector=vector,
                query_filter=qdrant_filter,
                limit=top_k,
                with_payload=True,
            )
        else:
            query_response = self.client.query_points(
                collection_name=self.config.qdrant.collection_name,
                query=vector,
                query_filter=qdrant_filter,
                limit=top_k,
                with_payload=True,
            )
            results = query_response.points
        return [
            {
                "id": str(hit.id),
                "score": hit.score,
                **(hit.payload or {}),
            }
            for hit in results
        ]

    def get_similar(
        self,
        point_id: str,
        payload_filter: Dict[str, Any] = None,
        top_k: int = 10,
    ) -> List[Dict[str, Any]]:
        from qdrant_client.models import Filter

        qdrant_filter = None
        if payload_filter:
            qdrant_filter = self._build_filter(payload_filter)

        results = self.client.recommend(
            collection_name=self.config.qdrant.collection_name,
            positive=[point_id],
            query_filter=qdrant_filter,
            limit=top_k,
            with_payload=True,
        )
        return [
            {
                "id": str(hit.id),
                "score": hit.score,
                **(hit.payload or {}),
            }
            for hit in results
        ]

    def _build_filter(self, payload_filter: Dict[str, Any]):
        """Convert our generic filter dict to Qdrant Filter model."""
        from qdrant_client.models import (
            Filter, FieldCondition, MatchValue, MatchAny, Range,
        )
        conditions = []
        for key, value in payload_filter.items():
            if isinstance(value, dict):
                if "any" in value:
                    conditions.append(
                        FieldCondition(key=key, match=MatchAny(any=value["any"]))
                    )
                elif "gte" in value or "lte" in value:
                    conditions.append(
                        FieldCondition(
                            key=key,
                            range=Range(
                                gte=value.get("gte"),
                                lte=value.get("lte"),
                            ),
                        )
                    )
                elif "not" in value:
                    pass
            elif isinstance(value, list):
                conditions.append(
                    FieldCondition(key=key, match=MatchAny(any=value))
                )
            else:
                conditions.append(
                    FieldCondition(key=key, match=MatchValue(value=value))
                )
        return Filter(must=conditions) if conditions else None


# ============================================================
# Neo4j Implementation
# ============================================================

class Neo4jStore(GraphStore):
    """
    Neo4j-backed KG0 graph operations.

    KG0 schema (produced by ``pipeline/kg0/kg0_from_db.py`` and
    ``pipeline/kg0/load_pages.py``):

        Infrastructure labels
            Collection, Document, Page, Abbreviation

        Entity labels
            Dynamic dual labels derived from the DB top_category /
            specific_category columns (PascalCase). Common primary
            labels: Person, Organization, Drug, Product, Location,
            Gpe, Topic, LegalFramework, Claim, Event, Regulation,
            BusinessConcept, BusinessEntity, ClinicalEntity, Role, ...

        Structural relationship types
            CONTAINS_DOCUMENTS   Collection -> Document
            HAS_PAGE             Document -> Page
            HAS_ABBREVIATION     Entity -> Abbreviation

        Document -> Entity relationship types (LABEL_TO_DOC_REL)
            MENTIONS_PERSON, MENTIONS_ORG, MENTIONS_DRUG,
            MENTIONS_PRODUCT, MENTIONS_LOCATION_IN_TEXT,
            MENTIONS_HEALTH, MENTIONS_DATE, HAS_EVENT, HAS_RISK,
            HAS_DECISION, HAS_REQUIREMENT, HAS_CLAIM, HAS_METRIC,
            HAS_IDENTIFIER, HAS_PROCEDURE, CITES

        Entity <-> Entity relationship types
            Free-text slugified verbs normalized by kg0_clean.py
            (WORKS_FOR, LOCATED_IN, HAS_CONTACT, SENT_TO, CITES,
            AFFILIATED_WITH, ...).

    Note on per-page family dispatch: artifact family ("email",
    "document", "spreadsheet", "text", "presentation") is a property
    of the Page layer (p.label), NOT the Document layer. 63% of the
    opioid corpus documents are mixed-label.
    """

    def __init__(self, config: AlignConfig):
        self.config = config
        self._driver = None

    @property
    def driver(self):
        if self._driver is None:
            from neo4j import GraphDatabase
            self._driver = GraphDatabase.driver(
                self.config.neo4j.uri,
                auth=(self.config.neo4j.user, self.config.neo4j.password),
                max_connection_pool_size=self.config.neo4j.max_connection_pool_size,
            )
        return self._driver

    def execute_cypher(
        self, query: str, parameters: Dict[str, Any] = None
    ) -> List[Dict[str, Any]]:
        with self.driver.session(database=self.config.neo4j.database) as session:
            result = session.run(query, parameters or {})
            return [record.data() for record in result]

    def get_node(self, node_id: str) -> Optional[Dict[str, Any]]:
        # Match ``kg_id`` / ``id`` list-safely (see ``find_paths``
        # comment for details) so merged rebuilds with list-valued
        # properties still resolve. ``elementId`` is kept as a last
        # resort for callers passing raw Neo4j element ids.
        results = self.execute_cypher(
            "MATCH (n) "
            "WHERE $id IN n.kg_id OR $id IN n.id OR elementId(n) = $id "
            "RETURN n, labels(n) AS labels "
            "LIMIT 1",
            {"id": node_id},
        )
        if results:
            node_data = dict(results[0]["n"])
            node_data["_labels"] = results[0]["labels"]
            node_data["_id"] = node_id
            return node_data
        return None

    def get_neighbors(
        self,
        node_id: str,
        relationship_types: Optional[List[str]] = None,
        direction: str = "BOTH",
        max_hops: int = 1,
    ) -> List[Dict[str, Any]]:
        rel_filter = ""
        if relationship_types:
            rel_types = "|".join(relationship_types)
            rel_filter = f":{rel_types}"

        if direction == "OUT":
            pattern = f"-[r{rel_filter}*1..{max_hops}]->"
        elif direction == "IN":
            pattern = f"<-[r{rel_filter}*1..{max_hops}]-"
        else:
            pattern = f"-[r{rel_filter}*1..{max_hops}]-"

        # Match on either ``kg_id`` or ``id`` so both legacy and KG0-native
        # node identifiers resolve. Using ``IN`` is list-safe: when the
        # property is a scalar, Neo4j treats it as a one-element list, so
        # the same clause handles both scalar ids and list-valued ids that
        # can appear after a merged KG0 rebuild. ``size(r)`` (not
        # ``length``) is required because ``r`` is bound to a
        # ``List<Relationship>`` by the variable-length pattern.
        query = (
            f"MATCH (start){pattern}(end) "
            f"WHERE $id IN start.kg_id OR $id IN start.id "
            f"RETURN end, labels(end) AS labels, "
            f"[rel IN r | type(rel)] AS rel_types, "
            f"size(r) AS hops "
            f"LIMIT 500"
        )
        results = self.execute_cypher(query, {"id": node_id})
        neighbors = []
        for rec in results:
            node_data = dict(rec["end"])
            node_data["_labels"] = rec["labels"]
            node_data["_rel_types"] = rec["rel_types"]
            node_data["_hops"] = rec["hops"]
            neighbors.append(node_data)
        return neighbors

    def find_paths(
        self,
        start_id: str,
        end_id: str,
        max_hops: int = 3,
        relationship_types: Optional[List[str]] = None,
    ) -> List[List[Dict[str, Any]]]:
        rel_filter = ""
        if relationship_types:
            rel_types = "|".join(relationship_types)
            rel_filter = f":{rel_types}"

        # Match by ``kg_id`` (primary in KG0) with ``id`` as fallback,
        # list-safe via ``IN`` so list-valued ids from merged rebuilds also
        # resolve.
        query = (
            f"MATCH path = shortestPath("
            f"(start)-[{rel_filter}*..{max_hops}]-(end)) "
            f"WHERE ($start_id IN start.kg_id OR $start_id IN start.id) "
            f"  AND ($end_id IN end.kg_id OR $end_id IN end.id) "
            f"RETURN [n IN nodes(path) | n] AS nodes, "
            f"[r IN relationships(path) | type(r)] AS rels "
            f"LIMIT 10"
        )
        results = self.execute_cypher(
            query, {"start_id": start_id, "end_id": end_id}
        )
        paths = []
        for rec in results:
            path_nodes = [dict(n) for n in rec["nodes"]]
            paths.append(path_nodes)
        return paths

    def get_subgraph(
        self,
        node_ids: List[str],
        max_hops: int = 1,
    ) -> Dict[str, Any]:
        # Match on kg_id/id list-safely. ``x IN n.kg_id`` handles both
        # scalar and list-valued properties uniformly; the outer
        # ``any(x IN $ids ...)`` filters the optional neighbor match.
        query = (
            "UNWIND $ids AS nid "
            "MATCH (n) WHERE nid IN n.kg_id OR nid IN n.id "
            f"OPTIONAL MATCH (n)-[r*1..{max_hops}]-(m) "
            "WHERE any(x IN $ids WHERE x IN m.kg_id OR x IN m.id) "
            "RETURN COLLECT(DISTINCT n) AS nodes, "
            "COLLECT(DISTINCT r) AS rels"
        )
        results = self.execute_cypher(query, {"ids": node_ids})
        if results:
            return results[0]
        return {"nodes": [], "rels": []}

    def close(self) -> None:
        if self._driver:
            self._driver.close()


# ============================================================
# Embedding Service
# ============================================================

class EmbeddingService(Embedder):
    """Produces dense embeddings for text using sentence-transformers."""

    def __init__(self, config: AlignConfig):
        self.config = config
        self._model = None

    @property
    def model(self):
        if self._model is None:
            from pipeline.database_env import load_pipeline_dotenv
            from sentence_transformers import SentenceTransformer

            load_pipeline_dotenv()
            self._model = SentenceTransformer(
                self.config.embedding.model_name
            )
        return self._model

    def embed(self, text: str) -> List[float]:
        return self.model.encode(
            text,
            convert_to_numpy=True,
            show_progress_bar=False,
        ).tolist()

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        embeddings = self.model.encode(
            texts,
            batch_size=self.config.embedding.batch_size,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return embeddings.tolist()


# ============================================================
# Composite Index Facade
# ============================================================

def _first_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        for item in value:
            text = str(item or "").strip()
            if text:
                return text
        return ""
    return str(value or "").strip()


class IndexFacade:
    """
    Facade coordinating semantic and graph stores.

    Every retrieval operation carries the scope predicate natively
    in each store's filter syntax, enforcing scope containment
    by construction.

    Constructor accepts optional ABC-typed implementations for each
    active store.  When omitted, production implementations are
    instantiated from the supplied config.  Pass in-memory fakes for
    testing::

        facade = IndexFacade(
            config,
            graph=StubGraphStore(nodes, edges),
        )
    """

    solr: Optional[LexicalIndex]
    qdrant: Optional[SemanticIndex]
    neo4j: GraphStore
    embedder: Optional[Embedder]

    def __init__(
        self,
        config: AlignConfig,
        lexical: Optional[LexicalIndex] = None,
        semantic: Optional[SemanticIndex] = None,
        graph: Optional[GraphStore] = None,
        embedder: Optional[Embedder] = None,
    ):
        self.config = config
        # Solr lexical and Qdrant semantic retrieval stay lazy: neither
        # backend connects until Phase 1 actually issues a search.
        self.solr = lexical if lexical is not None else SolrIndex(config)
        self.qdrant = semantic if semantic is not None else QdrantIndex(config)
        self.embedder = embedder if embedder is not None else EmbeddingService(config)
        self.neo4j = graph if graph is not None else Neo4jStore(config)

    def lexical_retrieve(
        self,
        query: CompiledRetrievalQuery,
        scope: CompiledScopePredicate,
    ) -> List[CandidateArtifact]:
        """Execute strict Solr lexical retrieval with scope filters."""
        if self.solr is None:
            raise RuntimeError("Solr lexical index is required for ALIGN Phase 1.")

        solr_query = query.solr_query or "*:*"
        fields = query.solr_fields or [
            "id",
            "title",
            "body",
            "document_id",
            "collection",
            "family",
            "date",
            "features",
            "labels",
            "page_count",
            "indexed_at",
            "score",
        ]
        raw_results = self.solr.search(
            solr_query,
            boost_query=query.solr_boost_query,
            filter_queries=scope.solr_fqs,
            fields=fields,
            top_k=query.top_k_lex,
        )

        candidates: List[CandidateArtifact] = []
        seen: set[str] = set()
        for doc in raw_results:
            artifact_id = str(doc.get("id") or doc.get("artifact_id") or "")
            if not artifact_id or artifact_id in seen:
                continue
            seen.add(artifact_id)
            score = float(doc.get("score") or 0.0)
            title = _first_text(doc.get("title"))
            if not title:
                title = _first_text(doc.get("subject"))
            if not title:
                title = _first_text(doc.get("document_name"))
            if not title:
                title = _first_text(doc.get("document_id"))
            metadata = {k: v for k, v in doc.items() if k not in ("id", "score")}
            if title:
                metadata.setdefault("artifact_name", title)
            candidates.append(
                CandidateArtifact(
                    artifact_id=artifact_id,
                    family=self.config.canonical_family(str(doc.get("family") or "DOCUMENT")),
                    artifact_name=title,
                    lex_score=score,
                    fused_score=score,
                    lex_rank=len(candidates) + 1,
                    metadata=metadata,
                )
            )
        return candidates

    def semantic_retrieve(
        self,
        query: CompiledRetrievalQuery,
        scope: CompiledScopePredicate,
    ) -> List[CandidateArtifact]:
        """Execute semantic retrieval via Qdrant with scope filters."""
        if query.qdrant_vector is None or self.qdrant is None:
            return []

        raw_results = self.qdrant.search(
            vector=query.qdrant_vector,
            payload_filter=scope.qdrant_filters,
            top_k=query.top_k_sem,
        )

        candidates: List[CandidateArtifact] = []
        seen: set[str] = set()
        for rank, doc in enumerate(raw_results, 1):
            artifact_id = str(doc.get("artifact_id") or doc.get("id") or "")
            if not artifact_id or artifact_id in seen:
                continue
            seen.add(artifact_id)
            fam = self.config.canonical_family(str(doc.get("family", "DOCUMENT")))
            candidates.append(CandidateArtifact(
                artifact_id=artifact_id,
                family=fam,
                artifact_name=str(doc.get("document_id") or ""),
                sem_score=float(doc.get("score", 0.0)),
                sem_rank=len(candidates) + 1,
                metadata={
                    **{k: v for k, v in doc.items() if k not in ("id", "score")},
                    "qdrant_point_id": str(doc.get("id") or ""),
                },
            ))
        return candidates

    def union_and_score(
        self,
        lex_results: List[CandidateArtifact],
        sem_results: List[CandidateArtifact],
        method: FusionMethod = FusionMethod.RRF,
    ) -> List[CandidateArtifact]:
        """Fuse lexical and semantic ranked lists."""
        if method == FusionMethod.RRF:
            return self._rrf_fusion(lex_results, sem_results)
        elif method == FusionMethod.LINEAR:
            return self._linear_fusion(lex_results, sem_results)
        else:
            return self._rrf_fusion(lex_results, sem_results)

    def _rrf_fusion(
        self,
        lex_results: List[CandidateArtifact],
        sem_results: List[CandidateArtifact],
    ) -> List[CandidateArtifact]:
        """Reciprocal Rank Fusion."""
        k = self.config.rrf_k
        combined: Dict[str, CandidateArtifact] = {}

        for candidate in lex_results:
            aid = candidate.artifact_id
            if aid not in combined:
                combined[aid] = CandidateArtifact(
                    artifact_id=aid,
                    family=candidate.family,
                    artifact_name=candidate.artifact_name,
                    metadata=candidate.metadata,
                )
            combined[aid].lex_score = candidate.lex_score
            combined[aid].lex_rank = candidate.lex_rank
            combined[aid].fused_score += 1.0 / (k + candidate.lex_rank)

        for candidate in sem_results:
            aid = candidate.artifact_id
            if aid not in combined:
                combined[aid] = CandidateArtifact(
                    artifact_id=aid,
                    family=candidate.family,
                    artifact_name=candidate.artifact_name,
                    metadata=candidate.metadata,
                )
            combined[aid].sem_score = candidate.sem_score
            combined[aid].sem_rank = candidate.sem_rank
            self._merge_candidate_metadata(combined[aid], candidate)
            combined[aid].fused_score += 1.0 / (k + candidate.sem_rank)

        fused = sorted(
            combined.values(),
            key=lambda c: (c.fused_score, str(c.artifact_id)),
            reverse=True,
        )
        return fused

    @staticmethod
    def _merge_candidate_metadata(
        target: CandidateArtifact,
        source: CandidateArtifact,
    ) -> None:
        if target.metadata is None:
            target.metadata = {}
        for key, value in (source.metadata or {}).items():
            if key not in target.metadata or target.metadata.get(key) in (None, ""):
                target.metadata[key] = value
                continue
            existing = target.metadata[key]
            if isinstance(existing, list):
                values = existing
            else:
                values = [existing]
            incoming = value if isinstance(value, list) else [value]
            if key in {"text", "summary", "description"}:
                merged_texts = [
                    str(item)
                    for item in values + incoming
                    if item is not None and str(item).strip()
                ]
                target.metadata[key] = " ".join(dict.fromkeys(merged_texts))
                continue
            if isinstance(existing, list):
                for item in incoming:
                    if item not in existing:
                        existing.append(item)

    def _linear_fusion(
        self,
        lex_results: List[CandidateArtifact],
        sem_results: List[CandidateArtifact],
        alpha: float = 0.5,
    ) -> List[CandidateArtifact]:
        """Linear score combination (requires score normalization)."""
        lex_max = max((c.lex_score for c in lex_results), default=1.0) or 1.0
        sem_max = max((c.sem_score for c in sem_results), default=1.0) or 1.0

        combined: Dict[str, CandidateArtifact] = {}
        for c in lex_results:
            aid = c.artifact_id
            if aid not in combined:
                combined[aid] = CandidateArtifact(
                    artifact_id=aid, family=c.family,
                    artifact_name=c.artifact_name, metadata=c.metadata,
                )
            combined[aid].lex_score = c.lex_score
            combined[aid].fused_score += alpha * (c.lex_score / lex_max)

        for c in sem_results:
            aid = c.artifact_id
            if aid not in combined:
                combined[aid] = CandidateArtifact(
                    artifact_id=aid, family=c.family,
                    artifact_name=c.artifact_name, metadata=c.metadata,
                )
            combined[aid].sem_score = c.sem_score
            combined[aid].fused_score += (1 - alpha) * (c.sem_score / sem_max)

        return sorted(
            combined.values(),
            key=lambda c: (c.fused_score, str(c.artifact_id)),
            reverse=True,
        )

    def get_artifact_text(self, artifact_id: str) -> Dict[str, Any]:
        """Retrieve the text + lightweight structure Phase 3 uses.

        When no Solr backend is injected, returns an empty stub so
        Phase 3 falls back to Neo4jStore.get_node() for artifact
        structure.
        """
        if self.solr is None:
            return {"id": artifact_id}
        return self.solr.get_text(
            artifact_id,
            fields=[
                "id",
                "body",
                "title",
                "document_id",
                "collection",
                "family",
                "date",
                "labels",
                "page_count",
                "indexed_at",
            ],
        )

    def get_artifact_texts_batch(
        self, artifact_ids: List[str]
    ) -> Dict[str, Dict[str, Any]]:
        """Batch retrieve the text + lightweight structure Phase 3 uses.

        When no Solr backend is injected, returns one empty stub per id
        so Phase 3 falls back to Neo4jStore.get_node().
        """
        if self.solr is None:
            return {aid: {"id": aid} for aid in artifact_ids}
        return self.solr.get_texts_batch(
            artifact_ids,
            fields=[
                "id",
                "body",
                "title",
                "document_id",
                "collection",
                "family",
                "date",
                "labels",
                "page_count",
                "indexed_at",
            ],
        )

    def close(self) -> None:
        """Release resources held by the underlying stores."""
        self.neo4j.close()
