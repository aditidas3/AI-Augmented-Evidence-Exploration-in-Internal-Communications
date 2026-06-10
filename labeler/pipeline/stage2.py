from __future__ import annotations

import io
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from labeler.models.llm import LLMProvider
from labeler.pipeline.prompts import (
    ENTITY_EXTRACTION_SYSTEM,
    ENTITY_BACKFILL_SYSTEM,
    WIKIPEDIA_ENRICHMENT_SYSTEM,
    RELATIONSHIP_EXTRACTION_SYSTEM,
    build_wikipedia_enrichment_user_prompt,
    build_relationship_user_prompt,
    build_entity_backfill_user_prompt,
)
from labeler.pipeline.stage2_manifest import build_stage2_manifest
from labeler.pipeline.stage2_outputs import Stage2OutputPaths
from labeler.pipeline.stage2_text import (
    build_allowed_entity_keys as _build_allowed_entity_keys,
    build_entity_summary_payload,
    build_label_statistics,
    entity_is_allowed as _entity_is_allowed,
    find_missing_entity_names as _find_missing_entity_names,
    sanitize_entity_inventory_text as _sanitize_entity_inventory_text,
    sanitize_relationship_output as _sanitize_relationship_output,
    sanitize_relationship_output_and_find_missing as _sanitize_relationship_output_and_find_missing,
    sanitize_wikipedia_enrichment_text as _sanitize_wikipedia_enrichment_text,
)

LOGGER = logging.getLogger(__name__)

@dataclass(frozen=True, slots=True)
class Stage2Config:
    output_dir: Path
    pdf_name: str
    pdf_hash: str
    schema_model: str | None = None
    validation_retries: int = 3
    progress_callback: Callable[[], None] | None = None


@dataclass(frozen=True, slots=True)
class Stage2Result:
    entities_path: Path
    wikipedia_enrichment_path: Path
    relationship_path: Path
    summary_json_path: Path
    manifest_path: Path
    success_count: int
    error_count: int
    output_files: list[str]


def run_stage2(
    *,
    provider: LLMProvider,
    config: Stage2Config,
    document_ocr_text: str,
) -> Stage2Result:
    document_text = document_ocr_text or ""
    if not _has_non_whitespace(document_text):
        raise RuntimeError("Stage 2 requires non-empty OCR text.")

    output_dir = config.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    output_paths = Stage2OutputPaths.for_output_dir(output_dir)

    LOGGER.info("Stage 2 entity extraction: chars=%s", len(document_text))
    _, entities_text = _generate_text_with_validation(
        provider=provider,
        step_name="entity extraction",
        system_prompt=ENTITY_EXTRACTION_SYSTEM,
        user_prompt=document_text,
        model=config.schema_model,
        temperature=0.2,
        validation_retries=config.validation_retries,
        sanitizer=_sanitize_entity_inventory_text,
        allow_empty=True,
    )
    _write_text_output(output_paths.entities, entities_text)
    _notify_progress(config, "entities")
    summary_entities_text = entities_text

    def _run_wikipedia_enrichment() -> str:
        if not _has_non_whitespace(summary_entities_text):
            LOGGER.info("Stage 2 Wikipedia enrichment skipped: no entities found.")
            return ""
        LOGGER.info("Stage 2 Wikipedia enrichment: source=%s", output_paths.entities)
        _, text = _generate_text_with_validation(
            provider=provider,
            step_name="Wikipedia enrichment",
            system_prompt=WIKIPEDIA_ENRICHMENT_SYSTEM,
            user_prompt=build_wikipedia_enrichment_user_prompt(entity_list_text=summary_entities_text),
            model=config.schema_model,
            temperature=0.2,
            validation_retries=config.validation_retries,
            sanitizer=_sanitize_wikipedia_enrichment_text,
        )
        return text

    def _run_relationship_extraction() -> tuple[str, str]:
        LOGGER.info("Stage 2 relationship extraction: chars=%s", len(document_text))
        return _generate_relationship_text_with_validation(
            provider=provider,
            document_text=document_text,
            entities_text=summary_entities_text,
            schema_model=config.schema_model,
            validation_retries=config.validation_retries,
            entities_path=output_paths.entities,
        )

    if _independent_stage2_workers(provider) >= 2:
        with ThreadPoolExecutor(max_workers=2) as executor:
            wikipedia_future = executor.submit(_run_wikipedia_enrichment)
            relationship_future = executor.submit(_run_relationship_extraction)
            wikipedia_enrichment_text = wikipedia_future.result()
            entities_text, relationship_text = relationship_future.result()
    else:
        wikipedia_enrichment_text = _run_wikipedia_enrichment()
        entities_text, relationship_text = _run_relationship_extraction()

    _write_text_output(output_paths.wikipedia_enrichment, wikipedia_enrichment_text)
    summary_payload = build_entity_summary_payload(
        document_name=config.pdf_name,
        entities_text=summary_entities_text,
        wikipedia_enrichment_text=wikipedia_enrichment_text,
    )
    _write_json_output(output_paths.summary_json, summary_payload)

    _write_text_output(output_paths.relationship, relationship_text)
    label_statistics = build_label_statistics(
        entities_text=entities_text,
        relationship_text=relationship_text,
        document_text=document_text,
    )

    manifest = build_stage2_manifest(
        pdf_name=config.pdf_name,
        pdf_hash=config.pdf_hash,
        model=config.schema_model,
        document_ocr_chars=len(document_text),
        output_paths=output_paths,
        statistics=label_statistics,
    )
    _write_json_output(output_paths.manifest, manifest)
    output_files = output_paths.resolved_files()

    LOGGER.info("Stage 2 files written: %s", len(output_files))

    return Stage2Result(
        entities_path=output_paths.entities,
        wikipedia_enrichment_path=output_paths.wikipedia_enrichment,
        relationship_path=output_paths.relationship,
        summary_json_path=output_paths.summary_json,
        manifest_path=output_paths.manifest,
        success_count=4,
        error_count=0,
        output_files=output_files,
    )


