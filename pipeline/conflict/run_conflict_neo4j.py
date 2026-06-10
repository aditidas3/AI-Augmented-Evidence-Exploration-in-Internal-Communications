"""
run_conflict_neo4j.py
=====================
Runs CONFLICT with Neo4j write-back and all scalability improvements.

What changed vs run_conflict_on_trace.py:
  1. Uses Neo4jGraphWriter — writes Defeaters + CONTRADICTS to Neo4j
  2. Batch writes (batch_size=100) — no single-write overhead
  3. MERGE-based — idempotent, safe to re-run on the same data
  4. UID cache — fast node_exists() without a DB round-trip per call
  5. Pair cap now logs a WARNING when it fires (was silent before)
  6. Graceful fallback to InMemoryGraphWriter if Neo4j is unavailable

No changes needed to conflict.py — Neo4jGraphWriter is a drop-in.

Setup:
    pip install neo4j

    Set environment variables (or edit the defaults below):
        NEO4J_URI       bolt://localhost:7687
        NEO4J_USER      neo4j
        NEO4J_PASSWORD  yourpassword
        NEO4J_DATABASE  neo4j

Run:
    python run_conflict_neo4j.py
"""

import json
import os
import sys
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Set

sys.path.insert(0, ".")

# ── Imports ───────────────────────────────────────────────────────────────────
try:
    from conflict import Conflict, ConflictConfig
    print("conflict.py          loaded")
except ImportError as e:
    print(f"ERROR: could not import conflict.py — {e}")
    sys.exit(1)

try:
    from neo4j_writer import Neo4jGraphWriter, neo4j_driver
    NEO4J_AVAILABLE = True
    print("neo4j_writer.py      loaded")
except ImportError:
    NEO4J_AVAILABLE = False
    print("neo4j_writer.py      not found — will use in-memory fallback")

try:
    from save_conflict_output import (
        CONFLICT_BUNDLE_NS,
        _build_conflict_structure,
    )
except ImportError as e:
    print(f"ERROR: could not import save_conflict_output helpers — {e}")
    sys.exit(1)


# ── Config ────────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parents[2]
TRACE_PATH = REPO_ROOT / "results" / "trace" / "trace_bundle.json"
OUTPUT_PATH = REPO_ROOT / "results" / "conflict" / "conflict_bundle.json"
BATCH_SIZE  = 100


# ── InMemoryGraphWriter (fallback) ────────────────────────────────────────────
class InMemoryGraphWriter:
    def __init__(self, name=""):
        self.name  = name
        self.nodes: List[Dict] = []
        self.edges: List[Dict] = []
        self._uids: Set[str]   = set()

    def create_node(self, labels, properties):
        uid = properties.get("uid", "")
        if uid and uid in self._uids:
            return
        self.nodes.append({"labels": labels, "properties": dict(properties)})
        if uid:
            self._uids.add(uid)

    def create_edge(self, from_uid, to_uid, rel_type, properties):
        self.edges.append({
            "type": rel_type, "from": from_uid,
            "to":   to_uid,   "properties": dict(properties),
        })

    def node_exists(self, uid):
        return uid in self._uids

    def flush(self):
        pass  # nothing to flush in-memory


