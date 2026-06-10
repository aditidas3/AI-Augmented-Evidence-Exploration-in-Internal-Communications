"""
align/phases/shared.py
Implementation of all six ALIGN phases.

The Pipeline
ALIGN processes an intent through six sequential phases, each building on the output of the previous one. There is also a Phase 0 (skeleton compilation) that runs before Phase 1.

Phase 0: Skeleton Compilation
Input: the intent's graph variables and their types, roles, and constraints.
Output: a skeleton pattern graph with concrete Neo4j label constraints.

Skeleton compilation translates each graph variable's entity category into the corresponding KG0 node labels via ontology.kg0_labels_for(category). The result is a pattern graph where each node position is annotated with the set of Neo4j labels that a matching KG0 node must carry (or, if the variable is typed with an abstract parent category, the union of labels across all child categories via expand_category). Edges in the skeleton carry relationship type constraints drawn from the intent and validated against the ontology's declared edge_types. The skeleton also records which variables are "anchor variables" (those that should be grounded to specific known entities from the query) versus "open variables" (those that the system should discover). This distinction drives Phase 3 and Phase 5.

Phase 1: Retrieval
Input: the intent's question text and any extracted keywords or entity mentions.
Output: a ranked list of (artifact_id, score) pairs.

Phase 1 queries both Solr (keyword retrieval) and Qdrant (semantic retrieval) and fuses the results using Reciprocal Rank Fusion (RRF). The RRF formula is score(d) = Σ 1 / (k + rank_i(d)) where k is the RRF constant (default 60) and the sum is over all retrieval sources in which document d appears. Results below min_retrieval_score are discarded. Solr queries are constructed from the intent's question text with field boosting (title fields weighted higher than body text). Qdrant queries embed the question text using the configured sentence-transformer model and perform approximate nearest-neighbor search over the chunk embeddings. Both sources return ranked lists that RRF merges into a single ranking. The output is typically hundreds of candidate artifacts. Phase 2 narrows this down.

Phase 2: Artifact Selection
Input: the ranked artifact list from Phase 1, plus the intent's metadata.
Output: at most k_artifacts (default 50) selected artifacts, chosen for relevance and diversity.

Phase 2 performs a greedy selection that balances retrieval score with artifact family coverage. At each step it picks the artifact that maximizes a combined score: the RRF retrieval score plus a family_coverage_weight bonus (default 2.0) if this artifact's family has not yet been represented in the selection, plus a diversity_weight penalty (default 0.5) that decays with the number of artifacts already selected from the same family. The family for each artifact type is determined by ontology.family_for_artifact_type(artifact_type). This ensures that the selection does not consist entirely of emails when PDFs and presentations also contain relevant material.

Phase 3: Anchor and Mention Extraction
Input: the selected artifacts and the skeleton's anchor variables.
Output: for each artifact, a set of anchors (specific text spans that mention entities relevant to the question) and a set of mentions (entity references detected in the text, linked to KG0 node IDs where possible). For each selected artifact, Phase 3 extracts up to k_anchors_per_artifact (default 20) text spans that are relevant to the intent's anchor variables. An anchor is a substring of the artifact's text that refers to a specific entity or concept that the question is about. Relevance is scored by a combination of embedding similarity (between the anchor span and the variable's description) and keyword overlap. Anchors below min_anchor_relevance (default 0.1) are discarded. Mention extraction identifies all entity references in the artifact text and attempts to link them to KG0 nodes. This is a lightweight entity linking step: for each detected mention, the system queries KG0 for candidate nodes whose names are similar, and assigns a confidence score based on string similarity and contextual embedding similarity. Mentions below mention_confidence_threshold (default 0.3) are discarded. The output of Phase 3 is the bridge between text (artifacts) and graph (KG0). Every subsequent phase operates on KG0 nodes and edges, but every claim can be traced back through Phase 3's anchors and mentions to a specific text span in a specific artifact.

Phase 4: Entity and Link Hypothesis Generation
Input: the anchors and mentions from Phase 3, plus the skeleton.
Output: a set of entity hypotheses (proposed KG0 nodes that might fill skeleton variables) and link hypotheses (proposed KG0 edges that might connect those nodes).

Phase 4 expands from the anchored mentions outward into KG0. For each mention that was linked to a KG0 node, the system explores the node's neighborhood up to kg0_max_path_hops (default 3) hops, looking for other nodes whose types match the skeleton's open variables. Each candidate node becomes an entity hypothesis, scored by the strength of the mention link, the proximity in KG0 to a grounded anchor, and the type compatibility with the skeleton variable it would fill (checked via the ontology). Link hypotheses are proposed edges between entity hypotheses that would satisfy the skeleton's edge constraints. These are discovered either by finding existing KG0 edges between hypothesis nodes, or by identifying short paths (up to kg0_max_path_hops) through intermediate nodes. Entity hypotheses whose embedding similarity to the question falls below entity_similarity_threshold (default 0.7) are pruned. The system retains at most max_entity_hypotheses (default 500) entity hypotheses and max_link_hypotheses (default 1000) link hypotheses, ranked by score.

Phase 5: Subgraph Discovery
Input: the skeleton, the entity hypotheses, and the link hypotheses from Phase 4.
Output: a ranked list of up to max_subgraphs (default 50) candidate subgraphs, each representing a complete or near-complete instantiation of the skeleton.

Phase 5 is the core combinatorial search. It attempts to find subgraphs of KG0 that match the skeleton pattern — assigning specific KG0 nodes to each skeleton variable and specific KG0 edges to each skeleton edge — such that the result is coherent, well-evidenced, and diverse. The search uses beam search with a beam width of beam_width (default 10). Each beam state is a partial assignment of KG0 nodes to skeleton variables. At each step, the search extends the partial assignment by trying all entity hypotheses for the next unassigned variable, scoring each extension by a weighted combination of factors. Hard constraints are structural requirements from the skeleton: type compatibility (the assigned node's labels must include at least one label expected by the variable's type) and edge existence (if the skeleton requires an edge between two assigned variables, the corresponding KG0 edge or short path must exist). Hard constraint violations are penalized by hard_constraint_weight (default 10.0). Soft constraints contribute to quality scoring without being absolute requirements. These include temporal_coherence_weight (default 1.5), which rewards subgraphs where the dates on connected nodes are temporally consistent; cross_artifact_bridge_weight (default 2.0), which rewards subgraphs that draw evidence from multiple artifacts rather than a single one; and soft_constraint_weight (default 1.0), which is the base weight for generic soft constraints like role compatibility. After beam search completes, the top subgraphs are re-ranked with a diversity_bonus (default 0.3) that rewards subgraphs differing from those already selected, preventing the output from being dominated by minor variations of the same answer.

Phase 6: Slot Binding
Input: the ranked subgraphs from Phase 5, plus the intent's slot definitions.
Output: for each slot in the intent, a ranked list of bindings — each binding being a specific value (a KG0 node, a text span, a date, etc.) with a confidence score and an evidence chain.

Phase 6 maps the abstract subgraph answers back to the user's question structure. For each slot in the intent (WHO did what, WHAT was the strategy, WHEN did it happen, etc.), the system identifies which skeleton variable(s) should fill that slot. This matching uses the ontology's is_slot_compatible and role_matches_slot methods: a WHO slot is filled by variables whose types the ontology declares as WHO-compatible (PERSON, ORGANIZATION, ROLE, etc.) and whose role keywords match WHO-associated terms (speaker, author, participant, etc.). For slots that also specify a target_schema_id, the system uses that as a direct lookup — the slot is filled by whichever variable the intent analysis explicitly tagged with that schema ID. This domain-agnostic fallback works even without an ontology. Once variables are matched to slots, the system constructs the binding by looking up the KG0 node assigned to that variable in the subgraph, extracting its display name and properties, and building an evidence chain: the chain of reasoning from the slot value back through the subgraph edges and Phase 3 anchors to the original artifact text spans. Evidence chains have a maximum depth of evidence_chain_max_depth (default 10) to prevent runaway traversals. Bindings below min_slot_confidence (default 0.2) are discarded. The remaining bindings are ranked by confidence within each slot, and the final output is the complete set of slot bindings across all subgraphs, deduplicated and merged where multiple subgraphs agree on the same binding.
"""

