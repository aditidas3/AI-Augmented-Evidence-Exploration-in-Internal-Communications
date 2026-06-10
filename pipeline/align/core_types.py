"""
align/core_types.py
All data models for ALIGN: Anchors, Mentions, Witnesses, IntentObject, and result types.

KG0: The Knowledge Graph
KG0 is a labeled property graph in Neo4j. Nodes represent entities extracted from the corpus: people, organizations, documents, events, topics, risk mentions, health mentions, decisions, regulations, drugs, locations, and so on. The specific set of node labels depends on the collection and is declared in the collection's ontology file (see §4). Edges represent relationships between entities: authorship, mention, employment, participation, discussion, temporal precedence, containment, reference, and others. Every KG0 node carries at minimum an ID, a display name, and a set of Neo4j labels. Most carry additional properties: dates, descriptions, confidence scores from the extraction pipeline, and provenance pointers back to the artifact and text span from which the entity was extracted. ALIGN reads these properties but never writes to KG0.

Artifacts
An artifact is a single retrievable document in the corpus: one email, one PDF, one presentation slide deck, one discussion thread. Artifacts live in Solr (full text) and have corresponding nodes in KG0 (as Document, Email, or similar labels). Artifacts are classified into families — EMAIL, THREAD, DOCUMENT, PDF, PRESENTATION — by the ontology's artifact_families mapping. Family classification drives Phase 2's diversity logic: the system prefers artifact selections that cover multiple families rather than concentrating on one type.

Intents
An intent is ALIGN's internal representation of the user's question after it has been analyzed by an upstream intent-analysis component (also not part of ALIGN itself). An intent contains the original question text, a set of slots to fill (each slot has a type like WHO, WHAT, WHEN, plus optional constraints and a target schema ID), and a set of graph variables that represent the entities and relationships the question is asking about. Each graph variable has a type (an entity category from the ontology), an optional role descriptor (free text like "author" or "decision-maker"), and optional constraints (temporal bounds, required properties, etc.).

The intent is the input to the pipeline. Everything ALIGN does is in service of filling the intent's slots with grounded, evidenced answers.

3.4 Skeletons
A skeleton is a compiled, executable form of the intent's graph structure. It is a small pattern graph — a set of typed variable nodes connected by typed edges — that ALIGN will attempt to match against KG0. Skeleton compilation (Phase 0, described below) translates the intent's abstract variables and relationships into concrete Neo4j label constraints and Cypher pattern fragments, using the ontology's category_to_kg0_labels mapping. The skeleton guides Phase 5's subgraph search: the beam search tries to find KG0 subgraphs that instantiate the skeleton pattern.
"""

from __future__ import annotations
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

from .utils.stable_serialization import (
    short_sha256_text as _short_sha256_text,
    stable_json as _stable_json,
)
from .utils.temporal_normalization import normalize_temporal_value as _normalize_temporal_value_impl


# ============================================================
# Enumerations
# ============================================================

class ScopeMode(str, Enum):
    STRICT = "STRICT"
    PREFER = "PREFER"


class FusionMethod(str, Enum):
    RRF = "rrf"
    LINEAR = "linear"
    LEARNED = "learned"


class SlotType(str, Enum):
    WHO = "WHO"
    WHAT = "WHAT"
    WHEN = "WHEN"
    WHERE = "WHERE"
    WHY = "WHY"
    HOW = "HOW"
    EVIDENCE = "EVIDENCE"
    OUTCOME = "OUTCOME"


class ObjectivePrimary(str, Enum):
    MAX_EVIDENCE_COVER = "MAX_EVIDENCE_COVER"
    MAX_TEMPORAL_COHERENCE = "MAX_TEMPORAL_COHERENCE"
    MAX_ROLE_COMPLETENESS = "MAX_ROLE_COMPLETENESS"


class ObjectiveSecondary(str, Enum):
    MAX_ROLE_COMPLETENESS = "MAX_ROLE_COMPLETENESS"
    MAX_TEMPORAL_COHERENCE = "MAX_TEMPORAL_COHERENCE"
    MAX_EDGE_CONFIDENCE = "MAX_EDGE_CONFIDENCE"
    MAX_CROSS_ARTIFACT_BRIDGES = "MAX_CROSS_ARTIFACT_BRIDGES"
    DIVERSIFY_SOURCES = "DIVERSIFY_SOURCES"


class ArtifactFamily(str, Enum):
    THREAD = "ARTIFACT_THREAD"
    EMAIL = "ARTIFACT_EMAIL"
    DOCUMENT = "ARTIFACT_DOCUMENT"
    PDF = "ARTIFACT_PDF"
    PRESENTATION = "ARTIFACT_PRESENTATION"
    PRESENTATION_SLIDE = "ARTIFACT_PRESENTATION_SLIDE"


