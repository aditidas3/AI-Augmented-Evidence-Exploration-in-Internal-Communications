from __future__ import annotations

import io
import json
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence, TextIO

from labeler.core.schemas import PageResult, RenderManifest, make_label_response
from labeler.pipeline.document_source import is_pdf_source_path, read_text_source
from labeler.pipeline.stage1 import (
    PageImageRef,
    Stage1Artifacts,
    Stage1Config,
    build_provider,
    run_stage1,
    sha256_file,
    write_stage1_outputs,
)
from labeler.pipeline.stage2 import Stage2Config, run_stage2
from labeler.pipeline.stage2_outputs import Stage2OutputPaths
from labeler.extraction.ocr import extract_text_from_files

LOGGER = logging.getLogger(__name__)
_JSON_DECODER = json.JSONDecoder()
_OCR_MANIFEST_READ_CHUNK_SIZE = 64 * 1024


def _resolve_stage1_model(config: "PipelineConfig") -> str | None:
    return (
        (config.model or "").strip()
        or _provider_model_env(config.provider_name)
        or os.environ.get("QWEN_MODEL", "").strip()
        or os.environ.get("OPENAI_MODEL", "").strip()
        or None
    )


def _resolve_stage2_model(config: "PipelineConfig", stage1_model: str | None) -> str | None:
    schema_provider_name = config.schema_provider_name or config.provider_name
    return (
        (config.schema_model or "").strip()
        or _provider_model_env(schema_provider_name)
        or os.environ.get("OPENAI_MODEL", "").strip()
        or stage1_model
        or None
    )


def _provider_model_env(provider_name: str | None) -> str:
    provider = (provider_name or "").lower().strip()
    if provider == "openrouter":
        return os.environ.get("OPENROUTER_MODEL", "").strip()
    if provider in {"deepseek", "deepseek-official"}:
        return os.environ.get("DEEPSEEK_MODEL", "").strip()
    if provider in {"vllm", "local-vllm"}:
        return os.environ.get("VLLM_MODEL", "").strip()
    if provider in {"qwen-local", "local-qwen"}:
        return os.environ.get("QWEN_MODEL", "").strip() or os.environ.get("QWEN_MODEL_ID", "").strip()
    return ""


class _LazyLLMProvider:
    def __init__(self, provider_name: str, model: str | None) -> None:
        self._provider_name = provider_name
        self._model = model
        self._provider: Any | None = None

        provider_key = provider_name.lower().strip()
        self.requires_base64_page_payloads = provider_key not in {"qwen-local", "local-qwen"}
        if provider_key in {"qwen-local", "local-qwen"}:
            self.max_concurrent_requests = 1

    def _get_provider(self) -> Any:
        if self._provider is None:
            self._provider = build_provider(self._provider_name, model=self._model)
        return self._provider

    def classify_batch(self, pdf_name: str, total_pages: int, pages: list[Any]) -> Any:
        return self._get_provider().classify_batch(pdf_name, total_pages, pages)

    def generate_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        temperature: float = 0.2,
    ) -> str:
        return self._get_provider().generate_text(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model,
            temperature=temperature,
        )

    def generate_structured_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
    ) -> str:
        return self._get_provider().generate_structured_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model,
        )


def _ordered_page_ocr_inputs(page_image_paths: dict[int, str]) -> tuple[list[int], list[str]]:
    ordered_pages = list(_ordered_page_indices(page_image_paths))
    ordered_image_paths = [_ocr_input_path(page_image_paths[page_index]) for page_index in ordered_pages]
    return ordered_pages, ordered_image_paths


def _ordered_page_indices(page_map: dict[int, Any]) -> Iterable[int]:
    previous: int | None = None
    for page_index in page_map:
        if previous is not None and page_index < previous:
            return sorted(page_map)
        previous = page_index
    return page_map.keys()


