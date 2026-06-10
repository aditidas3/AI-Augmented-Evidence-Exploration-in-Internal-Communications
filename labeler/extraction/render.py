from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TextIO

from labeler.core.schemas import RenderManifest, RenderedPage

LOGGER = logging.getLogger(__name__)
_JSON_DECODER = json.JSONDecoder()
_RENDER_MANIFEST_HEADER_READ_CHUNK_SIZE = 4096
_RENDER_MANIFEST_HEADER_SCAN_LIMIT = 64 * 1024
_RENDER_MANIFEST_FIELD_INCOMPLETE = object()
_RENDER_MANIFEST_HEADER_FIELDS = (
    "pdf_path",
    "dpi",
    "optimize_images",
    "max_dim",
    "jpeg_quality",
    "keep_original_images",
    "pdf_size",
    "pdf_mtime_ns",
)
_RENDER_MANIFEST_KEYS = frozenset(
    {
        "pdf_path",
        "dpi",
        "pages",
        "optimize_images",
        "max_dim",
        "jpeg_quality",
        "keep_original_images",
        "pdf_size",
        "pdf_mtime_ns",
    }
)
_RENDER_PAGE_KEYS = frozenset(
    {
        "page_index",
        "image_path",
        "width",
        "height",
        "dpi",
        "optimized_image_path",
        "llm_image_path",
    }
)
_REQUIRED_RENDER_PAGE_KEYS = frozenset(
    {
        "page_index",
        "image_path",
        "width",
        "height",
        "dpi",
        "llm_image_path",
    }
)


def _absolute_path(path: Path) -> str:
    return os.path.abspath(path)


def _require_pymupdf() -> object:
    try:
        import fitz  # type: ignore
    except ImportError as exc:
        raise RuntimeError("PyMuPDF is required. Install with `pip install pymupdf`.") from exc
    return fitz


def _require_pillow() -> object:
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Pillow is required. Install with `pip install pillow`.") from exc
    return Image


def render_pdf_pages(
    pdf_path: Path,
    out_dir: Path,
    dpi: int,
    optimize_images: bool,
    max_dim: int,
    jpeg_quality: int = 85,
    keep_original_images: bool = True,
) -> RenderManifest:
    pages_dir = out_dir / "pages"
    manifest_path = pages_dir / "manifest.json"
    reusable_manifest = _load_reusable_manifest(
        pdf_path=pdf_path,
        manifest_path=manifest_path,
        dpi=dpi,
        optimize_images=optimize_images,
        max_dim=max_dim,
        jpeg_quality=jpeg_quality,
        keep_original_images=keep_original_images,
    )
    if reusable_manifest is not None:
        LOGGER.info("Reusing existing render manifest: %s", manifest_path)
        return reusable_manifest

    pages_dir.mkdir(parents=True, exist_ok=True)
    fitz = _require_pymupdf()
    Image = _require_pillow() if optimize_images else None

    doc = fitz.open(str(pdf_path))
    try:
        zoom = dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        rendered: list[RenderedPage] = []
        resolved_pdf_path = _absolute_path(pdf_path)
        LOGGER.info("Rendering %s pages at %s DPI", doc.page_count, dpi)

        for idx in range(doc.page_count):
            page = doc.load_page(idx)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            page_index = idx + 1
            image_path = pages_dir / f"page_{page_index:04d}.png"
            source_image_path = image_path

            optimized_path: Path | None = None
            llm_image_path = image_path

            if optimize_images:
                optimized_path = pages_dir / f"page_{page_index:04d}.opt.jpg"
                if Image is None:
                    raise RuntimeError("Pillow is required when optimize_images is enabled.")
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                try:
                    if max_dim > 0 and (pix.width > max_dim or pix.height > max_dim):
                        img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
                    img.save(optimized_path, format="JPEG", quality=jpeg_quality, optimize=True)
                finally:
                    close = getattr(img, "close", None)
                    if callable(close):
                        close()
                llm_image_path = optimized_path
                if not keep_original_images:
                    source_image_path = optimized_path
                else:
                    pix.save(str(image_path))
            else:
                pix.save(str(image_path))

            source_image_abs = _absolute_path(source_image_path)
            optimized_image_abs = _absolute_path(optimized_path) if optimized_path else None
            if llm_image_path == source_image_path:
                llm_image_abs = source_image_abs
            elif optimized_path is not None and llm_image_path == optimized_path:
                llm_image_abs = optimized_image_abs
            else:
                llm_image_abs = _absolute_path(llm_image_path)

            rendered.append(
                RenderedPage(
                    page_index=page_index,
                    image_path=source_image_abs,
                    width=pix.width,
                    height=pix.height,
                    dpi=dpi,
                    optimized_image_path=optimized_image_abs,
                    llm_image_path=llm_image_abs,
                )
            )
    finally:
        doc.close()

    pdf_stat = pdf_path.stat()
    manifest = RenderManifest(
        pdf_path=resolved_pdf_path,
        dpi=dpi,
        pages=rendered,
        optimize_images=optimize_images,
        max_dim=max_dim,
        jpeg_quality=jpeg_quality,
        keep_original_images=keep_original_images,
        pdf_size=pdf_stat.st_size,
        pdf_mtime_ns=pdf_stat.st_mtime_ns,
    )
    _write_render_manifest(manifest_path, manifest)
    LOGGER.info("Saved render manifest: %s", manifest_path)
    return manifest


