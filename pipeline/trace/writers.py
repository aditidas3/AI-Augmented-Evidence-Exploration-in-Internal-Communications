"""
trace/writers.py — TRACE-owned graph-writer protocol and implementations.

Graph writers live inside TRACE because TRACE (and the downstream operators
that extend it, e.g. MAP-TRANSFORM) are the only code paths that write to
the Evidence Graph or Reasoning Graph.  Any operator that needs to write
receives a GraphWriter via dependency injection; the operator never owns
the graph — it writes through the protocol and the caller decides the
backend.

GraphWriter           Minimal protocol any graph driver must satisfy
InMemoryGraphWriter   In-memory stub for testing
MemgraphWriter        Production Memgraph adapter via mgclient
Neo4jWriter           Production Neo4j adapter via the neo4j driver
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol, Set, runtime_checkable

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
#  Protocol
# ═══════════════════════════════════════════════════════════════

@runtime_checkable
class GraphWriter(Protocol):
    """Minimal contract any graph driver must satisfy."""

    def create_node(
        self, labels: List[str], properties: Dict[str, Any]
    ) -> None: ...

    def create_edge(
        self,
        from_uid: str,
        to_uid: str,
        rel_type: str,
        properties: Dict[str, Any],
    ) -> None: ...

    def node_exists(self, uid: str) -> bool: ...


# ═══════════════════════════════════════════════════════════════
#  In-memory stub (testing)
# ═══════════════════════════════════════════════════════════════

class InMemoryGraphWriter:
    """
    Collects every write in plain lists — useful for unit-testing
    operators without a live graph instance.

    Edge-level dedup via deterministic edge UIDs.
    """

    def __init__(self) -> None:
        self.nodes: List[Dict[str, Any]] = []
        self.edges: List[Dict[str, Any]] = []
        self._uids: Set[str] = set()
        self._edge_keys: Set[str] = set()

    def create_node(
        self, labels: List[str], properties: Dict[str, Any]
    ) -> None:
        uid = properties.get("uid", "")
        if uid and uid in self._uids:
            return  # idempotent
        if uid:
            self._uids.add(uid)
        self.nodes.append({"labels": labels, "properties": properties})

    def create_edge(
        self,
        from_uid: str,
        to_uid: str,
        rel_type: str,
        properties: Dict[str, Any],
    ) -> None:
        edge_uid = properties.get("uid", "")
        if edge_uid and edge_uid in self._edge_keys:
            return  # idempotent
        if edge_uid:
            self._edge_keys.add(edge_uid)
        self.edges.append({
            "from": from_uid,
            "to": to_uid,
            "type": rel_type,
            "properties": properties or {},
        })

    def node_exists(self, uid: str) -> bool:
        return uid in self._uids

    def get_node(self, uid: str) -> Optional[Dict[str, Any]]:
        """Return a copy of node properties for GraphStore-compatible readers."""
        for node in self.nodes:
            props = node.get("properties", {})
            if props.get("uid") == uid:
                return dict(props)
        return None

    def set_properties(self, uid: str, properties: Dict[str, Any]) -> None:
        """Merge properties onto an existing node for MAP-TRANSFORM retractions."""
        for node in self.nodes:
            props = node.get("properties", {})
            if props.get("uid") == uid:
                props.update(properties)
                return


# ═══════════════════════════════════════════════════════════════
#  Memgraph production adapter
# ═══════════════════════════════════════════════════════════════

class MemgraphWriter:
    """
    Production adapter wrapping ``mgclient``.

    Edges use MERGE on the deterministic ``uid`` property to ensure
    full idempotency on re-runs.  ``_flat`` preserves lists so that
    tags (String[]) are stored as native graph list properties.
    """

    _DATETIME_KEYS = {
        "created",
        "modified",
        "createdAt",
        "integrityVerifiedAt",
        "reliabilityAssessedAt",
        "retractedAt",
        "temporalStart",
        "temporalEnd",
        "timestamp",
        "assertedAt",
        "confidenceAssessedAt",
        "scopeTempStart",
        "scopeTempEnd",
        "performedAt",
    }

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7687,
        username: str = "",
        password: str = "",
    ):
        try:
            import mgclient  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "pip install pymgclient  (or use InMemoryGraphWriter)"
            ) from exc
        conn_kwargs: Dict[str, Any] = {
            "host": host,
            "port": port,
        }
        if username or password:
            conn_kwargs["username"] = username
            conn_kwargs["password"] = password
        self._conn = mgclient.connect(**conn_kwargs)
        try:
            self._conn.autocommit = True
        except Exception:
            pass
        self._uids: Set[str] = set()

    # ── helpers ─────────────────────────────────────────────

    def _run(self, cypher: str, params: Dict[str, Any]) -> None:
        cur = self._conn.cursor()
        cur.execute(cypher, params)
        if not getattr(self._conn, "autocommit", False):
            self._conn.commit()

    @staticmethod
    def _flat(props: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for k, v in props.items():
            if v is None:
                continue
            out[k] = MemgraphWriter._coerce_value(k, v)
        return out

    @staticmethod
    def _coerce_value(key: str, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(k): MemgraphWriter._coerce_nested(v)
                for k, v in value.items()
                if v is not None
            }
        if isinstance(value, list):
            return [MemgraphWriter._coerce_nested(v) for v in value if v is not None]
        if key in MemgraphWriter._DATETIME_KEYS:
            dt = MemgraphWriter._parse_datetime(value)
            if dt is not None:
                return dt
        return value

    @staticmethod
    def _coerce_nested(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(k): MemgraphWriter._coerce_nested(v)
                for k, v in value.items()
                if v is not None
            }
        if isinstance(value, list):
            return [MemgraphWriter._coerce_nested(v) for v in value if v is not None]
        return value

    @staticmethod
    def _parse_datetime(value: Any) -> Optional[datetime]:
        if isinstance(value, datetime):
            return value
        if not isinstance(value, str):
            return None
        text = value.strip()
        if not text:
            return None
        text = text.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    # ── protocol implementation ─────────────────────────────

    def create_node(
        self, labels: List[str], properties: Dict[str, Any],
    ) -> None:
        # NOTE on idempotency: we deliberately do NOT short-circuit on
        # ``uid in self._uids``.  Memgraph's ``MERGE ... ON MATCH SET``
        # handles idempotency correctly — repeated writes of the same
        # uid are upserts, and later writes with richer properties
        # (e.g. a real RG Claim replacing an earlier EG shadow Claim
        # when both are bulk-loaded into a single database) must be
        # allowed to overwrite the earlier values.  A client-side
        # dedup would silently drop those legitimate updates.  The
        # ``self._uids`` set is still populated so ``node_exists``
        # can answer from client memory without a round-trip.
        uid = properties.get("uid", "")
        if uid:
            self._uids.add(uid)
        lbl = ":".join(labels)
        flat = self._flat(properties)
        set_parts = ", ".join(f"n.{k} = ${k}" for k in flat)
        cypher = (
            f"MERGE (n:{lbl} {{uid: $uid}}) "
            f"ON CREATE SET {set_parts} "
            f"ON MATCH SET {set_parts}"
        )
        self._run(cypher, flat)

    def create_edge(
        self, from_uid: str, to_uid: str,
        rel_type: str, properties: Dict[str, Any],
    ) -> None:
        flat = self._flat(properties or {})
        edge_uid = flat.get("uid", "")

        if edge_uid:
            other_keys = {k: v for k, v in flat.items() if k != "uid"}
            if other_keys:
                set_parts = ", ".join(
                    f"r.{k} = ${k}" for k in other_keys)
                set_clause = (
                    f"ON CREATE SET {set_parts} "
                    f"ON MATCH SET {set_parts}"
                )
            else:
                set_clause = ""
            cypher = (
                f"MATCH (a {{uid: $from_uid}}), (b {{uid: $to_uid}}) "
                f"MERGE (a)-[r:{rel_type} {{uid: $uid}}]->(b) "
                f"{set_clause}"
            )
        else:
            if flat:
                set_parts = ", ".join(f"r.{k} = ${k}" for k in flat)
                set_clause = f"SET {set_parts}"
            else:
                set_clause = ""
            cypher = (
                f"MATCH (a {{uid: $from_uid}}), (b {{uid: $to_uid}}) "
                f"CREATE (a)-[r:{rel_type}]->(b) {set_clause}"
            )

        self._run(cypher, {
            "from_uid": from_uid, "to_uid": to_uid, **flat})

    def node_exists(self, uid: str) -> bool:  # MemgraphWriter
        return uid in self._uids


# ═══════════════════════════════════════════════════════════════
#  Neo4j production adapter (bolt / neo4j protocol)
# ═══════════════════════════════════════════════════════════════

class Neo4jWriter:
    """
    Production adapter using the official ``neo4j`` Python driver.

    Same MERGE-on-uid idempotency semantics as MemgraphWriter but
    targets a Neo4j 5.x instance via the bolt or neo4j protocol.

    Properties are flattened: dicts become JSON strings, lists are
    preserved as native Neo4j list properties, datetime strings on
    known keys are converted to native DateTime.
    """

    _DATETIME_KEYS = MemgraphWriter._DATETIME_KEYS

    def __init__(
        self,
        uri: str = "neo4j://127.0.0.1:7687",
        user: str = "neo4j",
        password: str = "neo4j",
        database: str = "neo4j",
    ):
        try:
            # Guard against local directories named 'neo4j' shadowing
            # the real driver (e.g. pipeline_test/trace/neo4j/).
            import importlib, sys
            _neo4j_mod = sys.modules.get("neo4j")
            if _neo4j_mod and not hasattr(_neo4j_mod, "GraphDatabase"):
                sys.modules.pop("neo4j", None)
                for k in list(sys.modules):
                    if k.startswith("neo4j."):
                        sys.modules.pop(k, None)
                _neo4j_mod = None
            if _neo4j_mod is None:
                _neo4j_mod = importlib.import_module("neo4j")
            GraphDatabase = _neo4j_mod.GraphDatabase
        except (ImportError, AttributeError) as exc:
            raise ImportError(
                "pip install neo4j  (or use InMemoryGraphWriter)"
            ) from exc
        self._driver = GraphDatabase.driver(uri, auth=(user, password))
        self._database = database
        self._uids: Set[str] = set()

    def _run(self, cypher: str, params: Dict[str, Any]) -> Any:
        with self._driver.session(database=self._database) as session:
            return session.run(cypher, params)

    @staticmethod
    def _flat(props: Dict[str, Any]) -> Dict[str, Any]:
        import json as _json
        out: Dict[str, Any] = {}
        for k, v in props.items():
            if v is None:
                continue
            if isinstance(v, dict):
                out[k] = _json.dumps(v, default=str)
            elif isinstance(v, list):
                coerced = []
                for x in v:
                    if x is None:
                        continue
                    if isinstance(x, dict):
                        coerced.append(_json.dumps(x, default=str))
                    else:
                        coerced.append(x)
                out[k] = coerced
            elif k in Neo4jWriter._DATETIME_KEYS:
                dt = MemgraphWriter._parse_datetime(v)
                out[k] = dt if dt is not None else v
            else:
                out[k] = v
        return out

    def create_node(
        self, labels: List[str], properties: Dict[str, Any],
    ) -> None:
        # NOTE on idempotency: we deliberately do NOT short-circuit on
        # ``uid in self._uids``.  Neo4j's ``MERGE ... ON MATCH SET``
        # handles idempotency correctly — repeated writes of the same
        # uid are upserts, and later writes with richer properties
        # (e.g. a real RG Claim replacing an earlier EG shadow Claim
        # when both are bulk-loaded into a single database) must be
        # allowed to overwrite the earlier values.  A client-side
        # dedup would silently drop those legitimate updates.  The
        # ``self._uids`` set is still populated so ``node_exists``
        # can answer from client memory without a round-trip.
        uid = properties.get("uid", "")
        if uid:
            self._uids.add(uid)
        flat = self._flat(properties)
        set_parts = ", ".join(f"n.`{k}` = ${k}" for k in flat)
        label_str = ":".join(f"`{l}`" for l in labels)
        cypher = (
            f"MERGE (n:{label_str} {{uid: $uid}}) "
            f"ON CREATE SET {set_parts} "
            f"ON MATCH SET {set_parts}"
        )
        self._run(cypher, flat)

    def create_edge(
        self, from_uid: str, to_uid: str,
        rel_type: str, properties: Dict[str, Any],
    ) -> None:
        flat = self._flat(properties or {})
        edge_uid = flat.get("uid", "")

        if edge_uid:
            other_keys = {k: v for k, v in flat.items() if k != "uid"}
            if other_keys:
                set_parts = ", ".join(f"r.`{k}` = ${k}" for k in other_keys)
                set_clause = (
                    f"ON CREATE SET {set_parts} "
                    f"ON MATCH SET {set_parts}"
                )
            else:
                set_clause = ""
            cypher = (
                f"MATCH (a {{uid: $from_uid}}), (b {{uid: $to_uid}}) "
                f"MERGE (a)-[r:`{rel_type}` {{uid: $uid}}]->(b) "
                f"{set_clause}"
            )
        else:
            if flat:
                set_parts = ", ".join(f"r.`{k}` = ${k}" for k in flat)
                set_clause = f"SET {set_parts}"
            else:
                set_clause = ""
            cypher = (
                f"MATCH (a {{uid: $from_uid}}), (b {{uid: $to_uid}}) "
                f"CREATE (a)-[r:`{rel_type}`]->(b) {set_clause}"
            )

        self._run(cypher, {
            "from_uid": from_uid, "to_uid": to_uid, **flat})

    def node_exists(self, uid: str) -> bool:  # Neo4jWriter
        return uid in self._uids

    def close(self) -> None:
        self._driver.close()


__all__ = [
    "GraphWriter",
    "InMemoryGraphWriter",
    "MemgraphWriter",
    "Neo4jWriter",
]
