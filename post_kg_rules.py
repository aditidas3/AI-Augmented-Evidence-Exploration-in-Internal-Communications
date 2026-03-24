"""
post_kg_rules.py
================
Runs AFTER kg_loader.py. Connects to Neo4j and performs:
  - Structural integrity checks (orphan nodes, missing relationships)
  - Cardinality checks (min/max degree assertions)
  - Candidate detection for entity resolution (writes candidates.json
    for resolve_operator.py — context sourced from n.witnessContext)
  - Enrichment rules (derived relationships, secondary labels)
  - Summary statistics and quality report
"""

import re
import json
import argparse
from datetime import datetime
from collections import defaultdict

from neo4j import GraphDatabase


# ---------------------------------------------------------------------------
# Driver
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
# Reporting
# ---------------------------------------------------------------------------

REPORT = {
    "run_at": "",
    "checks": [],
    "enrichments": {
        "entity_resolution": [],
        "cross_doc_links":   [],
        "node_tagging":      [],
        "co_occurrence":     [],
        "sequence":          [],
    },
    "entity_resolution_summary": {},
    "stats": {},
    "issues": [],
}

_CURRENT_SECTION = "cross_doc_links"

def _set_section(section):
    global _CURRENT_SECTION
    _CURRENT_SECTION = section

def _check(name, status, detail="", count=0):
    entry = {"check": name, "status": status, "detail": detail, "count": count}
    REPORT["checks"].append(entry)
    icon = "✅" if status == "PASS" else ("⚠️ " if status == "WARN" else "❌")
    print(f"  {icon}  [{status}] {name}" + (f" — {detail}" if detail else "") + (f" (n={count})" if count else ""))

def _enrichment(rule, affected, category=None, detail=""):
    cat = category or _CURRENT_SECTION
    entry = {"rule": rule, "affected": affected, "detail": detail}
    REPORT["enrichments"][cat].append(entry)
    print(f"  ✨  {rule:<50} affected={affected:>5}" + (f"  — {detail}" if detail else ""))

def _issue(msg):
    REPORT["issues"].append(msg)


# ============================================================
# SECTION A — Node count statistics
# ============================================================

def check_node_counts(conn):
    print("\n[A] Node count statistics")
    labels = [
        "Document", "Person", "Organization", "Drug", "GPE",
        "Claim", "Topic", "Location", "Abbreviation",
        "EmailMessage", "Slide", "Sheet", "Citation",
        "LegalFramework", "TextContent", "TabularColumn", "Product",
    ]
    for label in labels:
        n = conn.scalar(f"MATCH (n:{label}) RETURN count(n) AS c")
        REPORT["stats"][f"count_{label}"] = n
        print(f"       {label:<22} {n:>6}")


# ============================================================
# SECTION B — Structural integrity checks
# ============================================================

def check_orphan_documents(conn):
    n = conn.scalar("MATCH (d:Document) WHERE NOT (d)-->() RETURN count(d) AS c")
    if n > 0:
        _check("Orphan Documents", "WARN", "Documents with no outgoing edges", n)
        _issue(f"{n} Document nodes have no relationships")
    else:
        _check("Orphan Documents", "PASS")

def check_orphan_persons(conn):
    n = conn.scalar("""
        MATCH (p:Person)
        WHERE NOT ()-[:HAS_CONTACT]->(p)
          AND NOT ()-[:SENT_BY]->(p)
          AND NOT ()-[:SENT_TO]->(p)
        RETURN count(p) AS c
    """)
    if n > 0:
        _check("Orphan Persons", "WARN", "Persons with no document/message links", n)
    else:
        _check("Orphan Persons", "PASS")

def check_orphan_orgs(conn):
    n = conn.scalar("""
        MATCH (o:Organization)
        WHERE NOT ()-[:HAS_CONTACT]->(o)
          AND NOT ()-[:WORKS_FOR]->(o)
          AND NOT ()-[:MENTIONS_ORG]->(o)
        RETURN count(o) AS c
    """)
    if n > 0:
        _check("Orphan Organizations", "WARN", "Orgs with no links", n)
    else:
        _check("Orphan Organizations", "PASS")

def check_orphan_gpe(conn):
    """GPE nodes must be linked from at least one Document via MENTIONS_GPE."""
    n = conn.scalar("""
        MATCH (g:GPE)
        WHERE NOT ()-[:MENTIONS_GPE]->(g)
        RETURN count(g) AS c
    """)
    if n > 0:
        _check("Orphan GPE nodes", "WARN", "GPE nodes with no MENTIONS_GPE link", n)
        _issue(f"{n} GPE nodes not linked to any Document")
    else:
        _check("Orphan GPE nodes", "PASS")

def check_claims_have_document(conn):
    n = conn.scalar("MATCH (c:Claim) WHERE NOT ()-[:HAS_CLAIM]->(c) RETURN count(c) AS c")
    if n > 0:
        _check("Unlinked Claims", "FAIL", "Claims with no parent Document", n)
        _issue(f"{n} Claim nodes not linked to any Document")
    else:
        _check("Unlinked Claims", "PASS")

