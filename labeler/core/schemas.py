from __future__ import annotations

import io
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

Label = Literal["email", "document", "spreadsheet", "presentation", "text"]
_LIST_TYPE = list
_LABEL_VALUES = {"email", "document", "spreadsheet", "presentation", "text"}


class PageResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_index: int = Field(ge=1)
    label: Label
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str

    @field_validator("label", mode="before")
    @classmethod
    def normalize_label(cls, value: object) -> str:
        if isinstance(value, str) and value in _LABEL_VALUES:
            return value
        return _trimmed_text(str(value)).lower()

    @field_validator("rationale", mode="before")
    @classmethod
    def truncate_rationale(cls, value: object) -> str:
        text = "" if value is None else _trimmed_text(str(value))
        return _first_words_or_text(text, 20)


class LabelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    pdf_name: str
    pages: list[PageResult]

    @model_validator(mode="after")
    def validate_unique_pages(self) -> "LabelResponse":
        if not _page_indices_strictly_increase(self.pages):
            _validate_unique_page_indices_unordered(self.pages)
        return self


class RenderedPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_index: int = Field(ge=1)
    image_path: str
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    dpi: int = Field(ge=1)
    optimized_image_path: str | None = None
    llm_image_path: str


class RenderManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pdf_path: str
    dpi: int
    pages: list[RenderedPage]
    optimize_images: bool | None = None
    max_dim: int | None = None
    jpeg_quality: int | None = None
    keep_original_images: bool | None = None
    pdf_size: int | None = None
    pdf_mtime_ns: int | None = None


class JsonlRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_index: int = Field(ge=1)
    label: Label
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    image_path: str


def validate_full_coverage(response: LabelResponse, expected_pages: int) -> None:
    pages = response.pages
    if _has_sequential_full_coverage(pages, expected_pages):
        return
    if _has_unordered_full_coverage(pages, expected_pages):
        return

    got_preview = _page_index_preview(pages, 8)
    expected_preview = list(range(1, min(expected_pages, 8) + 1))
    raise ValueError(
        f"Page coverage mismatch. expected={expected_preview}... total={expected_pages}, "
        f"got={got_preview}... total={len(pages)}"
    )


def _has_sequential_full_coverage(pages: Sequence[PageResult], expected_pages: int) -> bool:
    if len(pages) != expected_pages:
        return False
    expected_page_index = 1
    for page in pages:
        if page.page_index != expected_page_index:
            return False
        expected_page_index += 1
    return True


def _has_unordered_full_coverage(pages: Sequence[PageResult], expected_pages: int) -> bool:
    if len(pages) != expected_pages:
        return False
    seen = [False] * expected_pages
    seen_count = 0
    for page in pages:
        page_index = page.page_index
        if page_index < 1 or page_index > expected_pages or seen[page_index - 1]:
            return False
        seen[page_index - 1] = True
        seen_count += 1
    return seen_count == expected_pages


def _page_indices_strictly_increase(pages: Sequence[PageResult]) -> bool:
    previous_page_index = 0
    for page in pages:
        page_index = page.page_index
        if page_index <= previous_page_index:
            return False
        previous_page_index = page_index
    return True


def _validate_unique_page_indices_unordered(pages: Sequence[PageResult]) -> None:
    seen: set[int] = set()
    for page in pages:
        page_index = page.page_index
        if page_index in seen:
            raise ValueError("page_index values must be unique")
        seen.add(page_index)


def parse_label_response(data: object) -> LabelResponse:
    try:
        return LabelResponse.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"Invalid LLM response schema: {exc}") from exc


def page_results_from_normalized_payload(payload: object) -> list[PageResult] | None:
    if not isinstance(payload, dict):
        return None
    pages = payload.get("pages")
    if not isinstance(pages, list):
        return None

    results: list[PageResult] = []
    for item in pages:
        page = page_result_from_normalized_payload_item(item)
        if page is None:
            return None
        results.append(page)
    return results


def page_result_from_normalized_payload_item(item: object) -> PageResult | None:
    if not isinstance(item, dict):
        return None
    if len(item) != 4:
        return None
    page_index = item.get("page_index")
    label = item.get("label")
    confidence = item.get("confidence")
    rationale = item.get("rationale")
    if (
        type(page_index) is int
        and page_index >= 1
        and isinstance(label, str)
        and label in _LABEL_VALUES
        and type(confidence) in (int, float)
        and 0.0 <= float(confidence) <= 1.0
        and isinstance(rationale, str)
    ):
        return PageResult.model_construct(
            page_index=page_index,
            label=label,
            confidence=float(confidence),
            rationale=PageResult.truncate_rationale(rationale),
        )
    return None


def make_label_response(pdf_name: str, pages: Sequence[PageResult]) -> LabelResponse:
    page_list = pages if isinstance(pages, _LIST_TYPE) else list(pages)
    return LabelResponse.model_construct(schema_version="1.0", pdf_name=pdf_name, pages=page_list)


def manifest_to_jsonable(manifest: RenderManifest) -> dict:
    return manifest.model_dump(mode="json")


def _first_words_or_text(text: str, max_words: int) -> str:
    if max_words <= 0:
        return ""
    in_word = False
    word_count = 0
    max_words_end = 0
    for index, char in enumerate(text):
        if char.isspace():
            if in_word:
                word_count += 1
                if word_count == max_words:
                    max_words_end = index
                elif word_count > max_words:
                    return _join_words_in_range(text, 0, max_words_end)
                in_word = False
            continue
        if not in_word:
            in_word = True
    if in_word:
        word_count += 1
        if word_count > max_words:
            return _join_words_in_range(text, 0, max_words_end)
    return text


def _join_words_in_range(text: str, start: int, end: int) -> str:
    output = io.StringIO()
    in_word = False
    word_start = start
    wrote_word = False
    for index in range(start, end):
        char = text[index]
        if char.isspace():
            if in_word:
                if wrote_word:
                    output.write(" ")
                output.write(text[word_start:index])
                wrote_word = True
                in_word = False
            continue
        if not in_word:
            word_start = index
            in_word = True
    if in_word:
        if wrote_word:
            output.write(" ")
        output.write(text[word_start:end])
    return output.getvalue()


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


def _page_index_preview(pages: Sequence[PageResult], limit: int) -> list[int]:
    preview: list[int] = []
    for page in pages:
        if len(preview) >= limit:
            break
        preview.append(page.page_index)
    return preview
