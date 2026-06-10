"""
operators/types.py

Shared enumerations and small value types for the
CONFLICT · CONSTRUCT · EXPLAIN pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


# ═══════════════════════════════════════════════════════════════
# Enumerations
# ═══════════════════════════════════════════════════════════════

class StanceLabel(str, Enum):
    """Stance relation between two evidence objects."""
    SUPPORTS             = "SUPPORTS"
    REFUTES              = "REFUTES"
    AMBIGUOUS            = "AMBIGUOUS"
    SUPERSEDES_TEMPORAL  = "SUPERSEDES_TEMPORAL"
    SUPERSEDES_AUTHORITY = "SUPERSEDES_AUTHORITY"
    SUPERSEDES_OTHER     = "SUPERSEDES_OTHER"


class DisagreementDimension(str, Enum):
    """
    Semantic dimension of a conflict.
    DOMAIN_SPECIFIC is the extensibility escape hatch —
    pair it with DisagreementDescriptor.qualifier.
    """
    NUMERIC_DISCREPANCY          = "NUMERIC_DISCREPANCY"
    CATEGORICAL_INCOMPATIBILITY  = "CATEGORICAL_INCOMPATIBILITY"
    EPISTEMIC_STATUS_MISMATCH    = "EPISTEMIC_STATUS_MISMATCH"
    HEDGE_ASYMMETRY              = "HEDGE_ASYMMETRY"
    TEMPORAL_INCONSISTENCY       = "TEMPORAL_INCONSISTENCY"
    CAUSAL_DIRECTION_CONFLICT    = "CAUSAL_DIRECTION_CONFLICT"
    SCOPE_MISMATCH               = "SCOPE_MISMATCH"
    DOMAIN_SPECIFIC              = "DOMAIN_SPECIFIC"


class HumanJudgmentLabel(str, Enum):
    SUPERSEDES_TEMPORAL  = "SUPERSEDES_TEMPORAL"
    SUPERSEDES_AUTHORITY = "SUPERSEDES_AUTHORITY"
    BOTH_STAND           = "BOTH_STAND"
    UNRESOLVED           = "UNRESOLVED"


class BranchType(str, Enum):
    VERSION  = "VERSION"
    BRANCH   = "BRANCH"
    PARALLEL = "PARALLEL"


class PipelineEntry(str, Enum):
    ALIGN     = "ALIGN"
    TRACE     = "TRACE"
    CONSTRUCT = "CONSTRUCT"


class AudienceLevel(str, Enum):
    INVESTIGATOR = "INVESTIGATOR"
    REVIEWER     = "REVIEWER"
    SUMMARY      = "SUMMARY"


class DocumentType(str, Enum):
    POLICY_MEMO           = "POLICY_MEMO"
    INVESTIGATION_REPORT  = "INVESTIGATION_REPORT"
    EXECUTIVE_BRIEF       = "EXECUTIVE_BRIEF"
    TECHNICAL_APPENDIX    = "TECHNICAL_APPENDIX"
    DASHBOARD_CARD        = "DASHBOARD_CARD"


class ConflictDisplay(str, Enum):
    INLINE     = "INLINE"
    APPENDIX   = "APPENDIX"
    SUPPRESSED = "SUPPRESSED"


class ExplanationDepth(str, Enum):
    FULL           = "FULL"
    KEY_DECISIONS  = "KEY_DECISIONS"
    SUMMARY_ONLY   = "SUMMARY_ONLY"


class CitationStyle(str, Enum):
    INLINE_BRACKET = "INLINE_BRACKET"
    FOOTNOTE       = "FOOTNOTE"
    ENDNOTE        = "ENDNOTE"


class AnsBundleSection(str, Enum):
    FINDINGS        = "FINDINGS"
    EVIDENCE_CHAIN  = "EVIDENCE_CHAIN"
    TIMELINE        = "TIMELINE"
    EXHIBITS        = "EXHIBITS"
    LIMITATIONS     = "LIMITATIONS"
    REPRODUCIBILITY = "REPRODUCIBILITY"


class UncertaintyType(str, Enum):
    EVIDENTIAL = "EVIDENTIAL"
    TEMPORAL   = "TEMPORAL"
    EXTRACTION = "EXTRACTION"
    SCOPE      = "SCOPE"


class SensitivityLevel(str, Enum):
    LOW           = "LOW"
    MODERATE      = "MODERATE"
    HIGH          = "HIGH"
    NOT_COMPUTED  = "NOT_COMPUTED"


# ═══════════════════════════════════════════════════════════════
# Small value types
# ═══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class SpanRef:
    """Reference to a text span in a source artifact."""
    artifact_id: str
    start: int
    end: int
    text: str


@dataclass
class WitnessBundle:
    """Grounding witness for an evidence object or edge."""
    witness_id: str
    witness_type: str          # "extraction", "conflict_stance", "chain_link", …
    source_spans: List[Dict[str, Any]]
    module_call_id: Optional[str] = None
    confidence: float = 1.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DisagreementDescriptor:
    """Semantic nature of a conflict."""
    dimension: DisagreementDimension
    qualifier: Optional[str] = None     # sub-type or domain label
    description: str = ""


@dataclass
class HumanJudgment:
    """Investigator override on a conflict edge."""
    judgment: HumanJudgmentLabel
    judged_by: str
    judged_at: datetime
    rationale: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "judgment": self.judgment.value,
            "judged_by": self.judged_by,
            "judged_at": self.judged_at.isoformat(),
            "rationale": self.rationale,
        }