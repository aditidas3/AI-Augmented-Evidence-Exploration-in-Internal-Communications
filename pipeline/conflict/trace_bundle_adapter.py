"""Adapters from chain-first TraceBundle to CONFLICT inputs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from pipeline.trace.writers import InMemoryGraphWriter

from .conflict import WitnessRecord


@dataclass
class TraceBundleConflictView:
    bundle: Dict[str, Any]

    @property
    def eg_root_uid(self) -> str:
        return str(self.bundle.get("eg_delta", {}).get("root_uid", ""))

    @property
    def rg_root_uid(self) -> str:
        return str(self.bundle.get("rg_trace", {}).get("root_uid", ""))

    def trace_result_payload(self) -> Dict[str, Any]:
        return {
            "trace_bundle_id": self.bundle.get("trace_bundle_id", ""),
            "slot_candidates": self.bundle.get("slot_candidates", {}),
            "map_transform": self.bundle.get("map_transform", {}),
            "ranked_chains": self.bundle.get("ranked_chains", []),
            "provenance_manifest": self.bundle.get("provenance_manifest", {}),
        }

    def to_conflict_records(self) -> List[WitnessRecord]:
        records: List[WitnessRecord] = []
        for rows in self.bundle.get("slot_candidates", {}).values():
            for candidate in rows or []:
                records.append(_candidate_to_record(candidate))
        records.sort(key=lambda r: r.uid)
        return records

    def build_graph_writers(self) -> Tuple[InMemoryGraphWriter, InMemoryGraphWriter]:
        eg = InMemoryGraphWriter()
        rg = InMemoryGraphWriter()
        for node in self.bundle.get("eg_delta", {}).get("nodes", []) or []:
            eg.create_node(node.get("labels", []), dict(node.get("properties", {})))
        for node in self.bundle.get("rg_trace", {}).get("nodes", []) or []:
            rg.create_node(node.get("labels", []), dict(node.get("properties", {})))
        for edge in self.bundle.get("eg_delta", {}).get("edges", []) or []:
            eg.edges.append(dict(edge))
        for edge in self.bundle.get("rg_trace", {}).get("edges", []) or []:
            rg.edges.append(dict(edge))
        return eg, rg


def _candidate_to_record(candidate: Dict[str, Any]) -> WitnessRecord:
    witness_bundle = candidate.get("witness_bundle", {}) or {}
    object_id = str(candidate.get("object_id") or witness_bundle.get("witness_id") or "")
    return WitnessRecord(
        uid=object_id,
        slot_type=str(candidate.get("slot_type", "") or "").upper(),
        var_name=str(candidate.get("var_name", "") or ""),
        surface=str(candidate.get("surface", "") or ""),
        content_excerpt=str(witness_bundle.get("justification") or candidate.get("surface", "") or ""),
        reliability_score=float(candidate.get("confidence", candidate.get("score", 0.0)) or 0.0),
        kg0_entity_id=str(candidate.get("kg0_entity_id", "") or ""),
        anchor_id=str(candidate.get("anchor_id", "") or ""),
        artifact_id=str(candidate.get("artifact_id", "") or ""),
        quality=str(candidate.get("quality", "AMBIGUOUS") or "AMBIGUOUS"),
        claim_uid=str(candidate.get("claim_uid", "") or ""),
        raw=dict(candidate),
    )
