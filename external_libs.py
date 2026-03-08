"""
external_libs.py
================
External vocabulary enrichment for the Neo4j Knowledge Graph.

Runs AFTER kg_loader.py has loaded Drug nodes and Vocab nodes.
Queries external APIs and writes results as properties onto existing
Vocab nodes already in the graph (linked via HAS_VOCAB from Drug).

Supported sources
-----------------
  rxnorm         — NLM RxNorm REST API  (free, no key needed)
  fda_orange_book — FDA Orange Book API  (free, no key needed)

Adding a new source
-------------------
  1. Write a  fetch_<source>(lookup_key) -> dict  function below.
  2. Add one entry to VOCAB_SOURCES.
  3. Done — the generic enrich_vocab_for_source() loop handles the rest.
     Any fields not in known_fields land in raw_json automatically.

Node/property design
--------------------
  Vocab {
    # Core — written by kg_loader.py at load time
    name        : "rxnorm" | "fda_orange_book" | "context" | ...
    type        : "standardized" | "regulatory" | "contextual"
    contextUrl  : original @context URL from JSONL (if present)

    # RxNorm — written by external_libs.py
    rxcui           : RxNorm concept unique identifier
    canonicalName   : NLM preferred drug name
    drugClass       : ATC drug class string
    synonyms        : JSON array string of alternate names

    # FDA Orange Book — written by external_libs.py
    applicationNumber : NDA/ANDA number
    approvalDate      : date of original approval
    manufacturer      : sponsor / applicant name

    # All sources — written by external_libs.py
    raw_json    : JSON string of any API fields not covered above
    sourceUrl   : exact API endpoint that was called
    fetchedAt   : ISO-8601 UTC timestamp of the API call
  }

Usage
-----
  python external_libs.py \\
      --uri      bolt://localhost:7687 \\
      --user     neo4j \\
      --password <pw> \\
      [--source  rxnorm]      # default: all sources
      [--limit   20]          # max Drug nodes per source (for testing)
      [--dry-run]             # print what would happen, no Neo4j writes
"""

import json
import time
import argparse
import hashlib
import logging
from datetime import datetime, timezone
from collections import defaultdict

import requests
from neo4j import GraphDatabase
from neo4j.exceptions import Neo4jError


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("external_libs")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _s(v, default=""):
    return str(v).strip() if v not in (None, "") else default

def _sha(namespace, *parts):
    raw = namespace + "|" + "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:12]

def _kg_id(key_type, *parts):
    return _sha(key_type, *parts), key_type

def _now():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Neo4j connection
# ---------------------------------------------------------------------------

class KGConn:
    def __init__(self, uri, user, password):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))

    def close(self):
        self.driver.close()

    def query(self, cql, **params):
        with self.driver.session() as s:
            return [dict(r) for r in s.run(cql, **params)]

    def run(self, cql, **params):
        with self.driver.session() as s:
            s.run(cql, **params)

    def run_batch(self, cql, rows):
        if not rows:
            return
        with self.driver.session() as s:
            for i in range(0, len(rows), 200):
                batch = rows[i:i + 200]
                try:
                    s.run(cql, rows=batch)
                except Neo4jError as e:
                    log.error(f"Batch write failed: {e.message[:120]}")


# ===========================================================================
# API FETCH FUNCTIONS
#
# Each function:
#   - Takes a drug name string
#   - Returns a dict of properties to write onto the Vocab node
#   - Returns {}             on no match
#   - Returns {"_error": …}  on API failure
#
# Named fields must match known_fields in VOCAB_SOURCES.
# Extra fields are captured in raw_json automatically.
# ===========================================================================

RXNORM_BASE = "https://rxnav.nlm.nih.gov/REST"
FDA_OB_BASE  = "https://api.fda.gov/drug/drugsfda.json"

KNOWN_RXNORM_FIELDS  = {"rxcui", "canonicalName", "drugClass", "synonyms"}
KNOWN_FDA_OB_FIELDS  = {"applicationNumber", "approvalDate", "manufacturer"}


