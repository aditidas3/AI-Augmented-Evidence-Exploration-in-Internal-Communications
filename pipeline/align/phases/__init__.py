"""Compatibility re-export shim for ALIGN phase implementations.

The concrete phase classes live in ``phase0`` through ``phase6``.
Importing from ``pipeline.align.phases`` remains supported for existing callers.
"""

from __future__ import annotations

from .shared import (
    _NEO4J_QUERY_ERRORS,
    _normalize_time_filter_bound,
    _node_id_expression,
    _node_identity_value,
    _node_match_condition,
    _node_text_expression,
    _resolved_edge_relationship_types,
)
from .phase0 import Phase0_IntentValidation
from .phase1 import Phase1_ScopedRetrieval
from .phase2 import Phase2_ArtifactSelection
from .phase3 import Phase3_AnchorMentionExtraction
from .phase4 import Phase4_EntityLinkHypothesis
from .phase5 import Phase5_SubgraphDiscovery
from .phase6 import Phase6_SlotBinding

__all__ = [
    "_NEO4J_QUERY_ERRORS",
    "_normalize_time_filter_bound",
    "_node_id_expression",
    "_node_identity_value",
    "_node_match_condition",
    "_node_text_expression",
    "_resolved_edge_relationship_types",
    "Phase0_IntentValidation",
    "Phase1_ScopedRetrieval",
    "Phase2_ArtifactSelection",
    "Phase3_AnchorMentionExtraction",
    "Phase4_EntityLinkHypothesis",
    "Phase5_SubgraphDiscovery",
    "Phase6_SlotBinding",
]
