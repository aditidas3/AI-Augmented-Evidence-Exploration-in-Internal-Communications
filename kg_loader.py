"""
kg_loader.py
============
Loads cleaned JSONL files into Neo4j as a Knowledge Graph.

STRONG keys (globally unique - safe to MERGE across documents):
    Document     : record id
    Person       : email address
    Drug         : name (lowercased) — genericName removed from schema
    Organization : name (lowercased)
    GPE          : name (lowercased) — new node type
    EmailMessage : subject + dateSent — identifier removed from EMAIL schema
    Topic        : topic_string (new field name, was plain string)
    Location     : name

WEAK keys (sha256-derived — unique within our dataset but not guaranteed globally):
    Claim, Citation, Abbreviation, LegalFramework, Slide, Sheet,
    TextContent, TabularColumn, Product, Event, Finance, Metric,
    Risk, Requirement, Decision, DateMention, HealthMention,
    SignatureBlock, Figure, Link, CaseContext, SectionDetail,
    TableRegion, PivotTable, Formula, Assessment,
    CellIndex, Identifier, EmbeddedObject, Procedure

"""

import re
import json
import argparse
import hashlib
import logging
from pathlib import Path
from datetime import datetime
from collections import defaultdict

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
log = logging.getLogger("kg_loader")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _s(v, default=""):
    return str(v).strip() if v not in (None, "") else default

def _list(v):
    return v if isinstance(v, list) else []

def _ok(v):
    return _s(v) != ""

def _sha(namespace, *parts):
    """Stable 12-char hex uid from namespace + parts."""
    raw = namespace + "|" + "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:12]

def _kg_id(key_type, *parts):
    """
    Assign a kg_id with key_type tag so post-KG rules know
    whether this node was merged on a strong or weak key.
    key_type examples: 'person_strong', 'person_weak', 'doc_strong', 'org_strong' etc.
    Returns (kg_id_string, entity_type_string, confidence_string).
    The _strong/_weak suffix is kept ONLY inside the hash so existing kg_id values stay stable.
    It is never written as a visible property to Neo4j.
    """
    if "_" in key_type:
        entity_type, confidence = key_type.rsplit("_", 1)
        if confidence not in ("strong", "weak"):
            entity_type, confidence = key_type, "strong"
    else:
        entity_type, confidence = key_type, "strong"
    return _sha(key_type, *parts), entity_type, confidence


def _kt(key_type):
    """Return (entity_type, confidence) from a key_type string — convenience wrapper."""
    _, et, conf = _kg_id(key_type)
    return et, conf


# ---------------------------------------------------------------------------
# APOC detection
# ---------------------------------------------------------------------------

def detect_apoc(session):
    try:
        session.run("RETURN apoc.version() AS v").single()
        log.info("APOC detected — using apoc.merge.node for atomic writes")
        return True
    except Exception:
        log.info("APOC not available — using plain MERGE (safe for serial loads)")
        return False


# ---------------------------------------------------------------------------
# Merge helpers — APOC if available, plain MERGE fallback
# ---------------------------------------------------------------------------

def _merge_node(session, labels, match_props, set_props, use_apoc):
    """
    Merge a single node. Uses APOC if available, plain MERGE otherwise.
    labels     : list of Neo4j labels e.g. ["Person"]
    match_props: dict of properties to MERGE on (the unique key)
    set_props  : dict of all properties to SET after merge
    """
    if use_apoc:
        session.run(
            "CALL apoc.merge.node($labels, $match, $props)",
            labels=labels,
            match=match_props,
            props={**match_props, **set_props},
        )
    else:
        label_str = ":".join(labels)
        match_clause = " AND ".join(f"n.{k} = ${k}" for k in match_props)
        set_clause   = ", ".join(f"n.{k} = $set_{k}" for k in set_props)
        params = {**match_props, **{f"set_{k}": v for k, v in set_props.items()}}
        session.run(
            f"MERGE (n:{label_str} {{{', '.join(f'{k}: ${k}' for k in match_props)}}}) "
            f"SET {set_clause}",
            **params,
        )


def _merge_rel(session, from_label, from_key, from_val,
               to_label, to_key, to_val, rel_type, rel_props=None):
    """Merge a relationship between two already-existing nodes."""
    props_clause = ""
    params = {
        "fromVal": from_val,
        "toVal":   to_val,
    }
    if rel_props:
        props_clause = " SET " + ", ".join(f"r.{k} = $rp_{k}" for k in rel_props)
        params.update({f"rp_{k}": v for k, v in rel_props.items()})
    session.run(
        f"MATCH (a:{from_label} {{{from_key}: $fromVal}}) "
        f"MATCH (b:{to_label}   {{{to_key}:   $toVal}}) "
        f"MERGE (a)-[r:{rel_type}]->(b)"
        f"{props_clause}",
        **params,
    )


# ---------------------------------------------------------------------------
# Batch runner with per-record error handling
# ---------------------------------------------------------------------------

class KGLoader:
    def __init__(self, uri, user, password, dry_run=False, batch_size=500):
        self.dry_run    = dry_run
        self.batch_size = batch_size
        self.stats      = defaultdict(int)
        self.failures   = []

        if not dry_run:
            self.driver = GraphDatabase.driver(uri, auth=(user, password))
            with self.driver.session() as s:
                self.use_apoc = detect_apoc(s)
        else:
            self.driver   = None
            self.use_apoc = False
            log.info("DRY RUN — no Neo4j writes will be made")

    def close(self):
        if self.driver:
            self.driver.close()

    def run(self, query, **params):
        if self.dry_run:
            return
        with self.driver.session() as session:
            session.run(query, **params)

    def run_batch(self, query, rows):
        """Run a Cypher UNWIND query in batches, logging failures per record."""
        if self.dry_run or not rows:
            self.stats["dry_run_skipped"] += len(rows)
            return
        # Extract a human-readable label for each row for the failure log
        def _row_label(row):
            for key in ("kg_id", "recordId", "uid", "name", "identifier", "text"):
                if row.get(key):
                    return f"{key}={row[key]}"
            return str(row)[:80]

        with self.driver.session() as session:
            for i in range(0, len(rows), self.batch_size):
                batch = rows[i : i + self.batch_size]
                try:
                    session.run(query, rows=batch)
                    self.stats["rows_written"] += len(batch)
                except Neo4jError as e:
                    # Batch failed — retry one by one to isolate bad records
                    log.warning(f"Batch error, retrying individually: {e.message[:80]}")
                    for row in batch:
                        try:
                            session.run(query, rows=[row])
                            self.stats["rows_written"] += 1
                        except Neo4jError as row_err:
                            self.stats["rows_failed"] += 1
                            failure = {
                                "record":        _row_label(row),
                                "stage":         query.strip().splitlines()[1].strip(),
                                "neo4j_code":    row_err.code,
                                "neo4j_message": row_err.message,
                            }
                            self.failures.append(failure)
                            log.error(
                                f"Row failed | record={_row_label(row)} | "
                                f"code={row_err.code} | reason={row_err.message[:120]}"
                            )

    def run_merge(self, session, labels, match_props, set_props):
        """Merge a node using APOC or plain MERGE with failure isolation."""
        if self.dry_run:
            return
        try:
            _merge_node(session, labels, match_props, set_props, self.use_apoc)
            self.stats["rows_written"] += 1
        except Neo4jError as e:
            self.stats["rows_failed"] += 1
            self.failures.append({
                "labels":      labels,
                "match_props": match_props,
                "error":       str(e),
            })
            log.error(f"Merge failed for {labels} {match_props}: {e.message[:100]}")


# ---------------------------------------------------------------------------
# Schema constraints + indexes
# ---------------------------------------------------------------------------

CONSTRAINTS = [
    # Strong-key nodes — constraint on the natural key field
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:Document)      REQUIRE n.kg_id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:Person)        REQUIRE n.kg_id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:Organization)  REQUIRE n.kg_id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:Drug)          REQUIRE n.kg_id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:GPE)           REQUIRE n.kg_id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:Topic)         REQUIRE n.kg_id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:Location)      REQUIRE n.kg_id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:EmailMessage)  REQUIRE n.kg_id IS UNIQUE",
    # Weak-key nodes
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:Claim)         REQUIRE n.kg_id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:Citation)      REQUIRE n.kg_id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:Abbreviation)  REQUIRE n.kg_id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:LegalFramework)REQUIRE n.kg_id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:Slide)         REQUIRE n.kg_id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:Sheet)         REQUIRE n.kg_id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:TextContent)   REQUIRE n.kg_id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:TabularColumn) REQUIRE n.kg_id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:Product)       REQUIRE n.kg_id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:Event)         REQUIRE n.kg_id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:Finance)       REQUIRE n.kg_id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:Metric)        REQUIRE n.kg_id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:Risk)          REQUIRE n.kg_id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:Requirement)   REQUIRE n.kg_id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:Decision)      REQUIRE n.kg_id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:DateMention)   REQUIRE n.kg_id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:SignatureBlock)REQUIRE n.kg_id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:TableRegion)   REQUIRE n.kg_id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:PivotTable)    REQUIRE n.kg_id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:Formula)       REQUIRE n.kg_id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:Assessment)    REQUIRE n.kg_id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:Figure)        REQUIRE n.kg_id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:Link)          REQUIRE n.kg_id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:CaseContext)   REQUIRE n.kg_id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:SectionDetail) REQUIRE n.kg_id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:Identifier)    REQUIRE n.kg_id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:EmbeddedObject)REQUIRE n.kg_id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:Procedure)     REQUIRE n.kg_id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:CellIndex)     REQUIRE n.kg_id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:HealthMention) REQUIRE n.kg_id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:Vocab)         REQUIRE n.kg_id IS UNIQUE",
]

INDEXES = [
    "CREATE INDEX IF NOT EXISTS FOR (n:Document)     ON (n.batesNumber)",
    "CREATE INDEX IF NOT EXISTS FOR (n:Document)     ON (n.sourceFileType)",
    "CREATE INDEX IF NOT EXISTS FOR (n:Document)     ON (n.industry)",
    "CREATE INDEX IF NOT EXISTS FOR (n:Document)     ON (n.collection)",
    "CREATE INDEX IF NOT EXISTS FOR (n:Document)     ON (n.kg_key_type)",
    "CREATE INDEX IF NOT EXISTS FOR (n:Person)       ON (n.name)",
    "CREATE INDEX IF NOT EXISTS FOR (n:Person)       ON (n.email)",
    "CREATE INDEX IF NOT EXISTS FOR (n:Person)       ON (n.kg_key_type)",
    "CREATE INDEX IF NOT EXISTS FOR (n:Organization) ON (n.name)",
    "CREATE INDEX IF NOT EXISTS FOR (n:Drug)         ON (n.name)",
    "CREATE INDEX IF NOT EXISTS FOR (n:Drug)         ON (n.genericName)",
    "CREATE INDEX IF NOT EXISTS FOR (n:Product)      ON (n.name)",
    "CREATE INDEX IF NOT EXISTS FOR (n:TextContent)  ON (n.textDocumentId)",
    "CREATE INDEX IF NOT EXISTS FOR (n:Vocab)         ON (n.name)",
]

def setup_schema(loader):
    log.info("Setting up constraints and indexes...")
    for cql in CONSTRAINTS:
        loader.run(cql)
    for cql in INDEXES:
        loader.run(cql)
    log.info("Schema ready.")


# ---------------------------------------------------------------------------
# JSONL loader
# ---------------------------------------------------------------------------

def load_jsonl(path, limit=None):
    with open(path, encoding="utf-8") as f:
        content = f.read().strip()
    if content.startswith("["):
        records = json.loads(content)
        if not isinstance(records, list):
            records = [records]
    else:
        records = [json.loads(line) for line in content.splitlines() if line.strip()]
    return records[:limit] if limit else records


# ===========================================================================
# NODE LOADERS
# Each loader assigns kg_id (the MERGE key) and kg_key_type (strong/weak)
# ===========================================================================

# ---------------------------------------------------------------------------
# Document
# Strong key: bates_number > record id
# ---------------------------------------------------------------------------

UPSERT_DOCUMENT = """
UNWIND $rows AS r
MERGE (d:Document {kg_id: r.kg_id})
SET d.kg_key_type     = r.kg_key_type,
    d.confidence      = r.confidence,
    d.uid             = r.uid,
    d.recordId        = r.recordId,
    d.fileType        = r.fileType,
    d.url             = r.url,
    d.documentDate    = r.documentDate,
    d.type            = r.type,
    d.industry        = r.industry,
    d.language        = r.language,
    d.summary         = r.summary,
    d.batesNumber     = r.batesNumber,
    d.collection      = r.collection,
    d.source          = r.source,
    d.tid             = r.tid,
    d.dateAdded       = r.dateAdded,
    d.legalStatus     = r.legalStatus,
    d.sourceFileName  = r.sourceFileName,
    d.sourceFileType  = r.sourceFileType,
    d.sourceFileHash  = r.sourceFileHash,
    d.sourceFilePageCount = r.sourceFilePageCount,
    d.confidentialityNotice = r.confidentialityNotice
"""

def load_documents(loader, records, file_type):
    rows = []
    for rec in records:
        out    = rec.get("output", {})
        sf     = out.get("sourceFile", {}) or {}
        bates  = _s(out.get("bates_number"))
        rec_id = _s(rec.get("id"))
        # Strong key: record id is always present and pipeline-controlled.
        # bates_number may be absent — kept as a property but not the MERGE key.
        kg_id, kg_key_type, confidence = _kg_id("doc_strong", file_type, rec_id)
        rows.append({
            "kg_id":           kg_id,
            "kg_key_type":     kg_key_type,
            "uid":             _s(rec.get("_doc_uid", rec_id)),
            "recordId":        rec_id,
            "fileType":        file_type,
            "url":             _s(out.get("url")),
            "documentDate":    _s(out.get("documentDate")),
            "type":            _s(out.get("type")),
            "industry":        _s(out.get("industry")),
            "language":        _s(out.get("language", "")),  
            "summary":         _s(out.get("summary")),
            "batesNumber":     bates,
            "collection":      _s(out.get("collection")),
            "source":          _s(out.get("source")),
            "tid":             _s(out.get("tid")),
            "dateAdded":       _s(out.get("dateAdded", out.get("dateAddedUCSF", ""))),
            "legalStatus":     _s(out.get("legalStatus", "")),
            "sourceFileName":  _s(sf.get("fileName")),
            "sourceFileType":  _s(sf.get("fileType")),
            "sourceFileHash":  _s(sf.get("hash")),
            "sourceFilePageCount": _s(sf.get("pageCount")),
            "confidentialityNotice": _s(
                out.get("confidentiality_notice", out.get("confidentialityNotice", ""))
            ),
        })
    loader.run_batch(UPSERT_DOCUMENT, rows)
    log.info(f"  Upserted {len(rows)} Document nodes [{file_type}]")
    return {r["recordId"]: r["kg_id"] for r in rows}


