"""
post_kg_rules.py
================
Runs AFTER kg_loader.py. Connects to Neo4j and performs:
  - Structural integrity checks (orphan nodes, missing relationships)
  - Cardinality checks (min/max degree assertions)
  - Deduplication merges (Person by email, Organization by name)
  - Enrichment rules (infer missing labels, derive new relationships)
  - TXT-specific checks (TextContent, TabularColumn, Product, redaction flags)
  - Summary statistics and quality report

Usage:
    python post_kg_rules.py \
        --uri      bolt://localhost:7687 \
        --user     neo4j \
        --password <your-password> \
        --out      post_kg_report.json
"""

import json
import argparse
from datetime import datetime
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
    # Enrichments broken into labelled categories so the report is readable
    "enrichments": {
        "entity_resolution": [],  # dedup / merge of duplicate nodes
        "cross_doc_links":   [],  # new edges connecting nodes across documents
        "node_tagging":      [],  # adding secondary labels (e.g. :Country, :State)
        "co_occurrence":     [],  # co-appearance / co-mention derived edges
        "sequence":          [],  # ordering / threading edges
        "external_vocab":    [],  # enrichment from external APIs (RxNorm etc.)
    },
    "entity_resolution_summary": {},  # per-label: candidates_found, merged
    "stats": {},
    "issues": [],
}

# Active category for _enrichment() calls — updated by _set_section()
_CURRENT_SECTION = "cross_doc_links"

def _set_section(section):
    global _CURRENT_SECTION
    _CURRENT_SECTION = section

def _check(name, status, detail="", count=0):
    entry = {"check": name, "status": status, "detail": detail, "count": count}
    REPORT["checks"].append(entry)
    icon = "✅" if status == "PASS" else ("⚠️ " if status == "WARN" else "❌")
    print(f"  {icon}  [{status}] {name}" + (f" - {detail}" if detail else "") + (f" (n={count})" if count else ""))

def _enrichment(rule, affected, category=None, detail=""):
    """
    Record an enrichment result.
    category : overrides _CURRENT_SECTION if provided.
    detail   : short human-readable description of what this rule does.
    """
    cat = category or _CURRENT_SECTION
    entry = {"rule": rule, "affected": affected, "detail": detail}
    REPORT["enrichments"][cat].append(entry)
    print(f"  ✨  {rule:<45} affected={affected:>5}" + (f"  — {detail}" if detail else ""))

def _issue(msg):
    REPORT["issues"].append(msg)


# ============================================================
# SECTION 1 — Node count stats
# ============================================================

def check_node_counts(conn):
    print("\n[A] Node count statistics")
    labels = ["Document","Person","Organization","Drug","Claim","Topic",
              "Location","Abbreviation","EmailMessage","Slide","Sheet",
              "Citation","LegalFramework",
              "TextContent","TabularColumn","Product"]
    for label in labels:
        n = conn.scalar(f"MATCH (n:{label}) RETURN count(n) AS c")
        REPORT["stats"][f"count_{label}"] = n
        print(f"       {label:<22} {n:>6}")


# ============================================================
# SECTION 2 — Structural integrity checks
# ============================================================

def check_orphan_documents(conn):
    """Documents with zero outgoing relationships."""
    n = conn.scalar("""
        MATCH (d:Document)
        WHERE NOT (d)-->()
        RETURN count(d) AS c
    """)
    if n > 0:
        _check("Orphan Documents", "WARN", "Documents with no outgoing edges", n)
        _issue(f"{n} Document nodes have no relationships")
    else:
        _check("Orphan Documents", "PASS")

def check_orphan_persons(conn):
    """Persons with no document or message connections."""
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
    """Organizations with no document connections."""
    n = conn.scalar("""
        MATCH (o:Organization)
        WHERE NOT ()-[:HAS_CONTACT]->(o)
          AND NOT ()-[:WORKS_FOR]->(o)
        RETURN count(o) AS c
    """)
    if n > 0:
        _check("Orphan Organizations", "WARN", "Orgs with no links", n)
    else:
        _check("Orphan Organizations", "PASS")