def check_email_messages_have_sender(conn):
    n = conn.scalar("MATCH (m:EmailMessage) WHERE NOT (m)-[:SENT_BY]->() RETURN count(m) AS c")
    status = "WARN" if n > 0 else "PASS"
    _check("Messages missing sender", status, "EmailMessages with no SENT_BY", n)

def check_email_messages_have_recipient(conn):
    n = conn.scalar("MATCH (m:EmailMessage) WHERE NOT (m)-[:SENT_TO]->() RETURN count(m) AS c")
    status = "WARN" if n > 0 else "PASS"
    _check("Messages missing recipient", status, "EmailMessages with no SENT_TO", n)

def check_slides_have_document(conn):
    n = conn.scalar("MATCH (s:Slide) WHERE NOT ()-[:HAS_SLIDE]->(s) RETURN count(s) AS c")
    _check("Unlinked Slides", "WARN" if n > 0 else "PASS", count=n)

def check_sheets_have_document(conn):
    n = conn.scalar("MATCH (s:Sheet) WHERE NOT ()-[:HAS_SHEET]->(s) RETURN count(s) AS c")
    _check("Unlinked Sheets", "WARN" if n > 0 else "PASS", count=n)


# ============================================================
# SECTION C — Cardinality / distribution checks
# ============================================================

def check_document_degree(conn):
    rows = conn.query("""
        MATCH (d:Document)
        WITH d, size([(d)-->() | 1]) AS deg
        WHERE deg = 0
        RETURN d.uid AS uid, d.fileType AS ft
        LIMIT 10
    """)
    if rows:
        _check("Document min-degree", "WARN",
               f"Documents with degree=0: {[r['uid'] for r in rows]}", len(rows))
    else:
        _check("Document min-degree", "PASS")

def check_duplicate_person_emails(conn):
    rows = conn.query("""
        MATCH (p:Person)
        WHERE p.email <> ''
        WITH p.email AS email, collect(p.uid) AS uids, count(*) AS cnt
        WHERE cnt > 1
        RETURN email, uids, cnt
        ORDER BY cnt DESC LIMIT 20
    """)
    if rows:
        _check("Duplicate Person emails", "WARN",
               f"{len(rows)} email(s) shared by multiple Person nodes", len(rows))
        for r in rows:
            _issue(f"Email '{r['email']}' maps to {len(r['uids'])} Person nodes: {r['uids']}")
    else:
        _check("Duplicate Person emails", "PASS")

def check_duplicate_org_names(conn):
    raw_rows = conn.query("MATCH (o:Organization) WHERE o.name IS NOT NULL RETURN o.uid AS uid, o.name AS name")
    groups = defaultdict(list)
    for r in raw_rows:
        name = r["name"]
        if isinstance(name, (list, tuple)):
            name = name[0] if name else ""
        norm = str(name).strip().lower()
        if norm:
            groups[norm].append(r["uid"])
    rows = [{"norm": k, "uids": v, "cnt": len(v)} for k, v in groups.items() if len(v) > 1]
    rows.sort(key=lambda x: -x["cnt"])
    rows = rows[:20]
    if rows:
        _check("Duplicate Org names", "WARN",
               f"{len(rows)} org name(s) map to multiple nodes", len(rows))
    else:
        _check("Duplicate Org names", "PASS")

def check_high_degree_nodes(conn):
    threshold = 500
    rows = conn.query(f"""
        MATCH (n)
        WITH n, labels(n)[0] AS lbl, size([(n)--() | 1]) AS deg
        WHERE deg > {threshold}
        RETURN lbl, n.uid AS uid, deg
        ORDER BY deg DESC LIMIT 10
    """)
    if rows:
        _check("High-degree nodes", "WARN", f"Nodes with degree > {threshold}", len(rows))
        for r in rows:
            _issue(f"High-degree {r['lbl']} uid={r['uid']} degree={r['deg']}")
    else:
        _check("High-degree nodes", "PASS")


# ============================================================
# SECTION D — Data completeness checks
# ============================================================

def check_persons_missing_name(conn):
    n = conn.scalar("MATCH (p:Person) WHERE p.name IS NULL OR p.name = '' RETURN count(p) AS c")
    _check("Persons missing name", "WARN" if n > 0 else "PASS", count=n)

def check_documents_missing_industry(conn):
    n = conn.scalar("MATCH (d:Document) WHERE d.industry IS NULL OR d.industry = '' RETURN count(d) AS c")
    _check("Documents missing industry", "WARN" if n > 0 else "PASS", count=n)

def check_drugs_missing_name(conn):
    """Drug merges on name (genericName removed from schema)."""
    n = conn.scalar("MATCH (dr:Drug) WHERE dr.name IS NULL OR dr.name = '' RETURN count(dr) AS c")
    _check("Drugs missing name", "WARN" if n > 0 else "PASS", count=n)

def check_claims_missing_subject(conn):
    n = conn.scalar("MATCH (c:Claim) WHERE c.subject IS NULL OR c.subject = '' RETURN count(c) AS c")
    _check("Claims missing subject", "FAIL" if n > 0 else "PASS", count=n)

def check_gpe_missing_name(conn):
    n = conn.scalar("MATCH (g:GPE) WHERE g.name IS NULL OR g.name = '' RETURN count(g) AS c")
    _check("GPE nodes missing name", "WARN" if n > 0 else "PASS", count=n)