from __future__ import annotations
import hashlib
import json
import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Any, Dict, List, Set, Tuple

from ..core_types import (
    Anchor, Mention, Witness, IntentObject, IntentElementRef,
    CompiledScopePredicate, CompiledGraphSkeleton, CompiledRetrievalQuery,
    CandidateArtifact, EntityHypothesis, LinkHypothesis,
    Subgraph, SubgraphBinding, SlotBinding, SlotDef, AlignResult,
    ScopeMode, FusionMethod, Phase, EvidenceQuality,
    GraphSpec, GraphVar, GraphEdge, TemporalConstraint,
)
from ...operators.configs import AlignConfig
from ..infrastructure.index_facade import IndexFacade
from ..infrastructure.adapters import AdapterRegistry
from ..graph.helpers import (
    NEO4J_QUERY_ERRORS as _NEO4J_QUERY_ERRORS,
    document_date_scope_clause as _document_date_scope_clause,
    normalize_time_filter_bound as _normalize_time_filter_bound,
    node_id_expression as _node_id_expression,
    node_identity_value as _node_identity_value,
    node_match_condition as _node_match_condition,
    node_text_expression as _node_text_expression,
    resolved_edge_relationship_types as _resolved_edge_relationship_types,
)

logger = logging.getLogger(__name__)

# ============================================================
# Phase 0: Intent Validation and Compilation
# ============================================================
