# Operators Support Modules

This folder now contains shared support code used by ALIGN and the remaining
operator-adjacent data structures. The removed CONSTRUCT/EXPLAIN/bundle/
infrastructure modules are no longer part of this package.

## Current Modules

- `types.py`: shared enums and small value types.
- `configs.py`: configuration objects, including `AlignConfig` and
  `PolicySchema`.
- `ontology.py`: ontology types and loaders.
- `evidence.py`: evidence objects, chain nodes, evidence chains, and trace
  bundles.
- `evidence_graph.py`: in-memory evidence graph.
- `reasoning_graph.py`: reasoning graph node and edge tracking.
- `init.py`: compatibility facade that re-exports the remaining public types.
- `__init__.py`: package marker.

## Active External Usage

ALIGN imports `AlignConfig` from `pipeline.operators.configs` in:

- `pipeline/align/adapters.py`
- `pipeline/align/engine.py`
- `pipeline/align/index_facade.py`
- `pipeline/align/_phase_shared.py`

The `pipeline_test/align/neo4j` smoke scripts also import `AlignConfig` and
`Neo4jConfig` from `pipeline.operators.configs`.
