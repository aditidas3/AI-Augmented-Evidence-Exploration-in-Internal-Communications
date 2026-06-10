"""
neo4j_writer.py
===============
Drop-in Neo4j graph writer for CONFLICT, CONSTRUCT, and EXPLAIN.

Replaces InMemoryGraphWriter with a Neo4j-connected writer that:
  - Writes CONTRADICTS edges and Defeater nodes directly to Neo4j
  - Uses MERGE — idempotent, safe to re-run on same data
  - Batches writes (batch_size=100) for performance
  - Caches UIDs in memory to avoid DB round-trips on node_exists()
  - Streams witnesses in batches instead of loading all at once

Requirements:
    pip install neo4j

Environment variables (optional — falls back to defaults):
    NEO4J_URI       bolt://localhost:7687
    NEO4J_USER      neo4j
    NEO4J_PASSWORD  yourpassword
    NEO4J_DATABASE  neo4j
"""

from __future__ import annotations
import json, os, logging
from typing import Any, Dict, Generator, List, Optional, Set

log = logging.getLogger(__name__)


# ── Connection helper ─────────────────────────────────────────────────────────

def neo4j_driver(
    uri:      str = None,
    user:     str = None,
    password: str = None,
):
    """
    Create a verified Neo4j driver.
    Falls back to environment variables then hardcoded defaults.
    Always call driver.close() when done.

    Example:
        driver = neo4j_driver()   # reads from env vars
        # or
        driver = neo4j_driver(uri="bolt://localhost:7687", user="neo4j", password="pass")
    """
    try:
        from neo4j import GraphDatabase
    except ImportError:
        raise ImportError("Run: pip install neo4j")

    uri      = uri      or os.getenv("NEO4J_URI",      "bolt://localhost:7687")
    user     = user     or os.getenv("NEO4J_USER",     "neo4j")
    password = password or os.getenv("NEO4J_PASSWORD", "yourpassword")

    driver = GraphDatabase.driver(uri, auth=(user, password))
    driver.verify_connectivity()
    log.info(f"Neo4j connected at {uri}")
    return driver


# ── Neo4j Graph Writer ────────────────────────────────────────────────────────

