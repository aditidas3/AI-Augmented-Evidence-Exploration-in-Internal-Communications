from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ZoneText:
    left: str
    center: str
    right: str

    def to_jsonable(self) -> dict:
        return {"left": self.left, "center": self.center, "right": self.right}


@dataclass(frozen=True, slots=True)
class PageExtraction:
    page_index: int
    text_layer: str
    header: ZoneText
    footer: ZoneText
    body_text: str
    warnings: list[str]
    has_images: bool
    image_count: int

    def to_jsonable(self) -> dict:
        return {
            "page_index": self.page_index,
            "text_layer": self.text_layer,
            "header": self.header.to_jsonable(),
            "footer": self.footer.to_jsonable(),
            "body_text": self.body_text,
            "warnings": self.warnings,
            "has_images": self.has_images,
            "image_count": self.image_count,
        }


def _require_pymupdf() -> Any:
    try:
        import fitz  # type: ignore
    except ImportError as exc:
        raise RuntimeError("PyMuPDF is required. Install with `pip install pymupdf`.") from exc
    return fitz


def extract_layout_for_pages(
    pdf_path: Path,
    page_indexes: list[int],
    *,
    include_text: bool = True,
) -> dict[int, PageExtraction]:
    if not page_indexes:
        return {}

    fitz = _require_pymupdf()
    doc = fitz.open(str(pdf_path))
    try:
        results: dict[int, PageExtraction] = {}
        for page_index in _ordered_unique_page_indexes(page_indexes):
            page = doc.load_page(page_index - 1)

            # Basic visual detection: count raster images on the page.
            images = page.get_images(full=True)
            has_images = bool(images)
            image_count = len(images)

            text_layer = ""
            header = ZoneText(left="", center="", right="")
            footer = ZoneText(left="", center="", right="")
            body_text = ""
            warnings: list[str] = []
            if include_text:
                text_layer = _trimmed_text(page.get_text("text") or "")
                if text_layer:
                    span_rows = _collect_spans(page.get_text("dict"))
                    header, footer, body_text = _split_header_footer_body(
                        span_rows=span_rows,
                        page_width=float(page.rect.width),
                        page_height=float(page.rect.height),
                    )
                    if not body_text:
                        warnings.append("Text layer exists but body_text is empty after header/footer zoning.")
                else:
                    warnings.append("No text layer extracted for this page.")
            else:
                warnings.append("PDF text layer extraction disabled for this page.")

            results[page_index] = PageExtraction(
                page_index=page_index,
                text_layer=text_layer,
                header=header,
                footer=footer,
                body_text=body_text,
                warnings=warnings,
                has_images=has_images,
                image_count=image_count,
            )
        return results
    finally:
        doc.close()


def _ordered_unique_page_indexes(page_indexes: list[int]):
    if not page_indexes:
        return ()
    previous = page_indexes[0]
    unique_ordered: list[int] | None = None
    for index in range(1, len(page_indexes)):
        page_index = page_indexes[index]
        if page_index < previous:
            return sorted(dict.fromkeys(page_indexes))
        if page_index == previous:
            if unique_ordered is None:
                unique_ordered = page_indexes[:index]
            continue
        if unique_ordered is not None:
            unique_ordered.append(page_index)
        previous = page_index
    return page_indexes if unique_ordered is None else unique_ordered


def _collect_spans(text_dict: dict) -> list[tuple[float, float, float, str]]:
    spans: list[tuple[float, float, float, str]] = []
    previous_sort_key: tuple[float, float] | None = None
    spans_are_ordered = True
    for block in text_dict.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                raw_text = span.get("text") or ""
                text = _trimmed_text(raw_text if isinstance(raw_text, str) else str(raw_text))
                if not text:
                    continue
                bbox = span.get("bbox", [0, 0, 0, 0])
                x0, y0, x1, y1 = _safe_bbox(bbox)
                cx = (x0 + x1) / 2.0
                cy = (y0 + y1) / 2.0
                sort_key = (round(cy, 3), round(x0, 3))
                if previous_sort_key is not None and sort_key < previous_sort_key:
                    spans_are_ordered = False
                previous_sort_key = sort_key
                spans.append((cy, x0, cx, text))
    if not spans_are_ordered:
        _sort_span_rows(spans)
    return spans


