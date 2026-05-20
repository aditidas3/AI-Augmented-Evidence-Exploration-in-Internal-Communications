"""
resolve_operator.py
===================
LLM-powered entity resolution for the Knowledge Graph.

Reads candidate duplicate pairs produced by post_kg_rules.py, calls an LLM
to score similarity using witness context, and auto-merges nodes above the
confidence threshold directly in Neo4j.

"""

import os
import re
import json
import argparse
import logging
from datetime import datetime

from neo4j import GraphDatabase
from neo4j.exceptions import Neo4jError
from openai import OpenAI
from dotenv import load_dotenv
load_dotenv()

# ---------------------------------------------------------------------------
# LLM-MODEL
# ---------------------------------------------------------------------------
client = OpenAI(
        api_key=os.environ.get("QWEN_API_KEY"),
        base_url=os.environ.get("BASE_URL")
    )
MODEL = os.environ.get("MODEL_NAME")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("resolve_operator")

# ---------------------------------------------------------------------------
# Name normalization
# All entity-type normalization lives here, not in post_kg_rules.py.
# post_kg_rules only does raw lowercase grouping to find candidates.
# resolve_operator applies proper normalization before LLM scoring.
# ---------------------------------------------------------------------------

# Strips parenthetical abbreviations: "California (CA)" -> "California"
_LOC_ABBREV = re.compile(r'\s*\([^)]{1,10}\)\s*$')


def _normalize_for_label(label, value):
    """
    Apply label-specific normalization to an entity string before LLM scoring.
    - Person   : strip unicode combining characters (e.g. Piñon -> Pinon)
    - Location : strip parenthetical abbreviations (e.g. "California (CA)" -> "California")
    - All others: pass through as-is — the LLM handles surface variation itself
    """
    import unicodedata
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    s = str(value).strip() if value else ""
    if label == "Location":
        return _LOC_ABBREV.sub("", s).strip()
    if label == "Person":
        s = unicodedata.normalize("NFKD", s)
        s = "".join(c for c in s if not unicodedata.combining(c))
        return s
    return s



# ---------------------------------------------------------------------------
# Neo4j connection
# ---------------------------------------------------------------------------

class KGConn:
    def __init__(self, uri, user, password):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))

    def close(self):
        self.driver.close()

    def query(self, cql, **params):
        with self.driver.session() as session:
            result = session.run(cql, **params)
            return [dict(r) for r in result]

    def run(self, cql, **params):
        with self.driver.session() as session:
            session.run(cql, **params)

    def scalar(self, cql, **params):
        rows = self.query(cql, **params)
        if rows:
            return list(rows[0].values())[0]
        return None


# ---------------------------------------------------------------------------
# Context — sourced from n.witnessContext
#
# post_kg_rules.py embeds witness_context1 / witness_context2 directly in
# each candidate pair. resolve_operator reads those fields and passes them
# straight to the LLM — no graph traversal needed.
#
# All resolvable node types (Organization, Person, Drug, Location, GPE,
# Topic, Product)
# ---------------------------------------------------------------------------

def _format_context(label, name, witness_context):
    """
    Build the context string that is sent to the LLM for one entity.
    witness_context: the raw n.witnessContext property value (may be empty).
    """
    parts = [f"Name: {name}"]
    if witness_context and witness_context.strip():
        # Truncate very long witness text — 300 chars is enough for the LLM
        wc = witness_context.strip()[:300]
        parts.append(f"Witness context: {wc}")
    else:
        parts.append("(no witness context available — name comparison only)")
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# LLM resolution — calls Claude API
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an expert entity resolution system for a knowledge graph.

Your task: determine whether two entity strings refer to the SAME real-world entity.

You will receive:
- str1, str2: the two entity name strings
- context1, context2: witness context — the verbatim sentence or phrase from the source
  document in which each entity was mentioned. Use this to understand HOW the entity is
  used (e.g. a drug name in a dosage sentence vs a brand name in a marketing context).
  If witness context is absent, score on name similarity only.