class Neo4jGraphWriter:
    """
    Drop-in replacement for InMemoryGraphWriter from run_conflict_on_trace.py.

    Implements the exact same interface CONFLICT uses:
        create_node(labels, properties)
        create_edge(from_uid, to_uid, rel_type, properties)
        node_exists(uid) -> bool
        .nodes  property   (reads all nodes from DB)
        .edges  property   (reads all edges from DB)

    Scalability features:
        - Writes directly to Neo4j — no full-corpus in-memory load
        - MERGE prevents duplicate nodes and edges across runs
        - Batch writes — flushes every batch_size writes
        - UID cache — fast node_exists() without DB round-trip
    """

    def __init__(
        self,
        driver,
        graph:      str = None,
        batch_size: int = 100,
        name:       str = "",
    ):
        """
        Parameters
        ----------
        driver      : Neo4j driver from neo4j_driver()
        graph       : Neo4j database name — defaults to NEO4J_DATABASE env or "neo4j"
        batch_size  : number of writes to buffer before flushing to Neo4j
        name        : label for logging e.g. "EG" or "RG"
        """
        self._driver     = driver
        self._graph      = graph or os.getenv("NEO4J_DATABASE", "neo4j")
        self._batch_size = batch_size
        self.name        = name

        # In-memory UID cache — avoids a DB query on every node_exists() call
        self._uid_cache: Set[str] = set()

        # Local node mirror — keeps a copy of every node written so
        # CONFLICT Phase 1 can read witnesses without a DB round-trip.
        # This is the key fix for the 0-conflict bug:
        # When seeded from a bundle, nodes go into _local_nodes immediately
        # so they are available before Neo4j flushes complete.
        self._local_nodes: List[Dict] = []
        self._local_edges: List[Dict] = []

        # Write buffers — flushed when full or when flush() is called
        self._node_buf: List[Dict] = []
        self._edge_buf: List[Dict] = []

    # ── Same interface as InMemoryGraphWriter ─────────────────────────────────

    def create_node(self, labels: List[str], properties: Dict) -> None:
        """Buffer a node write. Flushes automatically when buffer is full.
        Also mirrors the node locally so CONFLICT Phase 1 can read it
        immediately without waiting for the Neo4j flush to complete."""
        uid = properties.get("uid", "")
        if uid and uid in self._uid_cache:
            return                              # already written — skip
        self._node_buf.append({
            "labels": labels,
            "props":  _serialise(properties),
        })
        # Mirror locally for immediate read-back
        self._local_nodes.append({
            "labels":     labels,
            "properties": dict(properties),
        })
        if uid:
            self._uid_cache.add(uid)
        if len(self._node_buf) >= self._batch_size:
            self._flush_nodes()

    def create_edge(
        self,
        from_uid:   str,
        to_uid:     str,
        rel_type:   str,
        properties: Dict,
    ) -> None:
        """Buffer an edge write. Flushes automatically when buffer is full.
        Also mirrors the edge locally for immediate read-back."""
        # Mirror locally
        self._local_edges.append({
            "type":       rel_type,
            "from":       from_uid,
            "to":         to_uid,
            "properties": dict(properties),
        })
        self._edge_buf.append({
            "from_uid": from_uid,
            "to_uid":   to_uid,
            "rel_type": rel_type,
            "props":    _serialise(properties),
        })
        if len(self._edge_buf) >= self._batch_size:
            self._flush_edges()

    def node_exists(self, uid: str) -> bool:
        """
        Check whether a node with this uid exists.
        Uses the in-memory UID cache first — this is always authoritative
        for nodes written in this session. Only falls back to DB if the
        UID is not in the cache (meaning it was written in a previous run).
        """
        if uid in self._uid_cache:
            return True
        # Not in local cache — check the DB for nodes from previous runs
        try:
            with self._driver.session(database=self._graph) as s:
                result = s.run(
                    "MATCH (n {uid:$uid}) RETURN count(n) AS c", uid=uid
                ).single()
                cnt = result["c"] if result else 0
            exists = cnt > 0
            if exists:
                self._uid_cache.add(uid)
            return exists
        except Exception:
            # If DB query fails fall back to cache only
            return False

    def flush(self) -> None:
        """
        Flush all remaining buffered writes to Neo4j.
        Always call this after all create_node / create_edge calls.
        """
        self._flush_nodes()
        self._flush_edges()
        log.info(f"Neo4j writer [{self.name}] flushed all pending writes")

    @property
    def nodes(self) -> List[Dict]:
        """
        Return all nodes written to this writer instance.
        Uses the local mirror first (fast, no DB round-trip).
        Falls back to scoped DB query if local mirror is empty.
        This fixes the 0-conflict bug where Neo4j writes were
        not yet flushed when CONFLICT Phase 1 tried to read witnesses.

        IMPORTANT: MATCH (n) without a filter returns ALL nodes in the
        database — including nodes from other runs. We scope the query
        to nodes whose UID is in our local UID cache (seeded during this
        run) to avoid picking up stale data from previous runs.

        For CONFLICT Phase 1 which needs EvidenceNode[Testimony] nodes
        this is the correct behaviour — only witnesses seeded in this
        run should be compared.
        """
        self._flush_nodes()

        # Use local mirror first — always up to date, no DB round-trip needed
        if self._local_nodes:
            return list(self._local_nodes)

        # If we have a local cache use it to scope the query
        if self._uid_cache:
            # Read back only nodes we wrote in this run
            all_nodes = []
            uid_list = list(self._uid_cache)
            # Batch the UID list in groups of 500 to avoid huge queries
            for i in range(0, len(uid_list), 500):
                batch = uid_list[i:i+500]
                with self._driver.session(database=self._graph) as s:
                    rows = s.run(
                        "MATCH (n) WHERE n.uid IN $uids "
                        "RETURN labels(n) AS l, properties(n) AS p",
                        uids=batch,
                    )
                    all_nodes.extend([
                        {"labels": r["l"], "properties": _deserialise(r["p"])}
                        for r in rows
                    ])
            return all_nodes
        else:
            # No cache — fall back to full scan (only safe on empty DB)
            with self._driver.session(database=self._graph) as s:
                rows = s.run(
                    "MATCH (n) RETURN labels(n) AS l, properties(n) AS p"
                )
                return [
                    {"labels": r["l"], "properties": _deserialise(r["p"])}
                    for r in rows
                ]

    @property
    def edges(self) -> List[Dict]:
        """
        Read all edges from Neo4j scoped to this writer instance.
        Only returns edges where both endpoints are in the UID cache.
        """
        self._flush_edges()

        # Use local mirror first — always up to date
        if self._local_edges:
            return list(self._local_edges)

        if self._uid_cache:
            uid_list = list(self._uid_cache)
            all_edges = []
            for i in range(0, len(uid_list), 500):
                batch = uid_list[i:i+500]
                with self._driver.session(database=self._graph) as s:
                    rows = s.run(
                        "MATCH (a)-[r]->(b) "
                        "WHERE a.uid IN $uids OR b.uid IN $uids "
                        "RETURN type(r) AS t, a.uid AS f, "
                        "b.uid AS to, properties(r) AS p",
                        uids=batch,
                    )
                    all_edges.extend([
                        {"type": r["t"], "from": r["f"],
                         "to": r["to"], "properties": _deserialise(r["p"])}
                        for r in rows
                    ])
            return all_edges
        else:
            with self._driver.session(database=self._graph) as s:
                rows = s.run(
                    "MATCH (a)-[r]->(b) "
                    "RETURN type(r) AS t, a.uid AS f, "
                    "b.uid AS to, properties(r) AS p"
                )
                return [
                    {"type": r["t"], "from": r["f"],
                     "to": r["to"], "properties": _deserialise(r["p"])}
                    for r in rows
                ]

    # ── Private flush methods ─────────────────────────────────────────────────

    def _flush_nodes(self) -> None:
        if not self._node_buf:
            return
        with self._driver.session(database=self._graph) as s:
            for item in self._node_buf:
                label_str = ":".join(item["labels"])
                uid       = item["props"].get("uid", "")
                s.run(
                    f"MERGE (n:{label_str} {{uid:$uid}}) SET n += $props",
                    uid=uid, props=item["props"]
                )
        log.debug(f"Neo4j [{self.name}] flushed {len(self._node_buf)} nodes")
        self._node_buf = []

    def _flush_edges(self) -> None:
        if not self._edge_buf:
            return
        with self._driver.session(database=self._graph) as s:
            for item in self._edge_buf:
                s.run(
                    f"MATCH (a {{uid:$f}}),(b {{uid:$t}}) "
                    f"MERGE (a)-[r:{item['rel_type']}]->(b) SET r += $props",
                    f=item["from_uid"], t=item["to_uid"], props=item["props"]
                )
        log.debug(f"Neo4j [{self.name}] flushed {len(self._edge_buf)} edges")
        self._edge_buf = []


