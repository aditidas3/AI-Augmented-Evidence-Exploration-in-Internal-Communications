"""
Run CONFLICT on the current chain-first TraceBundle and save a bundle
that CONSTRUCT can consume.

Run from the repository root:
    python pipeline/conflict/save_conflict_output.py

Input:
    results/trace/trace_bundle.json

Output:
    results/conflict/conflict_bundle.json
"""

from __future__ import annotations

import json
import sys
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pipeline.conflict.conflict import Conflict, ConflictConfig
from pipeline.conflict.trace_bundle_adapter import TraceBundleConflictView

TRACE_PATH = REPO_ROOT / "results" / "trace" / "trace_bundle.json"
OUTPUT_PATH = REPO_ROOT / "results" / "conflict" / "conflict_bundle.json"
CONFLICT_BUNDLE_NS = uuid.UUID("c0f1c700-0000-4000-a000-000000000001")


def build_conflict_bundle(trace_bundle: Dict[str, Any]) -> Dict[str, Any]:
    """Run CONFLICT and return the downstream chain-first conflict bundle."""
    view = TraceBundleConflictView(trace_bundle)
    eg, rg = view.build_graph_writers()
    result = Conflict(
        eg=eg,
        rg=rg,
        bridge=eg,
        cfg=ConflictConfig(
            skip_document_id_surfaces=True,
            tau_doc_id_max_length=12,
            update_claim_status=True,
            write_symmetric_contradicts=True,
        ),
    ).execute(
        trace_result=view.trace_result_payload(),
        rg_root_uid=view.rg_root_uid,
        eg_root_uid=view.eg_root_uid,
    )

    conflicts = [asdict(conflict) for conflict in result.conflicts]
    conflict_structure = _build_conflict_structure(
        trace_bundle=trace_bundle,
        conflicts=conflicts,
    )
    trace_bundle_id = str(trace_bundle.get("trace_bundle_id", ""))
    conflict_bundle_id = str(
        uuid.uuid5(CONFLICT_BUNDLE_NS, f"conflict_bundle::{trace_bundle_id}")
    )

    return {
        "schema_version": "conflict-bundle.chain.v1",
        "conflict_bundle_id": conflict_bundle_id,
        "trace_bundle_id": trace_bundle_id,
        "trace_bundle": trace_bundle,
        "conflict_structure": conflict_structure,
        "conflict_result": {
            "conflicts": conflicts,
            "defeater_uids": result.defeater_uids,
            "contradicts_edges": len(
                [edge for edge in eg.edges if edge.get("type") == "CONTRADICTS"]
            ),
            "claims_contested": result.claims_contested,
            "stats": result.stats,
            "diagnostics": result.diagnostics,
            "eg_root_uid": view.eg_root_uid,
            "rg_root_uid": view.rg_root_uid,
        },
        "eg_delta": _graph_to_dict(eg),
        "rg_delta": _graph_to_dict(rg),
        "provenance_manifest_delta": {
            "operator": "CONFLICT",
            "input_trace_bundle_id": trace_bundle_id,
            "output_conflict_bundle_id": conflict_bundle_id,
        },
    }


def _graph_to_dict(writer: Any) -> Dict[str, Any]:
    return {
        "nodes": list(getattr(writer, "nodes", []) or []),
        "edges": list(getattr(writer, "edges", []) or []),
    }