# ---------------------------------------------------------------------------
# Person
# Strong key: email address (if valid); weak key: sha256(name)
# ---------------------------------------------------------------------------

UPSERT_PERSON = """
UNWIND $rows AS r
MERGE (p:Person {kg_id: r.kg_id})
SET p.kg_key_type    = r.kg_key_type,
    p.confidence     = r.confidence,
    p.uid            = r.uid,
    p.name           = r.name,
    p.email          = r.email,
    p.phone          = r.phone,
    p.role           = r.role,
    p.address        = r.address,
    p.organization   = r.organization,
    p.witnessContext = r.witnessContext
"""

LINK_DOC_PERSON = """
UNWIND $rows AS r
MATCH (d:Document {kg_id: r.docKgId})
MATCH (p:Person   {kg_id: r.personKgId})
MERGE (d)-[:HAS_CONTACT]->(p)
"""

LINK_PERSON_ORG = """
UNWIND $rows AS r
MATCH (p:Person       {kg_id: r.personKgId})
MATCH (o:Organization {kg_id: r.orgKgId})
MERGE (p)-[:WORKS_FOR]->(o)
"""

def _person_kg_id(email, name, org=""):
    """Strong key if email present, weak key otherwise.
    Returns (kg_id, confidence) where confidence is 'strong' or 'weak'."""
    email = _s(email).lower()
    if email and "@" in email:
        kg_id, _, confidence = _kg_id("person_strong", email)
    else:
        normalized_org = _normalize_org_name(org)
        kg_id, _, confidence = _kg_id("person_weak", _s(name).lower(), normalized_org)
    return kg_id, confidence

def _collect_persons(out, doc_kg_id, source):
    """Yield (person_row, doc_kg_id) from any person-bearing structure."""
    people = []
    if source == "contacts":
        for c in _list(out.get("contacts", [])):
            if _s(c.get("contact_type", "individual")) != "individual":
                continue
            kg_id, confidence = _person_kg_id(c.get("email"), c.get("name"), c.get("organization"))
            people.append(({"kg_id": kg_id, "kg_key_type": "person", "confidence": confidence,
                             "uid": _s(c.get("_uid", kg_id)),
                             "name": _s(c.get("name")), "email": _s(c.get("email")),
                             "phone": _s(c.get("phone")), "role": _s(c.get("role")),
                             "address": _s(c.get("address")),
                             "organization": _s(c.get("organization")),
                             "witnessContext": _s(c.get("witness_context", ""))}, doc_kg_id))
    elif source == "hasPart":
        for msg in _list(out.get("hasPart", [])):
            sender = msg.get("sender", {})
            if isinstance(sender, dict):
                kg_id, confidence = _person_kg_id(sender.get("email"), sender.get("name"))
                people.append(({"kg_id": kg_id, "kg_key_type": "person", "confidence": confidence,
                                 "uid": _s(sender.get("_uid", kg_id)),
                                 "name": _s(sender.get("name")),
                                 "email": _s(sender.get("email")),
                                 "phone": "", "role": "", "address": "",
                                 "organization": "",
                                 "witnessContext": ""}, doc_kg_id))
            for r_obj in _list(msg.get("recipient", [])):
                if isinstance(r_obj, dict):
                    kg_id, confidence = _person_kg_id(r_obj.get("email"), r_obj.get("name"))
                    people.append(({"kg_id": kg_id, "kg_key_type": "person", "confidence": confidence,
                                    "uid": _s(r_obj.get("_uid", kg_id)),
                                    "name": _s(r_obj.get("name")),
                                    "email": _s(r_obj.get("email")),
                                    "phone": "", "role": "", "address": "",
                                    "organization": "",
                                    "witnessContext": ""}, doc_kg_id))
    elif source == "sharedEntities":
        for p in _list(out.get("sharedEntities", {}).get("people", [])):
            if not isinstance(p, dict):
                continue
            kg_id, confidence = _person_kg_id(p.get("email"), p.get("name"), p.get("organization"))
            people.append(({"kg_id": kg_id, "kg_key_type": "person", "confidence": confidence,
                             "uid": _s(p.get("_uid", kg_id)),
                             "name": _s(p.get("name")), "email": _s(p.get("email")),
                             "phone": _s(p.get("phone")), "role": _s(p.get("role")),
                             "address": _s(p.get("address")),
                             "organization": _s(p.get("organization")),
                             "witnessContext": _s(p.get("witness_context", ""))}, doc_kg_id))
    elif source == "author":
        for a in _list(out.get("author", [])):
            if not isinstance(a, dict):
                continue
            kg_id, confidence = _person_kg_id(a.get("email"), a.get("name"), a.get("organization"))
            people.append(({"kg_id": kg_id, "kg_key_type": "person", "confidence": confidence,
                             "uid": _s(a.get("_uid", kg_id)),
                             "name": _s(a.get("name")), "email": _s(a.get("email")),
                             "phone": _s(a.get("phone", "")), "role": "",
                             "address": _s(a.get("address", "")),
                             "organization": _s(a.get("organization", "")),
                             "witnessContext": ""}, doc_kg_id))
    return people

def load_persons(loader, records, file_type, doc_kg_id_map, org_kg_id_map):
    person_rows, link_rows, porg_rows = [], [], []
    for rec in records:
        doc_kg_id = doc_kg_id_map.get(_s(rec.get("id")), "")
        out = rec.get("output", {})
        sources = []
        # contacts[] on DOC and TXT only (legal doc types)
        if file_type in ("DOC", "TXT"):
            sources = ["contacts"]
        elif file_type == "EMAIL":
            sources = ["hasPart"]
        # author[] on all types — new object array
        sources.append("author")

        for src in sources:
            for p_row, d_kg_id in _collect_persons(out, doc_kg_id, src):
                if not p_row["kg_id"]:
                    continue
                person_rows.append(p_row)
                link_rows.append({"docKgId": d_kg_id, "personKgId": p_row["kg_id"]})
                org_name = p_row.get("organization", "").lower()
                if org_name and org_name in org_kg_id_map:
                    porg_rows.append({
                        "personKgId": p_row["kg_id"],
                        "orgKgId":    org_kg_id_map[org_name],
                    })

    if person_rows:
        loader.run_batch(UPSERT_PERSON, person_rows)
        loader.run_batch(LINK_DOC_PERSON, link_rows)
    if porg_rows:
        loader.run_batch(LINK_PERSON_ORG, porg_rows)
    log.info(f"  Upserted {len(person_rows)} Person nodes [{file_type}]")


# ---------------------------------------------------------------------------
# AUTHORED_BY
# Edge: Document -[AUTHORED_BY]-> Person
# Source: output.author[] — present on all 5 types
# ---------------------------------------------------------------------------

LINK_AUTHORED_BY = """
UNWIND $rows AS r
MATCH (d:Document {kg_id: r.docKgId})
MATCH (p:Person   {kg_id: r.personKgId})
MERGE (d)-[:AUTHORED_BY]->(p)
"""

def load_authored_by(loader, records, file_type, doc_kg_id_map):
    link_rows = []
    for rec in records:
        doc_kg_id = doc_kg_id_map.get(_s(rec.get("id")), "")
        if not doc_kg_id:
            continue
        for author in _list(rec.get("output", {}).get("author", [])):
            if not isinstance(author, dict):
                continue
            email = _s(author.get("email", "")).lower()
            email = email if "@" in email else ""
            name  = _s(author.get("name")).lower()
            org   = _s(author.get("organization")).lower()
            key   = email if email else f"{name}|{org}"
            p_kg_id = _kg_id("person_strong", email)[0] if email else \
                      _kg_id("person_weak", name, _normalize_org_name(org))[0]
            if p_kg_id:
                link_rows.append({"docKgId": doc_kg_id, "personKgId": p_kg_id})
    if link_rows:
        loader.run_batch(LINK_AUTHORED_BY, link_rows)
    log.info(f"  Wrote {len(link_rows)} AUTHORED_BY edges [{file_type}]")


# ---------------------------------------------------------------------------
# Organization
# Strong key: lowercased name
# ---------------------------------------------------------------------------

UPSERT_ORG = """
UNWIND $rows AS r
MERGE (o:Organization {kg_id: r.kg_id})
SET o.kg_key_type    = r.kg_key_type,
    o.confidence     = r.confidence,
    o.uid            = r.uid,
    o.name           = r.name,
    o.witnessContext = r.witnessContext
"""

LINK_DOC_ORG = """
UNWIND $rows AS r
MATCH (d:Document     {kg_id: r.docKgId})
MATCH (o:Organization {kg_id: r.orgKgId})
MERGE (d)-[:MENTIONS_ORG]->(o)
"""

_ORG_SUFFIXES = re.compile(
    r"""[,\s]+(inc\.?|incorporated|corp\.?|corporation|ltd\.?|limited|
    llc\.?|llp\.?|plc\.?|gmbh|ag|sa|bv|nv|pty\.?|co\.?|company|
    group|holdings?|international|intl\.?|enterprises?|associates?|
    partners?|services?|solutions?|technologies?|tech\.?|labs?|
    laboratory|laboratories)\.?$""",
    re.IGNORECASE | re.VERBOSE,
)

def _normalize_org_name(name):
    """Strip legal suffixes and punctuation for consistent org key generation."""
    name = _s(name).strip()
    if not name:
        return ""
    # Iteratively strip suffixes (e.g. "JUUL Labs, Inc." -> "JUUL Labs")
    prev = None
    while prev != name:
        prev = name
        name = _ORG_SUFFIXES.sub("", name).strip().rstrip(",").strip()
    return name.lower()

def _org_kg_id(name):
    normalized = _normalize_org_name(name)
    if not normalized:
        return "", "strong"
    kg_id, _, confidence = _kg_id("org_strong", normalized)
    return kg_id, confidence

def load_orgs(loader, records, file_type, doc_kg_id_map):
    org_rows, link_rows = [], []
    org_kg_id_map = {}

    for rec in records:
        doc_kg_id = doc_kg_id_map.get(_s(rec.get("id")), "")
        out = rec.get("output", {})

        # Collect org objects keyed by name — last witness_context seen wins
        org_map = {}  # normalized_name -> {"name": ..., "witnessContext": ...}

        for contact in _list(out.get("contacts", [])):
            if _s(contact.get("contact_type")) == "organization":
                n = _s(contact.get("name"))
                if n:
                    org_map[n.lower()] = {"name": n, "witnessContext": ""}
            if _ok(contact.get("organization")):
                n = _s(contact.get("organization"))
                if n:
                    org_map.setdefault(n.lower(), {"name": n, "witnessContext": ""})

        for hc in _list(out.get("hasContent") if isinstance(out.get("hasContent"), list) else []):
            if isinstance(hc, dict):
                for org in _list(hc.get("entities", {}).get("organizations", [])):
                    if isinstance(org, dict):
                        n = _s(org.get("name", ""))
                        if n:
                            org_map[n.lower()] = {"name": n,
                                "witnessContext": _s(org.get("witness_context", ""))}
                    else:
                        n = _s(org)
                        if n:
                            org_map.setdefault(n.lower(), {"name": n, "witnessContext": ""})

        for msg in _list(out.get("hasPart", [])):
            for org in _list(msg.get("entities", {}).get("organizations", [])):
                if isinstance(org, dict):
                    n = _s(org.get("name", ""))
                    if n:
                        org_map[n.lower()] = {"name": n,
                            "witnessContext": _s(org.get("witness_context", ""))}
                else:
                    n = _s(org)
                    if n:
                        org_map.setdefault(n.lower(), {"name": n, "witnessContext": ""})

        hc_ppt = out.get("hasContent", {})
        if isinstance(hc_ppt, dict):
            for slide in _list(hc_ppt.get("slides", [])):
                for org in _list(slide.get("entities", {}).get("organizations", [])):
                    if isinstance(org, dict):
                        n = _s(org.get("name", ""))
                        if n:
                            org_map[n.lower()] = {"name": n,
                                "witnessContext": _s(org.get("witness_context", ""))}
                    else:
                        n = _s(org)
                        if n:
                            org_map.setdefault(n.lower(), {"name": n, "witnessContext": ""})

        for norm_name, org_obj in org_map.items():
            name = org_obj["name"]
            kg_id, confidence = _org_kg_id(name)
            if not kg_id:
                continue
            org_kg_id_map[norm_name] = kg_id
            org_rows.append({"kg_id": kg_id, "kg_key_type": "organization", "confidence": confidence,
                              "uid": kg_id, "name": name,
                              "witnessContext": org_obj["witnessContext"]})
            link_rows.append({"docKgId": doc_kg_id, "orgKgId": kg_id})

    if org_rows:
        loader.run_batch(UPSERT_ORG, org_rows)
        loader.run_batch(LINK_DOC_ORG, link_rows)
    log.info(f"  Upserted {len(set(r['kg_id'] for r in org_rows))} Organization nodes [{file_type}]")
    return org_kg_id_map


# ---------------------------------------------------------------------------
# Drug
# Strong key: name (lowercased)
# ---------------------------------------------------------------------------

UPSERT_DRUG = """
UNWIND $rows AS r
MERGE (d:Drug {kg_id: r.kg_id})
SET d.kg_key_type    = r.kg_key_type,
    d.confidence     = r.confidence,
    d.uid            = r.uid,
    d.name           = r.name,
    d.description    = r.description,
    d.witnessContext  = r.witnessContext
"""