- entity_type_hint: the Neo4j label (Organization, Person, Location, Drug, GPE, Topic, Product)

You must respond with ONLY a valid JSON object — no explanation, no markdown, no preamble:
{
  "score": <float 0.0-1.0>,
  "canonical_form": "<preferred name string>",
  "entity_type": "<Organization|Person|Location|Drug|GPE|Topic|Product|LegalFramework>",
  "reasoning": "<one sentence explanation>"
}

Scoring guide:
  1.0  = definitely the same entity (e.g. "JUUL Labs" vs "Juul Labs, Inc.")
  0.85 = very likely the same (minor variation, consistent witness context)
  0.7  = probably the same (some ambiguity)
  0.5  = uncertain
  0.3  = probably different entities
  0.0  = definitely different

For canonical_form: choose the most complete, formal, and unambiguous version.
For entity_type: use the hint unless witness context clearly indicates otherwise."""


def resolve_pair_llm(client, str1, str2, context1, context2, entity_type_hint):
    """
    Call the LLM to score a candidate pair.
    context1 / context2 are pre-formatted strings from _format_context(),
    built from n.witnessContext — no Neo4j query needed here.
    Returns dict with score, canonical_form, entity_type, reasoning.
    """
    user_message = f"""Compare these two entities:

str1: "{str1}"
context1: {context1}

str2: "{str2}"
context2: {context2}

entity_type_hint: {entity_type_hint}

