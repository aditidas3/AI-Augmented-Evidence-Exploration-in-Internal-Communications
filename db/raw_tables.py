from __future__ import annotations

from pathlib import Path

DEFAULT_DOCS_ROOT = Path(__file__).resolve().parents[2] / "docs"

CATALOG_TABLE = "ucsf_opioid.master_collection_catalog"
NODE_TARGET_TABLE = "ucsf_opioid.node_raw_data_ucsf_50"
EDGE_TARGET_TABLE = "ucsf_opioid.edges_raw_data_ucsf_50"
UNKNOWN_COLLECTION = "Unknown Collection"
