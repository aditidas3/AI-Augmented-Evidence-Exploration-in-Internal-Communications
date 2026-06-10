from __future__ import annotations

"""
label_pdf_pages CLI

Examples:
  python labeler/cli.py --pdf input.pdf --out out_dir
  python labeler/cli.py --pdf batch.zip --out out_dir
  python labeler/cli.py --input notes.txt --out out_dir
  python labeler/cli.py --pdf input.pdf --out out_dir --provider vllm --model Qwen/Qwen3-VL-235B-A22B-Thinking
  python labeler/cli.py --pdf input.pdf --out out_dir --batch-size 6 --schema-model gpt-4.1
  python labeler/cli.py --pdf input.pdf --out out_dir --no-extract-schema
"""

import argparse
import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
from argparse import Namespace
from collections import deque
from collections.abc import Mapping
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
import tempfile
import zipfile

from dotenv import load_dotenv

from labeler.pipeline.document_source import (
    is_supported_input_filename,
    is_supported_zip_member_filename,
    is_zip_source_path,
)
from labeler.pipeline.orchestrator import PipelineConfig, run_pipeline
from labeler.pipeline.progress import initialize_progress_csv, update_progress_row, update_progress_rows

load_dotenv()

_PROGRESS_UPDATE_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class DocumentInput:
    path: Path
    doc_id: str


def _split_visible_devices(value: str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    normalized = value.strip()
    if not normalized or normalized.casefold() in {"none", "void", "-1", "all"}:
        return ()
    return tuple(part.strip() for part in normalized.split(",") if part.strip())


def _resolve_cuda_worker_slots(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    env_map = os.environ if env is None else env
    cuda_devices = _split_visible_devices(env_map.get("CUDA_VISIBLE_DEVICES"))
    if cuda_devices:
        return cuda_devices

    nvidia_devices = _split_visible_devices(env_map.get("NVIDIA_VISIBLE_DEVICES"))
    if nvidia_devices:
        return tuple(str(index) for index in range(len(nvidia_devices)))

    if env is None:
        return _detect_cuda_slots_with_nvidia_smi()
    return ()


def _detect_cuda_slots_with_nvidia_smi() -> tuple[str, ...]:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ()

    slots = tuple(line.strip() for line in result.stdout.splitlines() if line.strip())
    return slots


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="label_pdf_pages",
        description=(
            "Process PDFs and text-like documents, classify PDF pages, then optionally "
            "extract entities and relationships from document text."
        ),
    )
    parser.add_argument(
        "--pdf",
        "--input",
        dest="pdf",
        required=True,
        type=Path,
        help=(
            "Path to a PDF, text-like file, zip batch, or a directory containing supported files. "
            "Text-like files include txt, csv, xml, json, html, markdown, yaml, logs, and extensionless text."
        ),
    )
    parser.add_argument("--out", required=True, type=Path, help="Output directory (each document writes to --out/<id>/).")
    parser.add_argument(
        "--id",
        dest="doc_id",
        default=None,
        help="Document id for output subfolder (default: input filename without extension).",
    )

    parser.add_argument("--dpi", type=int, default=200, help="Render DPI (default: 200).")
    parser.add_argument("--batch-size", type=int, default=4, help="Stage 1 pages per LLM request (default: 4).")
    parser.add_argument(
        "--max-dim",
        type=int,
        default=1600,
        help="Max image dimension for optimized LLM images (default: 1600).",
    )
    parser.add_argument(
        "--optimize-images",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Save optimized JPEG copies for LLM payload reduction (default: true).",
    )

    parser.add_argument(
        "--provider",
        default="endpoint",
        choices=["endpoint", "openrouter", "deepseek", "vllm", "qwen-local"],
        help=(
            "LLM backend for Stage 1 and default Stage 2: "
            "endpoint=OPENAI_* env vars, openrouter=OPENROUTER_* env vars, "
            "deepseek=DEEPSEEK_* env vars, vllm=VLLM_* env vars or http://127.0.0.1:8000/v1, "
            "qwen-local=local transformers model from labeler.models.qwen."
        ),
    )

    parser.add_argument(
        "--model",
        default=None,
        help="Stage 1 model name override. If omitted, the selected provider uses its environment defaults.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Prepare the input but skip all LLM calls.")

    parser.add_argument(
        "--extract-schema",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable Stage 2 entity/relationship extraction (default: true).",
    )
    parser.add_argument(
        "--schema-model",
        default=None,
        help="Stage 2 model override (default: --model, otherwise the provider default model).",
    )
    parser.add_argument(
        "--schema-provider",
        default=None,
        choices=["endpoint", "openrouter", "deepseek", "vllm", "qwen-local"],
        help="Stage 2 provider override (default: --provider). Use deepseek to route schema extraction to DEEPSEEK_*.",
    )
    parser.add_argument(
        "--llm-validation-retries",
        type=int,
        default=3,
        help="Extra Stage 2 LLM regeneration attempts after invalid or empty output (default: 3).",
    )
    parser.add_argument(
        "--max-concurrent",
        type=int,
        default=1,
        help="Max concurrent LLM requests for Stage 1 batches (default: 10).",
    )
    parser.add_argument(
        "--document-concurrency",
        type=int,
        default=10,
        help="Number of documents to process in parallel for directory or zip inputs (default: 1).",
    )
    parser.add_argument(
        "--gpu-memory-utilization",
        type=float,
        default=None,
        metavar="FRACTION",
        help="vLLM GPU memory fraction (0.0–1.0) for olmocr. Use when free GPU memory is low (e.g. 0.09 for ~2 GiB free).",
    )
    parser.add_argument(
        "--skip-stage2-if-exists",
        action="store_true",
        help=(
            "Skip Stage 2 (OCR + entity/relationship extraction) when non-empty relationship.txt already exists in the output dir. "
            "If labels.json also exists, the completed document is skipped before input preparation."
        ),
    )

    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO).",
    )
    return parser


