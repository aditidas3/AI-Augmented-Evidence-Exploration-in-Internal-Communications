from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

_TIMEZONE_OFFSET_RE = re.compile(r"([+-]\d{2}:?\d{2})$")

try:  # neo4j is a hard dependency at runtime but keep the import guarded
    from neo4j.exceptions import DriverError, Neo4jError  # type: ignore

    NEO4J_QUERY_ERRORS: tuple = (Neo4jError, DriverError)
except ImportError:  # pragma: no cover - exercised only in stub envs
    NEO4J_QUERY_ERRORS = ()


def normalize_time_filter_bound(value: Any, *, end_of_day: bool) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "T" in raw:
        if raw.endswith("z"):
            return f"{raw[:-1]}Z"
        offset_match = _TIMEZONE_OFFSET_RE.search(raw)
        if offset_match:
            parsed_raw = raw
            offset = offset_match.group(1)
            if ":" not in offset:
                parsed_raw = f"{raw[:-5]}{offset[:3]}:{offset[3:]}"
            try:
                parsed = datetime.fromisoformat(parsed_raw)
            except ValueError:
                return raw
            utc_value = parsed.astimezone(timezone.utc).replace(tzinfo=None)
            timespec = "microseconds" if utc_value.microsecond else "seconds"
            return f"{utc_value.isoformat(timespec=timespec)}Z"
        return f"{raw}Z"
    suffix = "T23:59:59Z" if end_of_day else "T00:00:00Z"
    return f"{raw}{suffix}"


def document_date_scope_clause(
    field_expression: str,
    *,
    op: str,
    start: Any,
    end: Any,
) -> str:
    field = str(field_expression or "").strip()
    if not field:
        return ""

    def _to_date(value: Any) -> str:
        return str(value or "")[:10]

    missing_date = f'{field} IS NULL OR {field} = ""'
    if op == "between" and start and end:
        return (
            f'({missing_date} OR ({field} >= "{_to_date(start)}" '
            f'AND {field} <= "{_to_date(end)}"))'
        )
    if op == "before" and end:
        return f'({missing_date} OR {field} <= "{_to_date(end)}")'
    if op == "after" and start:
        return f'({missing_date} OR {field} >= "{_to_date(start)}")'
    return ""


def node_match_condition(graph_store: Any, alias: str, parameter_name: str) -> str:
    if hasattr(graph_store, "node_match_condition"):
        return graph_store.node_match_condition(alias, parameter_name)
    return f"{alias}.id = ${parameter_name}"


def node_id_expression(
    graph_store: Any,
    alias: str,
    *,
    as_name: Optional[str] = None,
) -> str:
    if hasattr(graph_store, "node_id_expression"):
        return graph_store.node_id_expression(alias, as_name=as_name)
    expression = f"{alias}.id"
    if as_name:
        return f"{expression} AS {as_name}"
    return expression


def node_identity_value(graph_store: Any, node: dict[str, Any]) -> str:
    id_field = getattr(graph_store, "id_field", "id")
    raw_value = node.get(id_field, node.get("id", node.get("name", "")))
    if isinstance(raw_value, list):
        for item in raw_value:
            if str(item or "").strip():
                return str(item).strip()
        return ""
    return str(raw_value)


def node_text_expression(alias: str) -> str:
    # witness (singular) is the property name written by kg0_from_db; the
    # legacy witnessContext field is retained in the coalesce chain for
    # backward compatibility with older graph snapshots.
    return (
        f"toLower(coalesce({alias}.name, {alias}.title, "
        f"{alias}.subject, {alias}.summary, {alias}.text, {alias}.description, "
        f"{alias}.rationale, {alias}.citationText, {alias}.context, "
        f"{alias}.caption, {alias}.notes, {alias}.abbvName, {alias}.fullForm, "
        f"{alias}.contextOfDate, {alias}.identifier, {alias}.recordId, "
        f"{alias}.sourceFileName, {alias}.witness, {alias}.witnessContext, ''))"
    )


def resolved_edge_relationship_types(
    config: Any,
    rel_type: str,
) -> Optional[list[str]]:
    if rel_type == "SEMANTIC":
        return None
    edge_upper = str(rel_type or "").upper()
    if edge_upper in getattr(config.ontology, "edge_type_aliases", {}):
        return [
            rel.upper()
            for rel in config.ontology.edge_type_aliases.get(edge_upper, [])
        ]
    resolved = config.ontology.resolve_edge_types(rel_type)
    return [rel.upper() for rel in resolved] if resolved else []