def check_claims_have_document(conn):
    """Every Claim must be linked from a Document."""
    n = conn.scalar("""
        MATCH (c:Claim)
        WHERE NOT ()-[:HAS_CLAIM]->(c)
        RETURN count(c) AS c
    """)
    if n > 0:
        _check("Unlinked Claims", "FAIL", "Claims with no parent Document", n)
        _issue(f"{n} Claim nodes not linked to any Document")
    else:
        _check("Unlinked Claims", "PASS")

def check_email_messages_have_sender(conn):
    """Every EmailMessage should have a SENT_BY relationship."""
    n = conn.scalar("""
        MATCH (m:EmailMessage)
        WHERE NOT (m)-[:SENT_BY]->()
        RETURN count(m) AS c
    """)
    if n > 0:
        _check("Messages missing sender", "WARN", "EmailMessages with no SENT_BY", n)
    else:
        _check("Messages missing sender", "PASS")

def check_email_messages_have_recipient(conn):
    """Every EmailMessage should have at least one SENT_TO relationship."""
    n = conn.scalar("""
        MATCH (m:EmailMessage)
        WHERE NOT (m)-[:SENT_TO]->()
        RETURN count(m) AS c
    """)
    if n > 0:
        _check("Messages missing recipient", "WARN", "EmailMessages with no SENT_TO", n)
    else:
        _check("Messages missing recipient", "PASS")

def check_slides_have_document(conn):
    n = conn.scalar("""
        MATCH (s:Slide) WHERE NOT ()-[:HAS_SLIDE]->(s) RETURN count(s) AS c
    """)
    status = "PASS" if n == 0 else "WARN"
    _check("Unlinked Slides", status, count=n)

def check_sheets_have_document(conn):
    n = conn.scalar("""
        MATCH (s:Sheet) WHERE NOT ()-[:HAS_SHEET]->(s) RETURN count(s) AS c
    """)
    status = "PASS" if n == 0 else "WARN"
    _check("Unlinked Sheets", status, count=n)


# ============================================================
# SECTION 3 — Cardinality / distribution checks
# ============================================================

def check_document_degree(conn):
    """Documents should have at least 1 outgoing relationship."""
    rows = conn.query("""
        MATCH (d:Document)
        WITH d, size([(d)-->() | 1]) AS deg
        WHERE deg = 0
        RETURN d.uid AS uid, d.fileType AS ft
        LIMIT 10
    """)
    if rows:
        _check("Document min-degree", "WARN", f"Documents with degree=0: {[r['uid'] for r in rows]}", len(rows))
    else:
        _check("Document min-degree", "PASS")

def check_duplicate_person_emails(conn):
    """Detect multiple Person nodes sharing the same non-empty email (should be merged)."""
    rows = conn.query("""
        MATCH (p:Person)
        WHERE p.email <> ''
        WITH p.email AS email, collect(p.uid) AS uids, count(*) AS cnt
        WHERE cnt > 1
        RETURN email, uids, cnt
        ORDER BY cnt DESC
        LIMIT 20
    """)
    if rows:
        _check("Duplicate Person emails", "WARN",
               f"{len(rows)} email(s) shared by multiple Person nodes", len(rows))
        for r in rows:
            _issue(f"Email '{r['email']}' maps to {len(r['uids'])} Person nodes: {r['uids']}")
    else:
        _check("Duplicate Person emails", "PASS")