def _write_render_manifest(manifest_path: Path, manifest: RenderManifest) -> None:
    with manifest_path.open("w", encoding="utf-8") as fh:
        fh.write("{\n")
        _write_json_field(fh, "pdf_path", manifest.pdf_path, trailing=True)
        _write_json_field(fh, "dpi", manifest.dpi, trailing=True)
        _write_json_field(fh, "optimize_images", manifest.optimize_images, trailing=True)
        _write_json_field(fh, "max_dim", manifest.max_dim, trailing=True)
        _write_json_field(fh, "jpeg_quality", manifest.jpeg_quality, trailing=True)
        _write_json_field(fh, "keep_original_images", manifest.keep_original_images, trailing=True)
        _write_json_field(fh, "pdf_size", manifest.pdf_size, trailing=True)
        _write_json_field(fh, "pdf_mtime_ns", manifest.pdf_mtime_ns, trailing=True)
        fh.write('  "pages": [\n')
        for index, page in enumerate(manifest.pages):
            if index:
                fh.write(",\n")
            fh.write("    ")
            _write_rendered_page(fh, page)
        fh.write("\n  ]\n")
        fh.write("}\n")


def _write_rendered_page(fh: TextIO, page: RenderedPage) -> None:
    fh.write("{")
    _write_inline_json_field(fh, "page_index", page.page_index, first=True)
    _write_inline_json_field(fh, "image_path", page.image_path)
    _write_inline_json_field(fh, "width", page.width)
    _write_inline_json_field(fh, "height", page.height)
    _write_inline_json_field(fh, "dpi", page.dpi)
    _write_inline_json_field(fh, "optimized_image_path", page.optimized_image_path)
    _write_inline_json_field(fh, "llm_image_path", page.llm_image_path)
    fh.write("}")


def _write_json_field(fh: TextIO, name: str, value: object, *, trailing: bool) -> None:
    fh.write(f'  "{name}":')
    json.dump(value, fh, ensure_ascii=False)
    fh.write(",\n" if trailing else "\n")


def _write_inline_json_field(fh: TextIO, name: str, value: object, *, first: bool = False) -> None:
    if not first:
        fh.write(",")
    fh.write(f'"{name}":')
    json.dump(value, fh, ensure_ascii=False)


def _load_reusable_manifest(
    *,
    pdf_path: Path,
    manifest_path: Path,
    dpi: int,
    optimize_images: bool,
    max_dim: int,
    jpeg_quality: int,
    keep_original_images: bool,
) -> RenderManifest | None:
    resolved_pdf = _absolute_path(pdf_path)
    try:
        pdf_stat = pdf_path.stat()
    except OSError:
        return None

    try:
        with manifest_path.open("r", encoding="utf-8") as fh:
            if _render_manifest_header_mismatch(
                fh,
                resolved_pdf=resolved_pdf,
                pdf_size=pdf_stat.st_size,
                pdf_mtime_ns=pdf_stat.st_mtime_ns,
                dpi=dpi,
                optimize_images=optimize_images,
                max_dim=max_dim,
                jpeg_quality=jpeg_quality,
                keep_original_images=keep_original_images,
            ):
                return None
            fh.seek(0)
            payload = json.load(fh)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None

    if not _manifest_cache_metadata_matches(
        payload,
        resolved_pdf=resolved_pdf,
        pdf_size=pdf_stat.st_size,
        pdf_mtime_ns=pdf_stat.st_mtime_ns,
        dpi=dpi,
        optimize_images=optimize_images,
        max_dim=max_dim,
        jpeg_quality=jpeg_quality,
        keep_original_images=keep_original_images,
    ):
        return None

    manifest = _construct_generated_manifest(payload)
    if manifest is None:
        try:
            manifest = RenderManifest.model_validate(payload)
        except Exception:
            return None

    if manifest.pdf_path != resolved_pdf:
        return None
    if manifest.dpi != dpi:
        return None
    if manifest.optimize_images is not optimize_images:
        return None
    if manifest.max_dim != max_dim:
        return None
    if manifest.jpeg_quality != jpeg_quality:
        return None
    manifest_keep_original = True if manifest.keep_original_images is None else manifest.keep_original_images
    if keep_original_images and not manifest_keep_original:
        return None
    if manifest.pdf_size != pdf_stat.st_size:
        return None
    if manifest.pdf_mtime_ns != pdf_stat.st_mtime_ns:
        return None
    if not manifest.pages:
        return None

    for page in manifest.pages:
        if not os.path.isfile(page.image_path):
            return None
        if page.llm_image_path != page.image_path and not os.path.isfile(page.llm_image_path):
            return None
        if optimize_images:
            if not page.optimized_image_path:
                return None
            if (
                page.optimized_image_path != page.image_path
                and page.optimized_image_path != page.llm_image_path
                and not os.path.isfile(page.optimized_image_path)
            ):
                return None
        elif page.optimized_image_path:
            return None
    return manifest