LINK_DOC_DRUG = """
UNWIND $rows AS r
MATCH (doc:Document {kg_id: r.docKgId})
MATCH (dr:Drug      {kg_id: r.drugKgId})
MERGE (doc)-[:MENTIONS_DRUG]->(dr)
"""

def _drug_kg_id(drug):
    name = _s(drug.get("name")).lower()
    if not name:
        return "", ""
    kg_id, _, confidence = _kg_id("drug_strong", name)
    return kg_id, confidence

def load_drugs(loader, records, file_type, doc_kg_id_map):
    drug_rows, link_rows = [], []
    for rec in records:
        doc_kg_id = doc_kg_id_map.get(_s(rec.get("id")), "")
        out = rec.get("output", {})
        drugs = []
        # All types: hasContent[].entities.drugs[]
        for hc in _list(out.get("hasContent") if isinstance(out.get("hasContent"), list) else []):
            if isinstance(hc, dict):
                drugs += _list(hc.get("entities", {}).get("drugs", []))
        # PPT: hasContent.slides[].entities.drugs[]
        hc_ppt = out.get("hasContent", {})
        if isinstance(hc_ppt, dict):
            for slide in _list(hc_ppt.get("slides", [])):
                drugs += _list(slide.get("entities", {}).get("drugs", []))
        # EMAIL: hasPart[].entities.drugs[]
        for msg in _list(out.get("hasPart", [])):
            drugs += _list(msg.get("entities", {}).get("drugs", []))
        for drug in drugs:
            if not isinstance(drug, dict):
                continue
            kg_id, confidence = _drug_kg_id(drug)
            if not kg_id:
                continue
            drug_rows.append({
                "kg_id":          kg_id,
                "kg_key_type":    "drug", "confidence": confidence,
                "uid":            _s(drug.get("_uid", kg_id)),
                "name":           _s(drug.get("name")),
                "description":    _s(drug.get("description")),
                "witnessContext": _s(drug.get("witness_context")),
            })
            link_rows.append({"docKgId": doc_kg_id, "drugKgId": kg_id})
    if drug_rows:
        loader.run_batch(UPSERT_DRUG, drug_rows)
        loader.run_batch(LINK_DOC_DRUG, link_rows)
    log.info(f"  Upserted {len(drug_rows)} Drug references [{file_type}]")


# ---------------------------------------------------------------------------
# EmailMessage
# Strong key: subject + dateSent
# ---------------------------------------------------------------------------

UPSERT_EMAIL_MSG = """
UNWIND $rows AS r
MERGE (m:EmailMessage {kg_id: r.kg_id})
SET m.kg_key_type  = r.kg_key_type,
    m.confidence   = r.confidence,
    m.uid          = r.uid,
    m.subject      = r.subject,
    m.dateSent     = r.dateSent,
    m.body         = r.body,
    m.semanticType = r.semanticType
"""

LINK_DOC_MSG = """
UNWIND $rows AS r
MATCH (d:Document     {kg_id: r.docKgId})
MATCH (m:EmailMessage {kg_id: r.msgKgId})
MERGE (d)-[:HAS_MESSAGE]->(m)
"""

LINK_MSG_PERSON = """
UNWIND $rows AS r
MATCH (m:EmailMessage {kg_id: r.msgKgId})
MATCH (p:Person       {kg_id: r.personKgId})
MERGE (m)-[:{rel_type}]->(p)
"""

def load_email_messages(loader, records, doc_kg_id_map):
    msg_rows, doc_link, sent_by, sent_to = [], [], [], []
    for rec in records:
        doc_kg_id = doc_kg_id_map.get(_s(rec.get("id")), "")
        out = rec.get("output", {})
        for msg in _list(out.get("hasPart", [])):
            subj  = _s(msg.get("subject"))
            sent  = _s(msg.get("dateSent"))
            # identifier removed from EMAIL schema — key on subject+dateSent
            kg_id, _, confidence = _kg_id("msg_strong", subj, sent)
            msg_rows.append({
                "kg_id": kg_id, "kg_key_type": "msg", "confidence": confidence,
                "uid": _s(msg.get("_uid", kg_id)),
                "subject": subj, "dateSent": sent,
                "body": _s(msg.get("body", ""))[:2000],
                "semanticType": _s(msg.get("semantic_type")),
            })
            doc_link.append({"docKgId": doc_kg_id, "msgKgId": kg_id})
            sender = msg.get("sender", {})
            if isinstance(sender, dict):
                p_kg_id, _ = _person_kg_id(sender.get("email"), sender.get("name"))
                if p_kg_id:
                    sent_by.append({"msgKgId": kg_id, "personKgId": p_kg_id})
            for r_obj in _list(msg.get("recipient", [])):
                if isinstance(r_obj, dict):
                    p_kg_id, _ = _person_kg_id(r_obj.get("email"), r_obj.get("name"))
                    if p_kg_id:
                        sent_to.append({"msgKgId": kg_id, "personKgId": p_kg_id})

    if msg_rows:
        loader.run_batch(UPSERT_EMAIL_MSG, msg_rows)
        loader.run_batch(LINK_DOC_MSG, doc_link)
    if sent_by:
        loader.run_batch("""
            UNWIND $rows AS r
            MATCH (m:EmailMessage {kg_id: r.msgKgId})
            MATCH (p:Person       {kg_id: r.personKgId})
            MERGE (m)-[:SENT_BY]->(p)
        """, sent_by)
    if sent_to:
        loader.run_batch("""
            UNWIND $rows AS r
            MATCH (m:EmailMessage {kg_id: r.msgKgId})
            MATCH (p:Person       {kg_id: r.personKgId})
            MERGE (m)-[:SENT_TO]->(p)
        """, sent_to)
    log.info(f"  Upserted {len(msg_rows)} EmailMessage nodes")


# ---------------------------------------------------------------------------
# Generic weak-key nodes (Claim, Citation, Abbreviation, LegalFramework,
# Slide, Sheet, TextContent, TabularColumn, Product, Event, Finance,
# Metric, Risk, Requirement, Decision, DateMention, SignatureBlock, etc.)
# All use sha256-based kg_id, all MERGE on kg_id.
# ---------------------------------------------------------------------------

def _generic_upsert_query(label, props):
    """Build a generic UNWIND MERGE query for a node label."""
    set_lines = "\n".join(f"    n.{p} = r.{p}," for p in props)
    set_lines = set_lines.rstrip(",")
    return f"""
UNWIND $rows AS r
MERGE (n:{label} {{kg_id: r.kg_id}})
SET n.kg_key_type = r.kg_key_type,
    n.confidence  = r.confidence,
{set_lines}
"""

def _generic_link_query(from_label, rel_type, to_label):
    return f"""
UNWIND $rows AS r
MATCH (a:{from_label} {{kg_id: r.fromKgId}})
MATCH (b:{to_label}   {{kg_id: r.toKgId}})
MERGE (a)-[:{rel_type}]->(b)
"""

# Claim
def load_claims(loader, records, doc_kg_id_map):
    rows, links = [], []
    for rec in records:
        doc_kg_id = doc_kg_id_map.get(_s(rec.get("id")), "")
        for claim in _list(rec.get("output", {}).get("claims", [])):
            if not isinstance(claim, dict):
                continue
            text = _s(claim.get("claim_text"))
            kg_id, kt, confidence = _kg_id("claim_weak", doc_kg_id, text[:80])
            rows.append({"kg_id": kg_id, "kg_key_type": kt, "confidence": confidence,
                         "text": text, "subject": _s(claim.get("subject")),
                         "qualifier": _s(claim.get("qualifier")),
                         "metric": _s(claim.get("metric")),
                         "value": _s(claim.get("value")),
                         "unit": _s(claim.get("unit")),
                         "context": _s(claim.get("context"))})
            links.append({"fromKgId": doc_kg_id, "toKgId": kg_id})
    if rows:
        loader.run_batch(_generic_upsert_query("Claim",
            ["text","subject","qualifier","metric","value","unit","context"]), rows)
        loader.run_batch(_generic_link_query("Document","HAS_CLAIM","Claim"), links)
    log.info(f"  Upserted {len(rows)} Claim nodes")

# LegalFramework
def load_legal_frameworks(loader, records, file_type, doc_kg_id_map):
    rows, links = [], []
    for rec in records:
        doc_kg_id = doc_kg_id_map.get(_s(rec.get("id")), "")
        lf = rec.get("output", {}).get("legalFramework", {})
        if not isinstance(lf, dict) or not (_ok(lf.get("type")) or _ok(lf.get("description"))):
            continue
        lf_type = _s(lf.get("type"))
        desc    = _s(lf.get("description"))
        kg_id, kt, confidence = _kg_id("lf_weak", lf_type, desc[:60])
        rows.append({"kg_id": kg_id, "kg_key_type": kt, "confidence": confidence,
                     "type": lf_type, "description": desc})
        links.append({"fromKgId": doc_kg_id, "toKgId": kg_id})
    if rows:
        loader.run_batch(_generic_upsert_query("LegalFramework", ["type","description"]), rows)
        loader.run_batch(_generic_link_query("Document","HAS_LEGAL_FRAMEWORK","LegalFramework"), links)
    log.info(f"  Upserted {len(rows)} LegalFramework nodes [{file_type}]")

# Abbreviation
def load_abbreviations(loader, records, file_type, doc_kg_id_map):
    rows, links = [], []
    for rec in records:
        doc_kg_id = doc_kg_id_map.get(_s(rec.get("id")), "")
        for abbr in _list(rec.get("output", {}).get("abbreviations", [])):
            if not isinstance(abbr, dict):
                continue
            name = _s(abbr.get("abbv_name"))
            if not name:
                continue
            kg_id, kt, confidence = _kg_id("abbr_weak", name.upper(), _s(abbr.get("full_form")))
            rows.append({"kg_id": kg_id, "kg_key_type": kt, "confidence": confidence,
                         "abbvName": name, "fullForm": _s(abbr.get("full_form")),
                         "description": _s(abbr.get("description")),
                         "context": _s(abbr.get("context"))})
            links.append({"fromKgId": doc_kg_id, "toKgId": kg_id,
                          "pageNumber": abbr.get("pageNumber", 0)})
    if rows:
        loader.run_batch(_generic_upsert_query("Abbreviation",
            ["abbvName","fullForm","description","context"]), rows)
        loader.run_batch("""
            UNWIND $rows AS r
            MATCH (d:Document     {kg_id: r.fromKgId})
            MATCH (a:Abbreviation {kg_id: r.toKgId})
            MERGE (d)-[rel:HAS_ABBREVIATION]->(a)
            SET rel.pageNumber = r.pageNumber
        """, links)
    log.info(f"  Upserted {len(rows)} Abbreviation nodes [{file_type}]")

# Citation
def load_citations(loader, records, doc_kg_id_map):
    rows, links = [], []
    for rec in records:
        doc_kg_id = doc_kg_id_map.get(_s(rec.get("id")), "")
        for cit in _list(rec.get("output", {}).get("bibliography", [])):
            if not isinstance(cit, dict):
                continue
            doi   = _s(cit.get("doi"))
            title = _s(cit.get("title"))
            kg_id, kt, confidence = _kg_id("cit_weak", doi or title)
            rows.append({"kg_id": kg_id, "kg_key_type": kt, "confidence": confidence,
                         "title": title, "doi": doi,
                         "publisher": _s(cit.get("publisher")),
                         "publicationDate": _s(cit.get("publication_date")),
                         "url": _s(cit.get("url")),
                         "citationText": _s(cit.get("citation_text")),
                         "notes": _s(cit.get("notes"))})
            links.append({"fromKgId": doc_kg_id, "toKgId": kg_id,
                          "pageNumber": cit.get("pageNumber", 0)})
    if rows:
        loader.run_batch(_generic_upsert_query("Citation",
            ["title","doi","publisher","publicationDate","url","citationText","notes"]), rows)
        loader.run_batch("""
            UNWIND $rows AS r
            MATCH (d:Document {kg_id: r.fromKgId})
            MATCH (c:Citation {kg_id: r.toKgId})
            MERGE (d)-[rel:CITES]->(c)
            SET rel.pageNumber = r.pageNumber
        """, links)
    log.info(f"  Upserted {len(rows)} Citation nodes")

# Location (strong key: name)
UPSERT_LOCATION = """
UNWIND $rows AS r
MERGE (l:Location {kg_id: r.kg_id})
SET l.kg_key_type    = r.kg_key_type,
    l.confidence     = r.confidence,
    l.name           = r.name,
    l.address        = r.address,
    l.witnessContext = r.witnessContext
"""
def load_locations(loader, records, file_type, doc_kg_id_map):
    rows, links = [], []
    for rec in records:
        doc_kg_id = doc_kg_id_map.get(_s(rec.get("id")), "")
        out = rec.get("output", {})
        # All types: hasContent[].entities.locations[]
        for hc in _list(out.get("hasContent") if isinstance(out.get("hasContent"), list) else []):
            if isinstance(hc, dict):
                for loc in _list(hc.get("entities", {}).get("locations", [])):
                    name = _s(loc.get("name") if isinstance(loc, dict) else loc)
                    if not name:
                        continue
                    kg_id, kt, confidence = _kg_id("loc_strong", name.lower())
                    rows.append({"kg_id": kg_id, "kg_key_type": kt, "confidence": confidence, "name": name,
                                 "address": _s(loc.get("address", "") if isinstance(loc, dict) else ""),
                                 "witnessContext": _s(loc.get("witness_context", "") if isinstance(loc, dict) else "")})
                    links.append({"fromKgId": doc_kg_id, "toKgId": kg_id})
        # EMAIL hasPart[].entities.locations[]
        for msg in _list(out.get("hasPart", [])):
            for loc in _list(msg.get("entities", {}).get("locations", [])):
                name = _s(loc.get("name") if isinstance(loc, dict) else loc)
                if not name:
                    continue
                kg_id, kt, confidence = _kg_id("loc_strong", name.lower())
                rows.append({"kg_id": kg_id, "kg_key_type": kt, "confidence": confidence, "name": name,
                             "address": _s(loc.get("address", "") if isinstance(loc, dict) else ""),
                             "witnessContext": _s(loc.get("witness_context", "") if isinstance(loc, dict) else "")})
                links.append({"fromKgId": doc_kg_id, "toKgId": kg_id})
        # PPT slides entities locations
        hc_ppt = out.get("hasContent", {})
        if isinstance(hc_ppt, dict):
            for slide in _list(hc_ppt.get("slides", [])):
                for loc in _list(slide.get("entities", {}).get("locations", [])):
                    name = _s(loc.get("name") if isinstance(loc, dict) else loc)
                    if not name:
                        continue
                    kg_id, kt, confidence = _kg_id("loc_strong", name.lower())
                    rows.append({"kg_id": kg_id, "kg_key_type": kt, "confidence": confidence, "name": name,
                                 "address": _s(loc.get("address", "") if isinstance(loc, dict) else ""),
                                 "witnessContext": _s(loc.get("witness_context", "") if isinstance(loc, dict) else "")})
                    links.append({"fromKgId": doc_kg_id, "toKgId": kg_id})
    if rows:
        loader.run_batch(UPSERT_LOCATION, rows)
        loader.run_batch(_generic_link_query("Document","LOCATED_IN","Location"), links)
    log.info(f"  Upserted {len(set(r['kg_id'] for r in rows))} Location nodes [{file_type}]")


