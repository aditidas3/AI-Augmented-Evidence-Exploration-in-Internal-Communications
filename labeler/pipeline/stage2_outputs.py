from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

STAGE2_ENTITIES_FILENAME = "entities.txt"
STAGE2_WIKIPEDIA_ENRICHMENT_FILENAME = "wikipedia_enrichment.txt"
STAGE2_RELATIONSHIP_FILENAME = "relationship.txt"
STAGE2_SUMMARY_JSON_FILENAME = "summary.json"
STAGE2_MANIFEST_FILENAME = "stage2_manifest.json"


@dataclass(frozen=True, slots=True)
class Stage2OutputPaths:
    entities: Path
    wikipedia_enrichment: Path
    relationship: Path
    summary_json: Path
    manifest: Path

    @classmethod
    def for_output_dir(cls, output_dir: Path) -> "Stage2OutputPaths":
        return cls(
            entities=output_dir / STAGE2_ENTITIES_FILENAME,
            wikipedia_enrichment=output_dir / STAGE2_WIKIPEDIA_ENRICHMENT_FILENAME,
            relationship=output_dir / STAGE2_RELATIONSHIP_FILENAME,
            summary_json=output_dir / STAGE2_SUMMARY_JSON_FILENAME,
            manifest=output_dir / STAGE2_MANIFEST_FILENAME,
        )

    def resolved_files(self) -> list[str]:
        return [
            os.path.abspath(self.entities),
            os.path.abspath(self.wikipedia_enrichment),
            os.path.abspath(self.relationship),
            os.path.abspath(self.summary_json),
            os.path.abspath(self.manifest),
        ]

    def manifest_outputs(self) -> dict[str, str]:
        return {
            "entities_path": os.path.abspath(self.entities),
            "wikipedia_enrichment_path": os.path.abspath(self.wikipedia_enrichment),
            "relationship_path": os.path.abspath(self.relationship),
            "summary_json_path": os.path.abspath(self.summary_json),
        }
