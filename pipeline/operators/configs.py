"""
operators/configs.py

All configuration for the unified ALIGN + operators pipeline:
infrastructure connections, domain-agnostic tuning knobs,
operator configs, policy schema, and master AlignConfig.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set

from .types import (
    AudienceLevel, AnsBundleSection, CitationStyle,
    ConflictDisplay, DocumentType, ExplanationDepth,
)
from .ontology import KG0Ontology, load_ontology


# ═══════════════════════════════════════════════════════════════
# Infrastructure connection configs
# ═══════════════════════════════════════════════════════════════

@dataclass
class Neo4jConfig:
    """Connection settings for a Neo4j instance."""
    uri: str = "bolt://localhost:7687"
    user: str = "neo4j"
    password: str = "password"
    database: str = "neo4j"
    max_connection_pool_size: int = 50


@dataclass
class SolrConfig:
    """Connection settings for an Apache Solr collection."""
    url: str = "http://localhost:8983/solr"
    collection: str = "align_artifacts"
    timeout: int = 30
    batch_size: int = 500


@dataclass
class QdrantConfig:
    """Connection settings for a Qdrant vector-search instance."""
    host: str = "localhost"
    port: int = 6333
    grpc_port: int = 6334
    collection_name: str = "align_embeddings"
    embedding_dim: int = 768
    use_grpc: bool = True


@dataclass
class EmbeddingConfig:
    """Sentence-transformer model settings."""
    model_name: str = "sentence-transformers/all-mpnet-base-v2"
    dimension: int = 768
    batch_size: int = 64
    max_seq_length: int = 512


# ═══════════════════════════════════════════════════════════════
# Operator configs (CONFLICT · CONSTRUCT · EXPLAIN)
# ═══════════════════════════════════════════════════════════════

@dataclass
class ConflictConfig:
    K_conflict_scope: int    = 200
    K_conflict_pairs: int    = 100
    tau_stance: float        = 0.6
    emit_supports: bool      = False
    enable_supersession: bool = True
    enable_clustering: bool   = True
    min_cluster_size: int     = 2
    require_shared_entity: bool    = True
    require_distinct_artifacts: bool = True


@dataclass
class ConstructConfig:
    max_chain_nodes_in_g_ans: int  = 100
    include_conflict_edges: bool   = True
    include_alt_hypotheses: bool   = True
    min_finding_confidence: float  = 0.3
    max_findings: Optional[int]    = None
    timeline_max_events: int       = 50
    exhibits_max: int              = 20
    narrative_model: str           = "narrative_composer"
    tether_model: str              = "witness_tether"


@dataclass
class ExplainConfig:
    compute_sensitivity: bool      = True
    max_sensitivity_probes: int    = 5
    tau_explain_conf: float        = 0.5
    explanation_model: str         = "explanation_generator"
    max_summary_tokens: int        = 500


# ═══════════════════════════════════════════════════════════════
# Policy schema
# ═══════════════════════════════════════════════════════════════

@dataclass
class PolicySchema:
    """Domain/style schema controlling CONSTRUCT and EXPLAIN output."""

    # ── Audience & domain ──────────────────────────────────
    audience: AudienceLevel
    domain: str                                                  # "public_health", …
    domain_vocabulary: Dict[str, str] = field(default_factory=dict)
    document_type: DocumentType = DocumentType.INVESTIGATION_REPORT

    # ── Content scope ──────────────────────────────────────
    max_findings: Optional[int]       = None
    max_narrative_tokens: Optional[int]= None
    sections: Set[AnsBundleSection]   = field(
        default_factory=lambda: set(AnsBundleSection),
    )

    # ── Evidence standards ─────────────────────────────────
    min_witness_depth: int  = 1
    require_dual_source: bool = False
    confidence_floor: float = 0.0

    # ── Conflict presentation ──────────────────────────────
    conflict_display: ConflictDisplay = ConflictDisplay.INLINE
    require_human_judgment: bool      = False
    show_supersession_rationale: bool = True

    # ── Explanation controls ───────────────────────────────
    explanation_depth: ExplanationDepth = ExplanationDepth.FULL
    compute_sensitivity: bool          = True
    max_sensitivity_probes: int        = 5

    # ── Formatting ─────────────────────────────────────────
    citation_style: CitationStyle = CitationStyle.INLINE_BRACKET
    include_rg_node_ids: bool     = False
    language: str                 = "en"

    # ── Reproducibility ────────────────────────────────────
    include_reproducibility_footer: bool = True
    embed_config_hash: bool              = True

    def config_hash(self) -> str:
        blob = json.dumps({
            "audience": self.audience.value,
            "domain": self.domain,
            "document_type": self.document_type.value,
            "confidence_floor": self.confidence_floor,
            "require_dual_source": self.require_dual_source,
        }, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


# ═══════════════════════════════════════════════════════════════
# Master ALIGN config
# ═══════════════════════════════════════════════════════════════

@dataclass
class AlignConfig:
    """
    Complete configuration for one ALIGN deployment.

    Infrastructure settings and numeric tuning knobs live here.
    All domain-specific type knowledge lives in ``ontology``.
    Phase logic is ontology-agnostic: it asks the ontology
    questions and acts on the answers.
    """

    # ── Per-collection ontology ────────────────────────────
    ontology: KG0Ontology = field(default_factory=KG0Ontology)

    # ── Infrastructure ─────────────────────────────────────
    neo4j: Neo4jConfig = field(default_factory=Neo4jConfig)
    solr: SolrConfig = field(default_factory=SolrConfig)
    qdrant: QdrantConfig = field(default_factory=QdrantConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)

    # ── Phase 1: Retrieval ─────────────────────────────────
    rrf_k: int = 60
    min_retrieval_score: float = 0.01

    # ── Phase 2: Artifact selection ────────────────────────
    k_artifacts: int = 50
    family_coverage_weight: float = 2.0
    diversity_weight: float = 0.5
    document_group_boost_weight: float = 2.0
    intent_relevance_weight: float = 0.35
    min_intent_relevance_score: float = 2.5
    min_required_hard_entity_coverage: float = 0.5
    enforce_required_families: bool = True
    enable_constructibility_repair: bool = True
    enforce_constructibility: bool = False

    # ── Phase 3: Anchor / mention extraction ───────────────
    k_anchors_per_artifact: int = 20
    min_anchor_relevance: float = 0.1
    mention_confidence_threshold: float = 0.3

    # ── Phase 4: Entity / link hypotheses ──────────────────
    entity_similarity_threshold: float = 0.7
    max_entity_hypotheses: int = 500
    max_link_hypotheses: int = 1000
    kg0_max_path_hops: int = 3

    # ── Phase 5: Subgraph discovery ────────────────────────
    beam_width: int = 10
    max_subgraphs: int = 50
    hard_constraint_weight: float = 10.0
    soft_constraint_weight: float = 1.0
    temporal_coherence_weight: float = 1.5
    cross_artifact_bridge_weight: float = 2.0
    diversity_bonus: float = 0.3

    # ── Phase 6: Slot binding ──────────────────────────────
    min_slot_confidence: float = 0.2
    evidence_chain_max_depth: int = 10

    # ── General ────────────────────────────────────────────
    max_hops: int = 3
    deterministic_seed: int = 42
    log_witnesses: bool = True
    enforce_graph_spec_soundness: bool = True
    defer_kg_structure_until_post_phase5: bool = True
    # Run the Phase 0 contract check against the live index once per
    # engine to catch schema drift (e.g. top_category casing,
    # kg_id/id property presence) before any query executes. Set
    # ``skip_contract_check=True`` for unit tests that stub out the
    # index or run against an empty graph.
    skip_contract_check: bool = False
    # Emit a warning when Phase 6 produces fewer witnesses per
    # subgraph than this ratio. ``0.0`` disables the check; ``0.5``
    # surfaces sparsely-bound intents (e.g. single-slot specs) so
    # the operator can adjust SlotSpec breadth before TRACE runs.
    min_witnesses_per_subgraph: float = 0.5

    # ----------------------------------------------------------
    # Convenience accessors that delegate to the ontology
    # ----------------------------------------------------------

    def kg0_labels_for_category(self, category: str) -> List[str]:
        """Neo4j labels for an entity category (delegates to ontology)."""
        return self.ontology.kg0_labels_for(category)

    def family_for_artifact_type(self, artifact_type: str) -> str:
        """Adapter family for an artifact type (delegates to ontology)."""
        return self.ontology.family_for_artifact_type(artifact_type)

    def canonical_family(self, family: str) -> str:
        """
        Normalize family labels so config-driven family constraints are stable.

        This is intentionally lightweight (no ontology dependency) so it can
        normalize families coming from external indices (Solr/Qdrant/etc.).
        """
        raw = (family or "").strip().upper()
        raw = raw.replace("-", "_").replace(" ", "_")
        if raw.startswith("ARTIFACT_"):
            raw = raw[len("ARTIFACT_") :]
        aliases = {
            "DOC": "DOCUMENT",
            "DOCS": "DOCUMENT",
            "DOCUMENTS": "DOCUMENT",
            "PPT": "PRESENTATION",
            "PPTX": "PRESENTATION",
            "SLIDES": "PRESENTATION",
            "EMAILS": "EMAIL",
            "MAIL": "EMAIL",
            "PDF": "DOCUMENT",
            "PDFS": "DOCUMENT",
            "THREAD": "EMAIL",
            "THREADS": "EMAIL",
            "ATTACHMENT": "DOCUMENT",
            "ATTACHMENTS": "DOCUMENT",
        }
        return aliases.get(raw, raw or "DOCUMENT")

    def canonical_families(self, families: Iterable[str]) -> Set[str]:
        return {self.canonical_family(f) for f in families}

    # ----------------------------------------------------------
    # Factory helpers
    # ----------------------------------------------------------

    @classmethod
    def with_ontology(
        cls,
        ontology_source: Any,
        **overrides: Any,
    ) -> AlignConfig:
        """
        Create an AlignConfig with an ontology loaded from *source*.

        Usage::

            cfg = AlignConfig.with_ontology("collections/tobacco/ontology.yaml")
            cfg = AlignConfig.with_ontology(raw_dict, k_artifacts=100)
        """
        ont = load_ontology(ontology_source)
        return cls(ontology=ont, **overrides)