# ============================================================
# SECTION D2 — TXT-specific structural checks
# ============================================================

def check_text_contents_have_document(conn):
    n = conn.scalar("MATCH (tc:TextContent) WHERE NOT ()-[:HAS_TEXT_CONTENT]->(tc) RETURN count(tc) AS c")
    status = "FAIL" if n > 0 else "PASS"
    _check("Unlinked TextContent nodes", status, count=n)
    if n > 0:
        _issue(f"{n} TextContent nodes not linked to any Document")

def check_tabular_columns_have_text_content(conn):
    n = conn.scalar("MATCH (col:TabularColumn) WHERE NOT ()-[:HAS_COLUMN]->(col) RETURN count(col) AS c")
    _check("Unlinked TabularColumn nodes", "WARN" if n > 0 else "PASS", count=n)

def check_txt_redaction_coverage(conn):
    n = conn.scalar("MATCH (tc:TextContent {hasRedactions: true}) RETURN count(tc) AS c")
    total = conn.scalar("MATCH (tc:TextContent) RETURN count(tc) AS c") or 1
    _check("TextContent with redactions", "WARN" if n > 0 else "PASS",
           f"{n}/{total} TextContent nodes contain redacted values", n)

def check_txt_documents_have_text_content(conn):
    n = conn.scalar("""
        MATCH (d:Document {fileType: 'TXT'})
        WHERE NOT (d)-[:HAS_TEXT_CONTENT]->()
        RETURN count(d) AS c
    """)
    _check("TXT Documents missing TextContent", "WARN" if n > 0 else "PASS", count=n)

def check_product_nodes_linked(conn):
    n = conn.scalar("MATCH (p:Product) WHERE NOT ()-[:MENTIONS_PRODUCT]->(p) RETURN count(p) AS c")
    _check("Orphan Product nodes", "WARN" if n > 0 else "PASS", count=n)


# ============================================================
# SECTION D3 — Vocab integrity checks
# ============================================================

def check_vocab_nodes_linked(conn):
    n = conn.scalar("MATCH (v:Vocab) WHERE NOT ()-[:HAS_VOCAB]->(v) RETURN count(v) AS c")
    _check("Orphan Vocab nodes", "WARN" if n > 0 else "PASS",
           "Vocab nodes with no parent Document", n)
    if n > 0:
        _issue(f"{n} Vocab node(s) not linked to any Document")


# ============================================================
# SECTION E — Entity resolution candidate detection
#
# Each candidate pair written to candidates.json carries:
#   - label, kg_id1/2, str1/2
#   - witness_context1/2: n.witnessContext read directly from Neo4j
#     (replaces connected-node traversal in resolve_operator._REL_MAP)
#
# Node types with witnessContext in schema (all resolvable types):
#   Organization, Person, Drug, Location, GPE, Topic, Product
# Node types WITHOUT witnessContext:
#   LegalFramework — LLM scores on description similarity only
# ============================================================

# Lightweight suffix regexes for candidate grouping only.
# Full normalization lives in resolve_operator.py.
_GROUP_ORG_SUFFIXES = re.compile(
    r"""[,\s]+(inc\.?|corp\.?|ltd\.?|llc\.?|llp\.?|plc\.?|gmbh|
    company|group|holdings?|international|labs?|laboratory|laboratories)\.?$""",
    re.IGNORECASE | re.VERBOSE,
)
_GROUP_LOC_ABBREV = re.compile(r'\s*\([^)]{1,10}\)\s*$')

# Person credential suffixes — stripped before grouping key is computed.
# e.g. "Dwayne A. Pinon, R.Ph." -> "dwayne a. pinon"
#      "Bob Rappaport, M.D."    -> "bob rappaport"
_PERSON_CREDENTIALS = re.compile(
    r"""[,\s]+(m\.?d\.?|ph\.?d\.?|r\.?ph\.?|m\.?b\.?a\.?|j\.?d\.?|
    d\.?o\.?|pharm\.?d\.?|r\.?n\.?|m\.?s\.?|b\.?s\.?|esq\.?|
    jr\.?|sr\.?|ii|iii|iv|inc\.?)\.?$""",
    re.IGNORECASE | re.VERBOSE,
)


def _normalize_person_name(raw):
    """
    Normalize a person name string for candidate grouping.
    The LLM in resolve_operator.py does final confirmation,
    so false positives here are acceptable — missed pairs are not.
    """
    import unicodedata
    s = str(raw).strip() if raw else ""
    if not s:
        return ""
    # Strip credentials iteratively (can stack: "Name, R.Ph., M.B.A.")
    prev = None
    while prev != s:
        prev = s
        s = _PERSON_CREDENTIALS.sub("", s).strip().rstrip(",").strip()
    # Flip "Last, First" -> "First Last"
    if "," in s:
        parts = [p.strip() for p in s.split(",", 1)]
        if len(parts) == 2 and parts[1]:
            s = f"{parts[1]} {parts[0]}"
    # Strip unicode combining characters for accent-insensitive grouping
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower().strip()