def _validate_args(parser: argparse.ArgumentParser, args: Namespace) -> Path:
    pdf_path: Path = args.pdf
    if not pdf_path.exists():
        parser.error(f"--pdf does not exist: {pdf_path}")
    if args.dpi <= 0:
        parser.error("--dpi must be >= 1")
    if args.batch_size <= 0:
        parser.error("--batch-size must be >= 1")
    if args.max_concurrent <= 0:
        parser.error("--max-concurrent must be >= 1")
    if args.document_concurrency <= 0:
        parser.error("--document-concurrency must be >= 1")
    if args.llm_validation_retries < 0:
        parser.error("--llm-validation-retries must be >= 0")
    return pdf_path


def _build_pipeline_config(
    args: Namespace,
    *,
    pdf_path: Path,
    doc_id: str,
    cuda_visible_devices: str | None = None,
    progress_callback=None,
) -> PipelineConfig:
    return PipelineConfig(
        pdf_path=pdf_path,
        output_dir=args.out / doc_id,
        dpi=args.dpi,
        batch_size=args.batch_size,
        max_dim=args.max_dim,
        optimize_images=args.optimize_images,
        provider_name=args.provider,
        model=args.model,
        dry_run=args.dry_run,
        extract_schema=args.extract_schema,
        schema_provider_name=args.schema_provider,
        schema_model=args.schema_model,
        llm_validation_retries=args.llm_validation_retries,
        max_concurrent=args.max_concurrent,
        gpu_memory_utilization=args.gpu_memory_utilization,
        cuda_visible_devices=cuda_visible_devices,
        progress_callback=progress_callback,
        skip_stage2_if_output_exists=args.skip_stage2_if_exists,
    )


def _run_one_document(
    args: Namespace,
    *,
    pdf_path: Path,
    doc_id: str,
    update_progress: bool = True,
    cuda_visible_devices: str | None = None,
) -> bool:
    log = logging.getLogger(__name__)
    progress_callback = _build_progress_callback(args, doc_id) if update_progress else None
    config = _build_pipeline_config(
        args,
        pdf_path=pdf_path,
        doc_id=doc_id,
        cuda_visible_devices=cuda_visible_devices,
        progress_callback=progress_callback,
    )

    if cuda_visible_devices is None:
        log.info("Output directory: %s (doc id: %s)", config.output_dir, doc_id)
    else:
        log.info(
            "Output directory: %s (doc id: %s, CUDA_VISIBLE_DEVICES=%s)",
            config.output_dir,
            doc_id,
            cuda_visible_devices,
        )
    try:
        run_pipeline(config)
        return True
    except Exception as exc:
        log.error("Pipeline failed for %s: %s", pdf_path.name, exc)
        return False
    finally:
        if update_progress:
            _update_progress_row_safely(args, doc_id)


def _build_progress_callback(args: Namespace, doc_id: str):
    return lambda: _update_progress_row_safely(args, doc_id)