def _extract_joined_page_ocr_text(
    *,
    page_image_paths: dict[int, str],
    gpu_memory_utilization: float | None,
    cuda_visible_devices: str | None = None,
) -> str:
    if not page_image_paths:
        return ""
    ordered_pages, ordered_image_paths = _ordered_page_ocr_inputs(page_image_paths)

    LOGGER.info("Running OCR on %s rendered page images.", len(page_image_paths))
    ocr_results = extract_text_from_files(
        ordered_image_paths,
        gpu_memory_utilization=gpu_memory_utilization,
        cuda_visible_devices=cuda_visible_devices,
    )

    output = io.StringIO()
    wrote_page = False
    wrote_text = False
    for page_index, image_path in zip(ordered_pages, ordered_image_paths):
        raw_text = ocr_results.get(image_path, "")
        if wrote_page:
            output.write("\n\n")
        output.write(f"===== Page {page_index} =====")
        if _write_trimmed_text(output, raw_text, leading_newline=True):
            wrote_text = True
        else:
            LOGGER.warning("OCR returned no text for page %s", page_index)
        wrote_page = True
    if not wrote_text:
        return ""
    output.write("\n")
    return output.getvalue()


def _extract_joined_rendered_page_ocr_text(
    *,
    rendered_pages: Sequence[Any],
    gpu_memory_utilization: float | None,
    cuda_visible_devices: str | None = None,
) -> str:
    if not rendered_pages:
        return ""

    image_paths = [_ocr_input_path(page.image_path) for page in rendered_pages]
    LOGGER.info("Running OCR on %s rendered page images.", len(rendered_pages))
    ocr_results = extract_text_from_files(
        image_paths,
        gpu_memory_utilization=gpu_memory_utilization,
        cuda_visible_devices=cuda_visible_devices,
    )

    output = io.StringIO()
    wrote_page = False
    wrote_text = False
    for page, image_path in zip(rendered_pages, image_paths):
        raw_text = ocr_results.get(image_path, "")
        if wrote_page:
            output.write("\n\n")
        output.write(f"===== Page {page.page_index} =====")
        if _write_trimmed_text(output, raw_text, leading_newline=True):
            wrote_text = True
        else:
            LOGGER.warning("OCR returned no text for page %s", page.page_index)
        wrote_page = True
    if not wrote_text:
        return ""
    output.write("\n")
    return output.getvalue()


def _ocr_input_path(image_path: os.PathLike[str] | str) -> str:
    raw_path = os.fspath(image_path)
    if os.path.isabs(raw_path):
        return raw_path
    return os.path.abspath(raw_path)


def _extract_page_ocr_texts(
    *,
    page_image_paths: dict[int, str],
    gpu_memory_utilization: float | None,
    cuda_visible_devices: str | None = None,
) -> dict[int, str]:
    if not page_image_paths:
        return {}
    ordered_pages, ordered_image_paths = _ordered_page_ocr_inputs(page_image_paths)

    LOGGER.info("Running OCR on %s rendered page images.", len(page_image_paths))
    ocr_results = extract_text_from_files(
        ordered_image_paths,
        gpu_memory_utilization=gpu_memory_utilization,
        cuda_visible_devices=cuda_visible_devices,
    )

    page_texts: dict[int, str] = {}
    for page_index, image_path in zip(ordered_pages, ordered_image_paths):
        raw_text = ocr_results.get(image_path, "")
        text = _trimmed_text(raw_text)
        if not text:
            LOGGER.warning("OCR returned no text for page %s", page_index)
        page_texts[page_index] = text
    return page_texts


def _join_page_ocr_texts(page_texts: dict[int, str]) -> str:
    if not page_texts:
        return ""

    output = io.StringIO()
    wrote_page = False
    wrote_text = False
    for page_index in _ordered_page_indices(page_texts):
        if wrote_page:
            output.write("\n\n")
        header = f"===== Page {page_index} ====="
        body = page_texts[page_index]
        output.write(header)
        if _write_trimmed_text(output, body, leading_newline=True):
            wrote_text = True
        wrote_page = True
    if not wrote_text:
        return ""
    output.write("\n")
    return output.getvalue()


def _ocr_manifest_path(output_dir: Path) -> Path:
    return output_dir / "ocr_manifest.json"


def _ocr_text_path(output_dir: Path) -> Path:
    return output_dir / f"{output_dir.name}.txt"