# CANDIDATE_CONFIG
# has_witness_context: True if kg_loader stores witnessContext on the node.
# resolve_operator reads n.witnessContext for these; others get empty context.
CANDIDATE_CONFIG = [
    {
        "label":               "Organization",
        "match_field":         "name",
        "normalize":           "org",
        "has_witness_context": True,    
        "weak_only":           False,
        "detail":              "Org nodes with similar names (legal suffixes stripped)",
    },
    {
        "label":               "Person",
        "match_field":         "name",
        "normalize":           "person",
        "has_witness_context": True,
        "weak_only":           False,   # scan all persons — email presence does not guarantee dedup at load time
        "detail":              "Person nodes sharing same normalized name",
    },
    {
        "label":               "Drug",
        "match_field":         "name",
        "normalize":           "lower",
        "has_witness_context": True,
        "weak_only":           False,
        "detail":              "Drug nodes sharing same name",
    },
    {
        "label":               "Topic",
        "match_field":         "name",
        "normalize":           "lower",
        "has_witness_context": True,
        "weak_only":           False,
        "detail":              "Topic nodes with the same normalised name",
    },
    {
        "label":               "Location",
        "match_field":         "name",
        "normalize":           "location",
        "has_witness_context": True,
        "weak_only":           False,
        "detail":              "Location nodes with same base name (abbreviations stripped)",
    },
    {
        "label":               "GPE",
        "match_field":         "name",
        "normalize":           "lower",
        "has_witness_context": True,
        "weak_only":           False,
        "detail":              "GPE (geo-political entity) nodes with the same normalised name",
    },
    {
        "label":               "Product",
        "match_field":         "name",
        "normalize":           "lower",
        "has_witness_context": True,    
        "weak_only":           False,
        "detail":              "Product nodes with the same name",
    },
    {
        "label":               "LegalFramework",
        "match_field":         "description",
        "normalize":           "lower",
        "has_witness_context": False,
        "weak_only":           False,
        "detail":              "LegalFramework nodes with the same description",
    },
]


def _normalize_candidate_key(normalize_mode, value):
    """Minimal normalization for grouping — full normalization in resolve_operator.py."""
    s = str(value).strip() if value else ""
    if not s:
        return ""
    if normalize_mode == "person":
        return _normalize_person_name(s)
    if normalize_mode == "org":
        s = _GROUP_ORG_SUFFIXES.sub("", s).strip().rstrip(",").strip()
    elif normalize_mode == "location":
        s = _GROUP_LOC_ABBREV.sub("", s).strip()
    return s.lower()


def _extract_last_name(normalized_name):
    """
    Extract the last name token from a normalized person name
    (post credential-stripping and Last,First flipping).
    Used as a secondary grouping key to catch nickname variants
    like 'Tasha Polster' vs 'Polster, Natasha'.

    Returns the last whitespace-delimited token, or '' if not extractable.
    Examples:
      "tasha polster"   -> "polster"
      "natasha polster" -> "polster"
      "dwayne a. pinon" -> "pinon"
      "t. bennett"      -> "bennett"
    """
    parts = normalized_name.strip().split()
    if len(parts) >= 2:
        return parts[-1]
    return ""


def _coerce_row(row, has_witness_context):
    """Coerce Neo4j row values — kg_id, val, witnessContext can all come back as lists."""
    raw = row["val"]
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else ""
    raw = str(raw)

    kg_id = row["kg_id"]
    if isinstance(kg_id, (list, tuple)):
        kg_id = kg_id[0] if kg_id else ""
    kg_id = str(kg_id)

    wc = ""
    if has_witness_context:
        wc = row.get("witness_context", "")
        if isinstance(wc, (list, tuple)):
            wc = next((v for v in wc if v and str(v).strip()), "")
        wc = str(wc).strip() if wc else ""

    return kg_id, raw, wc


def _emit_pairs(label, nodes):
    """Emit all unique (i, j) pairs from a group of nodes."""
    pairs = []
    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            a, b = nodes[i], nodes[j]
            pairs.append({
                "label":            label,
                "kg_id1":           a["kg_id"],
                "str1":             a["val"],
                "witness_context1": a["wc"],
                "kg_id2":           b["kg_id"],
                "str2":             b["val"],
                "witness_context2": b["wc"],
            })
    return pairs