# ---------------------------------------------------------------------------
# GPE (Geo-Political Entities) — new node type
# Strong key: name (lowercased)
# Edge: Document -[MENTIONS_GPE]-> GPE
# ---------------------------------------------------------------------------

UPSERT_GPE = """
UNWIND $rows AS r
MERGE (g:GPE {kg_id: r.kg_id})
SET g.kg_key_type    = r.kg_key_type,
    g.confidence     = r.confidence,
    g.name           = r.name,
    g.witnessContext = r.witnessContext
"""

def load_gpe(loader, records, file_type, doc_kg_id_map):
    rows, links = [], []
    for rec in records:
        doc_kg_id = doc_kg_id_map.get(_s(rec.get("id")), "")
        out = rec.get("output", {})
        for hc in _list(out.get("hasContent") if isinstance(out.get("hasContent"), list) else []):
            if isinstance(hc, dict):
                for gpe in _list(hc.get("entities", {}).get("gpe", [])):
                    name = _s(gpe.get("name") if isinstance(gpe, dict) else gpe)
                    if not name:
                        continue
                    kg_id, kt, confidence = _kg_id("gpe_strong", name.lower())
                    rows.append({"kg_id": kg_id, "kg_key_type": kt, "confidence": confidence, "name": name,
                                 "witnessContext": _s(gpe.get("witness_context", "") if isinstance(gpe, dict) else "")})
                    links.append({"fromKgId": doc_kg_id, "toKgId": kg_id})
        for msg in _list(out.get("hasPart", [])):
            for gpe in _list(msg.get("entities", {}).get("gpe", [])):
                name = _s(gpe.get("name") if isinstance(gpe, dict) else gpe)
                if not name:
                    continue
                kg_id, kt, confidence = _kg_id("gpe_strong", name.lower())
                rows.append({"kg_id": kg_id, "kg_key_type": kt, "confidence": confidence, "name": name,
                             "witnessContext": _s(gpe.get("witness_context", "") if isinstance(gpe, dict) else "")})
                links.append({"fromKgId": doc_kg_id, "toKgId": kg_id})
        hc_ppt = out.get("hasContent", {})
        if isinstance(hc_ppt, dict):
            for slide in _list(hc_ppt.get("slides", [])):
                for gpe in _list(slide.get("entities", {}).get("gpe", [])):
                    name = _s(gpe.get("name") if isinstance(gpe, dict) else gpe)
                    if not name:
                        continue
                    kg_id, kt, confidence = _kg_id("gpe_strong", name.lower())
                    rows.append({"kg_id": kg_id, "kg_key_type": kt, "confidence": confidence, "name": name,
                                 "witnessContext": _s(gpe.get("witness_context", "") if isinstance(gpe, dict) else "")})
                    links.append({"fromKgId": doc_kg_id, "toKgId": kg_id})
    if rows:
        loader.run_batch(UPSERT_GPE, rows)
        loader.run_batch(_generic_link_query("Document","MENTIONS_GPE","GPE"), links)
    log.info(f"  Upserted {len(set(r['kg_id'] for r in rows))} GPE nodes [{file_type}]")

# Topic (strong key: name)
UPSERT_TOPIC = """
UNWIND $rows AS r
MERGE (t:Topic {kg_id: r.kg_id})
SET t.kg_key_type    = r.kg_key_type,
    t.confidence     = r.confidence,
    t.name           = r.name,
    t.category       = r.category,
    t.witnessContext = r.witnessContext
"""
def load_topics(loader, records, file_type, doc_kg_id_map):
    rows, links = [], []
    for rec in records:
        doc_kg_id = doc_kg_id_map.get(_s(rec.get("id")), "")
        out = rec.get("output", {})
        # All types: hasContent[].entities.topics[]
        for hc in _list(out.get("hasContent") if isinstance(out.get("hasContent"), list) else []):
            if isinstance(hc, dict):
                for t in _list(hc.get("entities", {}).get("topics", [])):
                    # topic_string is the new field name; fallback to plain string
                    if isinstance(t, str):
                        name = _s(t)
                    elif isinstance(t, dict):
                        name = _s(t.get("topic_string", t.get("name", "")))
                    else:
                        continue
                    if not name:
                        continue
                    kg_id, kt, confidence = _kg_id("topic_strong", name.lower())
                    rows.append({"kg_id": kg_id, "kg_key_type": kt, "confidence": confidence, "name": name,
                                 "category": _s(t.get("category", "") if isinstance(t, dict) else ""),
                                 "witnessContext": _s(t.get("witness_context", "") if isinstance(t, dict) else "")})
                    links.append({"fromKgId": doc_kg_id, "toKgId": kg_id})
        # EMAIL hasPart[].entities.topics[]
        for msg in _list(out.get("hasPart", [])):
            for t in _list(msg.get("entities", {}).get("topics", [])):
                name = _s(t.get("topic_string", "") if isinstance(t, dict) else _s(t))
                if not name:
                    continue
                kg_id, kt, confidence = _kg_id("topic_strong", name.lower())
                rows.append({"kg_id": kg_id, "kg_key_type": kt, "confidence": confidence, "name": name,
                             "category": _s(t.get("category", "") if isinstance(t, dict) else ""),
                             "witnessContext": _s(t.get("witness_context", "") if isinstance(t, dict) else "")})
                links.append({"fromKgId": doc_kg_id, "toKgId": kg_id})
        # PPT slides entities topics
        hc_ppt = out.get("hasContent", {})
        if isinstance(hc_ppt, dict):
            for slide in _list(hc_ppt.get("slides", [])):
                for t in _list(slide.get("entities", {}).get("topics", [])):
                    name = _s(t.get("topic_string", "") if isinstance(t, dict) else _s(t))
                    if not name:
                        continue
                    kg_id, kt, confidence = _kg_id("topic_strong", name.lower())
                    rows.append({"kg_id": kg_id, "kg_key_type": kt, "confidence": confidence, "name": name,
                                 "category": _s(t.get("category", "") if isinstance(t, dict) else ""),
                                 "witnessContext": _s(t.get("witness_context", "") if isinstance(t, dict) else "")})
                    links.append({"fromKgId": doc_kg_id, "toKgId": kg_id})
    if rows:
        loader.run_batch(UPSERT_TOPIC, rows)
        loader.run_batch(_generic_link_query("Document","COVERS_TOPIC","Topic"), links)
    log.info(f"  Upserted {len(set(r['kg_id'] for r in rows))} Topic nodes [{file_type}]")

# Slides (PPT)
def load_slides(loader, records, doc_kg_id_map):
    rows, links = [], []
    for rec in records:
        doc_kg_id = doc_kg_id_map.get(_s(rec.get("id")), "")
        slides = rec.get("output", {}).get("hasContent", {})
        if isinstance(slides, dict):
            slides = slides.get("slides", [])
        for slide in _list(slides):
            if not isinstance(slide, dict):
                continue
            pn = slide.get("pageNumber", 0)
            kg_id, kt, confidence = _kg_id("slide_weak", doc_kg_id, str(pn))
            rows.append({"kg_id": kg_id, "kg_key_type": kt, "confidence": confidence,
                         "pageNumber": pn, "title": _s(slide.get("title")),
                         "keyClaim": _s(slide.get("keyClaim")),
                         "speakerNotes": _s(slide.get("speakerNotes"))})
            links.append({"fromKgId": doc_kg_id, "toKgId": kg_id, "pageNumber": pn})
    if rows:
        loader.run_batch(_generic_upsert_query("Slide",
            ["pageNumber","title","keyClaim","speakerNotes"]), rows)
        loader.run_batch("""
            UNWIND $rows AS r
            MATCH (d:Document {kg_id: r.fromKgId})
            MATCH (s:Slide    {kg_id: r.toKgId})
            MERGE (d)-[rel:HAS_SLIDE]->(s)
            SET rel.order = r.pageNumber
        """, links)
    log.info(f"  Upserted {len(rows)} Slide nodes")

# Sheets (XLS)
def load_sheets(loader, records, doc_kg_id_map):
    rows, links = [], []
    for rec in records:
        doc_kg_id = doc_kg_id_map.get(_s(rec.get("id")), "")
        for i, hc in enumerate(_list(rec.get("output", {}).get("hasContent", []))):
            if not isinstance(hc, dict):
                continue
            pn = hc.get("pageNumber", i)
            kg_id, kt, confidence = _kg_id("sheet_weak", doc_kg_id, str(pn))
            rows.append({"kg_id": kg_id, "kg_key_type": kt, "confidence": confidence,
                         "pageNumber": pn, "title": _s(hc.get("title")),
                         "mainEntity": _s(hc.get("mainEntity")),
                         "summary": _s(hc.get("summary")),
                         "notes": _s(hc.get("notes"))})
            links.append({"fromKgId": doc_kg_id, "toKgId": kg_id, "pageNumber": pn})
    if rows:
        loader.run_batch(_generic_upsert_query("Sheet",
            ["pageNumber","title","mainEntity","summary","notes"]), rows)
        loader.run_batch("""
            UNWIND $rows AS r
            MATCH (d:Document {kg_id: r.fromKgId})
            MATCH (s:Sheet    {kg_id: r.toKgId})
            MERGE (d)-[rel:HAS_SHEET]->(s)
            SET rel.order = r.pageNumber
        """, links)
    log.info(f"  Upserted {len(rows)} Sheet nodes")

# TextContent (TXT)
def load_text_content(loader, records, doc_kg_id_map):
    tc_rows, doc_links, col_rows, col_links = [], [], [], []
    for rec in records:
        doc_kg_id = doc_kg_id_map.get(_s(rec.get("id")), "")
        for i, hc in enumerate(_list(rec.get("output", {}).get("hasContent", []))):
            if not isinstance(hc, dict):
                continue
            tc_uid = _s(hc.get("_uid", ""))
            tc_kg_id = _kg_id("tc_weak", doc_kg_id, _s(hc.get("textDocumentId", str(i))))[0]
            # tabular path updated: structure.tabular -> tabular (moved up in new schema)
            tabular  = hc.get("tabular", {}) or {}
            dims     = tabular.get("dimensions", {}) or {}
            dialect  = tabular.get("dialect", {}) or {}
            tc_rows.append({
                "kg_id": tc_kg_id, "kg_key_type": "tc", "confidence": "weak",
                "uid": tc_uid, "textDocumentId": _s(hc.get("textDocumentId")),
                "title": _s(hc.get("title")), "summary": _s(hc.get("summary")),
                "creationDate": _s(hc.get("creationDate")),
                "submittedDate": _s(hc.get("submittedDate")),
                "submittedBy": _s(hc.get("submittedBy")),
                "submittedTo": _s(hc.get("submittedTo")),
                "tableType": _s(tabular.get("tableType")),
                "rowCount": dims.get("rowCount", 0),
                "columnCount": dims.get("columnCount", 0),
                "csvDelimiter": _s(dialect.get("delimiter")),
                "csvEncoding": _s(dialect.get("encoding")),
                "hasHeaderRow": dialect.get("hasHeaderRow", False),
                "hasRedactions": hc.get("_has_redactions", False),
            })
            doc_links.append({"fromKgId": doc_kg_id, "toKgId": tc_kg_id})
            # TabularColumns
            for col in _list(tabular.get("columns", [])):
                if not isinstance(col, dict):
                    continue
                col_kg_id = _kg_id("col_weak", tc_kg_id,
                                   _s(col.get("name")), str(col.get("index","")))[0]
                col_rows.append({
                    "kg_id": col_kg_id, "kg_key_type": "col", "confidence": "weak",
                    "name": _s(col.get("name")), "index": col.get("index", 0),
                    "inferredType": _s(col.get("inferredType")),
                    "nullable": col.get("nullable", True),
                    "description": _s(col.get("description")),
                    "units": _s(col.get("units")),
                })
                col_links.append({"fromKgId": tc_kg_id, "toKgId": col_kg_id,
                                   "colIndex": col.get("index", 0)})
    if tc_rows:
        loader.run_batch(_generic_upsert_query("TextContent", [
            "uid","textDocumentId","title","summary","creationDate","submittedDate",
            "submittedBy","submittedTo","tableType","rowCount","columnCount",
            "csvDelimiter","csvEncoding","hasHeaderRow","hasRedactions"]), tc_rows)
        loader.run_batch(_generic_link_query("Document","HAS_TEXT_CONTENT","TextContent"), doc_links)
    if col_rows:
        loader.run_batch(_generic_upsert_query("TabularColumn",
            ["name","index","inferredType","nullable","description","units"]), col_rows)
        loader.run_batch("""
            UNWIND $rows AS r
            MATCH (tc:TextContent  {kg_id: r.fromKgId})
            MATCH (c:TabularColumn {kg_id: r.toKgId})
            MERGE (tc)-[rel:HAS_COLUMN]->(c)
            SET rel.colIndex = r.colIndex
        """, col_links)
    log.info(f"  Upserted {len(tc_rows)} TextContent, {len(col_rows)} TabularColumn nodes")


