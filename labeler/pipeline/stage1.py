from __future__ import annotations

import hashlib
import json
import logging
import os
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence, TextIO

from labeler.models.llm import LLMProvider, OpenAICompatibleClient
from labeler.models.page_input import PageInput, detect_mime_type, file_to_base64
from labeler.extraction.render import render_pdf_pages
from labeler.core.schemas import (
    JsonlRow,
    LabelResponse,
    PageResult,
    RenderManifest,
    RenderedPage,
    make_label_response,
    page_result_from_normalized_payload_item,
    validate_full_coverage,
)

LOGGER = logging.getLogger(__name__)
_LABEL_VALUES = {"email", "document", "spreadsheet", "presentation", "text"}
_JSON_DECODER = json.JSONDecoder()
_LABEL_CACHE_HASH_READ_CHUNK_SIZE = 256
_LABEL_CACHE_HASH_SCAN_LIMIT = 64 * 1024
_LABEL_CACHE_FIELD_INCOMPLETE = object()


@dataclass(frozen=True, slots=True)
class Stage1Config:
    pdf_path: Path
    output_dir: Path
    dpi: int = 200
    batch_size: int = 4
    max_dim: int = 1600
    optimize_images: bool = True
    model: str | None = None
    provider_name: str = "endpoint"
    dry_run: bool = False
    max_concurrent: int = 10
    keep_original_images: bool = True


@dataclass(frozen=True, slots=True)
class Stage1Artifacts:
    pdf_name: str
    pdf_hash: str
    manifest: RenderManifest
    page_inputs: list["PageImageRef"]
    labels_json_path: Path
    labels_jsonl_path: Path


@dataclass(frozen=True, slots=True)
class PageImageRef:
    """Lightweight rendered-page reference.

    The large base64 payload is loaded only when a batch is sent to the LLM.
    """

    page_index: int
    image_path: Path
    image_hash: str
    mime_type: str

    def to_page_input(self, *, include_base64: bool = True) -> PageInput:
        image_b64 = ""
        image_hash = self.image_hash
        if include_base64:
            if image_hash:
                image_b64, _ = file_to_base64(self.image_path, compute_hash=False)
            else:
                image_b64, image_hash = file_to_base64(self.image_path, compute_hash=True)
        return PageInput(
            page_index=self.page_index,
            image_path=self.image_path,
            image_b64=image_b64,
            image_hash=image_hash,
            mime_type=self.mime_type,
        )


def build_provider(provider_name: str, model: str | None = None) -> LLMProvider:
    provider = provider_name.lower().strip()
    if provider in {"endpoint", "openai", "openai-compatible"}:
        client = OpenAICompatibleClient.from_env(model=model)
        LOGGER.info("Using endpoint LLM provider with model %s via %s", client.model, client.base_url)
        return client
    if provider in {"openrouter"}:
        client = OpenAICompatibleClient.from_env(
            model=model,
            env_prefix="OPENROUTER",
            default_base_url="https://openrouter.ai/api/v1",
        )
        LOGGER.info("Using OpenRouter LLM provider with model %s via %s", client.model, client.base_url)
        return client
    if provider in {"deepseek", "deepseek-official"}:
        client = OpenAICompatibleClient.from_env(
            model=model,
            env_prefix="DEEPSEEK",
            default_base_url="https://api.deepseek.com",
        )
        LOGGER.info("Using DeepSeek LLM provider with model %s via %s", client.model, client.base_url)
        return client
    if provider in {"vllm", "local-vllm"}:
        client = OpenAICompatibleClient.from_env(
            model=model,
            env_prefix="VLLM",
            default_base_url="http://127.0.0.1:8000/v1",
            default_api_key="EMPTY",
        )
        LOGGER.info("Using local vLLM provider with model %s via %s", client.model, client.base_url)
        return client
    if provider in {"qwen-local", "local-qwen"}:
        from labeler.models.qwen import QwenVLLocalClient

        client = QwenVLLocalClient(model_id=model)
        LOGGER.info("Using local Qwen provider with model %s", client.model_id)
        return client
    raise ValueError(f"Unsupported provider: {provider_name}")