def _update_progress_row_safely(args: Namespace, doc_id: str) -> None:
    log = logging.getLogger(__name__)
    try:
        with _PROGRESS_UPDATE_LOCK:
            update_progress_row(args.out, doc_id)
    except Exception as exc:
        log.error("Failed to update progress CSV for %s: %s", doc_id, exc)


def _update_progress_rows_safely(args: Namespace, doc_ids: list[str]) -> None:
    if not doc_ids:
        return
    log = logging.getLogger(__name__)
    try:
        with _PROGRESS_UPDATE_LOCK:
            update_progress_rows(args.out, doc_ids)
    except Exception as exc:
        log.error("Failed to update progress CSV for %s document(s): %s", len(doc_ids), exc)


def _list_pdf_files(pdf_dir: Path) -> list[Path]:
    pdf_files: list[Path] = []
    with os.scandir(pdf_dir) as entries:
        for entry in entries:
            if _is_pdf_filename(entry.name) and entry.is_file():
                pdf_files.append(Path(entry.path))
    pdf_files.sort()
    return pdf_files


def _is_pdf_filename(name: str) -> bool:
    return len(name) >= 4 and name[-4] == "." and name[-3:].casefold() == "pdf"


def _list_input_files(input_dir: Path) -> list[Path]:
    input_files: list[Path] = []
    with os.scandir(input_dir) as entries:
        for entry in entries:
            if is_supported_input_filename(entry.name) and entry.is_file():
                input_files.append(Path(entry.path))
    input_files.sort()
    return input_files


def _run_documents_in_directory(args: Namespace, document_inputs: list[DocumentInput | Path]) -> int:
    log = logging.getLogger(__name__)
    documents = [_coerce_document_input(item) for item in document_inputs]
    cuda_slots = _resolve_cuda_worker_slots()
    if args.document_concurrency <= 1:
        failed = 0
        completed_doc_ids: list[str] = []
        cuda_visible_devices = cuda_slots[0] if cuda_slots else None
        for document in documents:
            if not _run_one_document(
                args,
                pdf_path=document.path,
                doc_id=document.doc_id,
                update_progress=True,
                cuda_visible_devices=cuda_visible_devices,
            ):
                failed += 1
            completed_doc_ids.append(document.doc_id)
        _update_progress_rows_safely(args, completed_doc_ids)
        return failed

    max_workers = min(args.document_concurrency, len(documents))
    if cuda_slots:
        max_workers = min(max_workers, len(cuda_slots))
    log.info(
        "Document concurrency: requested=%s, effective=%s. Stage 1 per-document max-concurrent=%s. GPU slots=%s.",
        args.document_concurrency,
        max_workers,
        args.max_concurrent,
        len(cuda_slots) if cuda_slots else "unassigned",
    )
    failed = 0
    completed_doc_ids: list[str] = []
    document_iter = iter(documents)
    available_cuda_slots = deque(cuda_slots)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {}

        def _submit_next_document() -> bool:
            try:
                document = next(document_iter)
            except StopIteration:
                return False
            cuda_visible_devices = available_cuda_slots.popleft() if available_cuda_slots else None
            futures[
                executor.submit(
                    _run_one_document,
                    args,
                    pdf_path=document.path,
                    doc_id=document.doc_id,
                    update_progress=True,
                    cuda_visible_devices=cuda_visible_devices,
                )
            ] = (document, cuda_visible_devices)
            return True

        for _ in range(max_workers):
            if not _submit_next_document():
                break

        while futures:
            done, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                document, cuda_visible_devices = futures.pop(future)
                ok = False
                try:
                    ok = bool(future.result())
                except Exception as exc:
                    log.error("Pipeline worker failed for %s: %s", document.path.name, exc)
                completed_doc_ids.append(document.doc_id)
                if not ok:
                    failed += 1
                if cuda_visible_devices is not None:
                    available_cuda_slots.append(cuda_visible_devices)
                _submit_next_document()
    _update_progress_rows_safely(args, completed_doc_ids)
    return failed


def _coerce_document_input(document: DocumentInput | Path) -> DocumentInput:
    if isinstance(document, DocumentInput):
        return document
    return DocumentInput(path=document, doc_id=document.stem)


def _collect_input_documents(input_path: Path, *, temp_root: Path) -> list[DocumentInput]:
    if input_path.is_dir():
        documents: list[DocumentInput] = []
        for path in _list_input_files(input_path):
            documents.extend(_document_inputs_for_file(path, temp_root=temp_root))
        return _dedupe_document_ids(documents)
    return _dedupe_document_ids(_document_inputs_for_file(input_path, temp_root=temp_root))


