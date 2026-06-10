from __future__ import annotations

from typing import Iterable

from ..core_types import CandidateArtifact


def artifact_bridge_tokens(candidate: CandidateArtifact) -> set[str]:
    metadata = candidate.metadata or {}
    parts: list[str] = []
    for key in ("title", "subject", "participants", "collection", "family"):
        value = metadata.get(key)
        if value is None:
            continue
        if isinstance(value, list):
            parts.extend(str(item) for item in value)
        else:
            parts.append(str(value))
    text = " ".join(parts).lower()
    return {
        token
        for token in text.replace(";", " ").replace(",", " ").split()
        if len(token) >= 3
    }


def has_cross_family_bridge(selected: list[CandidateArtifact]) -> bool:
    if len(selected) < 2:
        return False
    tokens_by_id = {
        candidate.artifact_id: artifact_bridge_tokens(candidate)
        for candidate in selected
    }
    for i in range(len(selected)):
        for j in range(i + 1, len(selected)):
            left = selected[i]
            right = selected[j]
            if left.family == right.family:
                continue
            if tokens_by_id[left.artifact_id] & tokens_by_id[right.artifact_id]:
                return True
    return False


def artifact_selection_score(
    candidate: CandidateArtifact,
    selected: Iterable[CandidateArtifact],
    *,
    required_families: set[str],
    covered_families: set[str],
    family_coverage_weight: float,
    diversity_weight: float,
) -> float:
    score = candidate.fused_score
    if candidate.family in required_families and candidate.family not in covered_families:
        score += family_coverage_weight

    selected_list = list(selected)
    family_count = sum(
        1 for artifact in selected_list if artifact.family == candidate.family
    )
    score -= diversity_weight * family_count

    candidate_collection = (candidate.metadata or {}).get("collection", "")
    if candidate_collection:
        collection_count = sum(
            1
            for artifact in selected_list
            if (artifact.metadata or {}).get("collection", "") == candidate_collection
        )
        score -= 0.5 * diversity_weight * collection_count

    return score
