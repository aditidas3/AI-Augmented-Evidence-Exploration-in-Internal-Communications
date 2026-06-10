"""
Extract text from scanned PDFs using the olmocr vision-language model (local GPU).

olmocr (https://github.com/allenai/olmocr) converts PDFs and image-based documents
into clean, structured plain text with support for equations, tables, handwriting,
and complex multi-column layouts.

Designed for Nextflow: use --output to write to file for process output channels;
exits 0 on success, 1 on failure.

Requirements:
- Python >= 3.11
- NVIDIA GPU with >= 12GB VRAM
- poppler-utils and fonts (Ubuntu: sudo apt-get install poppler-utils ttf-mscorefonts-installer ...)

Install: pip install olmocr[gpu] --extra-index-url https://download.pytorch.org/whl/cu128
"""

import os
import subprocess
import sys
import tempfile
import logging
from pathlib import Path

LOGGER = logging.getLogger(__name__)


def _normalize_input_paths(input_paths: list[str]) -> list[str]:
    if not input_paths:
        raise ValueError("At least one input path is required.")

    normalized_paths: list[str] = []
    stems: set[str] = set()
    for input_path in input_paths:
        absolute_path = os.path.abspath(input_path)
        if not os.path.exists(absolute_path):
            raise FileNotFoundError(f"PDF not found: {absolute_path}")
        stem = Path(absolute_path).stem
        if stem in stems:
            raise ValueError(
                "olmocr batch inputs must have unique filename stems. "
                f"Duplicate stem detected: {stem}"
            )
        stems.add(stem)
        normalized_paths.append(absolute_path)
    return normalized_paths


def _build_olmocr_command(
    *,
    workspace: str,
    input_paths: list[str],
    model: str,
    workers: int,
    gpu_memory_utilization: float | None,
) -> list[str]:
    cmd = [
        sys.executable,
        "-m",
        "olmocr.pipeline",
        workspace,
        "--markdown",
        "--pdfs",
        *input_paths,
        "--model",
        model,
        "--workers",
        str(workers),
        "--pages_per_group",
        "1",
        "--max_concurrent_requests",
        "2",
    ]
    if gpu_memory_utilization is not None:
        cmd.extend(["--gpu-memory-utilization", str(gpu_memory_utilization)])
    return cmd


def _format_olmocr_output(result: subprocess.CompletedProcess[str]) -> str:
    output = "\n".join(
        part.strip()
        for part in (result.stderr, result.stdout)
        if part and part.strip()
    )
    return output or "<no olmocr stdout/stderr captured>"


def _run_olmocr_pipeline(
    *,
    workspace: str,
    input_paths: list[str],
    model: str,
    workers: int,
    gpu_memory_utilization: float | None,
    cuda_visible_devices: str | None,
) -> subprocess.CompletedProcess[str]:
    cmd = _build_olmocr_command(
        workspace=workspace,
        input_paths=input_paths,
        model=model,
        workers=workers,
        gpu_memory_utilization=gpu_memory_utilization,
    )

    env = os.environ.copy()
    if cuda_visible_devices is not None:
        env["CUDA_VISIBLE_DEVICES"] = cuda_visible_devices

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("olmocr pipeline timed out")

    if result.returncode != 0:
        raise RuntimeError(
            f"olmocr pipeline failed (exit {result.returncode}):\n{_format_olmocr_output(result)}"
        )
    return result


def _resolve_markdown_output_path(workspace: str, input_path: str) -> str | None:
    stem = Path(input_path).stem
    candidate_stems = [stem]
    if stem.endswith(".opt"):
        candidate_stems.append(stem[: -len(".opt")])

    md_dir = os.path.join(workspace, "markdown")
    for candidate_stem in candidate_stems:
        direct_path = os.path.join(md_dir, f"{candidate_stem}.md")
        if os.path.exists(direct_path):
            return direct_path

    if not os.path.isdir(md_dir):
        return None

    for root, _, files in os.walk(md_dir):
        for filename in files:
            for candidate_stem in candidate_stems:
                if filename == f"{candidate_stem}.md":
                    return os.path.join(root, filename)
    return None