def _sort_span_rows(spans: list[tuple[float, float, float, str]]) -> None:
    spans.sort(key=lambda v: (round(v[0], 3), round(v[1], 3)))


def _safe_bbox(bbox: list | tuple) -> tuple[float, float, float, float]:
    if len(bbox) >= 4:
        return float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])
    return 0.0, 0.0, 0.0, 0.0


def _split_header_footer_body(
    *,
    span_rows: list[tuple[float, float, float, str]],
    page_width: float,
    page_height: float,
) -> tuple[ZoneText, ZoneText, str]:
    top_limit = page_height * 0.10
    bottom_limit = page_height * 0.90
    left_limit = page_width / 3.0
    right_limit = 2.0 * page_width / 3.0

    header_left: _JoinedTextBuilder | None = None
    header_center: _JoinedTextBuilder | None = None
    header_right: _JoinedTextBuilder | None = None
    footer_left: _JoinedTextBuilder | None = None
    footer_center: _JoinedTextBuilder | None = None
    footer_right: _JoinedTextBuilder | None = None
    body_parts: _JoinedTextBuilder | None = None

    for y_center, _, cx, text in span_rows:
        if y_center <= top_limit:
            if cx < left_limit:
                header_left = _add_joined_text(header_left, text)
            elif cx < right_limit:
                header_center = _add_joined_text(header_center, text)
            else:
                header_right = _add_joined_text(header_right, text)
        elif y_center >= bottom_limit:
            if cx < left_limit:
                footer_left = _add_joined_text(footer_left, text)
            elif cx < right_limit:
                footer_center = _add_joined_text(footer_center, text)
            else:
                footer_right = _add_joined_text(footer_right, text)
        else:
            body_parts = _add_joined_text(body_parts, text)

    header = ZoneText(
        left=_joined_text(header_left),
        center=_joined_text(header_center),
        right=_joined_text(header_right),
    )
    footer = ZoneText(
        left=_joined_text(footer_left),
        center=_joined_text(footer_center),
        right=_joined_text(footer_right),
    )
    body_text = _joined_text(body_parts)
    return header, footer, body_text


def _add_joined_text(builder: "_JoinedTextBuilder | None", text: str) -> "_JoinedTextBuilder | None":
    stripped = _trimmed_text(text)
    if not stripped:
        return builder
    if builder is None:
        builder = _JoinedTextBuilder()
    builder.add_stripped(stripped)
    return builder


def _joined_text(builder: "_JoinedTextBuilder | None") -> str:
    return "" if builder is None else builder.text()


def _normalize_join(parts: list[str]) -> str:
    return " ".join(_iter_stripped_parts(parts))


def _iter_stripped_parts(parts: list[str]):
    for part in parts:
        if not part:
            continue
        stripped = _trimmed_text(part)
        if stripped:
            yield stripped


class _JoinedTextBuilder:
    def __init__(self) -> None:
        self._first_part = ""
        self._buffer: io.StringIO | None = None
        self._has_parts = False

    def add(self, text: str) -> None:
        stripped = _trimmed_text(text)
        if not stripped:
            return
        self.add_stripped(stripped)

    def add_stripped(self, stripped: str) -> None:
        if not self._has_parts:
            self._first_part = stripped
            self._has_parts = True
            return
        if self._buffer is None:
            self._buffer = io.StringIO()
            self._buffer.write(self._first_part)
        self._buffer.write(" ")
        self._buffer.write(stripped)

    def text(self) -> str:
        if not self._has_parts:
            return ""
        if self._buffer is None:
            return self._first_part
        return self._buffer.getvalue()


def _trimmed_text(text: str) -> str:
    source = text or ""
    start = 0
    end = len(source)
    while start < end and source[start].isspace():
        start += 1
    while end > start and source[end - 1].isspace():
        end -= 1
    if start == end:
        return ""
    if start == 0 and end == len(source):
        return source
    return source[start:end]
