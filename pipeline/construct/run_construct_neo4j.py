"""
run_construct_neo4j.py
======================
Runs CONSTRUCT with Neo4j write-back.

What this script does:
    1. Loads conflict_bundle.json (produced by CONFLICT)
    2. Connects to Neo4j
    3. Seeds the graph writers from the bundle
    4. Runs CONSTRUCT — builds the formal argument structure
    5. Writes all new nodes and edges directly to Neo4j:
         - 12 new Inference nodes (Rule 1 slot support + Rule 2 cross-slot)
         - 1 Synthesised Claim node (Rule 5)
         - 1 synthesis Inference node (Rule 5)
         - constructScore fields on existing TRACE Inferences (Rule 3)
         - 55 new edges (HAS_PREMISE, HAS_CONCLUSION, CONTAINS_INFERENCE)
    6. Saves construct_bundle.json for EXPLAIN

No changes to construct.py needed — Neo4jGraphWriter is a drop-in replacement.

Setup:
    pip install neo4j

    Set environment variables:
        NEO4J_URI       bolt://localhost:7687
        NEO4J_USER      neo4j
        NEO4J_PASSWORD  yourpassword
        NEO4J_DATABASE  neo4j

Run:
    python run_construct_neo4j.py
"""

import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Set

sys.path.insert(0, ".")

# ── Imports ───────────────────────────────────────────────────────────────────
try:
    from construct import Construct
    print("construct.py         loaded")
except ImportError as e:
    print(f"ERROR: could not import construct.py — {e}")
    sys.exit(1)

try:
    from neo4j_writer import Neo4jGraphWriter, neo4j_driver
    NEO4J_AVAILABLE = True
    print("neo4j_writer.py      loaded")
except ImportError:
    NEO4J_AVAILABLE = False
    print("neo4j_writer.py      not found — will use in-memory fallback")


# ── Config ────────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parents[2]
INPUT_PATH  = REPO_ROOT / "results" / "conflict" / "conflict_bundle.json"
OUTPUT_PATH = REPO_ROOT / "results" / "construct" / "construct_bundle.json"
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
        pass


# ── Seed a writer from node/edge lists ───────────────────────────────────────
def seed_writer(writer, nodes, edges):
    for node in nodes:
        writer.create_node(node["labels"], dict(node.get("properties", {})))
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