def _build_conflict_structure(
    *,
    trace_bundle: Dict[str, Any],
    conflicts: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    candidates = _candidate_index(trace_bundle)
    object_to_candidate = {
        str(candidate.get("object_id", "")): candidate_id
        for candidate_id, candidate in candidates.items()
        if candidate.get("object_id")
    }
    mappings = list((trace_bundle.get("map_transform", {}) or {}).get("retained", []) or [])
    edges: Dict[str, Dict[str, Any]] = {}
    for conflict in conflicts:
        source_object_id = str(conflict.get("witness_a_uid", ""))
        target_object_id = str(conflict.get("witness_b_uid", ""))
        source_candidate_id = object_to_candidate.get(source_object_id, "")
        target_candidate_id = object_to_candidate.get(target_object_id, "")
        mapping_id = _matching_mapping_id(
            mappings,
            source_object_id=source_object_id,
            target_object_id=target_object_id,
            source_candidate_id=source_candidate_id,
            target_candidate_id=target_candidate_id,
        )
        edge_id = str(conflict.get("conflict_id", ""))
        edges[edge_id] = {
            "edge_id": edge_id,
            "conflict_id": edge_id,
            "rule": conflict.get("rule", ""),
            "stance": _stance_for_conflict(conflict),
            "defeater_type": conflict.get("defeater_type", ""),
            "description": conflict.get("description", ""),
            "confidence": conflict.get("confidence", 0.0),
            "source_object_id": source_object_id,
            "target_object_id": target_object_id,
            "source_candidate_id": source_candidate_id,
            "target_candidate_id": target_candidate_id,
            "mapping_id": mapping_id,
            "chain_ids": _chain_ids_for_edge(
                trace_bundle,
                source_candidate_id=source_candidate_id,
                target_candidate_id=target_candidate_id,
                mapping_id=mapping_id,
            ),
            "weaker_witness_uid": conflict.get("weaker_witness_uid", ""),
            "claim_a_uid": conflict.get("claim_a_uid", ""),
            "claim_b_uid": conflict.get("claim_b_uid", ""),
        }
    return {
        "edges": edges,
        "clusters": {},
        "scope_size": len(candidates),
        "pairs_evaluated": len(edges),
    }


def _candidate_index(trace_bundle: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {
        str(candidate.get("candidate_id", "")): candidate
        for rows in (trace_bundle.get("slot_candidates", {}) or {}).values()
        for candidate in rows or []
        if candidate.get("candidate_id")
    }


def _matching_mapping_id(
    mappings: Iterable[Dict[str, Any]],
    *,
    source_object_id: str,
    target_object_id: str,
    source_candidate_id: str,
    target_candidate_id: str,
) -> str:
    wanted_objects = {source_object_id, target_object_id}
    wanted_candidates = {source_candidate_id, target_candidate_id}
    for mapping in mappings:
        mapping_objects = {
            str(mapping.get("source_object_id", "")),
            str(mapping.get("target_object_id", "")),
        }
        mapping_candidates = {
            str(mapping.get("source_candidate_id", "")),
            str(mapping.get("target_candidate_id", "")),
        }
        if source_object_id and target_object_id and mapping_objects == wanted_objects:
            return str(mapping.get("mapping_id", ""))
        if source_candidate_id and target_candidate_id and mapping_candidates == wanted_candidates:
            return str(mapping.get("mapping_id", ""))
    return ""


def _chain_ids_for_edge(
    trace_bundle: Dict[str, Any],
    *,
    source_candidate_id: str,
    target_candidate_id: str,
    mapping_id: str,
) -> List[str]:
    referenced_candidates = {
        candidate_id
        for candidate_id in (source_candidate_id, target_candidate_id)
        if candidate_id
    }
    chain_ids: List[str] = []
    for chain in trace_bundle.get("ranked_chains", []) or []:
        chain_candidate_ids = set(chain.get("slot_candidate_ids", []) or [])
        chain_mapping_ids = set(chain.get("mapping_ids", []) or [])
        candidate_match = (
            len(referenced_candidates) == 2
            and referenced_candidates <= chain_candidate_ids
        )
        mapping_match = bool(mapping_id and mapping_id in chain_mapping_ids)
        if candidate_match or mapping_match:
            chain_ids.append(str(chain.get("chain_id", "")))
    return [chain_id for chain_id in chain_ids if chain_id]


def _stance_for_conflict(conflict: Dict[str, Any]) -> str:
    if str(conflict.get("defeater_type", "")).lower() == "rebutting":
        return "REFUTES"
    return "UNDERCUTS"


def _load_json(path: Path) -> Dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)


def main() -> None:
    print("=" * 62)
    print("  CONFLICT OPERATOR")
    print("=" * 62)
    print(f"\nLoading {TRACE_PATH}...")
    trace_bundle = _load_json(TRACE_PATH)
    output = build_conflict_bundle(trace_bundle)
    result = output["conflict_result"]
    stats = result.get("stats", {})

    print("Running CONFLICT...")
    print(f"  Conflicts found   : {stats.get('conflicts_found', 0)}")
    print(f"  Rule 1 Mismatch   : {stats.get('rule1_surface_mismatch', 0)}")
    print(f"  Rule 5 Reliability: {stats.get('rule5_reliability', 0)}")
    print(f"  Defeaters created : {stats.get('defeaters_created', 0)}")
    print(f"  Claims contested  : {stats.get('claims_contested', 0)}")
    print(f"  Conflict edges    : {len(output['conflict_structure']['edges'])}")

    _write_json(OUTPUT_PATH, output)
    print(f"\nSaved to {OUTPUT_PATH}")
    print("  This file is ready to use as input for CONSTRUCT.")


if __name__ == "__main__":
    main()