class BridgeEdgeType(str, Enum):
    SEMANTIC = "SEMANTIC"
    THREAD = "THREAD"
    ATTACHMENT = "ATTACHMENT"
    STRUCTURAL = "STRUCTURAL"
    CITATION = "CITATION"


class Phase(str, Enum):
    PHASE_0_VALIDATION = "PHASE_0_VALIDATION"
    PHASE_1_RETRIEVAL = "PHASE_1_RETRIEVAL"
    PHASE_2_SELECTION = "PHASE_2_SELECTION"
    PHASE_3_ANCHORS = "PHASE_3_ANCHORS"
    PHASE_4_HYPOTHESES = "PHASE_4_HYPOTHESES"
    PHASE_5_SUBGRAPH = "PHASE_5_SUBGRAPH"
    PHASE_6_BINDING = "PHASE_6_BINDING"


class EvidenceQuality(str, Enum):
    GROUNDED = "GROUNDED"
    INFERRED = "INFERRED"
    AMBIGUOUS = "AMBIGUOUS"


def _normalize_temporal_value(value: Any) -> str:
    """Normalize a date-ish value into ``YYYY-MM-DD`` for lexical comparison.

    Accepts:

    * ISO timestamps and dates (``2010-06-28T00:00:00Z`` → ``2010-06-28``)
    * Free-text variants the LLM and Postgres catalog emit, e.g.
      ``"2010 June 28"``, ``"2013 April"``, ``"2015"``, ``"Q1 FY14"``.

    Missing month / day default to ``01`` so the year-only forms still
    compare correctly against ISO bounds. Anything that can't be
    parsed as a leading 4-digit year is returned untouched (lexical
    comparison will treat it as opaque).
    """
    return _normalize_temporal_value_impl(value)


# ============================================================
# Anchor Path Components
# ============================================================

@dataclass(frozen=True)
class AnchorPathComponent:
    """Base class for hierarchical anchor path segments."""
    component_type: str
    index: Union[int, str]

    def __str__(self) -> str:
        return f"{self.component_type}{self.index}"


@dataclass(frozen=True)
class PageComponent(AnchorPathComponent):
    component_type: str = field(default="p", init=False)
    index: int = 0


@dataclass(frozen=True)
class ParagraphComponent(AnchorPathComponent):
    component_type: str = field(default="para", init=False)
    index: int = 0


@dataclass(frozen=True)
class FigureComponent(AnchorPathComponent):
    component_type: str = field(default="fig", init=False)
    index: int = 0


@dataclass(frozen=True)
class TableComponent(AnchorPathComponent):
    component_type: str = field(default="table", init=False)
    index: int = 0


@dataclass(frozen=True)
class RowComponent(AnchorPathComponent):
    component_type: str = field(default="row", init=False)
    index: int = 0


@dataclass(frozen=True)
class ColComponent(AnchorPathComponent):
    component_type: str = field(default="col", init=False)
    index: int = 0


@dataclass(frozen=True)
class SlideComponent(AnchorPathComponent):
    component_type: str = field(default="slide", init=False)
    index: int = 0


@dataclass(frozen=True)
class TextFrameComponent(AnchorPathComponent):
    component_type: str = field(default="tf", init=False)
    index: int = 0


@dataclass(frozen=True)
class BulletComponent(AnchorPathComponent):
    component_type: str = field(default="bullet", init=False)
    index: int = 0


@dataclass(frozen=True)
class MessageComponent(AnchorPathComponent):
    component_type: str = field(default="msg", init=False)
    index: int = 0


@dataclass(frozen=True)
class HeaderComponent(AnchorPathComponent):
    component_type: str = field(default="header", init=False)
    index: str = ""


@dataclass(frozen=True)
class BodyComponent(AnchorPathComponent):
    component_type: str = field(default="body", init=False)
    index: str = ""


@dataclass(frozen=True)
class SpeakerNotesComponent(AnchorPathComponent):
    component_type: str = field(default="notes", init=False)
    index: str = ""


# ============================================================
# Anchor
# ============================================================