# ── Write CONSTRUCT results back to Neo4j ────────────────────────────────────
def write_construct_to_neo4j(result, rg_writer, eg_writer, is_neo4j: bool) -> None:
    """
    Write all nodes and edges that CONSTRUCT created back to Neo4j.

    What gets written:
        New nodes   — 12 Inference nodes + 1 Synthesised Claim + 1 synthesis Inference
        New edges   — HAS_PREMISE, HAS_CONCLUSION, CONTAINS_INFERENCE (55 total)
        Updated     — constructScore field on existing TRACE Inferences (Rule 3)
    """
    if not is_neo4j:
        return  # In-memory mode — nothing extra to write

    print("\n  Writing CONSTRUCT results to Neo4j...")

    # Write new nodes (Inferences, Synthesised Claim)
    n_nodes = 0
    for node in result.new_nodes:
        rg_writer.create_node(node["labels"], dict(node.get("properties", {})))
        n_nodes += 1

    # Write new edges
    n_edges = 0
    for edge in result.new_edges:
        rg_writer.create_edge(
            edge.get("from", ""),
            edge.get("to",   ""),
            edge.get("type", ""),
            dict(edge.get("properties", {})),
        )
        n_edges += 1

    # Update existing TRACE Inferences with constructScore (Rule 3)
    # Uses SET to add new fields without overwriting confidenceScore
    n_updated = 0
    if hasattr(rg_writer, "_driver"):
        with rg_writer._driver.session(database=rg_writer._graph) as s:
            for u in result.updated_inferences:
                if "constructScore" in u:
                    s.run(
                        "MATCH (n {uid: $uid}) "
                        "SET n.constructScore = $cscore, "
                        "    n.constructScoreReason = $reason, "
                        "    n.originalConfidence = $orig",
                        uid    = u["uid"],
                        cscore = u.get("constructScore", u.get("updated_conf", 0)),
                        reason = u.get("constructScoreReason",
                                       f"Rule3 — {u.get('defeaters_applied', 0)} defeater(s)"),
                        orig   = u.get("originalConfidence", u.get("original_conf", 0)),
                    )
                    n_updated += 1

    # Flush all buffered writes
    rg_writer.flush()

    print(f"    New nodes written   : {n_nodes}")
    print(f"    New edges written   : {n_edges}")
    print(f"    Inferences updated  : {n_updated} (constructScore added)")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print()
    print("=" * 62)
    print("  CONSTRUCT OPERATOR — Neo4j Write-Back Mode")
    print("=" * 62)

    # 1. Load conflict_bundle.json
    print(f"\nLoading {INPUT_PATH}...")
    with open(INPUT_PATH) as f:
        data = json.load(f)

    if "trace_bundle" in data or "ranked_chains" in data:
        print("  Format      : chain-first conflict bundle")
        op = Construct(data)
        result = op.execute()
        output = {
            "schema_version": "construct-bundle.chain.v1",
            "construct_bundle_id": result.ans_bundle.get("bundle_id", result.synthesis_uid),
            "trace_bundle_id": data.get("trace_bundle_id", ""),
            "conflict_bundle_id": data.get("conflict_bundle_id", ""),
            "selected_chain_id": result.selected_chain_id,
            "trace_bundle": data.get("trace_bundle", {}),
            "conflict_bundle": data,
            "ans_bundle": result.ans_bundle,
            "g_ans": result.g_ans,
            "citation_map": result.citation_map,
            "limitations": result.limitations,
            "construct_result": {
                "selected_chain_id": result.selected_chain_id,
                "synthesis_uid": result.synthesis_uid,
                "synthesis_confidence": result.synthesis_conf,
                "synthesis_type": result.synthesis_type,
                "stats": result.stats,
                "diagnostics": result.diagnostics,
            },
            "rg_delta": {
                "nodes": result.new_nodes,
                "edges": result.new_edges,
            },
            "provenance_manifest_delta": {
                "operator": "CONSTRUCT",
                "input_conflict_bundle_id": data.get("conflict_bundle_id", ""),
                "output_construct_bundle_id": result.ans_bundle.get("bundle_id", result.synthesis_uid),
            },
        }
        print("\n── Output ───────────────────────────────────────────────────")
        print(f"  Selected chain    : {result.selected_chain_id}")
        print(f"  Findings          : {result.stats.get('findings_count', 0)}")
        print(f"  Citations         : {result.stats.get('citations', 0)}")
        print(f"  Limitations       : {result.stats.get('limitations', 0)}")
        print(f"  Confidence        : {result.stats.get('synthesis_confidence', 0.0)}")
        print(f"\nSaving {OUTPUT_PATH}...")
        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, default=str)
        print("  Saved — ready for EXPLAIN")
        return

    tr          = data.get("trace_result", {})
    eg_nodes    = data["eg"].get("nodes", [])
    eg_edges    = data["eg"].get("edges", [])
    rg_nodes    = data["rg"].get("nodes", [])
    rg_edges    = data["rg"].get("edges", [])
    eg_root_uid = tr.get("eg_root_uid", "")
    rg_root_uid = tr.get("rg_root_uid", "")

    print(f"  eg_root_uid : {eg_root_uid}")
    print(f"  rg_root_uid : {rg_root_uid}")
    print(f"  EG nodes    : {len(eg_nodes)}  EG edges: {len(eg_edges)}")
    print(f"  RG nodes    : {len(rg_nodes)}  RG edges: {len(rg_edges)}")
    print(f"  Claims      : {tr.get('stats', {}).get('claims', '?')}")
    print(f"  Defeaters   : {len([n for n in rg_nodes if 'Defeater' in n.get('labels',[])])}")

    # 2. Connect to Neo4j
    driver    = None
    is_neo4j  = False

    if NEO4J_AVAILABLE:
        print("\nConnecting to Neo4j...")
        try:
            driver   = neo4j_driver()
            is_neo4j = True
            print(f"  Connected")
        except Exception as e:
            print(f"  Could not connect: {e}")
            print("  Falling back to in-memory mode")

    # 3. Create graph writers
    if is_neo4j:
        rg = Neo4jGraphWriter(driver, batch_size=BATCH_SIZE, name="RG")
        eg = Neo4jGraphWriter(driver, batch_size=BATCH_SIZE, name="EG")
        print(f"  Writer: Neo4jGraphWriter (batch_size={BATCH_SIZE})")
    else:
        rg = InMemoryGraphWriter("RG")
        eg = InMemoryGraphWriter("EG")
        print("  Writer: InMemoryGraphWriter (fallback)")

    # 4. Seed writers from bundle
    print("\nSeeding graph writers...")
    seed_writer(eg, eg_nodes, eg_edges)
    seed_writer(rg, rg_nodes, rg_edges)
    print(f"  EG seeded: {len(eg_nodes)} nodes, {len(eg_edges)} edges")
    print(f"  RG seeded: {len(rg_nodes)} nodes, {len(rg_edges)} edges")

    # 5. Run CONSTRUCT
    print("\nRunning CONSTRUCT...")
    op     = Construct(data)
    result = op.execute()

    # 6. Write results to Neo4j
    write_construct_to_neo4j(result, rg, eg, is_neo4j)

    # 7. Print results
    print()
    print("=" * 62)
    print("  RESULTS")
    print("=" * 62)
    s = result.stats
    print(f"  Backend              : {'Neo4j' if is_neo4j else 'InMemory'}")
    print(f"  Claims loaded        : {s.get('claims_loaded', 0)}")
    print(f"  Inferences loaded    : {s.get('inferences_loaded', 0)}")
    print(f"  Defeaters loaded     : {s.get('defeaters_loaded', 0)}")
    print(f"  Contested claims     : {s.get('contested_claims', 0)}")
    print(f"  Inferences weakened  : {s.get('inferences_weakened', 0)}")
    print(f"  New nodes written    : {s.get('new_nodes_written', 0)}")
    print(f"  New edges written    : {s.get('new_edges_written', 0)}")
    print(f"  Synthesis type       : {s.get('synthesis_type', '')}")
    print(f"  Synthesis confidence : {s.get('synthesis_confidence', '')}")
    print(f"  All sentences tethered: {s.get('all_sentences_tethered', '')}")
    print(f"  Limitations found    : {s.get('limitations', 0)}")
    print(f"  Timeline events      : {s.get('timeline_events', 0)}")
    print(f"  Exhibits collected   : {s.get('exhibits', 0)}")

    if is_neo4j:
        print()
        print("  Neo4j writes:")
        print(f"    New Inference nodes       → RG in Neo4j")
        print(f"    Synthesised Claim node    → RG in Neo4j")
        print(f"    Synthesis Inference node  → RG in Neo4j")
        print(f"    constructScore            → updated on TRACE Inferences")
        print(f"    New edges (HAS_PREMISE etc) → RG in Neo4j")

    # Algorithm 4 summary
    print()
    print("── Algorithm 4 Components ───────────────────────────────────")
    print(f"  WitnessTether    : {len(result.cite_map)} slots tethered")
    print(f"  AllTethered      : {result.all_tethered}")
    print(f"  Timeline events  : {len(result.timeline)}")
    print(f"  Exhibits         : {len(result.exhibits)}")
    print(f"  Limitations      : {len(result.limitations)}")
    if result.limitations:
        for lim in result.limitations:
            print(f"    ⚠ {lim}")

    # 8. Save construct_bundle.json for EXPLAIN
    print(f"\nSaving {OUTPUT_PATH}...")

    # Rebuild output — include Algorithm 4 components
    output = {
        "trace_result":    tr,
        "conflict_result": data.get("conflict_result", {}),
        "eg":              data["eg"],
        "rg": {
            "nodes": rg_nodes + result.new_nodes,
            "edges": rg_edges + result.new_edges,
        },
        "construct_result": {
            "new_nodes":            result.new_nodes,
            "new_edges":            result.new_edges,
            "updated_inferences":   result.updated_inferences,
            "synthesis_uid":        result.synthesis_uid,
            "synthesis_confidence": result.synthesis_conf,
            "synthesis_type":       result.synthesis_type,
            "stats":                result.stats,
            "diagnostics":          result.diagnostics,
            "eg_root_uid":          eg_root_uid,
            "rg_root_uid":          rg_root_uid,
            "neo4j":                is_neo4j,
            # Algorithm 4 structured components
            "cite_map":             result.cite_map,
            "timeline":             result.timeline,
            "exhibits":             result.exhibits,
            "limitations":          result.limitations,
            "all_tethered":         result.all_tethered,
        },
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2, default=str)

    print(f"  Saved — ready for EXPLAIN")

    # 9. Close Neo4j
    if driver:
        driver.close()
        print("\n  Neo4j connection closed")


if __name__ == "__main__":
    main()