def fetch_rxnorm(drug_name: str) -> dict:
    """
    Query NLM RxNorm for a drug by name.
    Returns: rxcui, canonicalName, drugClass, synonyms, sourceUrl.
    Sets rxcui = "NOT_FOUND" if no RxNorm match exists.
    """
    if not drug_name:
        return {}
    try:
        # Step 1 — resolve name to RxCUI
        r = requests.get(
            f"{RXNORM_BASE}/rxcui.json",
            params={"name": drug_name, "search": 2},
            timeout=10,
        )
        r.raise_for_status()
        rxcui_list = r.json().get("idGroup", {}).get("rxnormId") or []

        if not rxcui_list:
            return {
                "rxcui":         "NOT_FOUND",
                "canonicalName": drug_name,
                "drugClass":     "",
                "synonyms":      "[]",
                "sourceUrl":     f"{RXNORM_BASE}/rxcui.json?name={drug_name}",
            }

        rxcui = rxcui_list[0]
        time.sleep(0.1)

        # Step 2 — canonical name and properties
        r2 = requests.get(
            f"{RXNORM_BASE}/rxcui/{rxcui}/properties.json",
            timeout=10,
        )
        r2.raise_for_status()
        props     = r2.json().get("properties", {})
        canonical = _s(props.get("name", drug_name))
        time.sleep(0.1)

        # Step 3 — drug class via ATC (best-effort)
        drug_class = ""
        try:
            r3 = requests.get(
                f"{RXNORM_BASE}/rxcui/{rxcui}/classes.json",
                params={"relaSource": "ATC"},
                timeout=10,
            )
            r3.raise_for_status()
            classes = (
                r3.json()
                  .get("rxclassDrugInfoList", {})
                  .get("rxclassDrugInfo", [])
            )
            drug_class = ", ".join(
                c.get("rxclassMinConceptItem", {}).get("className", "")
                for c in classes[:3]
                if c.get("rxclassMinConceptItem", {}).get("className")
            )
            time.sleep(0.1)
        except Exception:
            pass

        # Step 4 — synonyms (best-effort, capped at 10)
        synonyms = []
        try:
            r4 = requests.get(
                f"{RXNORM_BASE}/rxcui/{rxcui}/allrelated.json",
                timeout=10,
            )
            r4.raise_for_status()
            for grp in r4.json().get("allRelatedGroup", {}).get("conceptGroup", []):
                for concept in grp.get("conceptProperties", []):
                    name = _s(concept.get("name"))
                    if name and name.lower() != canonical.lower():
                        synonyms.append(name)
            synonyms = list(dict.fromkeys(synonyms))[:10]
            time.sleep(0.1)
        except Exception:
            pass

        return {
            "rxcui":         rxcui,
            "canonicalName": canonical,
            "drugClass":     drug_class,
            "synonyms":      json.dumps(synonyms),
            "sourceUrl":     f"{RXNORM_BASE}/rxcui/{rxcui}/properties.json",
        }

    except requests.RequestException as e:
        log.warning(f"RxNorm API error for '{drug_name}': {e}")
        return {"_error": str(e)}


def fetch_fda_orange_book(drug_name: str) -> dict:
    """
    Query FDA Orange Book for a drug by generic name.
    Returns: applicationNumber, approvalDate, manufacturer, sourceUrl.
    Sets applicationNumber = "NOT_FOUND" if no match.
    Any extra API fields go into raw_json automatically via _extra.
    """
    if not drug_name:
        return {}
    try:
        r = requests.get(
            FDA_OB_BASE,
            params={
                "search": f'products.active_ingredients.name:"{drug_name}"',
                "limit":  1,
            },
            timeout=10,
        )
        r.raise_for_status()
        results = r.json().get("results", [])

        if not results:
            return {
                "applicationNumber": "NOT_FOUND",
                "approvalDate":      "",
                "manufacturer":      "",
                "sourceUrl":         FDA_OB_BASE,
            }

        hit = results[0]

        # Most recent original approval date
        approval_date = ""
        for sub in hit.get("submissions", []):
            if (sub.get("submission_type") == "ORIG"
                    and sub.get("submission_status") == "AP"):
                approval_date = _s(sub.get("submission_status_date", ""))
                break

        # Named fields we handle explicitly; everything else -> raw_json
        known_keys = {"application_number", "sponsor_name", "submissions", "products"}
        extra = {k: v for k, v in hit.items() if k not in known_keys}

        return {
            "applicationNumber": _s(hit.get("application_number", "NOT_FOUND")),
            "approvalDate":      approval_date,
            "manufacturer":      _s(hit.get("sponsor_name", "")),
            "sourceUrl":         FDA_OB_BASE,
            "_extra":            extra,  # automatically captured in raw_json
        }

    except requests.RequestException as e:
        log.warning(f"FDA Orange Book API error for '{drug_name}': {e}")
        return {"_error": str(e)}