def run_stage1(config: Stage1Config, provider: LLMProvider | None = None) -> Stage1Artifacts:
    output_dir = config.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest = render_pdf_pages(
        pdf_path=config.pdf_path,
        out_dir=output_dir,
        dpi=config.dpi,
        optimize_images=config.optimize_images,
        max_dim=config.max_dim,
        keep_original_images=config.keep_original_images,
    )
    page_inputs = prepare_page_assets(manifest.pages, compute_hash=False)
    LOGGER.info("Prepared %s page image references (lazy image hashes)", len(page_inputs))

    pdf_name = config.pdf_path.name
    labels_json_path = output_dir / "labels.json"
    labels_jsonl_path = output_dir / "labels.jsonl"

    if config.dry_run:
        write_dry_run_summary(output_dir=output_dir, pages=page_inputs)
        LOGGER.info("Dry run complete. No Stage 1 LLM calls were made.")
        return Stage1Artifacts(
            pdf_name=pdf_name,
            pdf_hash="",
            manifest=manifest,
            page_inputs=page_inputs,
            labels_json_path=labels_json_path,
            labels_jsonl_path=labels_jsonl_path,
        )

    pdf_hash = sha256_file(config.pdf_path)
    cache_dir = output_dir / "cache" / pdf_hash
    cache_dir.mkdir(parents=True, exist_ok=True)

    result_slots: list[PageResult | None] = [None] * len(page_inputs)
    cache_hits = 0
    pending: list[PageImageRef] = []
    for page in page_inputs:
        cached = load_label_from_cache(cache_dir, page)
        if cached is not None:
            cache_hits += 1
            _store_page_result(result_slots, cached)
        else:
            pending.append(page)
    LOGGER.info("Stage 1 cache hits: %s, misses: %s", cache_hits, len(pending))

    if pending:
        client = provider or build_provider(config.provider_name, model=config.model)
        pending_batch_count = batch_count(len(pending), config.batch_size)
        max_workers = min(max(1, config.max_concurrent), pending_batch_count) if pending_batch_count else 1
        provider_max_concurrent = getattr(client, "max_concurrent_requests", None)
        if isinstance(provider_max_concurrent, int) and provider_max_concurrent > 0:
            max_workers = min(max_workers, provider_max_concurrent)
        include_base64_payloads = bool(getattr(client, "requires_base64_page_payloads", True))
        LOGGER.info("Stage 1 concurrency: requested=%s, effective=%s", config.max_concurrent, max_workers)

        def _process_batch(batch: list[PageImageRef]) -> list[PageResult]:
            if LOGGER.isEnabledFor(logging.INFO):
                LOGGER.info("Stage 1 labeling batch pages %s", [p.page_index for p in batch])
            payloads = [page.to_page_input(include_base64=include_base64_payloads) for page in batch]
            results = client.classify_batch(
                pdf_name=pdf_name,
                total_pages=len(page_inputs),
                pages=payloads,
            )
            _save_batch_labels_to_cache(cache_dir, batch, payloads, results)
            return results

        batch_iter = iter_page_batches(pending, config.batch_size)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = set()

            def _submit_next_batch() -> bool:
                try:
                    batch = next(batch_iter)
                except StopIteration:
                    return False
                futures.add(executor.submit(_process_batch, batch))
                return True

            for _ in range(max_workers):
                if not _submit_next_batch():
                    break

            while futures:
                done, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in done:
                    futures.remove(future)
                    results = future.result()
                    for result in results:
                        _store_page_result(result_slots, result)
                    _submit_next_batch()
    else:
        LOGGER.info("Stage 1 cache satisfied all pages; no LLM calls needed.")

    ordered_results = _collect_ordered_page_results(result_slots)
    response = make_label_response(pdf_name=pdf_name, pages=ordered_results)
    validate_full_coverage(response, expected_pages=len(page_inputs))
    write_stage1_outputs(output_dir=output_dir, response=response, page_inputs=page_inputs)

    return Stage1Artifacts(
        pdf_name=pdf_name,
        pdf_hash=pdf_hash,
        manifest=manifest,
        page_inputs=page_inputs,
        labels_json_path=labels_json_path,
        labels_jsonl_path=labels_jsonl_path,
    )


def _store_page_result(result_slots: list[PageResult | None], result: PageResult) -> None:
    page_index = result.page_index
    if 1 <= page_index <= len(result_slots):
        result_slots[page_index - 1] = result


def _collect_ordered_page_results(result_slots: Sequence[PageResult | None]) -> list[PageResult]:
    ordered_results: list[PageResult] = []
    for offset, result in enumerate(result_slots, start=1):
        if result is None:
            raise RuntimeError(f"Missing model result for page {offset}")
        ordered_results.append(result)
    return ordered_results


def prepare_page_assets(rendered_pages: list[RenderedPage], *, compute_hash: bool = True) -> list[PageImageRef]:
    pages: list[PageImageRef] = []
    for page in rendered_pages:
        image_path = Path(page.llm_image_path)
        pages.append(
            PageImageRef(
                page_index=page.page_index,
                image_path=image_path,
                image_hash=sha256_file(image_path) if compute_hash else "",
                mime_type=detect_mime_type(image_path),
            )
        )
    return pages