def _construct_generated_manifest(payload: dict) -> RenderManifest | None:
    if not _has_only_keys(payload, _RENDER_MANIFEST_KEYS):
        return None

    pdf_path = payload.get("pdf_path")
    dpi = payload.get("dpi")
    optimize_images = payload.get("optimize_images")
    max_dim = payload.get("max_dim")
    jpeg_quality = payload.get("jpeg_quality")
    keep_original_images = payload.get("keep_original_images")
    pdf_size = payload.get("pdf_size")
    pdf_mtime_ns = payload.get("pdf_mtime_ns")
    pages_payload = payload.get("pages")

    if not isinstance(pdf_path, str):
        return None
    if not _is_real_int(dpi) or dpi < 1:
        return None
    if optimize_images is not None and not isinstance(optimize_images, bool):
        return None
    if max_dim is not None and not _is_real_int(max_dim):
        return None
    if jpeg_quality is not None and not _is_real_int(jpeg_quality):
        return None
    if keep_original_images is not None and not isinstance(keep_original_images, bool):
        return None
    if pdf_size is not None and not _is_real_int(pdf_size):
        return None
    if pdf_mtime_ns is not None and not _is_real_int(pdf_mtime_ns):
        return None
    if not isinstance(pages_payload, list) or not pages_payload:
        return None

    pages: list[RenderedPage] = []
    for page_payload in pages_payload:
        page = _construct_generated_page(page_payload)
        if page is None:
            return None
        pages.append(page)

    return RenderManifest.model_construct(
        pdf_path=pdf_path,
        dpi=dpi,
        pages=pages,
        optimize_images=optimize_images,
        max_dim=max_dim,
        jpeg_quality=jpeg_quality,
        keep_original_images=keep_original_images,
        pdf_size=pdf_size,
        pdf_mtime_ns=pdf_mtime_ns,
    )


def _construct_generated_page(payload: object) -> RenderedPage | None:
    if not isinstance(payload, dict):
        return None
    if not _has_only_keys(payload, _RENDER_PAGE_KEYS) or not _has_required_keys(
        payload,
        _REQUIRED_RENDER_PAGE_KEYS,
    ):
        return None

    page_index = payload.get("page_index")
    image_path = payload.get("image_path")
    width = payload.get("width")
    height = payload.get("height")
    dpi = payload.get("dpi")
    optimized_image_path = payload.get("optimized_image_path")
    llm_image_path = payload.get("llm_image_path")

    if not _is_real_int(page_index) or page_index < 1:
        return None
    if not isinstance(image_path, str):
        return None
    if not _is_real_int(width) or width < 1:
        return None
    if not _is_real_int(height) or height < 1:
        return None
    if not _is_real_int(dpi) or dpi < 1:
        return None
    if optimized_image_path is not None and not isinstance(optimized_image_path, str):
        return None
    if not isinstance(llm_image_path, str):
        return None

    return RenderedPage.model_construct(
        page_index=page_index,
        image_path=image_path,
        width=width,
        height=height,
        dpi=dpi,
        optimized_image_path=optimized_image_path,
        llm_image_path=llm_image_path,
    )


def _is_real_int(value: object) -> bool:
    return type(value) is int


def _has_only_keys(payload: dict, allowed_keys: frozenset[str]) -> bool:
    for key in payload:
        if key not in allowed_keys:
            return False
    return True


