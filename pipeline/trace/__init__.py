"""
pipeline.trace — TRACE operator: AlignBundle → Evidence Graph / Reasoning Graph

Public API:
    Trace          Stateful single-use mapper
    run_trace      One-liner convenience function
    generate_trace_bundle  Run TRACE and return TraceBundle JSON
    TraceConfig    All tuneable knobs
    TraceResult    Immutable execution output
    GraphWriter    Protocol any graph driver must satisfy
    InMemoryGraphWriter   In-memory stub for testing
    MemgraphWriter        Production Memgraph adapter
    Neo4jWriter           Production Neo4j adapter
"""

from .config import TraceConfig, TRACE_NS
from .writers import (
    GraphWriter,
    InMemoryGraphWriter,
    MemgraphWriter,
    Neo4jWriter,
)
from .result import TraceResult
from .trace2 import Trace, generate_trace_bundle, run_trace

__all__ = [
    "Trace",
    "generate_trace_bundle",
    "run_trace",
    "TraceConfig",
    "TraceResult",
    "TRACE_NS",
    "GraphWriter",
    "InMemoryGraphWriter",
    "MemgraphWriter",
    "Neo4jWriter",
]