@dataclass
class Anchor:
    """
    A hierarchical structural locator into an artifact.
    
    The path is stable across re-renderings, navigable by a human
    reviewer, and supports containment/adjacency queries.
    
    Examples:
        DOC-0042.p3.para7
        EMAIL-1187.body.para4
        THREAD-0091.msg3.body.para2
        PRES-0055.slide4.tf2.bullet3
        PDF-0223.p12.table1.row3.col2
    """
    anchor_id: str
    artifact_id: str
    path: List[AnchorPathComponent]
    raw_text: str
    relevance_score: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def address(self) -> str:
        """Human-readable structural address."""
        components = ".".join(str(c) for c in self.path)
        return f"{self.artifact_id}.{components}"

    def contains(self, other: Anchor) -> bool:
        """Check if this anchor structurally contains another."""
        if self.artifact_id != other.artifact_id:
            return False
        if len(self.path) >= len(other.path):
            return False
        return all(
            s == o for s, o in zip(self.path, other.path[:len(self.path)])
        )

    def is_adjacent(self, other: Anchor) -> bool:
        """Check if two anchors are structurally adjacent (same parent, consecutive index)."""
        if self.artifact_id != other.artifact_id:
            return False
        if len(self.path) != len(other.path):
            return False
        if len(self.path) == 0:
            return False
        # Same parent path
        if self.path[:-1] != other.path[:-1]:
            return False
        last_self = self.path[-1]
        last_other = other.path[-1]
        if last_self.component_type != last_other.component_type:
            return False
        if isinstance(last_self.index, int) and isinstance(last_other.index, int):
            return abs(last_self.index - last_other.index) == 1
        return False

    def same_page(self, other: Anchor) -> bool:
        """Check if two anchors are on the same page."""
        if self.artifact_id != other.artifact_id:
            return False
        self_pages = [c for c in self.path if isinstance(c, (PageComponent, SlideComponent))]
        other_pages = [c for c in other.path if isinstance(c, (PageComponent, SlideComponent))]
        if not self_pages or not other_pages:
            return False
        return self_pages[0] == other_pages[0]

    def content_hash(self) -> str:
        """Stable hash of anchor content for replay verification."""
        payload = json.dumps({
            "artifact_id": self.artifact_id,
            "path": [{"type": c.component_type, "index": c.index} for c in self.path],
            "raw_text": self.raw_text
        }, sort_keys=True)
        return _short_sha256_text(payload)

    @staticmethod
    def generate_id() -> str:
        return f"anch-{uuid.uuid4().hex[:12]}"


# ============================================================
# Mention
# ============================================================

@dataclass
class Mention:
    """
    A typed semantic element extracted at an anchor location.
    
    The mention set at an anchor is the exhaustive semantic inventory
    of that structural unit — all recognized entities, concepts,
    strategies, risks, claims, etc., regardless of whether the current
    query requires them.
    """
    mention_id: str
    anchor_id: str
    surface: str
    category: str  # ENTITY_PERSON, ENTITY_STRATEGY, ENTITY_RISK_FINDING, etc.
    category_scores: Dict[str, float] = field(default_factory=dict)
    normalized: str = ""
    confidence: float = 0.0
    span_start: int = 0  # character offset within anchor raw_text
    span_end: int = 0
    qualifiers: Dict[str, Any] = field(default_factory=dict)
    kg0_entity_id: Optional[str] = None  # link to KG0 entity if resolved

    @property
    def span_text(self) -> str:
        """The exact surface text of this mention."""
        return self.surface

    def content_hash(self) -> str:
        payload = json.dumps({
            "anchor_id": self.anchor_id,
            "surface": self.surface,
            "category": self.category,
            "span_start": self.span_start,
            "span_end": self.span_end,
        }, sort_keys=True)
        return _short_sha256_text(payload)

    @staticmethod
    def generate_id() -> str:
        return f"ment-{uuid.uuid4().hex[:12]}"


# ============================================================
# Witness
# ============================================================

@dataclass
class IntentElementRef:
    """Reference to a specific element in the intent object."""
    element_type: str  # "graph_var", "slot", "entity_hint", "trigger_term"
    element_id: str    # e.g., "RF", "S-EVIDENCE-1", "SE3"
    element_detail: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Witness:
    """
    Binds a query element to an anchor+mention pair.
    
    Witnesses are the operational inputs to TRACE. Each witness
    carries enough information for TRACE to expand outward through
    the document structure without re-running extraction.
    
    The witness chain (via parent_witnesses) is also the basis for
    the EVIDENCE_CHAIN output schema.
    """
    witness_id: str
    phase: Phase
    intent_element: IntentElementRef
    anchor: Anchor
    mention: Mention
    score: float = 0.0
    quality: EvidenceQuality = EvidenceQuality.GROUNDED
    parent_witnesses: List[str] = field(default_factory=list)  # witness_ids
    content_hash: str = ""
    justification: str = ""
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    metadata: Dict[str, Any] = field(default_factory=dict)

    def compute_content_hash(self) -> str:
        """Compute deterministic hash for replay verification."""
        payload = json.dumps({
            "intent_element": {
                "type": self.intent_element.element_type,
                "id": self.intent_element.element_id,
            },
            "anchor_hash": self.anchor.content_hash(),
            "mention_hash": self.mention.content_hash(),
            "parent_witnesses": sorted(self.parent_witnesses),
        }, sort_keys=True)
        h = _short_sha256_text(payload)
        self.content_hash = h
        return h

    @staticmethod
    def generate_id() -> str:
        return f"wit-{uuid.uuid4().hex[:12]}"