# ── Streaming witness loader (scalability) ────────────────────────────────────

def stream_witnesses(
    driver,
    graph:      str = "neo4j",
    batch_size: int = 500,
) -> Generator[List[Dict], None, None]:
    """
    Stream EvidenceNode[Testimony] nodes from Neo4j in batches.

    Use this instead of loading writer.nodes all at once when the
    corpus is large — at 30M+ documents the EG could have millions
    of nodes that would not fit in memory.

    Example:
        for batch in stream_witnesses(driver, batch_size=500):
            for node in batch:
                # process each witness node
    """
    skip = 0
    while True:
        with driver.session(database=graph) as s:
            rows = list(s.run(
                "MATCH (n:EvidenceNode) "
                "WHERE n.domainType CONTAINS 'Witness' "
                "RETURN labels(n) AS l, properties(n) AS p "
                f"SKIP {skip} LIMIT {batch_size}"
            ))
        if not rows:
            break
        yield [
            {"labels": r["l"], "properties": _deserialise(r["p"])}
            for r in rows
        ]
        skip += batch_size


# ── Property serialisation helpers ────────────────────────────────────────────

def _serialise(props: Dict) -> Dict:
    """
    Neo4j does not store nested dicts or lists of dicts as property values.
    Serialise them to JSON strings for storage.
    """
    out = {}
    for k, v in props.items():
        if isinstance(v, (dict, list)):
            out[k] = json.dumps(v, default=str)
        elif v is None:
            out[k] = ""
        else:
            out[k] = v
    return out


def _deserialise(props: Dict) -> Dict:
    """Reverse of _serialise — JSON strings back to dicts/lists."""
    out = {}
    for k, v in props.items():
        if isinstance(v, str) and v and v[0] in ("{", "["):
            try:
                out[k] = json.loads(v)
            except (json.JSONDecodeError, ValueError):
                out[k] = v
        else:
            out[k] = v
    return out