# ===========================================================================
# VOCAB_SOURCES registry
#
# To add a new vocabulary source:
#   1. Write a fetch_<n>(drug_name) -> dict function above.
#   2. Append one dict here — nothing else in this file needs to change.
# ===========================================================================

VOCAB_SOURCES = [
    {
        "name":         "rxnorm",
        "type":         "standardized",
        "fetch_fn":     fetch_rxnorm,
        "key_field":    "genericName",   # Drug node property used as lookup key
        "known_fields": KNOWN_RXNORM_FIELDS,
        "description":  "NLM RxNorm — standardised drug identifiers and classifications",
    },
    {
        "name":         "fda_orange_book",
        "type":         "regulatory",
        "fetch_fn":     fetch_fda_orange_book,
        "key_field":    "genericName",
        "known_fields": KNOWN_FDA_OB_FIELDS,
        "description":  "FDA Orange Book — approved drug applications",
    },
]


# ===========================================================================
# Cypher — write enrichment results onto Vocab nodes
# ===========================================================================

UPSERT_VOCAB_ENRICHMENT = """
UNWIND $rows AS r
MERGE (v:Vocab {kg_id: r.kg_id})
SET v.kg_key_type        = r.kg_key_type,
    v.name               = r.name,
    v.type               = r.type,
    v.rxcui              = r.rxcui,
    v.canonicalName      = r.canonicalName,
    v.drugClass          = r.drugClass,
    v.synonyms           = r.synonyms,
    v.applicationNumber  = r.applicationNumber,
    v.approvalDate       = r.approvalDate,
    v.manufacturer       = r.manufacturer,
    v.raw_json           = r.raw_json,
    v.sourceUrl          = r.sourceUrl,
    v.fetchedAt          = r.fetchedAt
"""

LINK_DRUG_VOCAB = """
UNWIND $rows AS r
MATCH (d:Drug  {kg_id: r.drugKgId})
MATCH (v:Vocab {kg_id: r.vocabKgId})
MERGE (d)-[:HAS_VOCAB]->(v)
"""


# ===========================================================================
# Generic enrichment engine — one function handles all VOCAB_SOURCES entries
# ===========================================================================