# ============================================================
# Intent Object and Sub-structures
# ============================================================

@dataclass
class IntentHeader:
    intent_id: str
    schema_version: str
    question_id: str
    created_at: str
    question_text: str


@dataclass
class EntityHint:
    entity_id: str
    surface: str
    category: str
    normalized: str = ""
    confidence: float = 0.0
    qualifiers: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CollectionFilter:
    include: List[str] = field(default_factory=list)
    exclude: List[str] = field(default_factory=list)


@dataclass
class TimeFilter:
    op: str = "none"  # "none", "between", "before", "after"
    start: Optional[str] = None
    end: Optional[str] = None
    timezone: str = "America/Los_Angeles"


@dataclass
class ScopeSpec:
    mode: ScopeMode = ScopeMode.PREFER
    collections: CollectionFilter = field(default_factory=CollectionFilter)
    artifact_types: List[str] = field(default_factory=list)
    time_filter: TimeFilter = field(default_factory=TimeFilter)
    exclude_features: List[str] = field(default_factory=list)
    metadata_filters: List[Dict[str, Any]] = field(default_factory=list)
    scope_notes: str = ""


@dataclass
class RetrievalSpec:
    query_text: str = ""
    query_expansions: List[str] = field(default_factory=list)
    field_boosts: Dict[str, float] = field(default_factory=dict)
    top_k_lex: int = 250
    top_k_sem: int = 250
    fusion_method: FusionMethod = FusionMethod.RRF


@dataclass
class GlobalTrigger:
    trigger_id: str = ""
    terms: List[str] = field(default_factory=list)


@dataclass
class GraphVar:
    var: str
    type: str
    role: str = ""
    hint: str = ""
    hard: bool = False


@dataclass
class GraphEdge:
    src: str
    rel: str
    dst: str
    hard: bool = False
    notes: str = ""


@dataclass
class TemporalConstraint:
    kind: str = "ORDER"  # "ORDER", "WITHIN", "CONCURRENT", "BEFORE_DATE", "AFTER_DATE"
    before: str = ""
    after: str = ""
    window_days: Optional[int] = None
    vars: List[str] = field(default_factory=list)
    notes: str = ""


@dataclass
class AttachmentExpectation:
    thread_must_have_attachments: bool = False


@dataclass
class CrossArtifactConstraints:
    required_families: List[str] = field(default_factory=list)
    bridge_edge_types: List[str] = field(default_factory=list)
    attachment_expectation: AttachmentExpectation = field(
        default_factory=AttachmentExpectation
    )


@dataclass
class GraphObjective:
    primary: str = "MAX_EVIDENCE_COVER"
    secondary: List[str] = field(default_factory=list)
    # Default chosen to match ``AlignConfig.beam_width`` so Phase 6
    # processes every subgraph beam search retains. The previous
    # default of 3 silently truncated witnesses for any intent that
    # did not set the field explicitly, even when Phase 5 found
    # 10 distinct hint-grounded entities.
    return_top_k_alternatives: int = 10


@dataclass
class GraphSpec:
    query_name: str = ""
    vars: List[GraphVar] = field(default_factory=list)
    edges: List[GraphEdge] = field(default_factory=list)
    temporal_constraints: List[TemporalConstraint] = field(default_factory=list)
    cross_artifact_constraints: CrossArtifactConstraints = field(
        default_factory=CrossArtifactConstraints
    )
    objective: GraphObjective = field(default_factory=GraphObjective)

    @property
    def hard_vars(self) -> List[GraphVar]:
        return [v for v in self.vars if v.hard]

    @property
    def soft_vars(self) -> List[GraphVar]:
        return [v for v in self.vars if not v.hard]

    @property
    def hard_edges(self) -> List[GraphEdge]:
        return [e for e in self.edges if e.hard]

    @property
    def soft_edges(self) -> List[GraphEdge]:
        return [e for e in self.edges if not e.hard]

    def var_by_name(self, name: str) -> Optional[GraphVar]:
        for v in self.vars:
            if v.var == name:
                return v
        return None


@dataclass
class SlotDef:
    slot_id: str
    slot_type: str  # WHO, WHAT, WHEN, etc.
    description: str = ""
    allowed_artifact_types: List[str] = field(default_factory=list)
    target_schema_id: str = ""
    target_var: str = ""
    graph_spec: Optional[GraphSpec] = None  # slot-level graph spec (question 2 pattern)