def check_duplicate_org_names(conn):
    """Detect multiple Org nodes sharing the same name (case-insensitive)."""
    rows = conn.query("""
        MATCH (o:Organization)
        WITH toLower(trim(o.name)) AS norm, collect(o.uid) AS uids, count(*) AS cnt
        WHERE cnt > 1 AND norm <> ''
        RETURN norm, uids, cnt
        ORDER BY cnt DESC
        LIMIT 20
    """)
    if rows:
        _check("Duplicate Org names", "WARN",
               f"{len(rows)} org name(s) map to multiple nodes", len(rows))
    else:
        _check("Duplicate Org names", "PASS")

def check_high_degree_nodes(conn):
    """Flag any node with unusually high degree (possible super-node)."""
    threshold = 500
    rows = conn.query(f"""
        MATCH (n)
        WITH n, labels(n)[0] AS lbl, size([(n)--() | 1]) AS deg
        WHERE deg > {threshold}
        RETURN lbl, n.uid AS uid, deg
        ORDER BY deg DESC
        LIMIT 10
    """)
    if rows:
        _check("High-degree nodes", "WARN",
               f"Nodes with degree > {threshold}", len(rows))
        for r in rows:
            _issue(f"High-degree {r['lbl']} uid={r['uid']} degree={r['deg']}")
    else:
        _check("High-degree nodes", "PASS")


# ============================================================
# SECTION 4 — Generalised entity resolution
#
# ENTITY_RESOLUTION_CONFIG drives everything.
# To add a new label: add one entry — no other code changes needed.
#
# Each entry:
#   label        : Neo4j node label to deduplicate
#   match_fields : list of node properties whose normalised combined value
#                  must match for two nodes to be considered duplicates
#   normalize    : "lower" | "none"  — how to normalise the match key
#   key_type     : "strong" | "weak" — strong = auto-merge; weak = flag only
#   detail       : human-readable description for the report
# ============================================================

ENTITY_RESOLUTION_CONFIG = [
    {
        "label":        "Person",
        "match_fields": ["email"],
        "normalize":    "lower",
        "key_type":     "strong",
        "detail":       "Person nodes sharing the same email address",
    },
    {
        "label":        "Organization",
        "match_fields": ["name"],
        "normalize":    "lower",
        "key_type":     "strong",
        "detail":       "Organization nodes with the same normalised name",
    },
    {
        "label":        "Drug",
        "match_fields": ["genericName"],
        "normalize":    "lower",
        "key_type":     "strong",
        "detail":       "Drug nodes sharing the same generic name",
    },
    {
        "label":        "Location",
        "match_fields": ["name"],
        "normalize":    "lower",
        "key_type":     "strong",
        "detail":       "Location nodes with the same normalised name",
    },
    {
        "label":        "Topic",
        "match_fields": ["name"],
        "normalize":    "lower",
        "key_type":     "strong",
        "detail":       "Topic nodes with the same normalised name",
    },
    {
        "label":        "Product",
        "match_fields": ["name"],
        "normalize":    "lower",
        "key_type":     "weak",
        "detail":       "Product nodes with the same name (flagged, not auto-merged)",
    },
    {
        "label":        "LegalFramework",
        "match_fields": ["type", "description"],
        "normalize":    "lower",
        "key_type":     "weak",
        "detail":       "LegalFramework nodes with the same type+description (flagged only)",
    },
]


def _normalise(value, mode):
    """Normalise a property value for comparison."""
    s = str(value).strip() if value else ""
    if mode == "lower":
        return s.lower()
    return s


def _make_match_key(node, fields, normalize):
    """Build a combined match key from one or more node properties."""
    parts = [_normalise(node.get(f, ""), normalize) for f in fields]
    return "|".join(parts)


def _get_degree(conn, label, kg_id):
    return conn.scalar(
        f"MATCH (n:{label} {{kg_id:$kg_id}})--() RETURN count(*) AS c",
        kg_id=kg_id,
    ) or 0


