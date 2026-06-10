from __future__ import annotations

import ast
import json
import re
from typing import Any, Iterable

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
_PAGE_INDEX_KEYS = ("page_index", "pageIndex", "page", "page_number", "pageNumber")
_LABEL_VALUES = {"email", "document", "spreadsheet", "presentation", "text"}
_INT_TYPE = int
_LABEL_ALIASES = {
    "doc": "document",
    "docs": "document",
    "slide": "presentation",
    "slides": "presentation",
    "ppt": "presentation",
    "powerpoint": "presentation",
    "sheet": "spreadsheet",
    "excel": "spreadsheet",
    "mail": "email",
}


def extract_json_object_text(text: str) -> str | None:
    start, end = _non_whitespace_bounds(text)
    if start < 0:
        return None
    if text[start] == "{" and text[end] == "}":
        return text if start == 0 and end == len(text) - 1 else text[start : end + 1]
    if "{" not in text:
        return None
    return next(_iter_balanced_snippets(text, "{", "}", limit=1), None)


def iter_json_candidates(text: str) -> Iterable[str]:
    source = text or ""
    if "<" in source and _THINK_BLOCK_RE.search(source):
        source = _THINK_BLOCK_RE.sub(" ", source)
    first_seen: str | None = None
    seen: set[str] | None = None

    def should_yield(value: str) -> str | None:
        nonlocal first_seen, seen
        candidate = _trimmed_text(value)
        if not candidate:
            return None
        if first_seen is None:
            first_seen = candidate
            return candidate
        if candidate == first_seen:
            return None
        if seen is None:
            seen = {first_seen}
        if candidate in seen:
            return None
        seen.add(candidate)
        return candidate

    start, end = _non_whitespace_bounds(source)
    if start >= 0 and source[start] in "{[":
        if _matching_close_char(source[start]) == source[end]:
            candidate = should_yield(source if start == 0 and end == len(source) - 1 else source[start : end + 1])
            if candidate is not None:
                yield candidate
                if start == 0 and end == len(source) - 1:
                    return

    if "```" in source:
        for fence in _JSON_FENCE_RE.finditer(source):
            candidate = should_yield(fence.group(1))
            if candidate is not None:
                yield candidate

    if "{" in source:
        for obj in _iter_balanced_snippets(source, "{", "}"):
            candidate = should_yield(obj)
            if candidate is not None:
                yield candidate
    if "[" in source:
        for arr in _iter_balanced_snippets(source, "[", "]"):
            candidate = should_yield(arr)
            if candidate is not None:
                yield candidate


def _matching_close_char(open_char: str) -> str:
    return "}" if open_char == "{" else "]"


def try_load_json_like(text: str) -> Any | None:
    candidate = _trimmed_text(text or "")
    if not candidate:
        return None

    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    if "," in candidate:
        repaired_candidate = _remove_trailing_commas_json(candidate)
        if repaired_candidate is not candidate:
            try:
                return json.loads(repaired_candidate)
            except json.JSONDecodeError:
                pass

    try:
        loaded = ast.literal_eval(candidate)
    except Exception:
        return None

    if isinstance(loaded, (dict, list)):
        return loaded
    return None


def normalize_batch_payload(*, loaded: Any, pdf_name: str, expected_indices: list[int]) -> dict[str, Any] | None:
    pages_raw = _extract_pages_list(loaded)
    if pages_raw is None:
        return None

    if len(expected_indices) == 1:
        page = _normalize_single_expected_page(pages_raw, expected_indices[0])
        if page is None:
            return None
        return {
            "schema_version": "1.0",
            "pdf_name": pdf_name,
            "pages": [page],
        }

    ordered_pages = _normalize_pages_in_expected_order(pages_raw, expected_indices)
    if ordered_pages is not None:
        return {
            "schema_version": "1.0",
            "pdf_name": pdf_name,
            "pages": ordered_pages,
        }

    expected_set = set(expected_indices)
    pages_by_index: dict[int, dict[str, Any]] = {}
    for item in pages_raw:
        if not isinstance(item, dict):
            continue
        page_index = _extract_page_index(item)
        if page_index is None or page_index not in expected_set:
            continue
        if page_index in pages_by_index:
            continue

        page = _normalize_page_payload_item(item, page_index)
        if page is None:
            continue
        pages_by_index[page_index] = page

    if len(pages_by_index) != len(expected_set):
        return None

    ordered_pages = [pages_by_index[idx] for idx in expected_indices]
    return {
        "schema_version": "1.0",
        "pdf_name": pdf_name,
        "pages": ordered_pages,
    }


def _normalize_pages_in_expected_order(pages_raw: list[Any], expected_indices: list[int]) -> list[dict[str, Any]] | None:
    if len(pages_raw) != len(expected_indices):
        return None

    pages: list[dict[str, Any]] = []
    for item, expected_index in zip(pages_raw, expected_indices):
        if not isinstance(item, dict):
            return None
        page_index = _extract_page_index(item)
        if page_index != expected_index:
            return None
        page = _normalize_page_payload_item(item, page_index)
        if page is None:
            return None
        pages.append(page)
    return pages


