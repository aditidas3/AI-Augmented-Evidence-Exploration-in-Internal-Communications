from __future__ import annotations

from ..core_types import Subgraph


def collect_subgraph_kg0_nodes(subgraph: Subgraph) -> set[str]:
    return {
        binding.kg0_node_id
        for binding in subgraph.bindings.values()
        if binding.kg0_node_id
    }


def diversity_rerank_subgraphs(
    subgraphs: list[Subgraph],
    *,
    diversity_bonus: float,
) -> list[Subgraph]:
    if len(subgraphs) <= 1:
        for subgraph in subgraphs:
            subgraph.diversity_score = 1.0
        return subgraphs

    lam = min(diversity_bonus, 0.5)
    max_score = max(subgraph.score for subgraph in subgraphs)
    min_score = min(subgraph.score for subgraph in subgraphs)
    score_range = max_score - min_score if max_score > min_score else 1.0

    def normalized_score(subgraph: Subgraph) -> float:
        return (subgraph.score - min_score) / score_range

    selected: list[Subgraph] = []
    remaining = list(subgraphs)
    seen_nodes: set[str] = set()

    while remaining:
        best: Subgraph | None = None
        best_combined = -1.0
        best_novelty = 0.0

        for subgraph in remaining:
            subgraph_nodes = collect_subgraph_kg0_nodes(subgraph)
            if subgraph_nodes:
                novelty = len(subgraph_nodes - seen_nodes) / len(subgraph_nodes)
            else:
                novelty = 1.0 if not selected else 0.0

            combined = (1.0 - lam) * normalized_score(subgraph) + lam * novelty
            if combined > best_combined:
                best_combined = combined
                best = subgraph
                best_novelty = novelty

        if best is None:
            break

        best.diversity_score = round(best_novelty, 3)
        seen_nodes |= collect_subgraph_kg0_nodes(best)
        selected.append(best)
        remaining.remove(best)

    return selected