def _merge_two_nodes_apoc(conn, label, keeper_kg_id, dup_kg_id):
    """
    Merge dup into keeper using APOC refactor (atomic, handles all rel types).
    Returns True if succeeded, False if APOC unavailable.
    """
    try:
        conn.run(f"""
            MATCH (keep:{label} {{kg_id:$keep}}), (dup:{label} {{kg_id:$dup}})
            CALL apoc.refactor.mergeNodes([keep, dup],
                {{properties:'combine', mergeRels:true}})
            YIELD node RETURN node
        """, keep=keeper_kg_id, dup=dup_kg_id)
        return True
    except Exception:
        return False


def _merge_two_nodes_cypher(conn, label, keeper_kg_id, dup_kg_id):
    """
    Cypher-only fallback: re-point all relationships to keeper, DETACH DELETE dup.
    Works for any node but does not combine properties.
    """
    # Re-point outgoing edges
    conn.run(f"""
        MATCH (dup:{label} {{kg_id:$dup}})-[r]->(target)
        MATCH (keep:{label} {{kg_id:$keep}})
        WHERE id(dup) <> id(keep) AND id(target) <> id(keep)
        WITH keep, target, type(r) AS rtype, r
        CALL apoc.merge.relationship(keep, rtype, {{}}, {{}}, target)
        YIELD rel
        DELETE r
    """, dup=dup_kg_id, keep=keeper_kg_id)
    # Re-point incoming edges
    conn.run(f"""
        MATCH (source)-[r]->(dup:{label} {{kg_id:$dup}})
        MATCH (keep:{label} {{kg_id:$keep}})
        WHERE id(dup) <> id(keep) AND id(source) <> id(keep)
        WITH source, keep, type(r) AS rtype, r
        CALL apoc.merge.relationship(source, rtype, {{}}, {{}}, keep)
        YIELD rel
        DELETE r
    """, dup=dup_kg_id, keep=keeper_kg_id)
    conn.run(f"MATCH (n:{label} {{kg_id:$dup}}) DETACH DELETE n", dup=dup_kg_id)


def resolve_entities(conn, use_apoc=True):
    """
    Run entity resolution for all labels in ENTITY_RESOLUTION_CONFIG.
    strong key_type  → auto-merge duplicates
    weak key_type    → flag in report only, no merge
    Updates REPORT["entity_resolution_summary"] with per-label results.
    """
    print("\n[E] Entity resolution")
    for cfg in ENTITY_RESOLUTION_CONFIG:
        label      = cfg["label"]
        fields     = cfg["match_fields"]
        normalize  = cfg["normalize"]
        key_type   = cfg["key_type"]
        detail     = cfg["detail"]
        rule_name  = f"ENTITY_RESOLUTION_{label.upper()}"

        # Pull all nodes with non-empty match fields
        field_conditions = " AND ".join(
            f"n.{f} IS NOT NULL AND n.{f} <> ''" for f in fields
        )
        prop_returns = ", ".join(f"n.{f} AS {f}" for f in fields)
        rows = conn.query(
            f"MATCH (n:{label}) WHERE {field_conditions} "
            f"RETURN n.kg_id AS kg_id, {prop_returns}"
        )

        # Group by normalised match key
        from collections import defaultdict
        groups = defaultdict(list)
        for row in rows:
            key = _make_match_key(row, fields, normalize)
            if key.replace("|","").strip():
                groups[key].append(row["kg_id"])

        candidates = {k: v for k, v in groups.items() if len(v) > 1}
        candidates_count = sum(len(v) for v in candidates.values())
        merged_count = 0
        flagged_count = 0

        for match_key, kg_ids in candidates.items():
            if key_type == "strong":
                # Pick keeper = node with highest degree
                ranked = sorted(
                    [(kg_id, _get_degree(conn, label, kg_id)) for kg_id in kg_ids],
                    key=lambda x: x[1], reverse=True
                )
                keeper_kg_id = ranked[0][0]
                for dup_kg_id, _ in ranked[1:]:
                    if use_apoc:
                        ok = _merge_two_nodes_apoc(conn, label, keeper_kg_id, dup_kg_id)
                        if not ok:
                            _merge_two_nodes_cypher(conn, label, keeper_kg_id, dup_kg_id)
                    else:
                        _merge_two_nodes_cypher(conn, label, keeper_kg_id, dup_kg_id)
                    merged_count += 1
            else:
                # weak — flag only
                flagged_count += len(kg_ids)
                _issue(
                    f"[{label}] {len(kg_ids)} nodes share {fields}={match_key!r} "
                    f"— review manually before merging"
                )

        REPORT["entity_resolution_summary"][label] = {
            "candidates_found": candidates_count,
            "groups":           len(candidates),
            "merged":           merged_count,
            "flagged_weak":     flagged_count,
        }

        action = f"merged {merged_count}" if key_type == "strong" else f"flagged {flagged_count} (weak key — no auto-merge)"
        _enrichment(rule_name, merged_count if key_type == "strong" else flagged_count,
                    category="entity_resolution", detail=f"{detail} — {action}")