def prepare_page_inputs(rendered_pages: list[RenderedPage]) -> list[PageInput]:
    return [page.to_page_input() for page in prepare_page_assets(rendered_pages)]


def batch_count(item_count: int, size: int) -> int:
    if size <= 0:
        raise ValueError("batch_size must be >= 1")
    return (item_count + size - 1) // size


def iter_page_batches(items: list[PageImageRef], size: int):
    if size <= 0:
        raise ValueError("batch_size must be >= 1")
    for index in range(0, len(items), size):
        yield items[index : index + size]


def chunk_pages(items: list[PageImageRef], size: int) -> list[list[PageImageRef]]:
    return list(iter_page_batches(items, size))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def label_cache_file(cache_dir: Path, page_index: int) -> Path:
    return cache_dir / f"page_{page_index:04d}.json"


def _save_batch_labels_to_cache(
    cache_dir: Path,
    batch: Sequence[PageImageRef],
    payloads: Sequence[PageInput],
    results: Sequence[PageResult],
) -> None:
    if len(batch) != len(payloads):
        raise RuntimeError("Stage 1 page batch and payload count mismatch.")

    results_are_ordered = len(results) == len(batch)
    if results_are_ordered:
        for page, page_result in zip(batch, results):
            if page_result.page_index != page.page_index:
                results_are_ordered = False
                break

    if results_are_ordered:
        for page, payload, page_result in zip(batch, payloads, results):
            save_label_to_cache(cache_dir, page, page_result, image_hash=payload.image_hash)
        return

    results_by_page: dict[int, PageResult] = {}
    for result in results:
        results_by_page[result.page_index] = result
    for page, payload in zip(batch, payloads):
        page_result = results_by_page.get(page.page_index)
        if page_result is None:
            raise RuntimeError(f"Missing model result for page {page.page_index}")
        save_label_to_cache(cache_dir, page, page_result, image_hash=payload.image_hash)


def load_label_from_cache(cache_dir: Path, page: PageImageRef) -> PageResult | None:
    path = label_cache_file(cache_dir, page.page_index)
    try:
        with path.open("r", encoding="utf-8") as f:
            image_hash = page.image_hash
            if image_hash:
                cached_image_hash = _read_generated_label_cache_image_hash(f)
                if cached_image_hash is not None and cached_image_hash != image_hash:
                    return None
                f.seek(0)
                if cached_image_hash == image_hash:
                    cached_result = _read_generated_label_cache_result(f)
                    if cached_result is not None:
                        return cached_result
                    f.seek(0)
            data = json.load(f)
        cached_image_hash = data.get("image_hash")
        if cached_image_hash and not image_hash:
            if _cached_image_metadata_matches(data, page.image_path):
                image_hash = cached_image_hash
            else:
                image_hash = sha256_file(page.image_path)
        if cached_image_hash != image_hash:
            return None
        row = data.get("result", {})
        return page_result_from_normalized_payload_item(row) or PageResult.model_validate(row)
    except FileNotFoundError:
        return None
    except Exception as exc:
        LOGGER.debug("Ignoring invalid Stage 1 cache entry %s: %s", path, exc)
        return None


def _read_generated_label_cache_image_hash(fh: TextIO) -> str | None:
    buffer = ""
    scanned = 0
    while scanned < _LABEL_CACHE_HASH_SCAN_LIMIT:
        chunk = fh.read(
            min(_LABEL_CACHE_HASH_READ_CHUNK_SIZE, _LABEL_CACHE_HASH_SCAN_LIMIT - scanned)
        )
        if not chunk:
            return None
        buffer += chunk
        scanned += len(chunk)

        result_index = buffer.find('"result"')
        key_index = buffer.find('"image_hash"')
        if key_index < 0:
            if result_index >= 0:
                return None
            continue
        if result_index >= 0 and result_index < key_index:
            return None

        value = _decode_label_cache_field_value(buffer, key_index, "image_hash")
        if value is _LABEL_CACHE_FIELD_INCOMPLETE:
            continue
        return value if isinstance(value, str) else None
    return None


def _read_generated_label_cache_result(fh: TextIO) -> PageResult | None:
    buffer = ""
    scanned = 0
    while scanned < _LABEL_CACHE_HASH_SCAN_LIMIT:
        chunk = fh.read(
            min(_LABEL_CACHE_HASH_READ_CHUNK_SIZE, _LABEL_CACHE_HASH_SCAN_LIMIT - scanned)
        )
        if not chunk:
            return None
        buffer += chunk
        scanned += len(chunk)

        key_index = buffer.find('"result"')
        if key_index < 0:
            continue
        value = _decode_label_cache_field_value(buffer, key_index, "result")
        if value is _LABEL_CACHE_FIELD_INCOMPLETE:
            continue
        if not isinstance(value, dict):
            return None
        return page_result_from_normalized_payload_item(value)
    return None