def _notify_progress(config: Stage2Config, stage_name: str) -> None:
    if config.progress_callback is None:
        return
    try:
        config.progress_callback()
    except Exception as exc:
        LOGGER.error("Stage 2 progress callback failed after %s for %s: %s", stage_name, config.output_dir.name, exc)


def _independent_stage2_workers(provider: LLMProvider) -> int:
    provider_limit = getattr(provider, "max_concurrent_requests", None)
    if isinstance(provider_limit, int) and provider_limit > 0:
        return min(2, provider_limit)
    return 2


def _generate_text_with_validation(
    *,
    provider: LLMProvider,
    step_name: str,
    system_prompt: str,
    user_prompt: str,
    model: str | None,
    temperature: float,
    validation_retries: int,
    sanitizer: Callable[[str], str],
    allow_empty: bool = False,
) -> tuple[str, str]:
    attempts = _validation_attempts(validation_retries)
    last_raw = ""
    last_sanitized = ""
    for attempt in range(1, attempts + 1):
        raw_text = provider.generate_text(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model,
            temperature=temperature,
        )
        sanitized = sanitizer(raw_text)
        if allow_empty or _has_non_whitespace(sanitized):
            return raw_text, sanitized
        last_raw = raw_text
        last_sanitized = sanitized
        if attempt < attempts:
            LOGGER.warning(
                "Stage 2 %s returned no valid rows on attempt %s/%s; retrying.",
                step_name,
                attempt,
                attempts,
            )
    raise RuntimeError(
        f"Stage 2 {step_name} failed validation after {attempts} attempts. "
        f"last_raw_preview={_preview(last_raw)!r} last_sanitized_preview={_preview(last_sanitized)!r}"
    )


def _generate_relationship_text_with_validation(
    *,
    provider: LLMProvider,
    document_text: str,
    entities_text: str,
    schema_model: str | None,
    validation_retries: int,
    entities_path: Path,
) -> tuple[str, str]:
    _validation_attempts(validation_retries)
    current_entities_text = entities_text
    relationship_user_prompt = build_relationship_user_prompt(
        document_text=document_text,
        entity_list_text=current_entities_text,
    )
    relationship_raw_text = provider.generate_text(
        system_prompt=RELATIONSHIP_EXTRACTION_SYSTEM,
        user_prompt=relationship_user_prompt,
        model=schema_model,
        temperature=0.2,
    )
    if not _has_non_whitespace(relationship_raw_text):
        return current_entities_text, ""

    allowed_entity_keys = _build_allowed_entity_keys(current_entities_text)
    relationship_text, missing_entities = _sanitize_relationship_output_and_find_missing(
        relationship_raw_text,
        entity_list_text=current_entities_text,
        allowed_entity_keys=allowed_entity_keys,
    )
    if missing_entities:
        entities_before_backfill = current_entities_text
        current_entities_text, backfilled = _backfill_missing_entities(
            provider=provider,
            document_text=document_text,
            entities_text=current_entities_text,
            relationship_raw_text=relationship_raw_text,
            schema_model=schema_model,
            allowed_entity_keys=allowed_entity_keys,
            missing_entities=missing_entities,
        )
        if current_entities_text is not entities_before_backfill:
            relationship_text = _sanitize_relationship_output(
                relationship_raw_text,
                entity_list_text=current_entities_text,
                allowed_entity_keys=allowed_entity_keys,
            )
        if backfilled:
            _write_text_output(entities_path, current_entities_text)
            LOGGER.info(
                "Entity backfill updated %s with %s recovered entities.",
                entities_path,
                len(backfilled),
            )
    return current_entities_text, relationship_text


