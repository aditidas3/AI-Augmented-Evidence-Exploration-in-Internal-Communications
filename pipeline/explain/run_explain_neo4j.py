"""
run_explain_neo4j.py
====================
Runs EXPLAIN with Neo4j write-back.

What this script does:
    1. Loads construct_output.json (produced by CONSTRUCT)
    2. Connects to Neo4j
    3. Runs EXPLAIN — all five Algorithm 5 phases
    4. Writes the ExplBundle to Neo4j:
         - ExplainNode (root node for this explanation run)
         - ProvenanceNarrative nodes (one per slot finding)
         - ConflictExplanation nodes (one per conflict/limitation)
         - DecisionPoint nodes (one per Inference decision)
         - UncertaintyMap node
         - EXPLAINS edges from ExplainNode to synthesis Claim
         - HAS_NARRATIVE, HAS_CONFLICT_EXPL, HAS_DECISION edges
    5. Saves explain_output.json and explain_output.txt

No changes to explain.py needed.

Setup:
    pip install neo4j

    Set environment variables:
        NEO4J_URI       bolt://localhost:7687
        NEO4J_USER      neo4j
        NEO4J_PASSWORD  yourpassword
        NEO4J_DATABASE  neo4j

Run:
    python run_explain_neo4j.py
"""

import json
import os
import sys
from typing import Dict, List

sys.path.insert(0, ".")

# ── Imports ───────────────────────────────────────────────────────────────────
try:
    from explain import Explain, ExplainResult
    print("explain.py           loaded")
except ImportError as e:
    print(f"ERROR: could not import explain.py — {e}")
    sys.exit(1)

try:
    from neo4j_writer import Neo4jGraphWriter, neo4j_driver
    NEO4J_AVAILABLE = True
    print("neo4j_writer.py      loaded")
except ImportError:
    NEO4J_AVAILABLE = False
    print("neo4j_writer.py      not found — will use in-memory fallback")


# ── Config ────────────────────────────────────────────────────────────────────
INPUT_PATH  = "construct_output.json"
OUTPUT_PATH = "explain_output.json"
TEXT_PATH   = "explain_output.txt"
BATCH_SIZE  = 100