# ---------------------------------------------------------------------------
# Semantic mention nodes — generic loader for Event, Finance, Metric,
# Risk, Requirement, Decision, DateMention, HealthMention, Product
# ---------------------------------------------------------------------------

def _collect_semantic(out, field):
    """Collect all instances of a semantic entity field across all schema types.

    All types store semantic entities under entities block (semanticMentions merged).

    Paths:
      DOC/XLS/TXT : hasContent[].entities.<field>
      EMAIL       : hasPart[].entities.<field>
      PPT         : hasContent.slides[].entities.<field>
    """
    items = []
    for hc in _list(out.get("hasContent") if isinstance(out.get("hasContent"), list) else []):
        if isinstance(hc, dict):
            items += _list(hc.get("entities", {}).get(field, []))
    # EMAIL hasPart entities path
    for msg in _list(out.get("hasPart", [])):
        items += _list(msg.get("entities", {}).get(field, []))
    # PPT slides entities path
    hc_dict = out.get("hasContent", {})
    if isinstance(hc_dict, dict):
        for slide in _list(hc_dict.get("slides", [])):
            items += _list(slide.get("entities", {}).get(field, []))
    return items

def load_semantic_nodes(loader, records, file_type, doc_kg_id_map,
                        field, label, rel_type, props_fn):
    rows, links = [], []
    for rec in records:
        doc_kg_id = doc_kg_id_map.get(_s(rec.get("id")), "")
        for item in _collect_semantic(rec.get("output", {}), field):
            row = props_fn(item, doc_kg_id)
            if not row:
                continue
            rows.append(row)
            links.append({"fromKgId": doc_kg_id, "toKgId": row["kg_id"]})
    if rows:
        prop_keys = [k for k in rows[0] if k not in ("kg_id", "kg_key_type")]
        loader.run_batch(_generic_upsert_query(label, prop_keys), rows)
        loader.run_batch(_generic_link_query("Document", rel_type, label), links)
    log.info(f"  Upserted {len(rows)} {label} nodes [{file_type}]")

def _event_props(item, doc_kg_id):
    if not isinstance(item, dict):
        return None
    text = _s(item.get("event_string", item.get("name", item.get("context", ""))))
    if not text:
        return None
    kg_id = _kg_id("event_weak", doc_kg_id, text[:80])[0]
    return {"kg_id": kg_id, "kg_key_type": "event", "confidence": "weak",
            "text": text, "witnessContext": _s(item.get("witness_context", ""))}

def _finance_props(item, doc_kg_id):
    if not isinstance(item, dict):
        return None
    text = _s(item.get("finance_string", item.get("amount", "")))
    if not text:
        return None
    kg_id = _kg_id("fin_weak", doc_kg_id, text[:80])[0]
    return {"kg_id": kg_id, "kg_key_type": "fin", "confidence": "weak",
            "text": text, "witnessContext": _s(item.get("witness_context", ""))}

def _metric_props(item, doc_kg_id):
    if not isinstance(item, dict):
        return None
    text = _s(item.get("metric_string", item.get("name", "")))
    if not text:
        return None
    kg_id = _kg_id("metric_weak", doc_kg_id, text[:80])[0]
    return {"kg_id": kg_id, "kg_key_type": "metric", "confidence": "weak",
            "text": text, "witnessContext": _s(item.get("witness_context", ""))}

def _str_item_props(label_ns, text_field):
    """Generic props builder for Risk, Requirement, Decision, HealthMention."""
    def fn(item, doc_kg_id):
        if isinstance(item, str):
            text = _s(item)
        elif isinstance(item, dict):
            # Try new *_string field first, fall back to description
            text = _s(item.get(text_field, item.get("description", "")))
        else:
            return None
        if not text:
            return None
        kg_id, entity_type, confidence = _kg_id(label_ns, doc_kg_id, text[:80])
        return {"kg_id": kg_id, "kg_key_type": entity_type, "confidence": confidence,
                "text": text,
                "witnessContext": _s(item.get("witness_context", "") if isinstance(item, dict) else ""),
                "category": _s(item.get("category", "") if isinstance(item, dict) else "")}
    return fn

def _date_mention_props(item, doc_kg_id):
    if not isinstance(item, dict):
        return None
    date = _s(item.get("date",""))
    if not date:
        return None
    kg_id = _kg_id("date_weak", doc_kg_id, date, _s(item.get("contextOfDate",""))[:40])[0]
    return {"kg_id": kg_id, "kg_key_type": "date", "confidence": "weak",
            "date": date, "contextOfDate": _s(item.get("contextOfDate"))}

def _product_props(item, doc_kg_id):
    if not isinstance(item, dict):
        return None
    name = _s(item.get("name",""))
    if not name:
        return None
    kg_id = _kg_id("product_weak", name.lower())[0]
    return {"kg_id": kg_id, "kg_key_type": "product", "confidence": "weak",
            "name": name, "model": _s(item.get("model")),
            "identifier": _s(item.get("identifier")),
            "witnessContext": _s(item.get("witness_context", ""))}


# ---------------------------------------------------------------------------
# SignatureBlock
# ---------------------------------------------------------------------------

def load_signature_blocks(loader, records, file_type, doc_kg_id_map):
    rows, links = [], []
    for rec in records:
        doc_kg_id = doc_kg_id_map.get(_s(rec.get("id")), "")
        out = rec.get("output", {})
        # All types use output.signatureBlocks[] top-level (plural)
        # Removed: output.hasContent[].signatureBlock (old TXT path)
        sigs = _list(out.get("signatureBlocks", []))
        for sig in sigs:
            if not isinstance(sig, dict):
                continue
            signer = _s(sig.get("signerName",""))
            if not signer:
                continue
            kg_id = _kg_id("sig_weak", doc_kg_id, signer, _s(sig.get("date","")))[0]
            rows.append({"kg_id": kg_id, "kg_key_type": "sig", "confidence": "weak",
                         "signerName": signer, "signerTitle": _s(sig.get("signerTitle")),
                         "organization": _s(sig.get("organization")),
                         "date": _s(sig.get("date")), "location": _s(sig.get("location")),
                         "signatureText": _s(sig.get("signatureText"))})
            links.append({"fromKgId": doc_kg_id, "toKgId": kg_id,
                          "pageNumber": sig.get("pageNumber", 0)})
    if rows:
        loader.run_batch(_generic_upsert_query("SignatureBlock",
            ["signerName","signerTitle","organization","date","location","signatureText"]), rows)
        loader.run_batch("""
            UNWIND $rows AS r
            MATCH (d:Document       {kg_id: r.fromKgId})
            MATCH (s:SignatureBlock {kg_id: r.toKgId})
            MERGE (d)-[rel:HAS_SIGNATURE]->(s)
            SET rel.pageNumber = r.pageNumber
        """, links)
    log.info(f"  Upserted {len(rows)} SignatureBlock nodes [{file_type}]")


# ===========================================================================
# Main orchestration
# ===========================================================================


# ---------------------------------------------------------------------------
# Vocab
# All 5 file types: output.vocab  (a @context URL string at load time)
# Core fields only written here — rxcui, applicationNumber, etc. are null
# until external_libs.py runs after post_kg_rules.py.
# Edge: Document -[HAS_VOCAB]-> Vocab
# ---------------------------------------------------------------------------

UPSERT_VOCAB = """
UNWIND $rows AS r
MERGE (v:Vocab {kg_id: r.kg_id})
SET v.kg_key_type = r.kg_key_type,
    v.confidence  = r.confidence,
    v.name        = r.name,
    v.type        = r.type,
    v.contextUrl  = r.contextUrl
"""

LINK_DOC_VOCAB = """
UNWIND $rows AS r
MATCH (d:Document {kg_id: r.docKgId})
MATCH (v:Vocab    {kg_id: r.vocabKgId})
MERGE (d)-[:HAS_VOCAB]->(v)
"""

def load_vocab(loader, records, file_type, doc_kg_id_map):
    rows, links = [], []
    for rec in records:
        doc_kg_id = doc_kg_id_map.get(_s(rec.get("id")), "")
        if not doc_kg_id:
            continue
        vocab_raw = rec.get("output", {}).get("vocab")
        if not vocab_raw:
            continue
        # vocab can be a string URL, a single dict, or a list of dicts
        if isinstance(vocab_raw, str):
            entries = [{"name": "context", "type": "contextual", "contextUrl": vocab_raw}]
        elif isinstance(vocab_raw, dict):
            entries = [vocab_raw]
        elif isinstance(vocab_raw, list):
            entries = vocab_raw
        else:
            continue
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                entry = {"contextUrl": _s(entry)}
            name    = _s(entry.get("name", "context"))
            vtype   = _s(entry.get("type", "contextual"))
            ctx_url = _s(entry.get("contextUrl",
                         entry.get("@vocab",
                         entry.get("content", ""))))
            kg_id, kg_key_type, confidence = _kg_id("vocab_weak", doc_kg_id, name, str(i))
            rows.append({
                "kg_id":      kg_id,
                "kg_key_type": kg_key_type,
                "name":       name,
                "type":       vtype,
                "contextUrl": ctx_url,
            })
            links.append({"docKgId": doc_kg_id, "vocabKgId": kg_id})
    if rows:
        loader.run_batch(UPSERT_VOCAB, rows)
        loader.run_batch(LINK_DOC_VOCAB, links)
    log.info(f"  Upserted {len(rows)} Vocab nodes [{file_type}]")


# ---------------------------------------------------------------------------
# EMAIL DateMention nodes linked to EmailMessage
# Edge: EmailMessage -[MENTIONS_DATE]-> DateMention
# ---------------------------------------------------------------------------
def load_email_date_mentions(loader, records, doc_kg_id_map):
    rows, links = [], []
    for rec in records:
        out = rec.get("output", {})
        for msg in _list(out.get("hasPart", [])):
            subj      = _s(msg.get("subject"))
            date_sent = _s(msg.get("dateSent"))
            # identifier removed — key matches updated EmailMessage key
            msg_kg_id = _kg_id("msg_strong", subj, date_sent)[0]
            # path updated: semanticMentions -> entities
            for dm in _list(msg.get("entities", {}).get("datesMentioned", [])):
                row = _date_mention_props(dm, msg_kg_id)
                if not row:
                    continue
                rows.append(row)
                links.append({"fromKgId": msg_kg_id, "toKgId": row["kg_id"]})
    if rows:
        prop_keys = [k for k in rows[0] if k not in ("kg_id", "kg_key_type")]
        loader.run_batch(_generic_upsert_query("DateMention", prop_keys), rows)
        loader.run_batch(_generic_link_query("EmailMessage", "MENTIONS_DATE", "DateMention"), links)
    log.info(f"  Upserted {len(rows)} DateMention nodes linked to EmailMessage [EMAIL]")