def _decode_label_cache_field_value(buffer: str, key_index: int, field: str) -> object:
    colon_index = buffer.find(":", key_index + len(field) + 2)
    if colon_index < 0:
        return _LABEL_CACHE_FIELD_INCOMPLETE
    value_index = _skip_json_whitespace(buffer, colon_index + 1)
    if value_index >= len(buffer):
        return _LABEL_CACHE_FIELD_INCOMPLETE
    try:
        value, _ = _JSON_DECODER.raw_decode(buffer, value_index)
    except json.JSONDecodeError:
        return _LABEL_CACHE_FIELD_INCOMPLETE
    return value


def _skip_json_whitespace(buffer: str, index: int) -> int:
    while index < len(buffer) and buffer[index] in " \t\r\n":
        index += 1
    return index


def save_label_to_cache(
    cache_dir: Path,
    page: PageImageRef,
    result: PageResult,
    *,
    image_hash: str | None = None,
) -> None:
    path = label_cache_file(cache_dir, page.page_index)
    cached_image_hash = image_hash or page.image_hash or sha256_file(page.image_path)
    payload = {
        "image_hash": cached_image_hash,
        "page_index": page.page_index,
        "image_path": str(page.image_path),
        "result": _page_result_to_jsonable(result),
    }
    payload.update(_image_cache_metadata(page.image_path))
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")


def _image_cache_metadata(image_path: Path) -> dict[str, int]:
    try:
        stat = image_path.stat()
    except OSError:
        return {}
    return {
        "image_size": stat.st_size,
        "image_mtime_ns": stat.st_mtime_ns,
    }


def _cached_image_metadata_matches(payload: dict, image_path: Path) -> bool:
    cached_size = payload.get("image_size")
    cached_mtime_ns = payload.get("image_mtime_ns")
    if not isinstance(cached_size, int) or not isinstance(cached_mtime_ns, int):
        return False
    try:
        stat = image_path.stat()
    except OSError:
        return False
    return stat.st_size == cached_size and stat.st_mtime_ns == cached_mtime_ns