@dataclass
class SlotSpec:
    global_trigger: GlobalTrigger = field(default_factory=GlobalTrigger)
    slots: List[SlotDef] = field(default_factory=list)
    graph_spec: Optional[GraphSpec] = None  # top-level graph spec


@dataclass
class Diagnostics:
    rule_hits: List[str] = field(default_factory=list)
    notes: Union[List[str], str] = field(default_factory=list)


@dataclass
class IntentObject:
    """
    Complete intent bundle as produced by the intent analysis pipeline.
    Schema derived from intent_bundle_compact_v1 across 36 exemplars.
    """
    header: IntentHeader = field(default_factory=lambda: IntentHeader(
        intent_id="", schema_version="", question_id="", created_at="", question_text=""
    ))
    entity_hints: List[EntityHint] = field(default_factory=list)
    scope_spec: ScopeSpec = field(default_factory=ScopeSpec)
    retrieval_spec: RetrievalSpec = field(default_factory=RetrievalSpec)
    slot_spec: SlotSpec = field(default_factory=SlotSpec)
    diagnostics: Diagnostics = field(default_factory=Diagnostics)

    @property
    def graph_spec(self) -> Optional[GraphSpec]:
        """
        The primary graph spec. May be at SlotSpec level or nested
        inside a slot (question 2 pattern where graph_spec is under
        S-EVIDENCE-1).
        """
        if self.slot_spec.graph_spec is not None:
            return self.slot_spec.graph_spec
        # Check for slot-level graph specs
        for s in self.slot_spec.slots:
            if s.graph_spec is not None:
                return s.graph_spec
        return None

    @property
    def required_families(self) -> List[str]:
        gs = self.graph_spec
        if gs and gs.cross_artifact_constraints.required_families:
            return gs.cross_artifact_constraints.required_families
        return []

    def content_hash(self) -> str:
        """Stable hash of the entire intent object for replay."""
        payload = _stable_json(
            {
                "header": self.header,
                "entity_hints": self.entity_hints,
                "scope_spec": self.scope_spec,
                "retrieval_spec": self.retrieval_spec,
                "slot_spec": self.slot_spec,
                "graph_spec": self.graph_spec,
                "diagnostics": self.diagnostics,
            }
        )
        return _short_sha256_text(payload)


# ============================================================
# Compiled Structures (Phase 0 outputs)
# ============================================================

@dataclass
class CompiledScopePredicate:
    """
    Scope predicate compiled from ScopeSpec, translated into
    filter expressions native to each store.

    Also retains the source ScopeSpec for in-memory evaluation
    (required by the post-hoc soundness assertion).
    """
    mode: ScopeMode
    source_spec: Optional[ScopeSpec] = None
    # Solr filter queries
    solr_fqs: List[str] = field(default_factory=list)
    # Qdrant payload filters
    qdrant_filters: Dict[str, Any] = field(default_factory=dict)
    # Cypher WHERE clause fragments
    cypher_where: List[str] = field(default_factory=list)
    # Content hash for replay
    predicate_hash: str = ""

    def evaluate(self, artifact_metadata: Dict[str, Any]) -> bool:
        """
        Evaluate scope predicate against artifact metadata in-memory.

        This is the callable P_scope(a) from the pseudocode, used for
        the post-hoc soundness assertion: assert forall a in Aset, P_scope(a).
        """
        if self.source_spec is None:
            return True
        # Under PREFER mode, scope predicates are soft preferences — every
        # candidate passes the soundness check; Phase 1 ranks, it doesn't
        # exclude.
        if self.mode != ScopeMode.STRICT:
            return True
        spec = self.source_spec

        # Collection filter
        collection = artifact_metadata.get("collection", "")
        if spec.collections.include and collection:
            if collection not in spec.collections.include:
                return False
        if spec.collections.exclude and collection:
            if collection in spec.collections.exclude:
                return False

        # Artifact type / family filter (normalize representation)
        if spec.artifact_types:
            allowed = {str(x).upper() for x in spec.artifact_types}
            family_raw = str(artifact_metadata.get("family", "") or "").upper()
            family_norm = (
                family_raw.replace("ARTIFACT_", "", 1)
                if family_raw.startswith("ARTIFACT_")
                else family_raw
            )
            allowed_norm = {
                a.replace("ARTIFACT_", "", 1) if a.startswith("ARTIFACT_") else a
                for a in allowed
            }
            if family_norm and family_norm not in allowed_norm:
                return False

        # Time filter
        tf = spec.time_filter
        date_val = _normalize_temporal_value(artifact_metadata.get("date", ""))
        if date_val and tf.op != "none":
            start = _normalize_temporal_value(tf.start)
            end = _normalize_temporal_value(tf.end)
            if tf.op == "between" and start and end:
                if not (start <= date_val <= end):
                    return False
            elif tf.op == "before" and end:
                if date_val > end:
                    return False
            elif tf.op == "after" and start:
                if date_val < start:
                    return False

        # Exclude features
        features = artifact_metadata.get("features", [])
        for feat in spec.exclude_features:
            if feat in features:
                return False

        return True

    def compute_hash(self) -> str:
        payload = _stable_json(
            {
                "mode": self.mode,
                "source_spec": self.source_spec,
                "solr_fqs": sorted(self.solr_fqs),
                "qdrant_filters": self.qdrant_filters,
                "cypher_where": sorted(self.cypher_where),
            }
        )
        self.predicate_hash = _short_sha256_text(payload)
        return self.predicate_hash