def run_pipeline(loader, records_map):
    log.info("=== Stage 1: Documents ===")
    doc_kg_id_maps = {}
    for ft, recs in records_map.items():
        doc_kg_id_maps[ft] = load_documents(loader, recs, ft)

    log.info("=== Stage 1b: Vocab nodes ===")
    for ft, recs in records_map.items():
        load_vocab(loader, recs, ft, doc_kg_id_maps[ft])

    log.info("=== Stage 2: Organizations (needed before Persons) ===")
    org_kg_id_maps = {}
    for ft, recs in records_map.items():
        org_kg_id_maps[ft] = load_orgs(loader, recs, ft, doc_kg_id_maps[ft])

    log.info("=== Stage 3: Persons (contacts[] DOC/TXT + author[] all types) ===")
    for ft, recs in records_map.items():
        load_persons(loader, recs, ft, doc_kg_id_maps[ft], org_kg_id_maps[ft])

    log.info("=== Stage 3b: AUTHORED_BY edges ===")
    for ft, recs in records_map.items():
        load_authored_by(loader, recs, ft, doc_kg_id_maps[ft])

    log.info("=== Stage 4: Drugs (simplified schema, entities block, all 5 types) ===")
    for ft, recs in records_map.items():
        load_drugs(loader, recs, ft, doc_kg_id_maps[ft])

    log.info("=== Stage 5: Email Messages ===")
    if "EMAIL" in records_map:
        load_email_messages(loader, records_map["EMAIL"], doc_kg_id_maps["EMAIL"])

    log.info("=== Stage 6: Locations ===")
    for ft, recs in records_map.items():
        load_locations(loader, recs, ft, doc_kg_id_maps[ft])

    log.info("=== Stage 6b: GPE (new node type) ===")
    for ft, recs in records_map.items():
        load_gpe(loader, recs, ft, doc_kg_id_maps[ft])

    log.info("=== Stage 7: Topics (all 5 types, topic_string field) ===")
    for ft, recs in records_map.items():
        load_topics(loader, recs, ft, doc_kg_id_maps[ft])

    log.info("=== Stage 8: Claims ===")
    if "DOC" in records_map:
        load_claims(loader, records_map["DOC"], doc_kg_id_maps["DOC"])

    log.info("=== Stage 9: Legal Frameworks ===")
    for ft, recs in records_map.items():
        load_legal_frameworks(loader, recs, ft, doc_kg_id_maps[ft])

    log.info("=== Stage 10: Abbreviations ===")
    for ft, recs in records_map.items():
        load_abbreviations(loader, recs, ft, doc_kg_id_maps[ft])

    log.info("=== Stage 11: Citations ===")
    if "DOC" in records_map:
        load_citations(loader, records_map["DOC"], doc_kg_id_maps["DOC"])

    log.info("=== Stage 12: PPT Slides ===")
    if "PPT" in records_map:
        load_slides(loader, records_map["PPT"], doc_kg_id_maps["PPT"])

    log.info("=== Stage 13: XLS Sheets ===")
    if "XLS" in records_map:
        load_sheets(loader, records_map["XLS"], doc_kg_id_maps["XLS"])

    log.info("=== Stage 14: TXT TextContent + TabularColumns ===")
    if "TXT" in records_map:
        load_text_content(loader, records_map["TXT"], doc_kg_id_maps["TXT"])

    log.info("=== Stage 15: Semantic Mention Nodes (entities block, all types) ===")
    for ft, recs in records_map.items():
        dmap = doc_kg_id_maps[ft]
        load_semantic_nodes(loader, recs, ft, dmap, "events",        "Event",        "HAS_EVENT",        _event_props)
        load_semantic_nodes(loader, recs, ft, dmap, "finances",      "Finance",      "HAS_FINANCE",      _finance_props)
        load_semantic_nodes(loader, recs, ft, dmap, "metrics",       "Metric",       "HAS_METRIC",       _metric_props)
        load_semantic_nodes(loader, recs, ft, dmap, "risks",         "Risk",         "HAS_RISK",         _str_item_props("risk_weak",   "risk_string"))
        load_semantic_nodes(loader, recs, ft, dmap, "requirements",  "Requirement",  "HAS_REQUIREMENT",  _str_item_props("req_weak",    "requirement_string"))
        load_semantic_nodes(loader, recs, ft, dmap, "decisionsMade", "Decision",     "HAS_DECISION",     _str_item_props("dec_weak",    "decision_string"))
        load_semantic_nodes(loader, recs, ft, dmap, "health",        "HealthMention","MENTIONS_HEALTH",  _str_item_props("health_weak", "health_string"))
        load_semantic_nodes(loader, recs, ft, dmap, "products",      "Product",      "MENTIONS_PRODUCT", _product_props)
        if ft != "EMAIL":
            load_semantic_nodes(loader, recs, ft, dmap, "datesMentioned", "DateMention", "MENTIONS_DATE", _date_mention_props)

    if "EMAIL" in records_map:
        load_email_date_mentions(loader, records_map["EMAIL"], doc_kg_id_maps["EMAIL"])

    log.info("=== Stage 16: Signature Blocks (DOC, PPT, XLS, TXT) ===")
    for ft in ("DOC", "PPT", "XLS", "TXT"):
        if ft in records_map:
            load_signature_blocks(loader, records_map[ft], ft, doc_kg_id_maps[ft])

    log.info("=== Stage 17: Figures (DOC, PPT, XLS — unified visuals) ===")
    for ft in ("DOC", "PPT", "XLS"):
        if ft in records_map:
            load_figures(loader, records_map[ft], ft, doc_kg_id_maps[ft])

    log.info("=== Stage 18: Links (DOC, PPT, TXT) ===")
    for ft in ("DOC", "PPT", "TXT"):
        if ft in records_map:
            load_links(loader, records_map[ft], ft, doc_kg_id_maps[ft])

    log.info("=== Stage 19: DOC/TXT CaseContext + SectionDetails ===")
    if "DOC" in records_map:
        load_case_context(loader, records_map["DOC"], doc_kg_id_maps["DOC"])
    for ft in ("DOC", "TXT"):
        if ft in records_map:
            load_section_details(loader, records_map[ft], ft, doc_kg_id_maps[ft])

    log.info("=== Stage 20: XLS TableRegions + Formulas + PivotTables + Assessments ===")
    if "XLS" in records_map:
        load_table_regions(loader, records_map["XLS"], doc_kg_id_maps["XLS"])
        load_pivot_tables(loader, records_map["XLS"], doc_kg_id_maps["XLS"])
        load_assessments(loader, records_map["XLS"], doc_kg_id_maps["XLS"])

    log.info("=== Stage 21: TXT CellIndex ===")
    if "TXT" in records_map:
        load_cell_index(loader, records_map["TXT"], doc_kg_id_maps["TXT"])

    log.info("=== Stage 22: Identifiers (all 5 types) ===")
    for ft, recs in records_map.items():
        load_identifiers(loader, recs, ft, doc_kg_id_maps[ft])

    log.info("=== Stage 23: EmbeddedObjects (DOC, PPT, XLS) ===")
    for ft in ("DOC", "PPT", "XLS"):
        if ft in records_map:
            load_embedded_objects(loader, records_map[ft], ft, doc_kg_id_maps[ft])

    log.info("=== Stage 24: Procedures (TXT only) ===")
    if "TXT" in records_map:
        load_procedures(loader, records_map["TXT"], "TXT", doc_kg_id_maps["TXT"])

    log.info("=== Stage 25: Cross-doc mention edges ===")
    for ft, recs in records_map.items():
        load_mention_edges(loader, recs, ft, doc_kg_id_maps[ft])


# ===========================================================================
# MISSING NODE LOADERS — Stages 17-26
# ===========================================================================

# ---------------------------------------------------------------------------
# DOC: Figures (hasContent[].visuals.figures[])
# Edge: Document -[HAS_FIGURE {pageNumber}]-> Figure
# ---------------------------------------------------------------------------
def load_figures(loader, records, file_type, doc_kg_id_map):
    rows, links = [], []
    for rec in records:
        doc_kg_id = doc_kg_id_map.get(_s(rec.get("id")), "")
        out = rec.get("output", {})
        # DOC/XLS: hasContent[].visuals.figures[]
        for hc in _list(out.get("hasContent") if isinstance(out.get("hasContent"), list) else []):
            if not isinstance(hc, dict):
                continue
            pn = hc.get("pageNumber", 0)
            for fig in _list(hc.get("visuals", {}).get("figures", [])):
                if not isinstance(fig, dict):
                    continue
                fig_id = _s(fig.get("id", ""))
                kg_id = _kg_id("fig_weak", doc_kg_id, fig_id or _s(fig.get("title","")))[0]
                rows.append({"kg_id": kg_id, "kg_key_type": "fig", "confidence": "weak",
                             "figureId": fig_id,
                             "title":    _s(fig.get("title")),
                             "caption":  _s(fig.get("caption")),
                             "context":  _s(fig.get("context")),
                             "notes":    _s(fig.get("notes"))})
                links.append({"fromKgId": doc_kg_id, "toKgId": kg_id, "pageNumber": pn})
            # XLS sheetObjects.visuals.figures[]
            for fig in _list(hc.get("sheetObjects", {}).get("visuals", {}).get("figures", [])):
                if not isinstance(fig, dict):
                    continue
                fig_id = _s(fig.get("id", ""))
                kg_id = _kg_id("fig_weak", doc_kg_id, fig_id or _s(fig.get("title","")))[0]
                rows.append({"kg_id": kg_id, "kg_key_type": "fig", "confidence": "weak",
                             "figureId": fig_id, "title": _s(fig.get("title")),
                             "caption": _s(fig.get("caption")), "context": _s(fig.get("context")),
                             "notes": _s(fig.get("notes"))})
                links.append({"fromKgId": doc_kg_id, "toKgId": kg_id, "pageNumber": pn})
        # PPT: hasContent.slides[].visuals.figures[]
        hc_ppt = out.get("hasContent", {})
        if isinstance(hc_ppt, dict):
            for slide in _list(hc_ppt.get("slides", [])):
                pn = slide.get("pageNumber", 0)
                for fig in _list(slide.get("visuals", {}).get("figures", [])):
                    if not isinstance(fig, dict):
                        continue
                    fig_id = _s(fig.get("id", ""))
                    kg_id = _kg_id("fig_weak", doc_kg_id, fig_id or _s(fig.get("title","")), str(pn))[0]
                    rows.append({"kg_id": kg_id, "kg_key_type": "fig", "confidence": "weak",
                                 "figureId": fig_id, "title": _s(fig.get("title")),
                                 "caption": _s(fig.get("caption")), "context": _s(fig.get("context")),
                                 "notes": _s(fig.get("notes"))})
                    links.append({"fromKgId": doc_kg_id, "toKgId": kg_id, "pageNumber": pn})
    if rows:
        loader.run_batch(_generic_upsert_query("Figure",
            ["figureId","title","caption","context","notes"]), rows)
        loader.run_batch("""
            UNWIND $rows AS r
            MATCH (d:Document {kg_id: r.fromKgId})
            MATCH (f:Figure   {kg_id: r.toKgId})
            MERGE (d)-[rel:HAS_FIGURE]->(f)
            SET rel.pageNumber = r.pageNumber
        """, links)
    log.info(f"  Upserted {len(rows)} Figure nodes [{file_type}]")


# ---------------------------------------------------------------------------
# Links (DOC: output.links[], PPT: slides[].links[])
# Edge: Document -[HAS_LINK {pageNumber}]-> Link
# ---------------------------------------------------------------------------
def load_links(loader, records, file_type, doc_kg_id_map):
    rows, links = [], []
    for rec in records:
        doc_kg_id = doc_kg_id_map.get(_s(rec.get("id")), "")
        out = rec.get("output", {})
        # DOC top-level links
        for lnk in _list(out.get("links", [])):
            if not isinstance(lnk, dict):
                continue
            url = _s(lnk.get("url",""))
            if not url:
                continue
            kg_id = _kg_id("link_weak", doc_kg_id, url)[0]
            rows.append({"kg_id": kg_id, "kg_key_type": "link", "confidence": "weak",
                         "url": url,
                         "displayText": _s(lnk.get("displayText")),
                         "linkType": _s(lnk.get("type"))})
            links.append({"fromKgId": doc_kg_id, "toKgId": kg_id,
                          "pageNumber": lnk.get("pageNumber", 0)})
        # PPT slide-level links
        slides = out.get("hasContent", {})
        if isinstance(slides, dict):
            for slide in _list(slides.get("slides", [])):
                if not isinstance(slide, dict):
                    continue
                pn = slide.get("pageNumber", 0)
                for lnk in _list(slide.get("links", [])):
                    if not isinstance(lnk, dict):
                        continue
                    url = _s(lnk.get("url",""))
                    if not url:
                        continue
                    kg_id = _kg_id("link_weak", doc_kg_id, url, str(pn))[0]
                    rows.append({"kg_id": kg_id, "kg_key_type": "link", "confidence": "weak",
                                 "url": url,
                                 "displayText": _s(lnk.get("displayText")),
                                 "linkType": _s(lnk.get("type"))})
                    links.append({"fromKgId": doc_kg_id, "toKgId": kg_id,
                                  "pageNumber": pn})
    if rows:
        loader.run_batch(_generic_upsert_query("Link",
            ["url","displayText","linkType"]), rows)
        loader.run_batch("""
            UNWIND $rows AS r
            MATCH (d:Document {kg_id: r.fromKgId})
            MATCH (l:Link     {kg_id: r.toKgId})
            MERGE (d)-[rel:HAS_LINK]->(l)
            SET rel.pageNumber = r.pageNumber
        """, links)
    log.info(f"  Upserted {len(rows)} Link nodes [{file_type}]")


# ---------------------------------------------------------------------------
# DOC: CaseContext (sections.caseContext)
# Edge: Document -[HAS_CASE_CONTEXT]-> CaseContext
# ---------------------------------------------------------------------------
def load_case_context(loader, records, doc_kg_id_map):
    rows, links = [], []
    for rec in records:
        doc_kg_id = doc_kg_id_map.get(_s(rec.get("id")), "")
        cc = rec.get("output", {}).get("sections", {}).get("caseContext", {})
        if not isinstance(cc, dict):
            continue
        case_num = _s(cc.get("case_number",""))
        filing   = _s(cc.get("filingDate",""))
        if not case_num and not filing:
            continue
        kg_id = _kg_id("cc_weak", doc_kg_id, case_num, filing)[0]
        rows.append({"kg_id": kg_id, "kg_key_type": "cc", "confidence": "weak",
                     "caseNumber": case_num,
                     "filingDate": filing,
                     "jurisdiction": _s(cc.get("jurisdiction")),
                     "presentedBy": _s(cc.get("presentedBy")),
                     "declarationSignedDate": _s(cc.get("declarationSignedDate")),
                     "declarationSignedLocation": _s(cc.get("declarationSignedLoction")),
                     "declarationSignedByAuthority": _s(cc.get("declarationSignedByAuthority"))})
        links.append({"fromKgId": doc_kg_id, "toKgId": kg_id})
    if rows:
        loader.run_batch(_generic_upsert_query("CaseContext", [
            "caseNumber","filingDate","jurisdiction","presentedBy",
            "declarationSignedDate","declarationSignedLocation",
            "declarationSignedByAuthority"]), rows)
        loader.run_batch(_generic_link_query("Document","HAS_CASE_CONTEXT","CaseContext"), links)
    log.info(f"  Upserted {len(rows)} CaseContext nodes")


# ---------------------------------------------------------------------------
# DOC: SectionDetails (sections.sectionDetails[])
# Edge: Document -[HAS_SECTION {sectionType}]-> SectionDetail
# ---------------------------------------------------------------------------
def load_section_details(loader, records, file_type, doc_kg_id_map):
    rows, links = [], []
    for rec in records:
        doc_kg_id = doc_kg_id_map.get(_s(rec.get("id")), "")
        for i, sd in enumerate(_list(
                rec.get("output", {}).get("sections", {}).get("sectionDetails", []))):
            if not isinstance(sd, dict):
                continue
            title = _s(sd.get("title",""))
            stype = _s(sd.get("section_type",""))
            kg_id = _kg_id("sd_weak", doc_kg_id, title, str(i))[0]
            rows.append({"kg_id": kg_id, "kg_key_type": "sd", "confidence": "weak",
                         "title": title, "sectionType": stype})
            links.append({"fromKgId": doc_kg_id, "toKgId": kg_id,
                          "sectionType": stype})
    if rows:
        loader.run_batch(_generic_upsert_query("SectionDetail",
            ["title","sectionType"]), rows)
        loader.run_batch("""
            UNWIND $rows AS r
            MATCH (d:Document      {kg_id: r.fromKgId})
            MATCH (s:SectionDetail {kg_id: r.toKgId})
            MERGE (d)-[rel:HAS_SECTION]->(s)
            SET rel.sectionType = r.sectionType
        """, links)
    log.info(f"  Upserted {len(rows)} SectionDetail nodes")