# ── Write ExplBundle to Neo4j ─────────────────────────────────────────────────
def write_explain_to_neo4j(
    result:   ExplainResult,
    driver,
    graph:    str,
    syn_uid:  str,
    rg_root:  str,
) -> None:
    """
    Write all Algorithm 5 components to Neo4j.

    Nodes written:
        ExplainNode         — root node for this explanation run
        ProvenanceNarrative — one per slot finding (Phase 2)
        ConflictExplanation — one per conflict/limitation (Phase 3)
        DecisionPoint       — one per Inference decision (Phase 4)
        UncertaintyMap      — aggregated uncertainty (Phase 5)

    Edges written:
        EXPLAINS            — ExplainNode -> Synthesised Claim
        HAS_NARRATIVE       — ExplainNode -> ProvenanceNarrative
        HAS_CONFLICT_EXPL   — ExplainNode -> ConflictExplanation
        HAS_DECISION        — ExplainNode -> DecisionPoint
        HAS_UNCERTAINTY     — ExplainNode -> UncertaintyMap
        TETHERED_TO         — each explanation -> its RG node
    """
    print("\n  Writing EXPLAIN results to Neo4j...")
    explain_uid = result.explain_node_uid

    with driver.session(database=graph) as s:

        # 1 — ExplainNode root
        s.run(
            "MERGE (n:ExplainNode {uid: $uid}) "
            "SET n.confidence_score = $conf, "
            "    n.confidence_label = $label, "
            "    n.answer_text = $answer, "
            "    n.tether_complete = $tc, "
            "    n.contested_slots = $cs",
            uid    = explain_uid,
            conf   = result.confidence_score,
            label  = result.confidence_label,
            answer = result.answer_text[:500],
            tc     = result.tether_complete,
            cs     = result.contested_slots,
        )
        print(f"    ExplainNode written  : {explain_uid[:30]}")

        # 2 — EXPLAINS edge to synthesised Claim
        if syn_uid:
            s.run(
                "MATCH (e:ExplainNode {uid:$euid}), (c {uid:$cuid}) "
                "MERGE (e)-[:EXPLAINS]->(c)",
                euid=explain_uid, cuid=syn_uid,
            )

        # 3 — Provenance Narrative nodes (Phase 2)
        for pn in result.provenance_narratives:
            pn_uid = f"pn::{explain_uid[:16]}::{pn.finding}"
            s.run(
                "MERGE (n:ProvenanceNarrative {uid:$uid}) "
                "SET n.finding=$finding, n.narrative=$narr, "
                "    n.rg_tether=$tether, n.statement=$stmt",
                uid=pn_uid, finding=pn.finding,
                narr=pn.narrative, tether=pn.rg_tether,
                stmt=pn.statement[:200],
            )
            s.run(
                "MATCH (e:ExplainNode {uid:$euid}), (n:ProvenanceNarrative {uid:$nuid}) "
                "MERGE (e)-[:HAS_NARRATIVE]->(n)",
                euid=explain_uid, nuid=pn_uid,
            )
            # TETHERED_TO edge to RG node
            if pn.rg_tether:
                s.run(
                    "MATCH (n:ProvenanceNarrative {uid:$nuid}), (r {uid:$ruid}) "
                    "MERGE (n)-[:TETHERED_TO]->(r)",
                    nuid=pn_uid, ruid=pn.rg_tether,
                )
        print(f"    ProvenanceNarratives : {len(result.provenance_narratives)}")

        # 4 — Conflict Explanation nodes (Phase 3)
        for i, ce in enumerate(result.conflict_explanations):
            ce_uid = f"ce::{explain_uid[:16]}::{ce.slot}::{i}"
            s.run(
                "MERGE (n:ConflictExplanation {uid:$uid}) "
                "SET n.slot=$slot, n.is_conflict=$ic, "
                "    n.explanation=$expl, n.rg_tether=$tether, "
                "    n.conflict_rule=$rule",
                uid=ce_uid, slot=ce.slot, ic=ce.is_conflict,
                expl=ce.explanation[:400], tether=ce.rg_tether,
                rule=ce.conflict_rule,
            )
            s.run(
                "MATCH (e:ExplainNode {uid:$euid}), (n:ConflictExplanation {uid:$nuid}) "
                "MERGE (e)-[:HAS_CONFLICT_EXPL]->(n)",
                euid=explain_uid, nuid=ce_uid,
            )
            if ce.rg_tether:
                s.run(
                    "MATCH (n:ConflictExplanation {uid:$nuid}), (r {uid:$ruid}) "
                    "MERGE (n)-[:TETHERED_TO]->(r)",
                    nuid=ce_uid, ruid=ce.rg_tether,
                )
        print(f"    ConflictExplanations : {len(result.conflict_explanations)}")

        # 5 — Decision Point nodes (Phase 4)
        for dp in result.decision_points:
            dp_uid = f"dp::{dp.inference_uid[:16]}"
            s.run(
                "MERGE (n:DecisionPoint {uid:$uid}) "
                "SET n.rule_name=$rule, n.slot=$slot, "
                "    n.original_conf=$orig, n.construct_score=$cscore, "
                "    n.sensitivity=$sens, n.sensitivity_margin=$margin, "
                "    n.rationale=$rat",
                uid=dp_uid, rule=dp.rule_name, slot=dp.slot,
                orig=dp.original_conf, cscore=dp.construct_score,
                sens=dp.sensitivity, margin=dp.sensitivity_margin,
                rat=dp.rationale[:200],
            )
            s.run(
                "MATCH (e:ExplainNode {uid:$euid}), (n:DecisionPoint {uid:$nuid}) "
                "MERGE (e)-[:HAS_DECISION]->(n)",
                euid=explain_uid, nuid=dp_uid,
            )
            # TETHERED_TO the Inference node
            s.run(
                "MATCH (n:DecisionPoint {uid:$nuid}), (r {uid:$ruid}) "
                "MERGE (n)-[:TETHERED_TO]->(r)",
                nuid=dp_uid, ruid=dp.inference_uid,
            )
        print(f"    DecisionPoints       : {len(result.decision_points)}")

        # 6 — UncertaintyMap node (Phase 5)
        um_uid = f"um::{explain_uid[:16]}"
        um     = result.uncertainty_map
        s.run(
            "MERGE (n:UncertaintyMap {uid:$uid}) "
            "SET n.overall_confidence=$conf, "
            "    n.contested_slots=$cs, "
            "    n.sensitive_slots=$ss, "
            "    n.stable_slots=$sst, "
            "    n.conflict_count=$cc, "
            "    n.tether_complete=$tc",
            uid=um_uid,
            conf=um.get("overall_confidence", 0),
            cs=um.get("contested_slots", []),
            ss=um.get("sensitive_slots", []),
            sst=um.get("stable_slots", []),
            cc=um.get("conflict_count", 0),
            tc=result.tether_complete,
        )
        s.run(
            "MATCH (e:ExplainNode {uid:$euid}), (n:UncertaintyMap {uid:$nuid}) "
            "MERGE (e)-[:HAS_UNCERTAINTY]->(n)",
            euid=explain_uid, nuid=um_uid,
        )
        print(f"    UncertaintyMap       : written")
        print(f"    TetherComplete       : {result.tether_complete}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print()
    print("=" * 62)
    print("  EXPLAIN OPERATOR — Neo4j Write-Back Mode")
    print("=" * 62)

    # 1. Load construct_output.json
    print(f"\nLoading {INPUT_PATH}...")
    with open(INPUT_PATH) as f:
        data = json.load(f)

    tr      = data.get("trace_result", {})
    cr      = data.get("construct_result", {})
    eg_root = tr.get("eg_root_uid", "")
    rg_root = tr.get("rg_root_uid", "")
    syn_uid = cr.get("synthesis_uid", "")

    print(f"  eg_root_uid : {eg_root}")
    print(f"  rg_root_uid : {rg_root}")
    print(f"  synthesis   : {syn_uid[:30] if syn_uid else 'not found'}")
    print(f"  limitations : {len(cr.get('limitations', []))}")
    print(f"  cite_map    : {list(cr.get('cite_map', {}).keys())}")

    # 2. Connect to Neo4j
    driver   = None
    is_neo4j = False

    if NEO4J_AVAILABLE:
        print("\nConnecting to Neo4j...")
        try:
            driver   = neo4j_driver()
            is_neo4j = True
            graph    = os.getenv("NEO4J_DATABASE", "neo4j")
            print(f"  Connected — database: {graph}")
        except Exception as e:
            print(f"  Could not connect: {e}")
            print("  Falling back to in-memory mode")

    # 3. Run EXPLAIN
    print("\nRunning EXPLAIN (all 5 Algorithm 5 phases)...")
    op     = Explain(data)
    result = op.execute()

    # 4. Write to Neo4j
    if is_neo4j:
        write_explain_to_neo4j(
            result=result, driver=driver,
            graph=graph, syn_uid=syn_uid, rg_root=rg_root,
        )

    # 5. Print results
    print()
    print("=" * 62)
    print("  RESULTS")
    print("=" * 62)
    print(f"  Backend                  : {'Neo4j' if is_neo4j else 'InMemory'}")
    print(f"  Confidence               : {result.confidence_score:.4f} ({result.confidence_label})")
    print(f"  Contested slots          : {result.contested_slots}")
    print(f"  Derivation nodes         : {len(result.derivation_subgraph.get('nodes',[]))}")
    print(f"  Provenance narratives    : {len(result.provenance_narratives)}")
    print(f"  Conflict explanations    : {len(result.conflict_explanations)}")
    print(f"  Decision points          : {len(result.decision_points)}")
    print(f"  TetherComplete           : {result.tether_complete}")
    print(f"  Citations                : {len(result.citations)}")

    print()
    print("── Answer ───────────────────────────────────────────────────")
    print(result.answer_text)

    print()
    print("── Decision Points ──────────────────────────────────────────")
    for dp in result.decision_points:
        print(f"  [{dp.sensitivity:<10}] {dp.rule_name:<45} margin={dp.sensitivity_margin:.3f}")

    print()
    print("── Uncertainty Map ──────────────────────────────────────────")
    for slot, entry in result.uncertainty_map.get("slots", {}).items():
        print(f"  {slot:<12} uncertainty={entry['uncertainty_level']:<8}  "
              f"sensitivity={entry['sensitivity']:<12}  "
              f"witnesses={entry['witnesses']}")

    print()
    print("── Conflict Explanations ────────────────────────────────────")
    for ce in result.conflict_explanations:
        tag = "CONFLICT" if ce.is_conflict else "LIMITATION"
        print(f"  [{tag}] {ce.explanation[:90]}")

    if is_neo4j:
        print()
        print("  Neo4j writes:")
        print(f"    ExplainNode              → RG in Neo4j")
        print(f"    ProvenanceNarrative nodes → RG in Neo4j")
        print(f"    ConflictExplanation nodes → RG in Neo4j")
        print(f"    DecisionPoint nodes       → RG in Neo4j")
        print(f"    UncertaintyMap node       → RG in Neo4j")
        print(f"    TETHERED_TO edges         → linking to RG nodes")

    # 6. Save JSON and text outputs
    print(f"\nSaving {OUTPUT_PATH}...")

    output = {
        "trace_result":    data["trace_result"],
        "conflict_result": data["conflict_result"],
        "construct_result":data["construct_result"],
        "eg":              data["eg"],
        "rg":              data["rg"],
        "explain_result": {
            "answer_text":          result.answer_text,
            "confidence_score":     result.confidence_score,
            "confidence_label":     result.confidence_label,
            "contested_slots":      result.contested_slots,
            "missing_slots":        result.missing_slots,
            "citations":            result.citations,
            "evidence_chain":       result.evidence_chain,
            "warnings":             result.warnings,
            "stats":                result.stats,
            "explain_node_uid":     result.explain_node_uid,
            "eg_root_uid":          eg_root,
            "rg_root_uid":          rg_root,
            "neo4j":                is_neo4j,
            "provenance_narratives": [
                {"finding": p.finding, "statement": p.statement,
                 "narrative": p.narrative, "rg_tether": p.rg_tether,
                 "derivation_path": p.derivation_path,
                 "decisions": p.decisions}
                for p in result.provenance_narratives
            ],
            "conflict_explanations": [
                {"limitation": c.limitation, "is_conflict": c.is_conflict,
                 "slot": c.slot, "explanation": c.explanation,
                 "context": c.context, "rg_tether": c.rg_tether}
                for c in result.conflict_explanations
            ],
            "decision_points": [
                {"rule_name": d.rule_name, "slot": d.slot,
                 "original_conf": d.original_conf,
                 "construct_score": d.construct_score,
                 "sensitivity": d.sensitivity,
                 "sensitivity_margin": d.sensitivity_margin,
                 "rationale": d.rationale}
                for d in result.decision_points
            ],
            "uncertainty_map":   result.uncertainty_map,
            "tether_map":        result.tether_map,
            "tether_complete":   result.tether_complete,
            "derivation_subgraph": {
                "node_count": len(result.derivation_subgraph.get("nodes", [])),
                "edge_count":  len(result.derivation_subgraph.get("edges", [])),
            },
        },
    }

    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2, default=str)

    # Plain text for investigator
    with open(TEXT_PATH, "w") as f:
        f.write("INVESTIGATOR ANSWER\n")
        f.write("=" * 60 + "\n\n")
        f.write(result.answer_text + "\n\n")
        f.write("CITATIONS\n" + "-" * 60 + "\n")
        for c in result.citations:
            f.write(f"  {c}\n")
        f.write("\nLIMITATIONS\n" + "-" * 60 + "\n")
        for ce in result.conflict_explanations:
            f.write(f"  {ce.explanation}\n")
        f.write("\nUNCERTAINTY MAP\n" + "-" * 60 + "\n")
        for slot, entry in result.uncertainty_map.get("slots", {}).items():
            f.write(f"  {slot:<12} {entry['uncertainty_level']:<8}  "
                    f"sensitivity={entry['sensitivity']}\n")
        f.write("\nEVIDENCE CHAIN\n" + "-" * 60 + "\n")
        for step in result.evidence_chain:
            f.write(f"  {step}\n")

    print(f"  Saved — ready for dashboard")

    if driver:
        driver.close()
        print("\n  Neo4j connection closed")


if __name__ == "__main__":
    main()