def _load_cached_ocr_text(
    *,
    output_dir: Path,
    pdf_hash: str,
    manifest,
) -> str | None:
    text_path = _ocr_text_path(output_dir)
    manifest_path = _ocr_manifest_path(output_dir)
    try:
        with manifest_path.open("r", encoding="utf-8") as fh:
            if not _ocr_cache_manifest_matches(fh, pdf_hash=pdf_hash, manifest=manifest):
                return None
    except (OSError, json.JSONDecodeError):
        return None
    try:
        with text_path.open("r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    if not _has_non_whitespace(text):
        return None
    LOGGER.info("Reusing cached OCR text: %s", text_path)
    return text


def _write_cached_ocr_text(
    *,
    output_dir: Path,
    pdf_hash: str,
    manifest,
    text: str,
) -> Path:
    text_path = _ocr_text_path(output_dir)
    with text_path.open("w", encoding="utf-8") as fh:
        fh.write(text)
    with _ocr_manifest_path(output_dir).open("w", encoding="utf-8") as fh:
        _write_ocr_cache_manifest(fh, pdf_hash=pdf_hash, manifest=manifest)
    return text_path


def _write_ocr_cache_manifest(fh: TextIO, *, pdf_hash: str, manifest) -> None:
    pages = manifest.pages
    fh.write('{"pdf_hash":')
    json.dump(pdf_hash, fh, ensure_ascii=False, separators=(",", ":"))
    fh.write(',"dpi":')
    fh.write(str(int(manifest.dpi)))
    fh.write(',"page_count":')
    fh.write(str(len(pages)))
    fh.write(',"page_indices":[')
    for index, page in enumerate(pages):
        if index:
            fh.write(",")
        fh.write(str(int(page.page_index)))
    fh.write("]}\n")


def _ocr_cache_manifest_matches(fh: TextIO, *, pdf_hash: str, manifest) -> bool:
    reader = _OcrManifestReader(fh)
    reader.skip_whitespace()
    if reader.read_char() != "{":
        return False

    matched_pdf_hash = False
    matched_dpi = False
    matched_page_count = False
    matched_page_indices = False
    pages: Sequence[Any] | None = None

    while True:
        reader.skip_whitespace()
        char = reader.peek()
        if not char:
            return False
        if char == "}":
            reader.read_char()
            return matched_pdf_hash and matched_dpi and matched_page_count and matched_page_indices

        key = reader.decode_value()
        reader.skip_whitespace()
        if reader.read_char() != ":":
            raise json.JSONDecodeError("Expected ':' after object key", "", 0)

        if key == "pdf_hash":
            if reader.decode_value() != pdf_hash:
                return False
            matched_pdf_hash = True
        elif key == "dpi":
            if _read_ocr_json_int(reader) != manifest.dpi:
                return False
            matched_dpi = True
        elif key == "page_count":
            if pages is None:
                pages = manifest.pages
            if _read_ocr_json_int(reader) != len(pages):
                return False
            matched_page_count = True
        elif key == "page_indices":
            if pages is None:
                pages = manifest.pages
            if not _read_ocr_page_indices_array_matches(reader, pages):
                return False
            matched_page_indices = True
        else:
            _skip_ocr_json_value(reader)

        if matched_pdf_hash and matched_dpi and matched_page_count and matched_page_indices:
            return True

        reader.skip_whitespace()
        char = reader.peek()
        if char == ",":
            reader.read_char()
            continue
        if char == "}":
            reader.read_char()
            return matched_pdf_hash and matched_dpi and matched_page_count and matched_page_indices
        if not char:
            return False
        raise json.JSONDecodeError("Expected ',' or '}' after object value", "", 0)


class _OcrManifestReader:
    __slots__ = ("_eof", "_handle", "_pos", "_text")

    def __init__(self, handle: TextIO) -> None:
        self._handle = handle
        self._text = ""
        self._pos = 0
        self._eof = False

    def peek(self) -> str:
        self._fill()
        if self._pos >= len(self._text):
            return ""
        return self._text[self._pos]

    def read_char(self) -> str:
        char = self.peek()
        if char:
            self._pos += 1
            self._compact()
        return char

    def skip_whitespace(self) -> None:
        while True:
            char = self.peek()
            if not char or not char.isspace():
                return
            self._pos += 1
            self._compact()

    def decode_value(self) -> Any:
        self.skip_whitespace()
        while True:
            try:
                value, end = _JSON_DECODER.raw_decode(self._text, self._pos)
            except json.JSONDecodeError:
                if self._eof:
                    raise
                self._fill(force=True)
                continue
            self._pos = end
            self._compact()
            return value

    def _fill(self, *, force: bool = False) -> None:
        if self._eof:
            return
        if not force and self._pos < len(self._text):
            return
        chunk = self._handle.read(_OCR_MANIFEST_READ_CHUNK_SIZE)
        if not chunk:
            self._eof = True
            return
        if self._pos:
            self._text = self._text[self._pos :] + chunk
            self._pos = 0
        else:
            self._text += chunk

    def _compact(self) -> None:
        if self._pos < _OCR_MANIFEST_READ_CHUNK_SIZE:
            return
        self._text = self._text[self._pos :]
        self._pos = 0


def _read_ocr_json_int(reader: _OcrManifestReader) -> int:
    reader.skip_whitespace()
    negative = False
    char = reader.peek()
    if char == "-":
        reader.read_char()
        negative = True
        char = reader.peek()
    has_digit = False
    value = 0
    while char and "0" <= char <= "9":
        has_digit = True
        value = (value * 10) + (ord(char) - 48)
        reader.read_char()
        char = reader.peek()
    if not has_digit:
        raise json.JSONDecodeError("Expected integer", "", 0)
    return -value if negative else value


def _read_ocr_page_indices_array_matches(reader: _OcrManifestReader, pages: Sequence[Any]) -> bool:
    reader.skip_whitespace()
    if reader.read_char() != "[":
        return False

    page_offset = 0
    while True:
        reader.skip_whitespace()
        char = reader.peek()
        if not char:
            return False
        if char == "]":
            reader.read_char()
            return page_offset == len(pages)
        if page_offset >= len(pages):
            return False

        page_index = _read_ocr_json_int(reader)
        if page_index != pages[page_offset].page_index:
            return False
        page_offset += 1

        reader.skip_whitespace()
        char = reader.peek()
        if char == ",":
            reader.read_char()
            continue
        if char == "]":
            reader.read_char()
            return page_offset == len(pages)
        return False


def _skip_ocr_json_value(reader: _OcrManifestReader) -> None:
    reader.skip_whitespace()
    char = reader.peek()
    if char == '"':
        _skip_ocr_json_string(reader)
        return
    if char == "{":
        _skip_ocr_json_container(reader, "}")
        return
    if char == "[":
        _skip_ocr_json_container(reader, "]")
        return
    while True:
        char = reader.peek()
        if not char or char in ",]}":
            return
        reader.read_char()


def _skip_ocr_json_container(reader: _OcrManifestReader, first_closer: str) -> None:
    reader.read_char()
    closers = [first_closer]
    while closers:
        char = reader.peek()
        if not char:
            raise json.JSONDecodeError("Unterminated JSON container", "", 0)
        if char == '"':
            _skip_ocr_json_string(reader)
            continue
        reader.read_char()
        if char == "{":
            closers.append("}")
        elif char == "[":
            closers.append("]")
        elif char == closers[-1]:
            closers.pop()


def _skip_ocr_json_string(reader: _OcrManifestReader) -> None:
    if reader.read_char() != '"':
        raise json.JSONDecodeError("Expected string", "", 0)
    escaped = False
    while True:
        char = reader.read_char()
        if not char:
            raise json.JSONDecodeError("Unterminated string", "", 0)
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            return


def _has_non_whitespace(text: str) -> bool:
    for char in text:
        if not char.isspace():
            return True
    return False


def _trimmed_text(text: str) -> str:
    start, end = _non_whitespace_bounds(text)
    if start == end:
        return ""
    if start == 0 and end == len(text):
        return text
    return text[start:end]


def _write_trimmed_text(output: io.StringIO, text: str, *, leading_newline: bool = False) -> bool:
    start, end = _non_whitespace_bounds(text)
    if start == end:
        return False
    if leading_newline:
        output.write("\n")
    if start == 0 and end == len(text):
        output.write(text)
    else:
        output.write(text[start:end])
    return True


def _non_whitespace_bounds(text: str) -> tuple[int, int]:
    start = 0
    end = len(text)
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    pdf_path: Path
    output_dir: Path
    dpi: int = 200
    batch_size: int = 4
    max_dim: int = 1600
    optimize_images: bool = True
    provider_name: str = "endpoint"
    model: str | None = None
    dry_run: bool = False
    extract_schema: bool = True
    schema_provider_name: str | None = None
    schema_model: str | None = None
    llm_validation_retries: int = 3
    max_concurrent: int = 10
    gpu_memory_utilization: float | None = None
    cuda_visible_devices: str | None = None
    progress_callback: Callable[[], None] | None = None
    skip_stage2_if_output_exists: bool = False


def _prepare_text_source_stage1(config: PipelineConfig) -> tuple[Stage1Artifacts, str, Path]:
    output_dir = config.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    source_hash = sha256_file(config.pdf_path)
    source_stat = config.pdf_path.stat()
    manifest = RenderManifest(
        pdf_path=str(config.pdf_path),
        dpi=1,
        pages=[],
        optimize_images=False,
        max_dim=None,
        jpeg_quality=None,
        keep_original_images=False,
        pdf_size=source_stat.st_size,
        pdf_mtime_ns=source_stat.st_mtime_ns,
    )

    document_text = _load_cached_ocr_text(
        output_dir=output_dir,
        pdf_hash=source_hash,
        manifest=manifest,
    )
    text_path = _ocr_text_path(output_dir)
    if document_text is None:
        document_text = read_text_source(config.pdf_path)
        if not _has_non_whitespace(document_text):
            raise RuntimeError(f"Text input produced no content for {config.pdf_path}.")
        text_path = _write_cached_ocr_text(
            output_dir=output_dir,
            pdf_hash=source_hash,
            manifest=manifest,
            text=document_text,
        )

    page = PageResult.model_construct(
        page_index=1,
        label="document",
        confidence=1.0,
        rationale="Text source ingested without page rendering.",
    )
    page_ref = PageImageRef(
        page_index=1,
        image_path=config.pdf_path,
        image_hash=source_hash,
        mime_type="text/plain",
    )
    write_stage1_outputs(
        output_dir=output_dir,
        response=make_label_response(pdf_name=config.pdf_path.name, pages=[page]),
        page_inputs=[page_ref],
    )
    return (
        Stage1Artifacts(
            pdf_name=config.pdf_path.name,
            pdf_hash=source_hash,
            manifest=manifest,
            page_inputs=[page_ref],
            labels_json_path=output_dir / "labels.json",
            labels_jsonl_path=output_dir / "labels.jsonl",
        ),
        document_text,
        text_path,
    )


def run_pipeline(config: PipelineConfig) -> None:
    stage2_paths = Stage2OutputPaths.for_output_dir(config.output_dir)
    if _can_skip_completed_pipeline(config, stage2_paths=stage2_paths):
        LOGGER.info(
            "Pipeline skipped: Stage 1 labels and non-empty Stage 2 relationship output already exist for %s.",
            config.output_dir,
        )
        _notify_progress(config, "completed-skip")
        return

    stage1_model = _resolve_stage1_model(config)
    stage2_model = _resolve_stage2_model(config, stage1_model)

    provider = None
    schema_provider = None
    if not config.dry_run:
        provider = _LazyLLMProvider(config.provider_name, stage1_model)
        schema_provider_name = (config.schema_provider_name or config.provider_name).strip()
        if schema_provider_name and schema_provider_name != config.provider_name:
            schema_provider = _LazyLLMProvider(schema_provider_name, stage2_model)
        else:
            schema_provider = provider

    source_text: str | None = None
    source_text_path: Path | None = None
    if is_pdf_source_path(config.pdf_path):
        stage1 = run_stage1(
            Stage1Config(
                pdf_path=config.pdf_path,
                output_dir=config.output_dir,
                dpi=config.dpi,
                batch_size=config.batch_size,
                max_dim=config.max_dim,
                optimize_images=config.optimize_images,
                model=stage1_model,
                provider_name=config.provider_name,
                dry_run=config.dry_run,
                max_concurrent=config.max_concurrent,
                keep_original_images=config.extract_schema,
            ),
            provider=provider,
        )
    else:
        stage1, source_text, source_text_path = _prepare_text_source_stage1(config)
    _notify_progress(config, "stage1")

    if config.dry_run:
        LOGGER.info("Dry-run enabled. Stage 2 skipped.")
        return

    if not config.extract_schema:
        LOGGER.info("Stage 2 entity/relationship extraction disabled.")
        return

    if provider is None:
        raise RuntimeError("LLM provider is required for Stage 2 extraction.")

    relationship_path = stage2_paths.relationship
    stage2_skipped = False
    if config.extract_schema and config.skip_stage2_if_output_exists and _is_nonempty_file(relationship_path):
        LOGGER.info(
            "Stage 2 skipped: non-empty output already exists (%s). Omit --skip-stage2-if-exists to re-run.",
            relationship_path,
        )
        stage2_skipped = True

    if config.extract_schema and not stage2_skipped and not stage1.page_inputs:
        LOGGER.warning("No rendered page inputs found. Stage 2 skipped.")
        stage2_skipped = True

    if config.extract_schema and not stage2_skipped:
        if source_text is not None:
            full_ocr_text = source_text
            ocr_txt_path = source_text_path or _ocr_text_path(config.output_dir)
            ocr_cache_hit = True
        else:
            full_ocr_text = _load_cached_ocr_text(
                output_dir=config.output_dir,
                pdf_hash=stage1.pdf_hash,
                manifest=stage1.manifest,
            )
            ocr_txt_path = _ocr_text_path(config.output_dir)
            ocr_cache_hit = full_ocr_text is not None
            if not ocr_cache_hit:
                full_ocr_text = _extract_joined_rendered_page_ocr_text(
                    rendered_pages=stage1.manifest.pages,
                    gpu_memory_utilization=config.gpu_memory_utilization,
                    cuda_visible_devices=config.cuda_visible_devices,
                )
        if not full_ocr_text or not _has_non_whitespace(full_ocr_text):
            raise RuntimeError(
                f"OCR produced no text content for {config.pdf_path}. "
                "Cannot proceed with Stage 2 entity/relationship extraction."
            )
        if not ocr_cache_hit:
            ocr_txt_path = _write_cached_ocr_text(
                output_dir=config.output_dir,
                pdf_hash=stage1.pdf_hash,
                manifest=stage1.manifest,
                text=full_ocr_text,
            )
        LOGGER.info(
            "Stage 2 OCR %s for %s: total_chars=%s, output_path=%s",
            "cache hit" if ocr_cache_hit else "completed",
            config.pdf_path,
            len(full_ocr_text),
            ocr_txt_path,
        )
        _notify_progress(config, "ocr")

        stage2 = run_stage2(
            provider=schema_provider or provider,
            config=Stage2Config(
                output_dir=config.output_dir,
                pdf_name=stage1.pdf_name,
                pdf_hash=stage1.pdf_hash,
                schema_model=stage2_model,
                validation_retries=config.llm_validation_retries,
                progress_callback=config.progress_callback,
            ),
            document_ocr_text=full_ocr_text,
        )
        LOGGER.info(
            "Stage 2 completed: success=%s, errors=%s, files=%s",
            stage2.success_count,
            stage2.error_count,
            len(stage2.output_files),
        )
        _notify_progress(config, "stage2")


def _can_skip_completed_pipeline(config: PipelineConfig, *, stage2_paths: Stage2OutputPaths) -> bool:
    if (
        config.dry_run
        or not config.extract_schema
        or not config.skip_stage2_if_output_exists
    ):
        return False
    return (config.output_dir / "labels.json").exists() and _is_nonempty_file(stage2_paths.relationship)


def _notify_progress(config: PipelineConfig, stage_name: str) -> None:
    if config.progress_callback is None:
        return
    try:
        config.progress_callback()
    except Exception as exc:
        LOGGER.error("Progress callback failed after %s for %s: %s", stage_name, config.output_dir.name, exc)


def _is_nonempty_file(path: Path) -> bool:
    try:
        file_stat = path.stat()
    except OSError:
        return False
    return stat.S_ISREG(file_stat.st_mode) and file_stat.st_size > 0