def _normalize_single_expected_page(pages_raw: list[Any], expected_index: int) -> dict[str, Any] | None:
    for item in pages_raw:
        if not isinstance(item, dict):
            continue
        page_index = _extract_page_index(item)
        if page_index != expected_index:
            continue
        page = _normalize_page_payload_item(item, page_index)
        if page is not None:
            return page
    return None


def _normalize_page_payload_item(item: dict[str, Any], page_index: int) -> dict[str, Any] | None:
    if "label" in item:
        label_value = item["label"]
    elif "type" in item:
        label_value = item["type"]
    else:
        label_value = item.get("category", "")
    label = _normalize_label(label_value)
    if label not in _LABEL_VALUES:
        return None

    if "confidence" in item:
        confidence_value = item["confidence"]
    elif "score" in item:
        confidence_value = item["score"]
    else:
        confidence_value = item.get("probability", 0.0)
    confidence = _normalize_confidence(confidence_value)
    if "rationale" in item:
        rationale_value = item["rationale"]
    elif "reason" in item:
        rationale_value = item["reason"]
    else:
        rationale_value = item.get("explanation", "")
    rationale = _normalize_text_value(rationale_value)
    return {
        "page_index": page_index,
        "label": label,
        "confidence": confidence,
        "rationale": rationale,
    }


def parse_json_object(text: str) -> dict[str, Any]:
    extracted = extract_json_object_text(text)
    if extracted is None:
        raise ValueError("No JSON object found in model response.")
    loaded = json.loads(extracted)
    if not isinstance(loaded, dict):
        raise ValueError("Model response must be a JSON object.")
    return loaded


def _extract_balanced_snippets(
    text: str,
    open_char: str,
    close_char: str,
    *,
    limit: int | None = None,
) -> list[str]:
    return list(
        _iter_balanced_snippets(
            text,
            open_char,
            close_char,
            limit=limit,
        )
    )


def _iter_balanced_snippets(
    text: str,
    open_char: str,
    close_char: str,
    *,
    limit: int | None = None,
) -> Iterable[str]:
    emitted = 0
    stack_depth = 0
    start = -1
    in_string = False
    escape = False

    for idx, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
            continue

        if ch == open_char:
            if stack_depth == 0:
                start = idx
            stack_depth += 1
            continue

        if ch == close_char and stack_depth > 0:
            stack_depth -= 1
            if stack_depth == 0 and start >= 0:
                yield text[start : idx + 1]
                emitted += 1
                if limit is not None and emitted >= limit:
                    return
                start = -1


def _remove_trailing_commas_json(text: str) -> str:
    out: list[str] | None = None
    in_string = False
    escape = False
    i = 0

    while i < len(text):
        ch = text[i]
        if in_string:
            if out is not None:
                out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            i += 1
            continue

        if ch == '"':
            in_string = True
            if out is not None:
                out.append(ch)
            i += 1
            continue

        if ch == ",":
            j = i + 1
            while j < len(text) and text[j] in " \t\r\n":
                j += 1
            if j < len(text) and text[j] in "}]":
                if out is None:
                    out = [text[:i]]
                i += 1
                continue

        if out is not None:
            out.append(ch)
        i += 1

    if out is None:
        return text
    return "".join(out)


def _normalize_label(value: Any) -> str:
    if isinstance(value, str):
        if value in _LABEL_VALUES:
            return value
        alias = _LABEL_ALIASES.get(value)
        if alias is not None:
            return alias
    raw = _normalize_text_value(value).lower()
    return _LABEL_ALIASES.get(raw, raw)


def _normalize_text_value(value: Any) -> str:
    raw = value or ""
    return _trimmed_text(raw if isinstance(raw, str) else str(raw))


def _normalize_confidence(value: Any) -> float:
    try:
        conf = float(value)
    except (TypeError, ValueError):
        return 0.0
    if conf > 1.0 and conf <= 100.0:
        conf = conf / 100.0
    if conf < 0.0:
        conf = 0.0
    if conf > 1.0:
        conf = 1.0
    return conf


def _extract_page_index(page: dict[str, Any]) -> int | None:
    for key in _PAGE_INDEX_KEYS:
        value = page.get(key)
        if value is None:
            continue
        if type(value) is _INT_TYPE:
            idx = value
        else:
            try:
                idx = int(value)
            except (TypeError, ValueError):
                continue
        if idx >= 1:
            return idx
    return None


def _trimmed_text(text: str) -> str:
    start, end = _non_whitespace_bounds(text)
    if start < 0:
        return ""
    if start == 0 and end == len(text) - 1:
        return text
    return text[start : end + 1]


def _non_whitespace_bounds(text: str) -> tuple[int, int]:
    if not text:
        return -1, -1

    start = 0
    end = len(text) - 1
    if not text[start].isspace() and not text[end].isspace():
        return start, end

    while start <= end and text[start].isspace():
        start += 1
    while end >= start and text[end].isspace():
        end -= 1
    if start > end:
        return -1, -1
    return start, end


def _extract_pages_list(loaded: Any) -> list[Any] | None:
    if isinstance(loaded, list):
        return loaded
    if not isinstance(loaded, dict):
        return None
    if isinstance(loaded.get("pages"), list):
        return loaded["pages"]
    if isinstance(loaded.get("results"), list):
        return loaded["results"]
    if isinstance(loaded.get("labels"), list):
        return loaded["labels"]
    return None