@dataclass
class CompiledGraphSkeleton:
    """
    Bounded query skeleton compiled from GraphSpec.
    Guarantees finite execution (no unbounded variable-length path expansions).
    """
    graph_spec: GraphSpec
    # For each var: allowed KG0 labels
    var_labels: Dict[str, List[str]] = field(default_factory=dict)
    # For each edge: allowed KG0 relationship types + max hop bound
    edge_patterns: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # Maximum hops for any single edge traversal
    max_hops: int = 3
    # Content hash for replay
    skeleton_hash: str = ""

    def compute_hash(self) -> str:
        payload = _stable_json(
            {
                "graph_spec": self.graph_spec,
                "var_labels": self.var_labels,
                "edge_patterns": self.edge_patterns,
                "max_hops": self.max_hops,
            }
        )
        self.skeleton_hash = _short_sha256_text(payload)
        return self.skeleton_hash


@dataclass
class CompiledRetrievalQuery:
    """Multi-backend query plan produced by BuildRetrievalQuery."""
    # Solr query components used by Phase 1 lexical retrieval.
    solr_query: str = ""
    solr_boost_query: str = ""
    solr_fields: List[str] = field(default_factory=list)
    # Qdrant query components used by Phase 1 semantic retrieval.
    qdrant_vector: Optional[List[float]] = None
    qdrant_sparse_vector: Optional[Dict[str, Any]] = None
    # Entity-name hint metadata retained for diagnostics and compatibility.
    entity_hint_terms: List[str] = field(default_factory=list)
    # Subset of entity_hint_terms that come from high-confidence ORG/PERSON
    # hints. When non-empty, Phase 1 requires every retrieved Document to
    # mention at least one of these, so docs matching only weak query-
    # expansion terms are rejected. Empty → no presence floor.
    required_hint_terms: List[str] = field(default_factory=list)
    # Limits
    top_k_lex: int = 250
    top_k_sem: int = 250
    fusion_method: FusionMethod = FusionMethod.RRF


# ============================================================
# Candidate and Result Types
# ============================================================

@dataclass
class CandidateArtifact:
    """An artifact returned by retrieval with fusion score."""
    artifact_id: str
    family: str
    artifact_name: str = ""
    lex_score: float = 0.0
    sem_score: float = 0.0
    fused_score: float = 0.0
    lex_rank: int = 0
    sem_rank: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EntityHypothesis:
    """
    Hypothesis that multiple mentions across artifacts
    refer to the same real-world entity.
    """
    hypothesis_id: str
    canonical_name: str
    category: str
    mentions: List[Mention] = field(default_factory=list)
    kg0_entity_ids: List[str] = field(default_factory=list)
    kg0_link_candidates: List[Dict[str, Any]] = field(default_factory=list)
    confidence: float = 0.0
    evidence: List[str] = field(default_factory=list)  # justification strings

    @staticmethod
    def generate_id() -> str:
        return f"ehyp-{uuid.uuid4().hex[:12]}"


@dataclass
class LinkHypothesis:
    """
    Hypothesis that two entity hypotheses are connected
    via a path in KG0.
    """
    hypothesis_id: str
    source_entity_id: str
    target_entity_id: str
    relationship_type: str
    path: List[str] = field(default_factory=list)  # sequence of KG0 node/edge IDs
    confidence: float = 0.0
    witness: Optional[Witness] = None

    @staticmethod
    def generate_id() -> str:
        return f"lhyp-{uuid.uuid4().hex[:12]}"