def _has_required_keys(payload: dict, required_keys: frozenset[str]) -> bool:
    for key in required_keys:
        if key not in payload:
            return False
    return True


def _render_manifest_header_mismatch(
    fh: TextIO,
    *,
    resolved_pdf: str,
    pdf_size: int,
    pdf_mtime_ns: int,
    dpi: int,
    optimize_images: bool,
    max_dim: int,
    jpeg_quality: int,
    keep_original_images: bool,
) -> bool:
    buffer = ""
    scanned = 0
    while scanned < _RENDER_MANIFEST_HEADER_SCAN_LIMIT:
        chunk = fh.read(
            min(_RENDER_MANIFEST_HEADER_READ_CHUNK_SIZE, _RENDER_MANIFEST_HEADER_SCAN_LIMIT - scanned)
        )
        if not chunk:
            return False
        buffer += chunk
        scanned += len(chunk)

        pages_index = buffer.find('"pages"')
        for field in _RENDER_MANIFEST_HEADER_FIELDS:
            key_index = buffer.find(f'"{field}"')
            if key_index < 0:
                continue
            if pages_index >= 0 and pages_index < key_index:
                continue
            value = _decode_render_manifest_field_value(buffer, key_index, field)
            if value is _RENDER_MANIFEST_FIELD_INCOMPLETE:
                continue
            if _render_manifest_header_value_mismatch(
                field,
                value,
                resolved_pdf=resolved_pdf,
                pdf_size=pdf_size,
                pdf_mtime_ns=pdf_mtime_ns,
                dpi=dpi,
                optimize_images=optimize_images,
                max_dim=max_dim,
                jpeg_quality=jpeg_quality,
                keep_original_images=keep_original_images,
            ):
                return True

        if pages_index >= 0:
            return False
    return False


def _decode_render_manifest_field_value(buffer: str, key_index: int, field: str) -> object:
    colon_index = buffer.find(":", key_index + len(field) + 2)
    if colon_index < 0:
        return _RENDER_MANIFEST_FIELD_INCOMPLETE
    value_index = _skip_json_whitespace(buffer, colon_index + 1)
    if value_index >= len(buffer):
        return _RENDER_MANIFEST_FIELD_INCOMPLETE
    try:
        value, _ = _JSON_DECODER.raw_decode(buffer, value_index)
    except json.JSONDecodeError:
        return _RENDER_MANIFEST_FIELD_INCOMPLETE
    return value


def _render_manifest_header_value_mismatch(
    field: str,
    value: object,
    *,
    resolved_pdf: str,
    pdf_size: int,
    pdf_mtime_ns: int,
    dpi: int,
    optimize_images: bool,
    max_dim: int,
    jpeg_quality: int,
    keep_original_images: bool,
) -> bool:
    if field == "pdf_path":
        return isinstance(value, str) and value != resolved_pdf
    if field == "dpi":
        return type(value) is int and value != dpi
    if field == "optimize_images":
        return isinstance(value, bool) and value is not optimize_images
    if field == "max_dim":
        return type(value) is int and value != max_dim
    if field == "jpeg_quality":
        return type(value) is int and value != jpeg_quality
    if field == "keep_original_images":
        manifest_keep_original = True if value is None else value
        return isinstance(manifest_keep_original, bool) and keep_original_images and not manifest_keep_original
    if field == "pdf_size":
        return type(value) is int and value != pdf_size
    if field == "pdf_mtime_ns":
        return type(value) is int and value != pdf_mtime_ns
    return False


def _skip_json_whitespace(buffer: str, index: int) -> int:
    while index < len(buffer) and buffer[index] in " \t\r\n":
        index += 1
    return index


def _manifest_cache_metadata_matches(
    payload: dict,
    *,
    resolved_pdf: str,
    pdf_size: int,
    pdf_mtime_ns: int,
    dpi: int,
    optimize_images: bool,
    max_dim: int,
    jpeg_quality: int,
    keep_original_images: bool,
) -> bool:
    if payload.get("pdf_path") != resolved_pdf:
        return False
    if payload.get("dpi") != dpi:
        return False
    if payload.get("optimize_images") != optimize_images:
        return False
    if payload.get("max_dim") != max_dim:
        return False
    if payload.get("jpeg_quality") != jpeg_quality:
        return False
    manifest_keep_original = payload.get("keep_original_images")
    if manifest_keep_original is None:
        manifest_keep_original = True
    if keep_original_images and not manifest_keep_original:
        return False
    if payload.get("pdf_size") != pdf_size:
        return False
    if payload.get("pdf_mtime_ns") != pdf_mtime_ns:
        return False
    if not payload.get("pages"):
        return False
    return True