# ============================================================
# SECTION 5 — Enrichment / derived relationship rules
# ============================================================

def enrich_coauthor_persons(conn):
    """
    (Person)-[:CO_APPEARED_IN]->(Person)
    Two persons who both appear in the same Document (via HAS_CONTACT) are co-referenced.
    """
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
    """
    (Document)-[:SAME_INDUSTRY]->(Document)
    Documents in the same industry are linked for cross-document querying.
    """
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
    """
    (Person)-[:ASSOCIATED_WITH_DRUG]->(Drug)
    If a Person sent an EmailMessage that mentions a Drug.
    """
    result = conn.query("""
        MATCH (p:Person)<-[:SENT_BY]-(m:EmailMessage)-[:MENTIONS_DRUG]->(dr:Drug)
        MERGE (p)-[:ASSOCIATED_WITH_DRUG]->(dr)
        RETURN count(*) AS c
    """)
    n = result[0]["c"] if result else 0
    _enrichment("ENRICH_PERSON_ASSOC_DRUG", n, category="cross_doc_links",
               detail="(Person)-[:ASSOCIATED_WITH_DRUG]->(Drug): person sent an email mentioning the drug")

def enrich_org_in_same_doc(conn):
    """
    (Organization)-[:CO_MENTIONED_IN]->(Organization)
    Two orgs co-mentioned in the same Document.
    """
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
    """
    (Person)-[:SPEAKS_ABOUT]->(Topic)
    If a person authored/contacted a Document that covers a Topic.
    """
    result = conn.query("""
        MATCH (d:Document)-[:HAS_CONTACT]->(p:Person),
              (d:Document)-[:COVERS_TOPIC]->(t:Topic)
        MERGE (p)-[:SPEAKS_ABOUT]->(t)
        RETURN count(*) AS c
    """)
    n = result[0]["c"] if result else 0
    _enrichment("ENRICH_PERSON_SPEAKS_ABOUT_TOPIC", n, category="cross_doc_links",
               detail="(Person)-[:SPEAKS_ABOUT]->(Topic): person is a contact in a document covering that topic")

def enrich_document_country_label(conn):
    """
    Add a :Country secondary label to Location nodes that are at country level
    (heuristic: all-caps or known country list match).
    This is a lightweight tagging rule, not a full geocoder.
    """
    known_countries = [
        "United States", "Belgium", "Canada", "United Kingdom", "Germany",
        "France", "Japan", "China", "South Korea", "Australia", "India",
        "Brazil", "Netherlands", "Sweden", "Switzerland",
    ]
    for country in known_countries:
        conn.run("""
            MATCH (l:Location {name: $name})
            SET l:Country
        """, name=country)
    # Also tag any location used as output.country directly
    conn.run("""
        MATCH (d:Document)-[:LOCATED_IN]->(l:Location)
        WHERE d.country = l.name AND l.name <> ''
        SET l:Country
    """)
    n = conn.scalar("MATCH (l:Location:Country) RETURN count(l) AS c")
    _enrichment("ENRICH_LOCATION_COUNTRY_LABEL", n, category="node_tagging",
               detail="Added :Country secondary label to known country Location nodes")

