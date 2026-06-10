from __future__ import annotations

from labeler.pipeline.stage2_outputs import Stage2OutputPaths

STAGE2_PIPELINE_STEPS = [
    "entity_extraction",
    "wikipedia_enrichment",
    "relationship_extraction",
]


def build_stage2_manifest(
    *,
    pdf_name: str,
    pdf_hash: str,
    model: str | None,
    document_ocr_chars: int,
    output_paths: Stage2OutputPaths,
    statistics: dict[str, object],
) -> dict[str, object]:
    return {
        "pdf_name": pdf_name,
        "pdf_hash": pdf_hash,
        "model": model,
        "pipeline": STAGE2_PIPELINE_STEPS,
        "inputs": {
            "document_ocr_chars": document_ocr_chars,
            "relationship_entities_source": str(output_paths.entities.name),
        },
        "outputs": output_paths.manifest_outputs(),
        "statistics": statistics,
    }