def detect_entity_candidates(conn, candidates_out="candidates.json"):
    """
    Scan Neo4j for potential duplicate node pairs per label in CANDIDATE_CONFIG.

    Person uses two grouping passes (full normalised name + last-name-only)
    to catch nickname variants like 'Tasha Polster' vs 'Polster, Natasha'.
    All other labels use a single normalised-name pass.

    witnessContext is read directly from Neo4j and embedded in each candidate.
    All list-type Neo4j property values are coerced to strings via _coerce_row().

    Writes candidates.json for resolve_operator.py to consume.
    """
    print("\n[E] Entity resolution — candidate detection")
    all_candidates = []

    for cfg in CANDIDATE_CONFIG:
        label               = cfg["label"]
        match_field         = cfg["match_field"]
        normalize           = cfg["normalize"]
        weak_only           = cfg.get("weak_only", False)
        has_witness_context = cfg.get("has_witness_context", False)
        detail              = cfg["detail"]

        # Build WHERE clause from config — no hardcoding
        where_clause = f"n.{match_field} IS NOT NULL AND n.{match_field} <> ''"
        if weak_only:
            where_clause += " AND (n.email IS NULL OR n.email = '')"

        # Fetch nodes — include witnessContext only for types that have it
        if has_witness_context:
            rows = conn.query(
                f"MATCH (n:{label}) WHERE {where_clause} "
                f"RETURN n.kg_id AS kg_id, n.{match_field} AS val, "
                f"coalesce(n.witnessContext, '') AS witness_context"
            )
        else:
            rows = conn.query(
                f"MATCH (n:{label}) WHERE {where_clause} "
                f"RETURN n.kg_id AS kg_id, n.{match_field} AS val"
            )

        # Coerce all values and build node dicts
        nodes = []
        for row in rows:
            kg_id, raw, wc = _coerce_row(row, has_witness_context)
            if raw:
                nodes.append({"kg_id": kg_id, "val": raw, "wc": wc})

        # --- Grouping ---
        # Person: two passes — full normalised name + last-name-only
        # All others: single normalised-name pass
        label_candidates = []

        if label == "Person":
            LAST_NAME_MIN_LEN = 4  # skip surnames like "lee", "kim"

            full_groups = defaultdict(list)
            last_groups = defaultdict(list)
            for n in nodes:
                full_key = _normalize_candidate_key(normalize, n["val"])
                if full_key:
                    full_groups[full_key].append(n)
                last_key = _extract_last_name(full_key)
                if len(last_key) >= LAST_NAME_MIN_LEN:
                    last_groups[last_key].append(n)

            # Collect pairs from both passes, dedup by sorted (kg_id1, kg_id2)
            seen = set()
            pass1_n = 0
            for key, members in full_groups.items():
                if len(members) < 2:
                    continue
                for pair in _emit_pairs(label, members):
                    dedup_key = tuple(sorted([pair["kg_id1"], pair["kg_id2"]]))
                    if dedup_key not in seen:
                        seen.add(dedup_key)
                        label_candidates.append(pair)
                        pass1_n += 1

            pass2_n = 0
            for key, members in last_groups.items():
                if len(members) < 2:
                    continue
                for pair in _emit_pairs(label, members):
                    dedup_key = tuple(sorted([pair["kg_id1"], pair["kg_id2"]]))
                    if dedup_key not in seen:
                        seen.add(dedup_key)
                        label_candidates.append(pair)
                        pass2_n += 1

            REPORT["entity_resolution_summary"][label] = {
                "candidate_pairs": len(label_candidates),
                "pass1_full_name": pass1_n,
                "pass2_last_name": pass2_n,
            }
            _enrichment(
                "CANDIDATES_PERSON",
                len(label_candidates),
                category="entity_resolution",
                detail=(f"{detail} — {pass1_n} full-name pairs + {pass2_n} "
                        f"last-name-only pairs = {len(label_candidates)} total"),
            )

        else:
            groups = defaultdict(list)
            for n in nodes:
                key = _normalize_candidate_key(normalize, n["val"])
                if key:
                    groups[key].append(n)

            for key, members in groups.items():
                if len(members) >= 2:
                    label_candidates.extend(_emit_pairs(label, members))

            REPORT["entity_resolution_summary"][label] = {
                "candidate_pairs": len(label_candidates),
            }
            _enrichment(
                f"CANDIDATES_{label.upper()}",
                len(label_candidates),
                category="entity_resolution",
                detail=f"{detail} — {len(label_candidates)} candidate pairs found",
            )

        all_candidates.extend(label_candidates)

    with open(candidates_out, "w", encoding="utf-8") as f:
        json.dump(all_candidates, f, indent=2, ensure_ascii=False)
    print(f"  Wrote {len(all_candidates)} candidate pairs → {candidates_out}")
    print(f"  Run: python resolve_operator.py --candidates {candidates_out} "
          f"--uri <uri> --user neo4j --password <pw>")
    return all_candidates


# ============================================================
# SECTION F — Cross-document & derived enrichment rules
# ============================================================

def enrich_coauthor_persons(conn):
    """(Person)-[:CO_APPEARED_IN]->(Person): both contacts in the same Document."""
    result = conn.query("""
        MATCH (d:Document)-[:HAS_CONTACT]->(p1:Person),
              (d:Document)-[:HAS_CONTACT]->(p2:Person)
        WHERE id(p1) < id(p2)
        MERGE (p1)-[r:CO_APPEARED_IN]->(p2)
        ON CREATE SET r.docCount = 1
        ON MATCH  SET r.docCount = r.docCount + 1
        RETURN count(r) AS c
    """)
    n = result[0]["c"] if result else 0
    _enrichment("ENRICH_CO_APPEARED_IN", n, category="co_occurrence",
                detail="(Person)-[:CO_APPEARED_IN]->(Person): both contacts in the same Document")

def enrich_same_industry(conn):
    """(Document)-[:SAME_INDUSTRY]->(Document): same industry field."""
    result = conn.query("""
        MATCH (d1:Document), (d2:Document)
        WHERE d1.industry = d2.industry
          AND d1.industry <> ''
          AND id(d1) < id(d2)
        MERGE (d1)-[:SAME_INDUSTRY]->(d2)
        RETURN count(*) AS c
    """)
    n = result[0]["c"] if result else 0
    _enrichment("ENRICH_SAME_INDUSTRY", n, category="cross_doc_links",
                detail="(Document)-[:SAME_INDUSTRY]->(Document): same industry field")