def _document_inputs_for_file(path: Path, *, temp_root: Path) -> list[DocumentInput]:
    if is_zip_source_path(path):
        return _extract_zip_documents(path, temp_root=temp_root)
    if not is_supported_input_filename(path.name):
        return []
    return [DocumentInput(path=path, doc_id=_sanitize_doc_id_part(path.stem) or "document")]


def _extract_zip_documents(zip_path: Path, *, temp_root: Path) -> list[DocumentInput]:
    archive_root = temp_root / _archive_extract_dir_name(zip_path)
    archive_root.mkdir(parents=True, exist_ok=True)
    documents: list[DocumentInput] = []
    with zipfile.ZipFile(zip_path) as zf:
        members = sorted(
            (info for info in zf.infolist() if not info.is_dir()),
            key=lambda info: info.filename,
        )
        for info in members:
            relative_path = _safe_zip_member_path(info.filename)
            if relative_path is None:
                logging.getLogger(__name__).warning("Skipping unsafe zip member: %s", info.filename)
                continue
            if not is_supported_zip_member_filename(relative_path.name):
                continue
            target = archive_root.joinpath(*relative_path.parts)
            _ensure_within_directory(archive_root, target)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination)
            documents.append(
                DocumentInput(
                    path=target,
                    doc_id=_zip_member_doc_id(relative_path),
                )
            )
    return documents


def _archive_extract_dir_name(zip_path: Path) -> str:
    digest = hashlib.sha1(str(zip_path.resolve()).encode("utf-8")).hexdigest()[:8]
    return f"{_sanitize_doc_id_part(zip_path.stem) or 'archive'}_{digest}"


def _safe_zip_member_path(name: str) -> PurePosixPath | None:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute():
        return None
    parts: list[str] = []
    for part in path.parts:
        if not part or part == "." or part == ".." or ":" in part:
            return None
        parts.append(part)
    if not parts:
        return None
    return PurePosixPath(*parts)


def _ensure_within_directory(root: Path, target: Path) -> None:
    root_resolved = root.resolve()
    target_resolved = target.resolve()
    try:
        target_resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(f"Unsafe zip extraction target: {target}") from exc


def _zip_member_doc_id(path: PurePosixPath) -> str:
    parts = list(path.parts)
    parts[-1] = PurePosixPath(parts[-1]).stem
    doc_id = "__".join(
        part for part in (_sanitize_doc_id_part(part) for part in parts) if part
    )
    return doc_id or "document"


def _dedupe_document_ids(documents: list[DocumentInput]) -> list[DocumentInput]:
    seen: dict[str, int] = {}
    deduped: list[DocumentInput] = []
    for document in documents:
        base_id = document.doc_id or "document"
        count = seen.get(base_id, 0) + 1
        seen[base_id] = count
        doc_id = base_id if count == 1 else f"{base_id}__{count}"
        deduped.append(DocumentInput(path=document.path, doc_id=doc_id))
    return deduped


_DOC_ID_UNSAFE_RE = re.compile(r"[^0-9A-Za-z._-]+")


def _sanitize_doc_id_part(value: str) -> str:
    text = _DOC_ID_UNSAFE_RE.sub("_", value.strip())
    return text.strip("._-")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    input_path = _validate_args(parser, args)
    log = logging.getLogger(__name__)

    with tempfile.TemporaryDirectory(prefix="labeler_inputs_") as tmp:
        try:
            documents = _collect_input_documents(input_path, temp_root=Path(tmp))
        except zipfile.BadZipFile:
            parser.error(f"Invalid zip file: {input_path}")
        except ValueError as exc:
            parser.error(str(exc))

        if not documents:
            parser.error(f"No supported input files found: {input_path}")

        doc_id_override = (args.doc_id or "").strip()
        if doc_id_override:
            if len(documents) != 1:
                parser.error("--id can only be used when the input resolves to one document.")
            documents = [DocumentInput(path=documents[0].path, doc_id=doc_id_override)]

        for document in documents:
            if not document.doc_id:
                parser.error("Document id cannot be empty (use --id or ensure the input has a name).")

        log.info("Processing %s document(s) from %s", len(documents), input_path)
        progress_csv = initialize_progress_csv(args.out, (document.doc_id for document in documents))
        log.info("Progress CSV initialized: %s", progress_csv)

        failed = _run_documents_in_directory(args, documents)

        if failed:
            log.error("Completed with %s failure(s) out of %s document(s).", failed, len(documents))
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main())