def enrich_vocab_for_source(conn, source_cfg, limit=None, dry_run=False):
    """
    For every Drug node, fetch vocab data from one external source and
    MERGE a Vocab node linked by HAS_VOCAB.
    Re-runs are fully idempotent — kg_id is deterministic per (drug, source).
    """
    name         = source_cfg["name"]
    vtype        = source_cfg["type"]
    fetch_fn     = source_cfg["fetch_fn"]
    key_field    = source_cfg["key_field"]
    known_fields = source_cfg["known_fields"]
    description  = source_cfg["description"]

    log.info(f"  Source: {name}  ({description})")

    drugs = conn.query(
        f"MATCH (d:Drug) "
        f"WHERE d.{key_field} IS NOT NULL AND d.{key_field} <> '' "
        f"RETURN d.kg_id AS kg_id, d.{key_field} AS lookup_key"
    )
    if limit:
        drugs = drugs[:limit]

    log.info(f"    {len(drugs)} Drug node(s) to process")

    vocab_rows = []
    link_rows  = []
    stats      = defaultdict(int)

    for drug in drugs:
        drug_kg_id = drug["kg_id"]
        lookup_key = drug["lookup_key"]

        if dry_run:
            log.info(f"    [DRY RUN] would fetch {name} for '{lookup_key}'")
            stats["dry_run"] += 1
            continue

        result = fetch_fn(lookup_key)

        if not result:
            stats["no_match"] += 1
            continue

        if "_error" in result:
            stats["api_error"] += 1
            log.warning(f"    Skipping '{lookup_key}': {result['_error'][:80]}")
            continue

        # Separate named fields from anything extra
        extra      = result.pop("_extra", {})
        source_url = result.pop("sourceUrl", "")

        known_props = {k: _s(result.get(k)) for k in known_fields}

        # Everything not in known_fields -> raw_json (nothing is ever lost)
        leftover = {k: v for k, v in result.items() if k not in known_fields}
        leftover.update(extra)
        raw_json = json.dumps(leftover, default=str) if leftover else ""

        vocab_kg_id, vocab_key_type = _kg_id("vocab_ext", drug_kg_id, name)

        vocab_rows.append({
            "kg_id":             vocab_kg_id,
            "kg_key_type":       vocab_key_type,
            "name":              name,
            "type":              vtype,
            # RxNorm fields (empty string if this is a different source)
            "rxcui":             known_props.get("rxcui", ""),
            "canonicalName":     known_props.get("canonicalName", ""),
            "drugClass":         known_props.get("drugClass", ""),
            "synonyms":          known_props.get("synonyms", ""),
            # FDA Orange Book fields
            "applicationNumber": known_props.get("applicationNumber", ""),
            "approvalDate":      known_props.get("approvalDate", ""),
            "manufacturer":      known_props.get("manufacturer", ""),
            # Always present
            "raw_json":          raw_json,
            "sourceUrl":         source_url,
            "fetchedAt":         _now(),
        })
        link_rows.append({"drugKgId": drug_kg_id, "vocabKgId": vocab_kg_id})

        not_found = (
            known_props.get("rxcui") == "NOT_FOUND"
            or known_props.get("applicationNumber") == "NOT_FOUND"
        )
        stats["not_found" if not_found else "enriched"] += 1

        # Polite rate limiting for public APIs
        time.sleep(0.15)

    if not dry_run:
        conn.run_batch(UPSERT_VOCAB_ENRICHMENT, vocab_rows)
        conn.run_batch(LINK_DRUG_VOCAB, link_rows)

    log.info(
        f"    {name}: enriched={stats['enriched']}  "
        f"not_found={stats['not_found']}  "
        f"api_error={stats['api_error']}  "
        f"dry_run={stats['dry_run']}"
    )
    return dict(stats)


# ===========================================================================
# CLI
# ===========================================================================

def main():
    available = ", ".join(s["name"] for s in VOCAB_SOURCES)
    parser = argparse.ArgumentParser(
        description="Enrich Drug Vocab nodes from external vocabulary APIs."
    )
    parser.add_argument("--uri",      default="bolt://localhost:7687")
    parser.add_argument("--user",     default="neo4j")
    parser.add_argument("--password", required=True)
    parser.add_argument(
        "--source", default="all",
        help=f"Comma-separated source names or 'all'. Available: {available}",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Max Drug nodes to process per source (useful for testing)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be fetched — no Neo4j writes",
    )
    args = parser.parse_args()

    requested = (
        [s.strip() for s in args.source.split(",")]
        if args.source != "all"
        else [s["name"] for s in VOCAB_SOURCES]
    )
    sources_to_run = [s for s in VOCAB_SOURCES if s["name"] in requested]
    unknown = set(requested) - {s["name"] for s in sources_to_run}
    if unknown:
        log.warning(f"Unknown source(s) ignored: {unknown}")
    if not sources_to_run:
        log.error("No valid sources selected. Exiting.")
        return

    conn = KGConn(args.uri, args.user, args.password)
    summary = {}

    log.info(f"External vocabulary enrichment: {[s['name'] for s in sources_to_run]}")
    if args.dry_run:
        log.info("DRY RUN — no Neo4j writes will be made")

    try:
        for source_cfg in sources_to_run:
            stats = enrich_vocab_for_source(
                conn, source_cfg,
                limit=args.limit,
                dry_run=args.dry_run,
            )
            summary[source_cfg["name"]] = stats
    finally:
        conn.close()

    log.info("=== Enrichment Summary ===")
    for src_name, stats in summary.items():
        log.info(f"  {src_name}: {stats}")


if __name__ == "__main__":
    main()