# ---------------------------------------------------------------------------
# XLS: TableRegions + Formulas
# Edges: Sheet -[HAS_TABLE_REGION]-> TableRegion
#        TableRegion -[HAS_FORMULA {cell}]-> Formula
# ---------------------------------------------------------------------------
def load_table_regions(loader, records, doc_kg_id_map):
    tr_rows, tr_links, f_rows, f_links = [], [], [], []
    for rec in records:
        doc_kg_id = doc_kg_id_map.get(_s(rec.get("id")), "")
        for i, hc in enumerate(_list(rec.get("output", {}).get("hasContent", []))):
            if not isinstance(hc, dict):
                continue
            sheet_kg_id = _kg_id("sheet_weak", doc_kg_id, str(hc.get("pageNumber", i)))[0]
            for tr in _list(hc.get("tableRegions", [])):
                if not isinstance(tr, dict):
                    continue
                region_id = _s(tr.get("regionId",""))
                range_a1  = _s(tr.get("rangeA1",""))
                tr_kg_id  = _kg_id("tr_weak", sheet_kg_id, region_id or range_a1)[0]
                layout = tr.get("layout", {}) or {}
                units  = tr.get("units",  {}) or {}
                tr_rows.append({"kg_id": tr_kg_id, "kg_key_type": "tr", "confidence": "weak",
                                "regionId": region_id, "rangeA1": range_a1,
                                "tableType": _s(tr.get("tableType")),
                                "hasMergedCell": layout.get("hasMergedCell", False),
                                "headerRows": str(layout.get("headerRows","")),
                                "indexColumn": _s(layout.get("indexColumn","")),
                                "currency": _s(units.get("currency")),
                                "scale": _s(units.get("scale")),
                                "unitText": _s(units.get("unitText"))})
                tr_links.append({"fromKgId": sheet_kg_id, "toKgId": tr_kg_id,
                                 "regionId": region_id})
                # Formulas inside this table region
                for formula in _list(tr.get("formulas", [])):
                    if not isinstance(formula, dict):
                        continue
                    cell = _s(formula.get("cell",""))
                    f_kg_id = _kg_id("formula_weak", tr_kg_id, cell)[0]
                    f_rows.append({"kg_id": f_kg_id, "kg_key_type": "formula", "confidence": "weak",
                                   "cell": cell,
                                   "formula": _s(formula.get("formula")),
                                   "calculatedValue": _s(formula.get("calculatedValue")),
                                   "isExternal": formula.get("isExternal", False),
                                   "externalTarget": _s(formula.get("externalTarget"))})
                    f_links.append({"fromKgId": tr_kg_id, "toKgId": f_kg_id, "cell": cell})
    if tr_rows:
        loader.run_batch(_generic_upsert_query("TableRegion", [
            "regionId","rangeA1","tableType","hasMergedCell","headerRows",
            "indexColumn","currency","scale","unitText"]), tr_rows)
        loader.run_batch("""
            UNWIND $rows AS r
            MATCH (s:Sheet       {kg_id: r.fromKgId})
            MATCH (t:TableRegion {kg_id: r.toKgId})
            MERGE (s)-[rel:HAS_TABLE_REGION]->(t)
            SET rel.regionId = r.regionId
        """, tr_links)
    if f_rows:
        loader.run_batch(_generic_upsert_query("Formula", [
            "cell","formula","calculatedValue","isExternal","externalTarget"]), f_rows)
        loader.run_batch("""
            UNWIND $rows AS r
            MATCH (t:TableRegion {kg_id: r.fromKgId})
            MATCH (f:Formula     {kg_id: r.toKgId})
            MERGE (t)-[rel:HAS_FORMULA]->(f)
            SET rel.cell = r.cell
        """, f_links)
    log.info(f"  Upserted {len(tr_rows)} TableRegion, {len(f_rows)} Formula nodes")


# ---------------------------------------------------------------------------
# XLS: PivotTables (hasContent[].sheetObjects.pivotTables[])
# Edge: Sheet -[HAS_PIVOT_TABLE]-> PivotTable
# ---------------------------------------------------------------------------
def load_pivot_tables(loader, records, doc_kg_id_map):
    rows, links = [], []
    for rec in records:
        doc_kg_id = doc_kg_id_map.get(_s(rec.get("id")), "")
        for i, hc in enumerate(_list(rec.get("output", {}).get("hasContent", []))):
            if not isinstance(hc, dict):
                continue
            sheet_kg_id = _kg_id("sheet_weak", doc_kg_id, str(hc.get("pageNumber", i)))[0]
            for pt in _list(hc.get("sheetObjects", {}).get("pivotTables", [])):
                if not isinstance(pt, dict):
                    continue
                name = _s(pt.get("name",""))
                kg_id = _kg_id("pt_weak", sheet_kg_id, name)[0]
                rows.append({"kg_id": kg_id, "kg_key_type": "pt", "confidence": "weak",
                             "name": name,
                             "rangeA1": _s(pt.get("rangeA1")),
                             "sourceRangeA1": _s(pt.get("sourceRangeA1")),
                             "notes": _s(pt.get("notes"))})
                links.append({"fromKgId": sheet_kg_id, "toKgId": kg_id})
    if rows:
        loader.run_batch(_generic_upsert_query("PivotTable",
            ["name","rangeA1","sourceRangeA1","notes"]), rows)
        loader.run_batch(_generic_link_query("Sheet","HAS_PIVOT_TABLE","PivotTable"), links)
    log.info(f"  Upserted {len(rows)} PivotTable nodes")


# ---------------------------------------------------------------------------
# XLS: Assessments (hasContent[].assessments)
# Edge: Sheet -[HAS_ASSESSMENT]-> Assessment
# ---------------------------------------------------------------------------
def load_assessments(loader, records, doc_kg_id_map):
    rows, links = [], []
    for rec in records:
        doc_kg_id = doc_kg_id_map.get(_s(rec.get("id")), "")
        for i, hc in enumerate(_list(rec.get("output", {}).get("hasContent", []))):
            if not isinstance(hc, dict):
                continue
            sheet_kg_id = _kg_id("sheet_weak", doc_kg_id, str(hc.get("pageNumber", i)))[0]
            asmnt = hc.get("assessments", {})
            if not isinstance(asmnt, dict):
                continue
            atype = _s(asmnt.get("assessmentType",""))
            rtype = _s(asmnt.get("riskType",""))
            if not atype and not rtype:
                continue
            kg_id = _kg_id("asmnt_weak", sheet_kg_id, atype, rtype)[0]
            rows.append({"kg_id": kg_id, "kg_key_type": "asmnt", "confidence": "weak",
                         "assessmentType": atype,
                         "riskType": rtype,
                         "riskDescription": _s(asmnt.get("riskDescription")),
                         "riskDataSource": _s(asmnt.get("riskDataSource"))})
            links.append({"fromKgId": sheet_kg_id, "toKgId": kg_id})
    if rows:
        loader.run_batch(_generic_upsert_query("Assessment", [
            "assessmentType","riskType","riskDescription","riskDataSource"]), rows)
        loader.run_batch(_generic_link_query("Sheet","HAS_ASSESSMENT","Assessment"), links)
    log.info(f"  Upserted {len(rows)} Assessment nodes")


# ---------------------------------------------------------------------------
# TXT: CellIndex (hasContent[].tabular.cellIndex[])
# Edge: TextContent -[HAS_CELL {rowNumber, columnName}]-> CellIndex
# ---------------------------------------------------------------------------
def load_cell_index(loader, records, doc_kg_id_map):
    rows, links = [], []
    for rec in records:
        doc_kg_id = doc_kg_id_map.get(_s(rec.get("id")), "")
        for i, hc in enumerate(_list(rec.get("output", {}).get("hasContent", []))):
            if not isinstance(hc, dict):
                continue
            tc_kg_id = _kg_id("tc_weak", doc_kg_id,
                              _s(hc.get("textDocumentId", str(i))))[0]
            for cell in _list(hc.get("structure", {}).get("tabular", {})
                              .get("cellIndex", [])):
                if not isinstance(cell, dict):
                    continue
                col  = _s(cell.get("columnName",""))
                row  = cell.get("rowNumber", 0)
                kg_id = _kg_id("cell_weak", tc_kg_id, col, str(row))[0]
                rows.append({"kg_id": kg_id, "kg_key_type": "cell", "confidence": "weak",
                             "columnName": col,
                             "rowNumber": row,
                             "value": _s(cell.get("value")),
                             "normalizedValue": _s(cell.get("normalizedValue")),
                             "valueType": _s(cell.get("valueType")),
                             "isRedacted": cell.get("isRedacted", False),
                             "redactionText": _s(cell.get("redactionText"))})
                links.append({"fromKgId": tc_kg_id, "toKgId": kg_id,
                              "rowNumber": row, "columnName": col})
    if rows:
        loader.run_batch(_generic_upsert_query("CellIndex", [
            "columnName","rowNumber","value","normalizedValue",
            "valueType","isRedacted","redactionText"]), rows)
        loader.run_batch("""
            UNWIND $rows AS r
            MATCH (tc:TextContent {kg_id: r.fromKgId})
            MATCH (c:CellIndex    {kg_id: r.toKgId})
            MERGE (tc)-[rel:HAS_CELL]->(c)
            SET rel.rowNumber = r.rowNumber, rel.columnName = r.columnName
        """, links)
    log.info(f"  Upserted {len(rows)} CellIndex nodes")


# ---------------------------------------------------------------------------
# Identifiers — all types: hasContent[].entities.identifiers[]
# Edge: Document -[HAS_IDENTIFIER {pageNumber}]-> Identifier
# ---------------------------------------------------------------------------
def load_identifiers(loader, records, file_type, doc_kg_id_map):
    rows, links = [], []
    for rec in records:
        doc_kg_id = doc_kg_id_map.get(_s(rec.get("id")), "")
        out = rec.get("output", {})
        idents = []
        # All types: hasContent[].entities.identifiers[]
        for hc in _list(out.get("hasContent", [])):
            if isinstance(hc, dict):
                idents += _list(hc.get("entities", {}).get("identifiers", []))
        # EMAIL: hasPart[].entities.identifiers[]
        for msg in _list(out.get("hasPart", [])):
            idents += _list(msg.get("entities", {}).get("identifiers", []))
        # PPT: hasContent.slides[].entities.identifiers[]
        hc_ppt = out.get("hasContent", {})
        if isinstance(hc_ppt, dict):
            for slide in _list(hc_ppt.get("slides", [])):
                idents += _list(slide.get("entities", {}).get("identifiers", []))
        for ident in idents:
            if not isinstance(ident, dict):
                continue
            itype = _s(ident.get("type",""))
            ival  = _s(ident.get("value",""))
            if not ival:
                continue
            kg_id = _kg_id("ident_weak", doc_kg_id, itype, ival)[0]
            rows.append({"kg_id": kg_id, "kg_key_type": "ident", "confidence": "weak",
                         "identifierType": itype, "value": ival})
            links.append({"fromKgId": doc_kg_id, "toKgId": kg_id,
                          "pageNumber": ident.get("pageNumber", 0)})
    if rows:
        loader.run_batch(_generic_upsert_query("Identifier",
            ["identifierType","value"]), rows)
        loader.run_batch("""
            UNWIND $rows AS r
            MATCH (d:Document   {kg_id: r.fromKgId})
            MATCH (i:Identifier {kg_id: r.toKgId})
            MERGE (d)-[rel:HAS_IDENTIFIER]->(i)
            SET rel.pageNumber = r.pageNumber
        """, links)
    log.info(f"  Upserted {len(rows)} Identifier nodes [{file_type}]")


# ---------------------------------------------------------------------------
# EmbeddedObjects — unified visuals.embeddedObjects[] path
# DOC: hasContent[].visuals.embeddedObjects[]
# PPT: hasContent.slides[].visuals.embeddedObjects[]
# XLS: hasContent[].sheetObjects.visuals.embeddedObjects[]

# Edge: Document -[HAS_EMBEDDED_OBJECT {pageNumber, objectType}]-> EmbeddedObject
# ---------------------------------------------------------------------------
def load_embedded_objects(loader, records, file_type, doc_kg_id_map):
    rows, links = [], []
    for rec in records:
        doc_kg_id = doc_kg_id_map.get(_s(rec.get("id")), "")
        out = rec.get("output", {})
        items = []  # (obj_dict, page_number)

        if file_type in ("DOC", "XLS"):
            for hc in _list(out.get("hasContent", [])):
                if not isinstance(hc, dict):
                    continue
                pn = hc.get("pageNumber", 0)
                # DOC/PPT: visuals.embeddedObjects[]
                for emb in _list(hc.get("visuals", {}).get("embeddedObjects", [])):
                    items.append((emb if isinstance(emb, dict) else {}, pn))
                # XLS: sheetObjects.visuals.embeddedObjects[]
                for emb in _list(hc.get("sheetObjects", {}).get("visuals", {}).get("embeddedObjects", [])):
                    items.append((emb if isinstance(emb, dict) else {}, pn))

        elif file_type == "PPT":
            slides = out.get("hasContent", {})
            if isinstance(slides, dict):
                for slide in _list(slides.get("slides", [])):
                    if not isinstance(slide, dict):
                        continue
                    pn = slide.get("pageNumber", 0)
                    for emb in _list(slide.get("visuals", {}).get("embeddedObjects", [])):
                        items.append((emb if isinstance(emb, dict) else {}, pn))

        for i, (obj, pn) in enumerate(items):
            obj_type = _s(obj.get("objectType", "embedded"))
            file_name = _s(obj.get("fileName", ""))
            kg_id = _kg_id("emb_weak", doc_kg_id, obj_type, str(pn), file_name, str(i))[0]
            rows.append({"kg_id": kg_id, "kg_key_type": "emb", "confidence": "weak",
                         "objectType": obj_type,
                         "fileName":   file_name,
                         "notes":      _s(obj.get("notes", ""))})
            links.append({"fromKgId": doc_kg_id, "toKgId": kg_id,
                          "pageNumber": pn, "objectType": obj_type})
    if rows:
        loader.run_batch(_generic_upsert_query("EmbeddedObject",
            ["objectType","fileName","notes"]), rows)
        loader.run_batch("""
            UNWIND $rows AS r
            MATCH (d:Document       {kg_id: r.fromKgId})
            MATCH (e:EmbeddedObject {kg_id: r.toKgId})
            MERGE (d)-[rel:HAS_EMBEDDED_OBJECT]->(e)
            SET rel.pageNumber = r.pageNumber, rel.objectType = r.objectType
        """, links)
    log.info(f"  Upserted {len(rows)} EmbeddedObject nodes [{file_type}]")