def _backfill_missing_entities(
    *,
    provider: LLMProvider,
    document_text: str,
    entities_text: str,
    relationship_raw_text: str,
    schema_model: str | None,
    allowed_entity_keys: set[str] | None = None,
    missing_entities: list[str] | None = None,
) -> tuple[str, list[str]]:
    """Call the LLM to extract entity rows for names that appear in the raw relationship
    output but are missing from entities_text. Returns (possibly updated) entity list
    text plus the list of missing names that were successfully recovered."""
    if missing_entities is None:
        missing = _find_missing_entity_names(
            raw_relationship_text=relationship_raw_text,
            entity_list_text=entities_text,
            allowed_entity_keys=allowed_entity_keys,
        )
    else:
        missing = missing_entities
    if not missing:
        return entities_text, []

    LOGGER.info(
        "Entity backfill: %s relationship entities are missing from entities.txt; re-running entity extraction.",
        len(missing),
    )
    backfill_raw = provider.generate_text(
        system_prompt=ENTITY_BACKFILL_SYSTEM,
        user_prompt=build_entity_backfill_user_prompt(
            document_text=document_text,
            missing_entities=missing,
        ),
        model=schema_model,
        temperature=0.2,
    )
    backfill_sanitized = _sanitize_entity_inventory_text(backfill_raw)
    if not _has_non_whitespace(backfill_sanitized):
        LOGGER.warning(
            "Entity backfill returned no usable rows for %s missing entities.",
            len(missing),
        )
        return entities_text, []

    merged, added_rows = _append_unique_normalized_entity_rows(entities_text, backfill_sanitized)
    if not added_rows:
        LOGGER.warning("Entity backfill produced no new entities after deduplication.")
        return entities_text, []

    new_entity_keys = _build_allowed_entity_keys(backfill_sanitized)
    if allowed_entity_keys is None:
        merged_keys = _build_allowed_entity_keys(merged)
    else:
        allowed_entity_keys.update(new_entity_keys)
        merged_keys = allowed_entity_keys
    recovered = [name for name in missing if _entity_is_allowed(name, merged_keys)]
    LOGGER.info(
        "Entity backfill: recovered %s / %s missing entities.",
        len(recovered),
        len(missing),
    )
    return merged, recovered


def _validation_attempts(validation_retries: int) -> int:
    if validation_retries < 0:
        raise ValueError("validation_retries must be >= 0")
    return validation_retries + 1


def _has_non_whitespace(text: str) -> bool:
    for char in text:
        if not char.isspace():
            return True
    return False


def _join_normalized_text_blocks(first: str, second: str) -> str:
    if not _has_non_whitespace(first):
        return second
    if first.endswith("\n"):
        return first + second
    return first + "\n" + second


def _append_unique_normalized_entity_rows(existing_text: str, additions_text: str) -> tuple[str, bool]:
    if not _has_non_whitespace(additions_text):
        return existing_text, False
    if not _has_non_whitespace(existing_text):
        return additions_text, True

    seen = {line for line in _iter_nonempty_lines(existing_text)}
    added = io.StringIO()
    added_any = False
    for line in _iter_nonempty_lines(additions_text):
        if line in seen:
            continue
        seen.add(line)
        added.write(line)
        added.write("\n")
        added_any = True
    if not added_any:
        return existing_text, False
    return _join_normalized_text_blocks(existing_text, added.getvalue()), True


def _iter_nonempty_lines(text: str):
    start = 0
    length = len(text)
    for index, char in enumerate(text):
        if char != "\n" and char != "\r":
            continue
        if start < index:
            yield text[start:index]
        if char == "\r" and index + 1 < length and text[index + 1] == "\n":
            start = index + 2
        else:
            start = index + 1
    if start < length:
        yield text[start:]


def _stripped_text_equal(left: str, right: str) -> bool:
    left_start, left_end = _non_whitespace_bounds(left)
    right_start, right_end = _non_whitespace_bounds(right)
    length = left_end - left_start
    if length != right_end - right_start:
        return False
    for offset in range(length):
        if left[left_start + offset] != right[right_start + offset]:
            return False
    return True


def _non_whitespace_bounds(text: str) -> tuple[int, int]:
    start = 0
    end = len(text)
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _preview(text: str, max_chars: int = 240) -> str:
    if max_chars <= 0:
        return ""
    output: list[str] = []
    pending_space = False
    for char in str(text or ""):
        if char.isspace():
            if output:
                pending_space = True
            continue
        if pending_space and output:
            if len(output) >= max_chars:
                return _truncate_preview(output, max_chars)
            output.append(" ")
            pending_space = False
        if len(output) >= max_chars:
            return _truncate_preview(output, max_chars)
        output.append(char)
    return "".join(output)


def _truncate_preview(chars: list[str], max_chars: int) -> str:
    if max_chars <= 3:
        return "." * max_chars
    return "".join(chars[: max_chars - 3]) + "..."


def _write_text_output(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8") as handle:
        _write_normalized_output_text(handle, text)


def _write_normalized_output_text(handle, text: str) -> None:
    source = text or ""
    start, end = _non_whitespace_bounds(source)
    if start == end:
        return
    if start == 0 and end == len(source) - 1 and source.endswith("\n"):
        handle.write(source)
        return
    handle.write(source[start:end])
    handle.write("\n")


def _write_json_output(path: Path, payload: object) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        handle.write("\n")