# ── Bundle loader ─────────────────────────────────────────────────────────────
def load_bundle(path: Path):
    print(f"\nLoading {path}...")
    with open(path, encoding="utf-8") as f:
        bundle = json.load(f)

    is_chain_first = "eg_delta" in bundle and "rg_trace" in bundle
    is_legacy_bundle = "eg" in bundle and "rg" in bundle and "trace_result" in bundle
    if is_chain_first:
        tr = {
            "eg_root_uid": bundle.get("eg_delta", {}).get("root_uid", ""),
            "rg_root_uid": bundle.get("rg_trace", {}).get("root_uid", ""),
            "stats": bundle.get("accuracy_report", {}),
        }
        eg_root_uid = tr.get("eg_root_uid", "")
        rg_root_uid = tr.get("rg_root_uid", "")
        eg_nodes    = bundle["eg_delta"].get("nodes", [])
        eg_edges    = bundle["eg_delta"].get("edges", [])
        rg_nodes    = bundle["rg_trace"].get("nodes", [])
        rg_edges    = bundle["rg_trace"].get("edges", [])
        print(f"  Format      : TRACE_BUNDLE")
        print(f"  TraceBundle : {bundle.get('trace_bundle_id', '')}")
    elif is_legacy_bundle:
        tr          = bundle["trace_result"]
        eg_root_uid = tr.get("eg_root_uid", "")
        rg_root_uid = tr.get("rg_root_uid", "")
        eg_nodes    = bundle["eg"].get("nodes", [])
        eg_edges    = bundle["eg"].get("edges", [])
        rg_nodes    = bundle["rg"].get("nodes", [])
        rg_edges    = bundle["rg"].get("edges", [])
        print(f"  Format      : LEGACY_BUNDLE")
        print(f"  Stats       : {tr.get('stats', {})}")
    else:
        eg_root_uid = bundle.get("eg_root_uid", "")
        rg_root_uid = bundle.get("rg_root_uid", "")
        all_nodes   = bundle.get("nodes", [])
        all_edges   = bundle.get("edges", [])
        RG_LABELS   = {"Claim", "Inference", "Defeater", "Agent", "GraphRoot"}
        eg_nodes    = [n for n in all_nodes if not set(n.get("labels",[])) & RG_LABELS]
        rg_nodes    = [n for n in all_nodes if     set(n.get("labels",[])) & RG_LABELS]
        eg_edges    = all_edges
        rg_edges    = all_edges
        tr          = {}
        print(f"  Format      : FLAT_LEGACY")

    print(f"  eg_root_uid : {eg_root_uid}")
    print(f"  rg_root_uid : {rg_root_uid}")
    print(f"  EG nodes    : {len(eg_nodes)}  EG edges: {len(eg_edges)}")
    print(f"  RG nodes    : {len(rg_nodes)}  RG edges: {len(rg_edges)}")

    return bundle, tr, eg_root_uid, rg_root_uid, eg_nodes, eg_edges, rg_nodes, rg_edges