# ---------------------------------------------------------------------------
# Procedures — TXT only
# TXT: hasContent[].structure.procedures[]
# Edge: Document -[HAS_PROCEDURE {pageNumber}]-> Procedure
# ---------------------------------------------------------------------------
def load_procedures(loader, records, file_type, doc_kg_id_map):
    rows, links = [], []
    if file_type != "TXT":
        return
    for rec in records:
        doc_kg_id = doc_kg_id_map.get(_s(rec.get("id")), "")
        out = rec.get("output", {})
        for hc in _list(out.get("hasContent", [])):
            if not isinstance(hc, dict):
                continue
            pn = hc.get("pageNumber", 0)
            for i, proc in enumerate(_list(hc.get("structure", {}).get("procedures", []))):
                if not isinstance(proc, dict):
                    proc = {"title": _s(proc)}
                title = _s(proc.get("title", ""))
                kg_id = _kg_id("proc_weak", doc_kg_id, title, str(pn), str(i))[0]
                rows.append({"kg_id": kg_id, "kg_key_type": "proc", "confidence": "weak",
                             "title":      title,
                             "pageNumber": proc.get("pageNumber", pn)})
                links.append({"fromKgId": doc_kg_id, "toKgId": kg_id,
                              "pageNumber": proc.get("pageNumber", pn)})
    if rows:
        loader.run_batch(_generic_upsert_query("Procedure", ["title","pageNumber"]), rows)
        loader.run_batch("""
            UNWIND $rows AS r
            MATCH (d:Document  {kg_id: r.fromKgId})
            MATCH (p:Procedure {kg_id: r.toKgId})
            MERGE (d)-[rel:HAS_PROCEDURE]->(p)
            SET rel.pageNumber = r.pageNumber
        """, links)
    log.info(f"  Upserted {len(rows)} Procedure nodes [TXT]")


# ---------------------------------------------------------------------------
# Cross-doc mention edges
# These are edges from Document to already-loaded nodes that were loaded
# in earlier stages but not yet linked from all source paths.
# Covers: MENTIONS_PERSON, MENTIONS_PERSON_IN_MSG, MENTIONS_PERSON_ON_SLIDE,
#         MENTIONS_DRUG_ON_SLIDE, MENTIONS_ORG_IN_TEXT, MENTIONS_PRODUCT_IN_TEXT,
#         MENTIONS_LOCATION_IN_TEXT, LISTED_IN
# ---------------------------------------------------------------------------
def load_mention_edges(loader, records, file_type, doc_kg_id_map):
    person_links, pslide_links, drug_slide_links = [], [], []
    pmsg_links, org_txt_links, prod_txt_links = [], [], []
    loc_txt_links = []
    porg_links = []

    for rec in records:
        doc_kg_id = doc_kg_id_map.get(_s(rec.get("id")), "")
        out = rec.get("output", {})

        # MENTIONS_PERSON — hasContent[].entities.people[] (DOC, PPT, XLS, TXT)
        # Removed: sharedEntities.people (XLS dropped sharedEntities)
        if file_type in ("DOC", "PPT", "XLS", "TXT"):
            for hc in _list(out.get("hasContent", []) if isinstance(out.get("hasContent"), list) else []):
                if isinstance(hc, dict):
                    for p in _list(hc.get("entities", {}).get("people", [])):
                        if not isinstance(p, dict):
                            continue
                        p_kg_id, _ = _person_kg_id(p.get("email"), p.get("name"), p.get("organization",""))
                        if p_kg_id:
                            person_links.append({"fromKgId": doc_kg_id, "toKgId": p_kg_id,
                                                 "pageNumber": p.get("pageNumber", 0)})
                            org_name = _s(p.get("organization", ""))
                            if org_name:
                                o_kg_id, _ = _org_kg_id(org_name)
                                if o_kg_id:
                                    porg_links.append({"personKgId": p_kg_id, "orgKgId": o_kg_id})

        # MENTIONS_PERSON_IN_MSG — EMAIL hasPart[].entities.people[]
        # Path updated: semanticMentions.people -> entities.people
        # Key updated: identifier removed from EmailMessage key
        if file_type == "EMAIL":
            for msg in _list(out.get("hasPart", [])):
                subject   = _s(msg.get("subject"))
                date_sent = _s(msg.get("dateSent"))
                msg_kg_id = _kg_id("msg_strong", subject, date_sent)[0]
                for p in _list(msg.get("entities", {}).get("people", [])):
                    if not isinstance(p, dict):
                        continue
                    p_kg_id, _ = _person_kg_id(p.get("email"), p.get("name"))
                    if p_kg_id:
                        pmsg_links.append({"fromKgId": msg_kg_id, "toKgId": p_kg_id})

        # MENTIONS_PERSON_ON_SLIDE + MENTIONS_DRUG_ON_SLIDE — PPT
        # Path updated: semanticMentions[] -> entities (single object per slide)
        if file_type == "PPT":
            slides = out.get("hasContent", {})
            if isinstance(slides, dict):
                for slide in _list(slides.get("slides", [])):
                    if not isinstance(slide, dict):
                        continue
                    pn = slide.get("pageNumber", 0)
                    slide_kg_id = _kg_id("slide_weak", doc_kg_id, str(pn))[0]
                    entities = slide.get("entities", {})
                    if not isinstance(entities, dict):
                        continue
                    for p in _list(entities.get("people", [])):
                        if not isinstance(p, dict):
                            continue
                        p_kg_id, _ = _person_kg_id(p.get("email"), p.get("name"))
                        if p_kg_id:
                            pslide_links.append({"fromKgId": slide_kg_id,
                                                 "toKgId": p_kg_id,
                                                 "pageNumber": pn})
                    for drug in _list(entities.get("drugs", [])):
                        if not isinstance(drug, dict):
                            continue
                        d_kg_id, _ = _drug_kg_id(drug)
                        if d_kg_id:
                            drug_slide_links.append({"fromKgId": slide_kg_id,
                                                     "toKgId": d_kg_id,
                                                     "pageNumber": pn})

        # MENTIONS_ORG_IN_TEXT / MENTIONS_PRODUCT_IN_TEXT / MENTIONS_LOCATION_IN_TEXT — TXT
        if file_type == "TXT":
            for hc in _list(out.get("hasContent", [])):
                if not isinstance(hc, dict):
                    continue
                tc_kg_id = _kg_id("tc_weak", doc_kg_id,
                                  _s(hc.get("textDocumentId", "")))[0]
                for org in _list(hc.get("entities", {}).get("organizations", [])):
                    name = _s(org.get("name") if isinstance(org, dict) else org)
                    if name:
                        o_kg_id, _ = _org_kg_id(name)
                        if o_kg_id:
                            org_txt_links.append({"fromKgId": tc_kg_id, "toKgId": o_kg_id})
                for prod in _list(hc.get("entities", {}).get("products", [])):
                    if not isinstance(prod, dict):
                        continue
                    p_row = _product_props(prod, doc_kg_id)
                    if p_row:
                        prod_txt_links.append({"fromKgId": tc_kg_id,
                                               "toKgId": p_row["kg_id"],
                                               "pageNumber": prod.get("pageNumber", 0)})
                for loc in _list(hc.get("entities", {}).get("locations", [])):
                    name = _s(loc.get("name") if isinstance(loc, dict) else loc)
                    if name:
                        l_kg_id = _kg_id("loc_strong", name.lower())[0]
                        loc_txt_links.append({"fromKgId": tc_kg_id, "toKgId": l_kg_id})

    # Write all edge batches
    if person_links:
        loader.run_batch("""
            UNWIND $rows AS r
            MATCH (d:Document {kg_id: r.fromKgId})
            MATCH (p:Person   {kg_id: r.toKgId})
            MERGE (d)-[rel:MENTIONS_PERSON]->(p)
            SET rel.pageNumber = r.pageNumber
        """, person_links)
    if pmsg_links:
        loader.run_batch("""
            UNWIND $rows AS r
            MATCH (m:EmailMessage {kg_id: r.fromKgId})
            MATCH (p:Person       {kg_id: r.toKgId})
            MERGE (m)-[:MENTIONS_PERSON_IN_MSG]->(p)
        """, pmsg_links)
    if pslide_links:
        loader.run_batch("""
            UNWIND $rows AS r
            MATCH (s:Slide  {kg_id: r.fromKgId})
            MATCH (p:Person {kg_id: r.toKgId})
            MERGE (s)-[rel:MENTIONS_PERSON_ON_SLIDE]->(p)
            SET rel.pageNumber = r.pageNumber
        """, pslide_links)
    if drug_slide_links:
        loader.run_batch("""
            UNWIND $rows AS r
            MATCH (s:Slide {kg_id: r.fromKgId})
            MATCH (d:Drug  {kg_id: r.toKgId})
            MERGE (s)-[rel:MENTIONS_DRUG_ON_SLIDE]->(d)
            SET rel.pageNumber = r.pageNumber
        """, drug_slide_links)
    if org_txt_links:
        loader.run_batch("""
            UNWIND $rows AS r
            MATCH (tc:TextContent  {kg_id: r.fromKgId})
            MATCH (o:Organization  {kg_id: r.toKgId})
            MERGE (tc)-[:MENTIONS_ORG_IN_TEXT]->(o)
        """, org_txt_links)
    if prod_txt_links:
        loader.run_batch("""
            UNWIND $rows AS r
            MATCH (tc:TextContent {kg_id: r.fromKgId})
            MATCH (p:Product      {kg_id: r.toKgId})
            MERGE (tc)-[rel:MENTIONS_PRODUCT_IN_TEXT]->(p)
            SET rel.pageNumber = r.pageNumber
        """, prod_txt_links)
    if loc_txt_links:
        loader.run_batch("""
            UNWIND $rows AS r
            MATCH (tc:TextContent {kg_id: r.fromKgId})
            MATCH (l:Location     {kg_id: r.toKgId})
            MERGE (tc)-[:MENTIONS_LOCATION_IN_TEXT]->(l)
        """, loc_txt_links)
    if porg_links:
        loader.run_batch("""
            UNWIND $rows AS r
            MATCH (p:Person       {kg_id: r.personKgId})
            MATCH (o:Organization {kg_id: r.orgKgId})
            MERGE (p)-[:WORKS_FOR]->(o)
        """, porg_links)
    log.info(f"  Wrote mention edges [{file_type}]")


# ===========================================================================
# CLI entry point
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(description="Load cleaned JSONL into Neo4j KG.")
    parser.add_argument("--clean-dir",  default=None,
                        help="Folder containing *_clean.jsonl files (e.g. ./clean). "
                             "Overrides individual --doc/--email/etc. if provided.")
    parser.add_argument("--doc",        default=None)
    parser.add_argument("--email",      default=None)
    parser.add_argument("--ppt",        default=None)
    parser.add_argument("--xls",        default=None)
    parser.add_argument("--txt",        default=None)
    parser.add_argument("--uri",        default="bolt://localhost:7687")
    parser.add_argument("--user",       default="neo4j")
    parser.add_argument("--password",   default="neo4j")
    parser.add_argument("--dry-run",    action="store_true",
                        help="Validate and parse without writing to Neo4j")
    parser.add_argument("--batch-size", type=int, default=500,
                        help="Records per Neo4j transaction (default: 500)")
    parser.add_argument("--limit",      type=int, default=None,
                        help="Max records per file — useful for testing")
    args = parser.parse_args()

    # If --clean-dir is given, resolve all file paths from that folder.
    # Individual --doc/--email/etc. flags override per-type if also provided.
    if args.clean_dir:
        d = Path(args.clean_dir)
        files = {
            "DOC":   str(args.doc   or d / "DOC_clean.jsonl"),
            "EMAIL": str(args.email or d / "EMAIL_clean.jsonl"),
            "PPT":   str(args.ppt   or d / "PPT_clean.jsonl"),
            "XLS":   str(args.xls   or d / "XLS_clean.jsonl"),
            "TXT":   str(args.txt   or d / "TXT_clean.jsonl"),
        }
    else:
        files = {
            "DOC":   args.doc,
            "EMAIL": args.email,
            "PPT":   args.ppt,
            "XLS":   args.xls,
            "TXT":   args.txt,
        }

    log.info("Loading JSONL files...")
    records_map = {}
    for ft, path in files.items():
        if not path:
            log.info(f"  {ft}: skipped — no input provided")
            continue
        p = Path(path)
        if p.exists():
            records_map[ft] = load_jsonl(path, limit=args.limit)
            log.info(f"  {ft}: {len(records_map[ft])} records from {path}")
        else:
            log.info(f"  {ft}: skipped — file not found: {path}")

    loader = KGLoader(
        uri        = args.uri,
        user       = args.user,
        password   = args.password,
        dry_run    = args.dry_run,
        batch_size = args.batch_size,
    )

    try:
        if not args.dry_run:
            setup_schema(loader)
        run_pipeline(loader, records_map)
    finally:
        # Write failure log
        if loader.failures:
            fail_path = Path("kg_load_failures.json")
            with open(fail_path, "w") as f:
                json.dump(loader.failures, f, indent=2)
            log.warning(f"  {len(loader.failures)} failures written to {fail_path}")

        loader.close()

        log.info("=== Load Summary ===")
        log.info(f"  Rows written : {loader.stats['rows_written']}")
        log.info(f"  Rows failed  : {loader.stats['rows_failed']}")
        if args.dry_run:
            log.info("  (DRY RUN — nothing written to Neo4j)")

if __name__ == "__main__":
    main()