def write_stage1_outputs(output_dir: Path, response: LabelResponse, page_inputs: Sequence[PageImageRef]) -> None:
    labels_json = output_dir / "labels.json"
    labels_jsonl = output_dir / "labels.jsonl"
    summary_txt = output_dir / "summary.txt"

    write_label_response_json(labels_json, response)
    page_inputs_by_index: dict[int, PageImageRef] | None = None
    jsonl_payload = _new_labels_jsonl_payload()
    counts: dict[str, int] = {}
    with labels_jsonl.open("w", encoding="utf-8") as f:
        for index, page in enumerate(response.pages):
            counts[page.label] = counts.get(page.label, 0) + 1
            page_input = page_inputs[index] if index < len(page_inputs) else None
            if page_input is None or page_input.page_index != page.page_index:
                if page_inputs_by_index is None:
                    page_inputs_by_index = {p.page_index: p for p in page_inputs}
                page_input = page_inputs_by_index[page.page_index]
            f.write(
                json.dumps(
                    _fill_labels_jsonl_payload(jsonl_payload, page, _output_image_path(page_input.image_path)),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            f.write("\n")

    with summary_txt.open("w", encoding="utf-8") as f:
        f.write(f"PDF: {response.pdf_name}\n")
        f.write(f"Total pages: {len(response.pages)}\n")
        f.write("\n")
        f.write("Counts by label:\n")
        for label in ["email", "document", "spreadsheet", "presentation", "text"]:
            f.write(f"- {label}: {counts.get(label, 0)}\n")
        f.write("\n")
        f.write("Per-page labels:\n")
        for page in response.pages:
            f.write(
                f"- page {page.page_index:04d}: {page.label} "
                f"(confidence={page.confidence:.2f}) - {page.rationale}\n"
            )
    LOGGER.info("Saved Stage 1 outputs: %s, %s, %s", labels_json, labels_jsonl, summary_txt)
    if LOGGER.isEnabledFor(logging.INFO):
        label_order = ["email", "document", "spreadsheet", "presentation", "text"]
        LOGGER.info(
            "Stage 1 labeled %s pages. Counts: %s",
            len(response.pages),
            ", ".join(f"{label}={counts.get(label, 0)}" for label in label_order if label in counts),
        )


def write_dry_run_summary(output_dir: Path, pages: Sequence[PageImageRef]) -> None:
    summary_txt = output_dir / "summary.txt"
    with summary_txt.open("w", encoding="utf-8") as f:
        f.write("Dry run: completed rendering + base64 preparation.\n")
        f.write(f"Total pages prepared: {len(pages)}\n")
        f.write("No LLM calls were made.\n")
    LOGGER.info("Dry run prepared %s pages. No labels generated.", len(pages))


def write_label_response_json(path: Path, response: LabelResponse) -> None:
    with path.open("w", encoding="utf-8") as f:
        f.write('{"schema_version":')
        json.dump(response.schema_version, f, ensure_ascii=False)
        f.write(',"pdf_name":')
        json.dump(response.pdf_name, f, ensure_ascii=False)
        f.write(',"pages":[')
        page_payload = _new_page_result_json_payload()
        for index, page in enumerate(response.pages):
            if index:
                f.write(",")
            f.write(
                json.dumps(
                    _fill_page_result_json_payload(page_payload, page),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        f.write("]}\n")


def _new_labels_jsonl_payload() -> dict[str, object]:
    return {
        "page_index": None,
        "label": None,
        "confidence": None,
        "rationale": None,
        "image_path": None,
    }


def _fill_labels_jsonl_payload(
    payload: dict[str, object],
    page: PageResult,
    image_path: str,
) -> dict[str, object]:
    payload["page_index"] = page.page_index
    payload["label"] = page.label
    payload["confidence"] = page.confidence
    payload["rationale"] = page.rationale
    payload["image_path"] = image_path
    return payload


def _output_image_path(image_path: os.PathLike[str] | str) -> str:
    raw_path = os.fspath(image_path)
    if os.path.isabs(raw_path):
        return raw_path
    return os.path.abspath(raw_path)


def _new_page_result_json_payload() -> dict[str, object]:
    return {
        "page_index": None,
        "label": None,
        "confidence": None,
        "rationale": None,
    }


def _fill_page_result_json_payload(
    payload: dict[str, object],
    page: PageResult,
) -> dict[str, object]:
    payload["page_index"] = page.page_index
    payload["label"] = page.label
    payload["confidence"] = page.confidence
    payload["rationale"] = page.rationale
    return payload


def _page_result_to_jsonable(page: PageResult) -> dict[str, object]:
    return {
        "page_index": page.page_index,
        "label": page.label,
        "confidence": page.confidence,
        "rationale": page.rationale,
    }


def load_stage1_labels_jsonl(path: Path) -> list[JsonlRow]:
    rows: list[JsonlRow] = []
    previous_page_index: int | None = None
    rows_are_ordered = True
    try:
        fh = path.open("r", encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Stage 1 labels file not found: {path}") from exc
    with fh:
        for line_no, line in enumerate(fh, start=1):
            if not _jsonl_line_has_payload(line):
                continue
            obj = json.loads(line)
            try:
                row = _jsonl_row_from_payload(obj)
            except Exception as exc:
                raise ValueError(f"Invalid labels.jsonl row at line {line_no}: {exc}") from exc
            if previous_page_index is not None and row.page_index < previous_page_index:
                rows_are_ordered = False
            previous_page_index = row.page_index
            rows.append(row)
    if not rows_are_ordered:
        _sort_label_rows(rows)
    return rows


def _sort_label_rows(rows: list[JsonlRow]) -> None:
    rows.sort(key=lambda r: r.page_index)


def _jsonl_row_from_payload(obj: object) -> JsonlRow:
    if not isinstance(obj, dict):
        return JsonlRow.model_validate(obj)

    page_index = obj.get("page_index")
    label = obj.get("label")
    confidence = obj.get("confidence")
    rationale = obj.get("rationale")
    image_path = obj.get("image_path")
    if (
        type(page_index) is int
        and page_index >= 1
        and isinstance(label, str)
        and label in _LABEL_VALUES
        and type(confidence) in (int, float)
        and 0.0 <= float(confidence) <= 1.0
        and isinstance(rationale, str)
        and isinstance(image_path, str)
    ):
        return JsonlRow.model_construct(
            page_index=page_index,
            label=label,
            confidence=float(confidence),
            rationale=rationale,
            image_path=image_path,
        )

    return JsonlRow.model_validate(obj)


def _has_non_whitespace(text: str) -> bool:
    for char in text:
        if not char.isspace():
            return True
    return False


def _jsonl_line_has_payload(line: str) -> bool:
    return bool(line) and (line[0] == "{" or _has_non_whitespace(line))