def extract_text_from_files(
    input_paths: list[str],
    workspace: str | None = None,
    model: str = "allenai/olmOCR-2-7B-1025-FP8",
    workers: int = 4,
    keep_workspace: bool = False,
    gpu_memory_utilization: float | None = None,
    cuda_visible_devices: str | None = None,
) -> dict[str, str]:
    normalized_paths = _normalize_input_paths(input_paths)

    use_temp = workspace is None
    if use_temp:
        workspace = tempfile.mkdtemp(prefix="olmocr_workspace_")

    workspace = os.path.abspath(workspace)
    os.makedirs(workspace, exist_ok=True)

    texts_by_path: dict[str, str] = {}
    try:
        for batch_index, start in enumerate(range(0, len(normalized_paths), 32), start=1):
            batch_paths = normalized_paths[start : start + 32]
            batch_workspace = workspace if len(normalized_paths) <= 32 else os.path.join(workspace, f"batch_{batch_index:04d}")
            os.makedirs(batch_workspace, exist_ok=True)
            result = _run_olmocr_pipeline(
                workspace=batch_workspace,
                input_paths=batch_paths,
                model=model,
                workers=workers,
                gpu_memory_utilization=gpu_memory_utilization,
                cuda_visible_devices=cuda_visible_devices,
            )
            for input_path in batch_paths:
                md_path = _resolve_markdown_output_path(batch_workspace, input_path)
                if not md_path or not os.path.exists(md_path):
                    LOGGER.warning(
                        "olmocr did not produce markdown output for %s; treating this page as empty OCR text. "
                        "batch_workspace=%s olmocr_output=%s",
                        Path(input_path).stem,
                        batch_workspace,
                        _format_olmocr_output(result),
                    )
                    texts_by_path[input_path] = ""
                    continue
                with open(md_path, "r", encoding="utf-8") as handle:
                    texts_by_path[input_path] = handle.read()
        return texts_by_path
    finally:
        if use_temp and not keep_workspace:
            import shutil

            shutil.rmtree(workspace, ignore_errors=True)


def extract_text_from_pdf(
    pdf_path: str,
    workspace: str | None = None,
    model: str = "allenai/olmOCR-2-7B-1025-FP8",
    workers: int = 4,
    keep_workspace: bool = False,
    gpu_memory_utilization: float | None = None,
    cuda_visible_devices: str | None = None,
) -> str:
    """
    Extract text from a scanned PDF using the olmocr model (local GPU).

    Args:
        pdf_path: Path to the PDF file (or path to PNG/JPEG image).
        workspace: Directory for olmocr output. If None, a temp dir is used.
        model: Model identifier (default: allenai/olmOCR-2-7B-1025-FP8).
        workers: Number of workers.
        keep_workspace: If True, do not delete workspace when using temp dir.
        gpu_memory_utilization: Fraction of GPU memory for vLLM (0.0–1.0). If set,
            passed to vLLM via VLLM_GPU_MEMORY_UTILIZATION. Use when free GPU memory
            is less than vLLM's default (0.9), e.g. shared GPU or low headroom
            (e.g. 0.09 for ~2 GiB free on a 22 GiB device).
        cuda_visible_devices: Optional CUDA_VISIBLE_DEVICES value for the olmocr
            subprocess.

    Returns:
        Extracted text as a string (plain text / markdown).

    Raises:
        FileNotFoundError: If the PDF does not exist.
        RuntimeError: If olmocr pipeline fails.
    """
    normalized_pdf_path = os.path.abspath(pdf_path)
    texts_by_path = extract_text_from_files(
        [normalized_pdf_path],
        workspace=workspace,
        model=model,
        workers=workers,
        keep_workspace=keep_workspace,
        gpu_memory_utilization=gpu_memory_utilization,
        cuda_visible_devices=cuda_visible_devices,
    )
    return texts_by_path[normalized_pdf_path]


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Extract text from scanned PDFs using olmocr (local GPU)"
    )
    parser.add_argument("pdf_path", help="Path to PDF (or PNG/JPEG)")
    parser.add_argument(
        "--workspace",
        default=None,
        help="Output workspace (default: temp dir)",
    )
    parser.add_argument(
        "--model",
        default="allenai/olmOCR-2-7B-1025-FP8",
        help="Model name (default: allenai/olmOCR-2-7B-1025-FP8)",
    )
    parser.add_argument(
        "--keep-workspace",
        action="store_true",
        help="Keep workspace directory after run",
    )
    parser.add_argument(
        "--gpu-memory-utilization",
        type=float,
        default=None,
        metavar="FRACTION",
        help="vLLM GPU memory fraction (0.0–1.0). Use when free GPU memory is low (e.g. 0.09 for ~2 GiB free).",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Write extracted text to file (for Nextflow output)",
    )
    args = parser.parse_args()

    try:
        text = extract_text_from_pdf(
            pdf_path=args.pdf_path,
            workspace=args.workspace,
            model=args.model,
            keep_workspace=args.keep_workspace,
            gpu_memory_utilization=args.gpu_memory_utilization,
        )
        if args.output:
            path = os.path.abspath(args.output)
            out_dir = os.path.dirname(path)
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        else:
            print(text)
        sys.exit(0)
    except Exception as e:
        print(e, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