def enrich_drug_mentioned_by_person(conn):
    """(Person)-[:ASSOCIATED_WITH_DRUG]->(Drug): person sent an email mentioning the drug."""
    result = conn.query("""
        MATCH (p:Person)<-[:SENT_BY]-(m:EmailMessage)-[:MENTIONS_DRUG]->(dr:Drug)
        MERGE (p)-[:ASSOCIATED_WITH_DRUG]->(dr)
        RETURN count(*) AS c
    """)
    n = result[0]["c"] if result else 0
    _enrichment("ENRICH_PERSON_ASSOC_DRUG", n, category="cross_doc_links",
                detail="(Person)-[:ASSOCIATED_WITH_DRUG]->(Drug): person sent an email mentioning the drug")

def enrich_org_in_same_doc(conn):
    """(Organization)-[:CO_MENTIONED_IN]->(Organization): both orgs in the same Document."""
    result = conn.query("""
        MATCH (d:Document)-[:HAS_CONTACT]->(o1:Organization),
              (d:Document)-[:HAS_CONTACT]->(o2:Organization)
        WHERE id(o1) < id(o2)
        MERGE (o1)-[r:CO_MENTIONED_IN]->(o2)
        ON CREATE SET r.docCount = 1
        ON MATCH  SET r.docCount = r.docCount + 1
        RETURN count(r) AS c
    """)
    n = result[0]["c"] if result else 0
    _enrichment("ENRICH_CO_MENTIONED_IN", n, category="co_occurrence",
                detail="(Org)-[:CO_MENTIONED_IN]->(Org): both mentioned in the same Document")

def enrich_person_covers_topic(conn):
    """(Person)-[:SPEAKS_ABOUT]->(Topic): person is a contact in a doc covering that topic."""
    result = conn.query("""
        MATCH (d:Document)-[:HAS_CONTACT]->(p:Person),
              (d:Document)-[:COVERS_TOPIC]->(t:Topic)
        MERGE (p)-[:SPEAKS_ABOUT]->(t)
        RETURN count(*) AS c
    """)
    n = result[0]["c"] if result else 0
    _enrichment("ENRICH_PERSON_SPEAKS_ABOUT_TOPIC", n, category="cross_doc_links",
                detail="(Person)-[:SPEAKS_ABOUT]->(Topic): person is a contact in a document covering that topic")

def enrich_email_thread_order(conn):
    """(EmailMessage)-[:NEXT_IN_THREAD]->(EmailMessage): consecutive messages in same Document."""
    result = conn.query("""
        MATCH (d:Document)-[:HAS_MESSAGE]->(m:EmailMessage)
        WITH d, m ORDER BY m.dateSent ASC
        WITH d, collect(m) AS msgs
        UNWIND range(0, size(msgs)-2) AS i
        WITH msgs[i] AS m1, msgs[i+1] AS m2
        MERGE (m1)-[:NEXT_IN_THREAD]->(m2)
        RETURN count(*) AS c
    """)
    n = result[0]["c"] if result else 0
    _enrichment("ENRICH_NEXT_IN_THREAD", n, category="sequence",
                detail="(EmailMessage)-[:NEXT_IN_THREAD]->(EmailMessage): consecutive messages ordered by dateSent")

def enrich_xls_org_to_doc(conn):
    """(Organization)-[:LISTED_IN]->(Document): org appeared in XLS sharedEntities."""
    result = conn.query("""
        MATCH (d:Document {fileType: 'XLS'})-[:HAS_CONTACT]->(o:Organization)
        MERGE (o)-[:LISTED_IN]->(d)
        RETURN count(*) AS c
    """)
    n = result[0]["c"] if result else 0
    _enrichment("ENRICH_ORG_LISTED_IN_XLS_DOC", n, category="cross_doc_links",
                detail="(Org)-[:LISTED_IN]->(Document): org appeared in XLS sharedEntities")

def enrich_gpe_co_occurrence(conn):
    """
    (GPE)-[:CO_MENTIONED_GPE]->(GPE)
    Two GPE nodes co-mentioned in the same Document.
    """
    result = conn.query("""
        MATCH (d:Document)-[:MENTIONS_GPE]->(g1:GPE),
              (d:Document)-[:MENTIONS_GPE]->(g2:GPE)
        WHERE id(g1) < id(g2)
        MERGE (g1)-[r:CO_MENTIONED_GPE]->(g2)
        ON CREATE SET r.docCount = 1
        ON MATCH  SET r.docCount = r.docCount + 1
        RETURN count(r) AS c
    """)
    n = result[0]["c"] if result else 0
    _enrichment("ENRICH_GPE_CO_MENTIONED", n, category="co_occurrence",
                detail="(GPE)-[:CO_MENTIONED_GPE]->(GPE): both geo-political entities in the same Document")