@dataclass
class SubgraphBinding:
    """A single variable binding within a subgraph."""
    var_name: str
    entity_hypothesis_id: Optional[str] = None
    kg0_node_id: Optional[str] = None
    anchor_id: Optional[str] = None
    mention_id: Optional[str] = None
    bound: bool = False
    quality: EvidenceQuality = EvidenceQuality.AMBIGUOUS


@dataclass
class Subgraph:
    """
    A candidate subgraph: a set of variable bindings that
    (partially) satisfy the graph spec.
    """
    subgraph_id: str
    bindings: Dict[str, SubgraphBinding] = field(default_factory=dict)
    edge_satisfactions: Dict[str, bool] = field(default_factory=dict)
    score: float = 0.0
    hard_coverage: float = 0.0  # fraction of hard constraints satisfied
    soft_coverage: float = 0.0  # fraction of soft constraints satisfied
    witnesses: List[Witness] = field(default_factory=list)
    # Phase 5 post-search assembly outputs
    coherence_score: float = 0.0
    diversity_score: float = 0.0
    frame_witness: Optional[Dict[str, Any]] = None
    snapshot: Optional[Dict[str, Any]] = None

    @property
    def is_valid(self) -> bool:
        """A subgraph is valid iff all hard constraints are satisfied."""
        return self.hard_coverage >= 1.0

    @staticmethod
    def generate_id() -> str:
        return f"sg-{uuid.uuid4().hex[:12]}"


@dataclass
class SlotBinding:
    """A filled slot in the final output."""
    slot_id: str
    slot_type: str
    description: str
    value: Any = None
    witnesses: List[Witness] = field(default_factory=list)
    quality: EvidenceQuality = EvidenceQuality.AMBIGUOUS
    confidence: float = 0.0


@dataclass
class AlignResult:
    """Complete output of the ALIGN pipeline."""
    intent_id: str
    question_text: str
    artifact_set: List[CandidateArtifact] = field(default_factory=list)
    slot_bindings: List[SlotBinding] = field(default_factory=list)
    subgraphs: List[Subgraph] = field(default_factory=list)
    all_witnesses: List[Witness] = field(default_factory=list)
    all_anchors: Dict[str, List[Anchor]] = field(default_factory=dict)
    all_mentions: Dict[str, List[Mention]] = field(default_factory=dict)
    suppressed_mentions: Dict[str, List[Mention]] = field(default_factory=dict)
    entity_hypotheses: List[EntityHypothesis] = field(default_factory=list)
    link_hypotheses: List[LinkHypothesis] = field(default_factory=list)
    diagnostics: Dict[str, Any] = field(default_factory=dict)
    replay_plan: Dict[str, str] = field(default_factory=dict)  # phase -> content_hash


# ============================================================
# Intent Object Deserialization
# ============================================================

def parse_intent_object(raw: Dict[str, Any]) -> IntentObject:
    """Parse a raw JSON intent bundle into a typed IntentObject."""

    header = IntentHeader(
        intent_id=raw["Header"]["intent_id"],
        schema_version=raw["Header"]["schema_version"],
        question_id=raw["Header"]["question_id"],
        created_at=raw["Header"]["created_at"],
        question_text=raw["Header"]["question_text"],
    )

    entity_hints = [
        EntityHint(
            entity_id=eh["entity_id"],
            surface=eh["surface"],
            category=eh["category"],
            normalized=eh.get("normalized", ""),
            confidence=eh.get("confidence", 0.0),
            qualifiers=eh.get("qualifiers", {}),
        )
        for eh in raw.get("EntityHints", [])
    ]

    # ScopeSpec
    raw_scope = raw.get("ScopeSpec", {})
    scope_spec = ScopeSpec(
        mode=ScopeMode(raw_scope.get("mode", "PREFER")),
        collections=CollectionFilter(
            include=raw_scope.get("collections", {}).get("include", []),
            exclude=raw_scope.get("collections", {}).get("exclude", []),
        ),
        artifact_types=raw_scope.get("artifact_types", []),
        time_filter=TimeFilter(
            op=raw_scope.get("time_filter", {}).get("op", "none"),
            start=raw_scope.get("time_filter", {}).get("start"),
            end=raw_scope.get("time_filter", {}).get("end"),
            timezone=raw_scope.get("time_filter", {}).get("timezone", "America/Los_Angeles"),
        ),
        exclude_features=raw_scope.get("exclude_features", []),
        metadata_filters=raw_scope.get("metadata_filters", []),
        scope_notes=raw_scope.get("scope_notes", ""),
    )

    # RetrievalSpec
    raw_ret = raw.get("RetrievalSpec", {})
    retrieval_spec = RetrievalSpec(
        query_text=raw_ret.get("query_text", ""),
        query_expansions=raw_ret.get("query_expansions", []),
        field_boosts=raw_ret.get("field_boosts", {}),
        top_k_lex=raw_ret.get("top_k_lex", 250),
        top_k_sem=raw_ret.get("top_k_sem", 250),
        fusion_method=FusionMethod(raw_ret.get("fusion_method", "rrf")),
    )

    # SlotSpec (with nested graph_spec handling)
    raw_slot = raw.get("SlotSpec", {})
    slot_spec = _parse_slot_spec(raw_slot)

    # Diagnostics
    raw_diag = raw.get("Diagnostics", {})
    diagnostics = Diagnostics(
        rule_hits=raw_diag.get("rule_hits", []),
        notes=raw_diag.get("notes", []),
    )

    return IntentObject(
        header=header,
        entity_hints=entity_hints,
        scope_spec=scope_spec,
        retrieval_spec=retrieval_spec,
        slot_spec=slot_spec,
        diagnostics=diagnostics,
    )


