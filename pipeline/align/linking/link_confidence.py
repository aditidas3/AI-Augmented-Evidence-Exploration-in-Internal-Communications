from __future__ import annotations

from typing import Any


def score_link_path_confidence(
    source_candidate: dict[str, Any],
    target_candidate: dict[str, Any],
    path_length: int,
) -> float:
    hop_count = max(0, path_length - 1)
    path_bonus = max(0.04, 0.20 - (0.04 * hop_count))
    confidence = 0.18
    confidence += 0.24 * float(source_candidate.get("quality", 0.0) or 0.0)
    confidence += 0.24 * float(target_candidate.get("quality", 0.0) or 0.0)
    confidence += path_bonus

    if source_candidate.get("selected"):
        confidence += 0.05
    if target_candidate.get("selected"):
        confidence += 0.05

    if (
        source_candidate.get("source") == "candidate_only"
        or target_candidate.get("source") == "candidate_only"
    ):
        confidence -= 0.08

    return round(max(0.10, min(0.95, confidence)), 4)


def shared_anchor_link_confidence(
    source_confidence: float,
    target_confidence: float,
    *,
    shared_anchor_count: int,
) -> float:
    confidence = 0.52
    confidence += min(0.14, 0.06 * shared_anchor_count)
    confidence += min(0.10, 0.10 * source_confidence)
    confidence += min(0.10, 0.10 * target_confidence)
    return round(min(0.86, confidence), 4)


def shared_artifact_link_confidence(
    source_confidence: float,
    target_confidence: float,
    *,
    shared_artifact_count: int,
    lexical_overlap: float,
) -> float:
    confidence = 0.48
    confidence += min(0.16, 0.16 * lexical_overlap)
    confidence += min(0.08, 0.05 * shared_artifact_count)
    confidence += min(0.10, 0.10 * source_confidence)
    confidence += min(0.10, 0.10 * target_confidence)
    return round(min(0.84, confidence), 4)