def enrich_person_associated_gpe(conn):
    """
    (Person)-[:ASSOCIATED_WITH_GPE]->(GPE)
    Person is a contact in a Document that mentions a GPE.
    """
    result = conn.query("""
        MATCH (d:Document)-[:HAS_CONTACT]->(p:Person),
              (d:Document)-[:MENTIONS_GPE]->(g:GPE)
        MERGE (p)-[:ASSOCIATED_WITH_GPE]->(g)
        RETURN count(*) AS c
    """)
    n = result[0]["c"] if result else 0
    _enrichment("ENRICH_PERSON_ASSOC_GPE", n, category="cross_doc_links",
                detail="(Person)-[:ASSOCIATED_WITH_GPE]->(GPE): person's document mentions the GPE")


# ============================================================
# SECTION F2 — Node tagging
# ============================================================

def enrich_document_country_label(conn):
    """Add :Country secondary label to known country Location nodes."""
    known_countries = [
        "United States", "Belgium", "Canada", "United Kingdom", "Germany",
        "France", "Japan", "China", "South Korea", "Australia", "India",
        "Brazil", "Netherlands", "Sweden", "Switzerland",
    ]
    for country in known_countries:
        conn.run("MATCH (l:Location {name: $name}) SET l:Country", name=country)
    conn.run("""
        MATCH (d:Document)-[:LOCATED_IN]->(l:Location)
        WHERE d.country = l.name AND l.name <> ''
        SET l:Country
    """)
    n = conn.scalar("MATCH (l:Location:Country) RETURN count(l) AS c")
    _enrichment("ENRICH_LOCATION_COUNTRY_LABEL", n, category="node_tagging",
                detail="Added :Country secondary label to known country Location nodes")

def enrich_txt_location_tag(conn):
    """Add :State secondary label to US state Location nodes found in TXT data."""
    us_states = [
        "Alabama","Alaska","Arizona","Arkansas","California","Colorado","Connecticut",
        "Delaware","Florida","Georgia","Hawaii","Idaho","Illinois","Indiana","Iowa",
        "Kansas","Kentucky","Louisiana","Maine","Maryland","Massachusetts","Michigan",
        "Minnesota","Mississippi","Missouri","Montana","Nebraska","Nevada",
        "New Hampshire","New Jersey","New Mexico","New York","North Carolina",
        "North Dakota","Ohio","Oklahoma","Oregon","Pennsylvania","Rhode Island",
        "South Carolina","South Dakota","Tennessee","Texas","Utah","Vermont",
        "Virginia","Washington","West Virginia","Wisconsin","Wyoming",
    ]
    for state in us_states:
        conn.run("MATCH (l:Location {name: $name}) SET l:State", name=state)
    n = conn.scalar("MATCH (l:Location:State) RETURN count(l) AS c")
    _enrichment("ENRICH_LOCATION_STATE_LABEL", n, category="node_tagging",
                detail="Added :State secondary label to US state Location nodes found in TXT")


# ============================================================
# SECTION F3 — TXT co-occurrence enrichments
# ============================================================

def enrich_txt_org_cross_doc(conn):
    """(Organization)-[:MENTIONED_IN_TEXT]->(Document): org found in TXT TextContent entity list."""
    result = conn.query("""
        MATCH (d:Document {fileType:'TXT'})-[:HAS_TEXT_CONTENT]->(tc:TextContent)
              -[:MENTIONS_ORG_IN_TEXT]->(o:Organization)
        MERGE (o)-[:MENTIONED_IN_TEXT]->(d)
        RETURN count(*) AS c
    """)
    n = result[0]["c"] if result else 0
    _enrichment("ENRICH_TXT_ORG_MENTIONED_IN_DOC", n, category="cross_doc_links",
                detail="(Org)-[:MENTIONED_IN_TEXT]->(Document): org found in TXT entity list")

def enrich_product_org_co_occurrence(conn):
    """(Product)-[:CO_OCCURS_WITH]->(Organization): share same TXT TextContent."""
    result = conn.query("""
        MATCH (tc:TextContent)-[:MENTIONS_PRODUCT_IN_TEXT]->(p:Product),
              (tc:TextContent)-[:MENTIONS_ORG_IN_TEXT]->(o:Organization)
        MERGE (p)-[r:CO_OCCURS_WITH]->(o)
        ON CREATE SET r.count = 1
        ON MATCH  SET r.count = r.count + 1
        RETURN count(r) AS c
    """)
    n = result[0]["c"] if result else 0
    _enrichment("ENRICH_PRODUCT_ORG_CO_OCCURS_IN_TXT", n, category="co_occurrence",
                detail="(Product)-[:CO_OCCURS_WITH]->(Org): share same TXT TextContent")

def enrich_shared_tabular_columns(conn):
    """(TabularColumn)-[:SAME_COLUMN_NAME]->(TabularColumn): identical name across TextContent nodes."""
    result = conn.query("""
        MATCH (c1:TabularColumn), (c2:TabularColumn)
        WHERE c1.name = c2.name AND id(c1) < id(c2) AND c1.name <> ''
        MERGE (c1)-[:SAME_COLUMN_NAME]->(c2)
        RETURN count(*) AS c
    """)
    n = result[0]["c"] if result else 0
    _enrichment("ENRICH_SHARED_TABULAR_COLUMN_NAMES", n, category="co_occurrence",
                detail="(TabularColumn)-[:SAME_COLUMN_NAME]->(TabularColumn): identical name across TextContent nodes")