def enrich_email_thread_order(conn):
    """
    (EmailMessage)-[:NEXT_IN_THREAD]->(EmailMessage)
    Link consecutive messages in the same thread (same Document, ordered by dateSent).
    """
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
               detail="(EmailMessage)-[:NEXT_IN_THREAD]->(EmailMessage): consecutive messages in same Document ordered by dateSent")

def enrich_xls_org_to_doc(conn):
    """
    (Organization)-[:LISTED_IN]->(Document)
    Organizations from XLS sharedEntities are explicitly listed in those documents.
    """
    result = conn.query("""
        MATCH (d:Document {fileType: 'XLS'})-[:HAS_CONTACT]->(o:Organization)
        MERGE (o)-[:LISTED_IN]->(d)
        RETURN count(*) AS c
    """)
    n = result[0]["c"] if result else 0
    _enrichment("ENRICH_ORG_LISTED_IN_XLS_DOC", n, category="cross_doc_links",
               detail="(Org)-[:LISTED_IN]->(Document): org appeared in XLS sharedEntities")


# ============================================================
# SECTION TXT — TXT-specific checks and enrichments
# ============================================================

def check_text_contents_have_document(conn):
    """Every TextContent must be linked from a Document."""
    n = conn.scalar("""
        MATCH (tc:TextContent)
        WHERE NOT ()-[:HAS_TEXT_CONTENT]->(tc)
        RETURN count(tc) AS c
    """)
    status = "PASS" if n == 0 else "FAIL"
    _check("Unlinked TextContent nodes", status, count=n)
    if n > 0:
        _issue(f"{n} TextContent nodes not linked to any Document")

def check_tabular_columns_have_text_content(conn):
    """Every TabularColumn must belong to a TextContent."""
    n = conn.scalar("""
        MATCH (col:TabularColumn)
        WHERE NOT ()-[:HAS_COLUMN]->(col)
        RETURN count(col) AS c
    """)
    status = "PASS" if n == 0 else "WARN"
    _check("Unlinked TabularColumn nodes", status, count=n)

def check_txt_redaction_coverage(conn):
    """Report how many TXT TextContent nodes have redacted data."""
    n = conn.scalar("""
        MATCH (tc:TextContent {hasRedactions: true})
        RETURN count(tc) AS c
    """)
    total = conn.scalar("MATCH (tc:TextContent) RETURN count(tc) AS c") or 1
    _check("TextContent with redactions", "WARN" if n > 0 else "PASS",
           f"{n}/{total} TextContent nodes contain UCSF-redacted values", n)

def check_txt_documents_have_text_content(conn):
    """Every TXT Document should have at least one HAS_TEXT_CONTENT relationship."""
    n = conn.scalar("""
        MATCH (d:Document {fileType: 'TXT'})
        WHERE NOT (d)-[:HAS_TEXT_CONTENT]->()
        RETURN count(d) AS c
    """)
    status = "PASS" if n == 0 else "WARN"
    _check("TXT Documents missing TextContent", status, count=n)

def check_product_nodes_linked(conn):
    """Every Product should be linked from at least one TextContent."""
    n = conn.scalar("""
        MATCH (p:Product)
        WHERE NOT ()-[:MENTIONS_PRODUCT]->(p)
        RETURN count(p) AS c
    """)
    status = "PASS" if n == 0 else "WARN"
    _check("Orphan Product nodes", status, count=n)