# ── Seed a writer from node/edge lists ───────────────────────────────────────
def seed_writer(writer, nodes, edges):
    for node in nodes:
        writer.create_node(node["labels"], dict(node.get("properties", {})))
    # For Neo4j writer — flush nodes before adding edges
    if hasattr(writer, "flush"):
        writer.flush()
    for edge in edges:
        writer.create_edge(
            edge.get("from", ""),
            edge.get("to",   ""),
            edge.get("type", ""),
            dict(edge.get("properties", {})),
        )
    if hasattr(writer, "flush"):
        writer.flush()


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print()
    print("=" * 62)
    print("  CONFLICT OPERATOR — Neo4j Write-Back Mode")
    print("=" * 62)

    # 1. Load bundle
    (bundle, tr, eg_root_uid, rg_root_uid,
     eg_nodes, eg_edges, rg_nodes, rg_edges) = load_bundle(TRACE_PATH)

    # 2. Connect to Neo4j (or fall back to in-memory)
    driver = None
    if NEO4J_AVAILABLE:
        print("\nConnecting to Neo4j...")
        try:
            driver = neo4j_driver()
            print("  Connected")
        except Exception as e:
            print(f"  Could not connect: {e}")
            print("  Falling back to in-memory mode")

    if driver:
        eg = Neo4jGraphWriter(driver, batch_size=BATCH_SIZE, name="EG")
        rg = Neo4jGraphWriter(driver, batch_size=BATCH_SIZE, name="RG")
        print(f"  Writer: Neo4jGraphWriter (batch_size={BATCH_SIZE})")
    else:
        eg = InMemoryGraphWriter("EG")
        rg = InMemoryGraphWriter("RG")
        print("  Writer: InMemoryGraphWriter (fallback)")

    # 3. Seed writers from TRACE bundle
    print("\nSeeding graph writers...")
    seed_writer(eg, eg_nodes, eg_edges)
    seed_writer(rg, rg_nodes, rg_edges)
    print(f"  EG seeded: {len(eg_nodes)} nodes, {len(eg_edges)} edges")
    print(f"  RG seeded: {len(rg_nodes)} nodes, {len(rg_edges)} edges")

    # 4. Run CONFLICT — identical to the original runner
    print("\nRunning CONFLICT...")

    class MockTrace:
        pass

    cfg = ConflictConfig(
        skip_document_id_surfaces   = True,
        tau_doc_id_max_length       = 12,
        update_claim_status         = True,
        write_symmetric_contradicts = True,
        # Pair cap now logs a WARNING when triggered (scalability fix)
        max_pairs_per_slot          = 200,
    )

    op     = Conflict(eg=eg, rg=rg, bridge=eg, cfg=cfg)
    result = op.execute(
        trace_result = MockTrace(),
        rg_root_uid  = rg_root_uid,
        eg_root_uid  = eg_root_uid,
    )

    # 5. Flush final writes to Neo4j
    eg.flush()
    rg.flush()

    # 6. Print results
    print()
    print("=" * 62)
    print("  RESULTS")
    print("=" * 62)
    s = result.stats
    print(f"  Backend          : {'Neo4j' if driver else 'InMemory'}")
    print(f"  Witnesses        : {s.get('witnesses_indexed', 0)}")
    print(f"  Slot groups      : {s.get('slot_groups', 0)}")
    print(f"  Conflicts found  : {s.get('conflicts_found', 0)}")
    print(f"  Rule 1 Mismatch  : {s.get('rule1_surface_mismatch', 0)}")
    print(f"  Rule 2 Temporal  : {s.get('rule2_temporal_clash', 0)}")
    print(f"  Rule 3 Negation  : {s.get('rule3_negation', 0)}")
    print(f"  Rule 4 CrossArt  : {s.get('rule4_cross_artifact', 0)}")
    print(f"  Rule 5 Reliab.   : {s.get('rule5_reliability_diverge', 0)}")
    print(f"  Defeaters created: {s.get('defeaters_created', 0)}")
    print(f"  Claims contested : {s.get('claims_contested', 0)}")

    if driver:
        print()
        print("  Neo4j writes:")
        print(f"    CONTRADICTS edges → EG in Neo4j")
        print(f"    Defeater nodes    → RG in Neo4j")
        print(f"    Claim status      → updated in Neo4j")

    if result.conflicts:
        print()
        print("  Conflicts:")
        for i, c in enumerate(result.conflicts, 1):
            print(f"    [{i}] {c.rule} ({c.defeater_type})")
            print(f"         {c.description[:100]}")

    # 7. Save conflict_bundle.json for CONSTRUCT
    print(f"\nSaving {OUTPUT_PATH}...")

    conflicts_out = []
    for c in result.conflicts:
        conflicts_out.append({
            "conflict_id":        c.conflict_id,
            "rule":               c.rule,
            "defeater_type":      c.defeater_type,
            "witness_a_uid":      c.witness_a_uid,
            "witness_b_uid":      c.witness_b_uid,
            "description":        c.description,
            "confidence":         c.confidence,
            "weaker_witness_uid": c.weaker_witness_uid,
            "claim_a_uid":        c.claim_a_uid,
            "claim_b_uid":        c.claim_b_uid,
            "negation_type":      getattr(c, "negation_type",  ""),
            "negation_cue":       getattr(c, "negation_cue",   ""),
            "negation_layer":     getattr(c, "negation_layer", ""),
        })

    # Use original bundle nodes/edges so CONSTRUCT has the full graph
    # ── Build output RG — use the writer's full state after CONFLICT runs ────────
    # The InMemoryGraphWriter (and Neo4jGraphWriter local mirror) accumulate
    # ALL nodes and edges — both seeded from TRACE and newly written by CONFLICT.
    # rg.nodes / rg.edges is therefore the complete post-CONFLICT RG.
    # eg.nodes / eg.edges is the complete post-CONFLICT EG (with CONTRADICTS).
    #
    # For Neo4j writer we use _local_nodes/_local_edges which are always
    # populated immediately (before the Neo4j batch flush completes).

    if hasattr(rg, "_local_nodes"):
        # Neo4jGraphWriter — use local mirror
        merged_rg_nodes = list(rg._local_nodes)
        merged_rg_edges = list(rg._local_edges)
        merged_eg_nodes = list(eg._local_nodes)
        merged_eg_edges = list(eg._local_edges)
    else:
        # InMemoryGraphWriter — use nodes/edges directly
        merged_rg_nodes = list(rg.nodes)
        merged_rg_edges = list(rg.edges)
        merged_eg_nodes = list(eg.nodes)
        merged_eg_edges = list(eg.edges)

    # Update Claim status for contested Claims in the merged nodes
    contested_uids = set(result.claims_contested)
    for node in merged_rg_nodes:
        if ("Claim" in node.get("labels", [])
                and node["properties"].get("uid") in contested_uids):
            node["properties"]["status"] = "contested"

    orig_eg = {"nodes": merged_eg_nodes, "edges": merged_eg_edges}

    if "trace_bundle_id" in bundle and "slot_candidates" in bundle:
        conflict_bundle_id = str(
            uuid.uuid5(CONFLICT_BUNDLE_NS, f"conflict_bundle::{bundle.get('trace_bundle_id', '')}")
        )
        chain_conflicts = [asdict(conflict) for conflict in result.conflicts]
        output = {
            "schema_version": "conflict-bundle.chain.v1",
            "conflict_bundle_id": conflict_bundle_id,
            "trace_bundle_id": bundle.get("trace_bundle_id", ""),
            "trace_bundle": bundle,
            "conflict_structure": _build_conflict_structure(
                trace_bundle=bundle,
                conflicts=chain_conflicts,
            ),
            "conflict_result": {
                "conflicts": chain_conflicts,
                "defeater_uids": result.defeater_uids,
                "contradicts_edges": len(
                    [edge for edge in merged_eg_edges if edge.get("type") == "CONTRADICTS"]
                ),
                "claims_contested": result.claims_contested,
                "stats": result.stats,
                "diagnostics": result.diagnostics,
                "eg_root_uid": eg_root_uid,
                "rg_root_uid": rg_root_uid,
                "neo4j": driver is not None,
            },
            "eg_delta": orig_eg,
            "rg_delta": {
                "nodes": merged_rg_nodes,
                "edges": merged_rg_edges,
            },
            "provenance_manifest_delta": {
                "operator": "CONFLICT",
                "input_trace_bundle_id": bundle.get("trace_bundle_id", ""),
                "output_conflict_bundle_id": conflict_bundle_id,
            },
        }
    else:
        output = {
            "trace_result":    tr,
            "eg":              orig_eg,
            "rg":              {
                "nodes": merged_rg_nodes,
                "edges": merged_rg_edges,
            },
            "conflict_result": {
                "conflicts":        conflicts_out,
                "defeater_uids":    result.defeater_uids,
                "claims_contested": result.claims_contested,
                "stats":            result.stats,
                "diagnostics":      result.diagnostics,
                "eg_root_uid":      eg_root_uid,
                "rg_root_uid":      rg_root_uid,
                "neo4j":            driver is not None,
            },
        }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, default=str)

    print(f"  Saved — ready for CONSTRUCT")

    # 8. Close Neo4j
    if driver:
        driver.close()
        print("\n  Neo4j connection closed")


if __name__ == "__main__":
    main()