# ============================================================
# SECTION G — Relationship distribution summary
# ============================================================

def check_relationship_counts(conn):
    print("\n[G] Relationship type counts")
    rows = conn.query("""
        MATCH ()-[r]->()
        RETURN type(r) AS relType, count(r) AS cnt
        ORDER BY cnt DESC
    """)
    for row in rows:
        REPORT["stats"][f"rel_{row['relType']}"] = row["cnt"]
        print(f"       {row['relType']:<40} {row['cnt']:>6}")


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Post-KG rules: validate and enrich the Neo4j KG."
    )
    parser.add_argument("--uri",             default="bolt://localhost:7687")
    parser.add_argument("--user",            default="neo4j")
    parser.add_argument("--password",        required=True)
    parser.add_argument("--out",             default="post_kg_report.json")
    parser.add_argument("--candidates-out",  default="candidates.json",
                        help="Path to write candidate pairs for resolve_operator.py (default: candidates.json)")
    parser.add_argument("--skip-resolution", action="store_true",
                        help="Skip entity resolution candidate detection")
    args = parser.parse_args()

    REPORT["run_at"] = datetime.utcnow().isoformat() + "Z"
    conn = KGConn(args.uri, args.user, args.password)

    print("\n━━━ POST-KG RULES ━━━")

    print("\n[A] Node count statistics")
    check_node_counts(conn)

    print("\n[B] Structural integrity checks")
    check_orphan_documents(conn)
    check_orphan_persons(conn)
    check_orphan_orgs(conn)
    check_orphan_gpe(conn)
    check_claims_have_document(conn)
    check_email_messages_have_sender(conn)
    check_email_messages_have_recipient(conn)
    check_slides_have_document(conn)
    check_sheets_have_document(conn)

    print("\n[C] Cardinality checks")
    check_document_degree(conn)
    check_duplicate_person_emails(conn)
    check_duplicate_org_names(conn)
    check_high_degree_nodes(conn)

    print("\n[D] Data completeness checks")
    check_persons_missing_name(conn)
    check_documents_missing_industry(conn)
    check_drugs_missing_name(conn)
    check_claims_missing_subject(conn)
    check_gpe_missing_name(conn)

    print("\n[D2] TXT-specific structural checks")
    check_text_contents_have_document(conn)
    check_tabular_columns_have_text_content(conn)
    check_txt_redaction_coverage(conn)
    check_txt_documents_have_text_content(conn)
    check_product_nodes_linked(conn)

    print("\n[D3] Vocab integrity checks")
    check_vocab_nodes_linked(conn)

    if not args.skip_resolution:
        candidates = detect_entity_candidates(conn, candidates_out=args.candidates_out)
        if candidates:
            print(f"\n  ➡️   {len(candidates)} candidate pairs written to {args.candidates_out}")
    else:
        print("\n[E] Skipping entity resolution candidate detection (--skip-resolution flag set)")

    print("\n[F] Cross-document & derived enrichments")
    _set_section("cross_doc_links")
    enrich_coauthor_persons(conn)
    enrich_same_industry(conn)
    enrich_drug_mentioned_by_person(conn)
    enrich_org_in_same_doc(conn)
    enrich_person_covers_topic(conn)
    enrich_email_thread_order(conn)
    enrich_xls_org_to_doc(conn)
    enrich_gpe_co_occurrence(conn)
    enrich_person_associated_gpe(conn)

    print("\n[F2] Node tagging")
    _set_section("node_tagging")
    enrich_document_country_label(conn)
    enrich_txt_location_tag(conn)

    print("\n[F3] Co-occurrence enrichments")
    _set_section("co_occurrence")
    enrich_txt_org_cross_doc(conn)
    enrich_product_org_co_occurrence(conn)
    enrich_shared_tabular_columns(conn)

    print("\n[G] Relationship type counts")
    check_relationship_counts(conn)

    conn.close()

    with open(args.out, "w") as f:
        json.dump(REPORT, f, indent=2, ensure_ascii=False)

    total_checks = len(REPORT["checks"])
    fails  = sum(1 for c in REPORT["checks"] if c["status"] == "FAIL")
    warns  = sum(1 for c in REPORT["checks"] if c["status"] == "WARN")
    passes = sum(1 for c in REPORT["checks"] if c["status"] == "PASS")

    print(f"\n{chr(9473)*50}")
    print(f"  Checks : {total_checks}  ✅ {passes}  ⚠️  {warns}  ❌ {fails}")
    print(f"  Issues : {len(REPORT['issues'])}")
    print()
    print("  Enrichments by category:")
    for cat, entries in REPORT["enrichments"].items():
        if entries:
            total_affected = sum(e["affected"] for e in entries)
            print(f"    {cat:<20} {len(entries)} rules   {total_affected} nodes/edges affected")
    print()
    print("  Entity resolution (candidates for resolve_operator.py):")
    for label, summary in REPORT["entity_resolution_summary"].items():
        pairs = summary.get("candidate_pairs", 0)
        if pairs > 0:
            print(f"    {label:<20} {pairs} candidate pairs")
    print(f"  Report : {args.out}")
    print(f"{chr(9473)*50}")


if __name__ == "__main__":
    main()