def check_vocab_nodes_linked(conn):
    """Every Vocab node must be linked from at least one Document via HAS_VOCAB."""
    n = conn.scalar("""
        MATCH (v:Vocab)
        WHERE NOT ()-[:HAS_VOCAB]->(v)
        RETURN count(v) AS c
    """)
    status = "PASS" if n == 0 else "WARN"
    _check("Orphan Vocab nodes", status,
           "Vocab nodes with no parent Document", n)
    if n > 0:
        _issue(f"{n} Vocab node(s) not linked to any Document")


def enrich_txt_org_cross_doc(conn):
    """
    (Organization)-[:MENTIONED_IN_TEXT]->(Document)
    Orgs found in TXT TextContent entity lists are tagged as mentioned in that Document.
    """
    result = conn.query("""
        MATCH (d:Document {fileType:'TXT'})-[:HAS_TEXT_CONTENT]->(tc:TextContent)-[:MENTIONS_ORG]->(o:Organization)
        MERGE (o)-[:MENTIONED_IN_TEXT]->(d)
        RETURN count(*) AS c
    """)
    n = result[0]["c"] if result else 0
    _enrichment("ENRICH_TXT_ORG_MENTIONED_IN_DOC", n, category="cross_doc_links",
               detail="(Org)-[:MENTIONED_IN_TEXT]->(Document): org found in TXT entity list")

def enrich_product_org_co_occurrence(conn):
    """
    (Product)-[:CO_OCCURS_WITH]->(Organization)
    Products and Orgs that appear in the same TextContent are likely related
    (e.g. a product sold by a retailer in a sales CSV).
    """
    result = conn.query("""
        MATCH (tc:TextContent)-[:MENTIONS_PRODUCT]->(p:Product),
              (tc:TextContent)-[:MENTIONS_ORG]->(o:Organization)
        MERGE (p)-[r:CO_OCCURS_WITH]->(o)
        ON CREATE SET r.count = 1
        ON MATCH  SET r.count = r.count + 1
        RETURN count(r) AS c
    """)
    n = result[0]["c"] if result else 0
    _enrichment("ENRICH_PRODUCT_ORG_CO_OCCURS_IN_TXT", n, category="co_occurrence",
               detail="(Product)-[:CO_OCCURS_WITH]->(Org): share same TXT TextContent")

def enrich_txt_location_tag(conn):
    """
    Tag Location nodes linked from TXT TextContent with :State label
    if they match known US state names (TXT data is US retail CSV).
    """
    us_states = [
        "Alabama","Alaska","Arizona","Arkansas","California","Colorado","Connecticut",
        "Delaware","Florida","Georgia","Hawaii","Idaho","Illinois","Indiana","Iowa",
        "Kansas","Kentucky","Louisiana","Maine","Maryland","Massachusetts","Michigan",
        "Minnesota","Mississippi","Missouri","Montana","Nebraska","Nevada",
        "New Hampshire","New Jersey","New Mexico","New York","North Carolina",
        "North Dakota","Ohio","Oklahoma","Oregon","Pennsylvania","Rhode Island",
        "South Carolina","South Dakota","Tennessee","Texas","Utah","Vermont",
        "Virginia","Washington","West Virginia","Wisconsin","Wyoming"
    ]
    for state in us_states:
        conn.run("MATCH (l:Location {name: $name}) SET l:State", name=state)
    n = conn.scalar("MATCH (l:Location:State) RETURN count(l) AS c")
    _enrichment("ENRICH_LOCATION_STATE_LABEL", n, category="node_tagging",
               detail="Added :State secondary label to US state Location nodes found in TXT")

def enrich_shared_tabular_columns(conn):
    """
    (TabularColumn)-[:SAME_COLUMN_NAME]->(TabularColumn)
    Link TabularColumn nodes with identical names across different TextContent nodes
    to surface reused schema patterns.
    """
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
# SECTION 6 — Data completeness checks
# ============================================================

def check_persons_missing_name(conn):
    n = conn.scalar("MATCH (p:Person) WHERE p.name IS NULL OR p.name = '' RETURN count(p) AS c")
    status = "PASS" if n == 0 else "WARN"
    _check("Persons missing name", status, count=n)