Respond with JSON only."""

    last_err = None
    for attempt in range(1, 4):  # up to 3 attempts
        try:
            response = client.chat.completions.create(
                model=MODEL,
                max_tokens=300,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ]
            )
            content = response.choices[0].message.content
            if content is None:
                raise ValueError("LLM returned empty (None) content")
            raw = content.strip()
            # Strip any accidental markdown fences
            raw = raw.replace("```json", "").replace("```", "").strip()
            if not raw:
                raise ValueError("LLM returned blank content after stripping")
            return json.loads(raw)
        except json.JSONDecodeError as e:
            log.error(f"LLM returned invalid JSON for ({str1!r}, {str2!r}): {e}")
            return {
                "score": 0.0,
                "canonical_form": str1,
                "entity_type": entity_type_hint,
                "reasoning": f"JSON parse error: {e}",
            }
        except Exception as e:
            last_err = e
            log.warning(f"  Attempt {attempt}/3 failed for ({str1!r}, {str2!r}): {e}")

    log.error(f"LLM call failed after 3 attempts for ({str1!r}, {str2!r}): {last_err}")
    return {
        "score": 0.0,
        "canonical_form": str1,
        "entity_type": entity_type_hint,
        "reasoning": f"API error after 3 attempts: {last_err}",
    }


# ---------------------------------------------------------------------------
# Neo4j merge — merges dup node into keeper
# ---------------------------------------------------------------------------

def _get_degree(conn, label, kg_id):
    return conn.scalar(
        f"MATCH (n:{label} {{kg_id:$kg_id}})--() RETURN count(*) AS c",
        kg_id=kg_id,
    ) or 0


def _merge_nodes_apoc(conn, label, keeper_kg_id, dup_kg_id, canonical_form):
    """
    Merge dup into keeper using APOC. Sets canonical name on keeper.
    Returns True if APOC succeeded, False to fall back to Cypher.
    """
    try:
        conn.run(f"""
            MATCH (keep:{label} {{kg_id:$keep}}), (dup:{label} {{kg_id:$dup}})
            CALL apoc.refactor.mergeNodes([keep, dup],
                {{properties:'combine', mergeRels:true}})
            YIELD node
            SET node.name = $canonical
            RETURN node
        """, keep=keeper_kg_id, dup=dup_kg_id, canonical=canonical_form)
        return True
    except Exception:
        return False


def _merge_nodes_cypher(conn, label, keeper_kg_id, dup_kg_id, canonical_form):
    """Cypher-only fallback merge."""
    # Re-point outgoing edges from dup to keeper
    conn.run(f"""
        MATCH (dup:{label} {{kg_id:$dup}})-[r]->(target)
        MATCH (keep:{label} {{kg_id:$keep}})
        WHERE id(dup) <> id(keep) AND id(target) <> id(keep)
        WITH keep, target, type(r) AS rtype
        CALL apoc.merge.relationship(keep, rtype, {{}}, {{}}, target)
        YIELD rel RETURN rel
    """, dup=dup_kg_id, keep=keeper_kg_id)
    # Re-point incoming edges to keeper
    conn.run(f"""
        MATCH (source)-[r]->(dup:{label} {{kg_id:$dup}})
        MATCH (keep:{label} {{kg_id:$keep}})
        WHERE id(dup) <> id(keep) AND id(source) <> id(keep)
        WITH source, keep, type(r) AS rtype
        CALL apoc.merge.relationship(source, rtype, {{}}, {{}}, keep)
        YIELD rel RETURN rel
    """, dup=dup_kg_id, keep=keeper_kg_id)
    # Set canonical name and delete dup
    conn.run(f"""
        MATCH (keep:{label} {{kg_id:$keep}})
        SET keep.name = $canonical
    """, keep=keeper_kg_id, canonical=canonical_form)
    conn.run(f"MATCH (n:{label} {{kg_id:$dup}}) DETACH DELETE n", dup=dup_kg_id)


def merge_pair(conn, label, kg_id1, kg_id2, canonical_form, dry_run=False):
    """
    Merge the lower-degree node into the higher-degree node.
    Sets canonical_form as the name on the surviving node.
    """
    if dry_run:
        log.info(f"  [DRY-RUN] Would merge {label} {kg_id1!r} + {kg_id2!r} → {canonical_form!r}")
        return

    deg1 = _get_degree(conn, label, kg_id1)
    deg2 = _get_degree(conn, label, kg_id2)

    # keeper = higher degree node (more connections = more important)
    if deg1 >= deg2:
        keeper_kg_id, dup_kg_id = kg_id1, kg_id2
    else:
        keeper_kg_id, dup_kg_id = kg_id2, kg_id1

    log.info(f"  Merging {label}: keeper={keeper_kg_id} (deg={max(deg1,deg2)}) "
             f"← dup={dup_kg_id} (deg={min(deg1,deg2)}) → canonical={canonical_form!r}")

    ok = _merge_nodes_apoc(conn, label, keeper_kg_id, dup_kg_id, canonical_form)
    if not ok:
        log.warning(f"  APOC failed, falling back to Cypher merge")
        try:
            _merge_nodes_cypher(conn, label, keeper_kg_id, dup_kg_id, canonical_form)
        except Neo4jError as e:
            log.error(f"  Cypher merge also failed: {e.message[:100]}")
            raise


# ---------------------------------------------------------------------------
# Main resolution loop
# ---------------------------------------------------------------------------

def run_resolution(candidates, conn, client, threshold=0.85, dry_run=False):
    """
    Process all candidate pairs:
    1. Read witness_context1/2 from the candidate dict (written by post_kg_rules.py)
    2. Call LLM to score
    3. Merge if score >= threshold
    Returns resolution report list.
    """
    report = []
    merged_count = 0
    skipped_count = 0
    error_count = 0

    total = len(candidates)
    for i, pair in enumerate(candidates, 1):
        label  = pair["label"]
        kg_id1 = pair["kg_id1"]
        if isinstance(kg_id1, list): kg_id1 = kg_id1[0]
        kg_id2 = pair["kg_id2"]
        if isinstance(kg_id2, list): kg_id2 = kg_id2[0]
        str1   = pair["str1"]
        str2   = pair["str2"]

        log.info(f"[{i}/{total}] {label}: {str1!r} vs {str2!r}")

        # Step 1: build context strings from witnessContext (no graph traversal)
        context1 = _format_context(label, str1, pair.get("witness_context1", ""))
        context2 = _format_context(label, str2, pair.get("witness_context2", ""))

        # Step 2: normalize strings then call LLM
        norm_str1 = _normalize_for_label(label, str1)
        norm_str2 = _normalize_for_label(label, str2)
        result = resolve_pair_llm(client, norm_str1, norm_str2, context1, context2, label)
        score          = float(result.get("score", 0.0))
        canonical_form = result.get("canonical_form", str1)
        entity_type    = result.get("entity_type", label)
        reasoning      = result.get("reasoning", "")

        log.info(f"  Score={score:.2f} | canonical={canonical_form!r} | {reasoning}")

        entry = {
            "label":             label,
            "kg_id1":            kg_id1,
            "str1":              str1,
            "witness_context1":  pair.get("witness_context1", ""),
            "kg_id2":            kg_id2,
            "str2":              str2,
            "witness_context2":  pair.get("witness_context2", ""),
            "score":             score,
            "canonical_form":    canonical_form,
            "entity_type":       entity_type,
            "reasoning":         reasoning,
            "action":            None,
        }

        # Step 3: merge or skip
        if score >= threshold:
            try:
                merge_pair(conn, label, kg_id1, kg_id2, canonical_form, dry_run=dry_run)
                entry["action"] = "merged" if not dry_run else "dry_run_merge"
                merged_count += 1
            except Exception as e:
                log.error(f"  Merge failed: {e}")
                entry["action"] = "merge_failed"
                entry["error"]  = str(e)
                error_count += 1
        else:
            log.info(f"  Score {score:.2f} < threshold {threshold} — skipping merge")
            entry["action"] = "skipped"
            skipped_count += 1

        report.append(entry)

    log.info(f"\n=== Resolution complete ===")
    log.info(f"  Total pairs : {total}")
    log.info(f"  Merged      : {merged_count}")
    log.info(f"  Skipped     : {skipped_count}")
    log.info(f"  Errors      : {error_count}")

    return report, merged_count, skipped_count, error_count


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="LLM-powered entity resolution — merges duplicate KG nodes."
    )
    parser.add_argument("--candidates",  default="candidates.json",
                        help="Path to candidates.json written by post_kg_rules.py")
    parser.add_argument("--uri",         default="bolt://localhost:7687")
    parser.add_argument("--user",        default="neo4j")
    parser.add_argument("--password",    required=True)
    parser.add_argument("--threshold",   type=float, default=0.85,
                        help="Minimum LLM score to auto-merge (default: 0.85)")
    parser.add_argument("--dry-run",     action="store_true",
                        help="Score pairs but do not write any merges to Neo4j")
    parser.add_argument("--out",         default="resolution_report.json",
                        help="Path to write resolution report (default: resolution_report.json)")
    args = parser.parse_args()

    # Load candidates
    with open(args.candidates, encoding="utf-8") as f:
        candidates = json.load(f)
    log.info(f"Loaded {len(candidates)} candidate pairs from {args.candidates}")

    if not candidates:
        log.info("No candidates to resolve — exiting.")
        return

    # Connect to Neo4j
    conn = KGConn(args.uri, args.user, args.password)

    log.info(f"Starting resolution — threshold={args.threshold} dry_run={args.dry_run}")

    # Run resolution
    report, merged, skipped, errors = run_resolution(
        candidates=candidates,
        conn=conn,
        client=client,
        threshold=args.threshold,
        dry_run=args.dry_run,
    )

    conn.close()

    # Write report
    output = {
        "run_at":     datetime.utcnow().isoformat() + "Z",
        "candidates": len(candidates),
        "threshold":  args.threshold,
        "dry_run":    args.dry_run,
        "summary": {
            "merged":  merged,
            "skipped": skipped,
            "errors":  errors,
        },
        "results": report,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    log.info(f"Report written to {args.out}")
    print(f"\n{'━'*50}")
    print(f"  Candidates : {len(candidates)}")
    print(f"  Merged     : {merged}")
    print(f"  Skipped    : {skipped}")
    print(f"  Errors     : {errors}")
    print(f"  Report     : {args.out}")
    print(f"{'━'*50}")


if __name__ == "__main__":
    main()
