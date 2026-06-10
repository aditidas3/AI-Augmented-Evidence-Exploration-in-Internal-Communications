from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..core_types import EvidenceQuality, Witness


def quality_weight(quality: EvidenceQuality) -> float:
    if quality == EvidenceQuality.GROUNDED:
        return 1.0
    if quality == EvidenceQuality.INFERRED:
        return 0.7
    return 0.4


def aggregate_quality(witnesses: list[Witness]) -> EvidenceQuality:
    if not witnesses:
        return EvidenceQuality.AMBIGUOUS
    qualities = [witness.quality for witness in witnesses]
    if all(quality == EvidenceQuality.GROUNDED for quality in qualities):
        return EvidenceQuality.GROUNDED
    if any(quality == EvidenceQuality.GROUNDED for quality in qualities):
        return EvidenceQuality.INFERRED
    return EvidenceQuality.AMBIGUOUS


def compute_slot_confidence(
    witnesses: list[Witness],
    evidence_pieces: list[dict[str, Any]],
) -> float:
    if not witnesses:
        return 0.0

    mention_score = 0.0
    if evidence_pieces:
        mention_values = [
            float(piece.get("confidence", 0.0) or 0.0)
            for piece in evidence_pieces
        ]
        mention_score = sum(mention_values) / len(mention_values)

    quality_score = sum(
        quality_weight(witness.quality)
        for witness in witnesses
    ) / len(witnesses)

    support_score = min(1.0, len(witnesses) / 3.0)

    artifact_ids = {
        witness.anchor.artifact_id
        for witness in witnesses
        if witness.anchor and witness.anchor.artifact_id
    }
    cross_artifact_score = min(
        1.0,
        max(0, len(artifact_ids) - 1) / 2.0,
    )

    consistency_score = 0.0
    if evidence_pieces:
        var_counts: dict[str, int] = defaultdict(int)
        for piece in evidence_pieces:
            var_name = str(piece.get("var", "") or "")
            if var_name:
                var_counts[var_name] += 1
        if var_counts:
            consistency_score = max(var_counts.values()) / len(evidence_pieces)

    confidence = (
        0.35 * mention_score
        + 0.25 * quality_score
        + 0.15 * support_score
        + 0.15 * consistency_score
        + 0.10 * cross_artifact_score
    )
    return max(0.0, min(1.0, confidence))