def _parse_graph_spec(raw: Dict[str, Any]) -> GraphSpec:
    """Parse a raw graph_spec dict into a typed GraphSpec."""
    cross_artifact_constraints = raw.get("cross_artifact_constraints", {})
    if not isinstance(cross_artifact_constraints, dict):
        cross_artifact_constraints = {}
    attachment_expectation = cross_artifact_constraints.get("attachment_expectation", {})
    if isinstance(attachment_expectation, bool):
        thread_must_have_attachments = attachment_expectation
    elif isinstance(attachment_expectation, dict):
        thread_must_have_attachments = attachment_expectation.get(
            "thread_must_have_attachments",
            False,
        )
    else:
        thread_must_have_attachments = False

    return GraphSpec(
        query_name=raw.get("query_name", ""),
        vars=[
            GraphVar(
                var=v["var"],
                type=v["type"],
                role=v.get("role", ""),
                hint=v.get("hint", ""),
                hard=v.get("hard", False),
            )
            for v in raw.get("vars", [])
        ],
        edges=[
            GraphEdge(
                src=e["src"],
                rel=e["rel"],
                dst=e["dst"],
                hard=e.get("hard", False),
                notes=e.get("notes", ""),
            )
            for e in raw.get("edges", [])
        ],
        temporal_constraints=[
            TemporalConstraint(
                kind=tc.get("kind", "ORDER"),
                before=tc.get("before", ""),
                after=tc.get("after", ""),
                window_days=tc.get("window_days"),
                vars=tc.get("vars", []),
                notes=tc.get("notes", ""),
            )
            for tc in raw.get("temporal_constraints", [])
        ],
        cross_artifact_constraints=CrossArtifactConstraints(
            required_families=cross_artifact_constraints.get("required_families", []),
            bridge_edge_types=cross_artifact_constraints.get("bridge_edge_types", []),
            attachment_expectation=AttachmentExpectation(
                thread_must_have_attachments=thread_must_have_attachments
            ),
        ),
        objective=GraphObjective(
            primary=raw.get("objective", {}).get("primary", "MAX_EVIDENCE_COVER"),
            secondary=raw.get("objective", {}).get("secondary", []),
            # See GraphObjective.return_top_k_alternatives docstring:
            # default mirrors beam_width so Phase 6 doesn't silently
            # truncate witnesses below the number of subgraphs found.
            return_top_k_alternatives=raw.get("objective", {}).get(
                "return_top_k_alternatives", 10
            ),
        ),
    )


def _parse_slot_spec(raw: Dict[str, Any]) -> SlotSpec:
    """Parse SlotSpec, handling both top-level and slot-level graph_specs."""
    raw_trigger = raw.get("global_trigger", {})
    global_trigger = GlobalTrigger(
        trigger_id=raw_trigger.get("trigger_id", ""),
        terms=raw_trigger.get("terms", []),
    )

    slots = []
    for s in raw.get("slots", []):
        slot_graph_spec = None
        if "graph_spec" in s and s["graph_spec"]:
            slot_graph_spec = _parse_graph_spec(s["graph_spec"])
        slots.append(SlotDef(
            slot_id=s["slot_id"],
            slot_type=s["slot_type"],
            description=s.get("description", ""),
            allowed_artifact_types=s.get("allowed_artifact_types", []),
            target_schema_id=s.get("target_schema_id", ""),
            target_var=s.get("target_var", s.get("target_graph_var", "")),
            graph_spec=slot_graph_spec,
        ))

    top_level_graph_spec = None
    if "graph_spec" in raw and raw["graph_spec"]:
        top_level_graph_spec = _parse_graph_spec(raw["graph_spec"])

    return SlotSpec(
        global_trigger=global_trigger,
        slots=slots,
        graph_spec=top_level_graph_spec,
    )