def check_documents_missing_industry(conn):
    n = conn.scalar("MATCH (d:Document) WHERE d.industry IS NULL OR d.industry = '' RETURN count(d) AS c")
    status = "PASS" if n == 0 else "WARN"
    _check("Documents missing industry", status, count=n)

def check_drugs_missing_generic(conn):
    n = conn.scalar("MATCH (dr:Drug) WHERE dr.genericName IS NULL OR dr.genericName = '' RETURN count(dr) AS c")
    status = "PASS" if n == 0 else "WARN"
    _check("Drugs missing genericName", status, count=n)

def check_claims_missing_subject(conn):
    n = conn.scalar("MATCH (c:Claim) WHERE c.subject IS NULL OR c.subject = '' RETURN count(c) AS c")
    status = "PASS" if n == 0 else "FAIL"
    _check("Claims missing subject", status, count=n)


# ============================================================
# SECTION 7 — Relationship distribution summary
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
        print(f"       {row['relType']:<35} {row['cnt']:>6}")


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Post-KG rules: validate and enrich the Neo4j KG.")
    parser.add_argument("--uri",      default="bolt://localhost:7687")
    parser.add_argument("--user",     default="neo4j")
    parser.add_argument("--password", required=True)
    parser.add_argument("--out",      default="post_kg_report.json")
    parser.add_argument("--skip-merge", action="store_true",
                        help="Skip deduplication merges (safe/read-only mode)")
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
    check_drugs_missing_generic(conn)
    check_claims_missing_subject(conn)

    print("\n[D2] TXT-specific structural checks")
    check_text_contents_have_document(conn)
    check_tabular_columns_have_text_content(conn)
    check_txt_redaction_coverage(conn)
    check_txt_documents_have_text_content(conn)
    check_product_nodes_linked(conn)

    print("\n[D3] Vocab integrity checks")
    check_vocab_nodes_linked(conn)

    if not args.skip_merge:
        resolve_entities(conn, use_apoc=True)
    else:
        print("\n[E] Skipping entity resolution (--skip-merge flag set)")

    print("\n[F] Cross-document & derived enrichments")
    _set_section("cross_doc_links")
    enrich_coauthor_persons(conn)
    enrich_same_industry(conn)
    enrich_drug_mentioned_by_person(conn)
    enrich_org_in_same_doc(conn)
    enrich_person_covers_topic(conn)
    enrich_email_thread_order(conn)
    enrich_xls_org_to_doc(conn)

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

    # Write report
    with open(args.out, "w") as f:
        json.dump(REPORT, f, indent=2, ensure_ascii=False)

    total_checks = len(REPORT["checks"])
    fails  = sum(1 for c in REPORT["checks"] if c["status"] == "FAIL")
    warns  = sum(1 for c in REPORT["checks"] if c["status"] == "WARN")
    passes = sum(1 for c in REPORT["checks"] if c["status"] == "PASS")

    print(f"\n{chr(9473)*50}")
    print(f"  Checks : {total_checks}  \u2705 {passes}  \u26a0\ufe0f  {warns}  \u274c {fails}")
    print(f"  Issues : {len(REPORT['issues'])}")
    print()
    print("  Enrichments by category:")
    for cat, entries in REPORT["enrichments"].items():
        if entries:
            total_affected = sum(e["affected"] for e in entries)
            print(f"    {cat:<20} {len(entries)} rules   {total_affected} nodes/edges affected")
    print()
    print("  Entity resolution:")
    for label, summary in REPORT["entity_resolution_summary"].items():
        if summary["candidates_found"] > 0:
            print(f"    {label:<20} {summary['candidates_found']} candidates   "
                  f"{summary['merged']} merged   {summary['flagged_weak']} flagged")
    print(f"  Report : {args.out}")
    print(f"{chr(9473)*50}")

if __name__ == "__main__":
    main()
