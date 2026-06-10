from __future__ import annotations

import io
import json
import logging
import os
import random

from dotenv import load_dotenv
import time
import urllib.error
import urllib.request
from typing import Any, Protocol

from labeler.core.schemas import PageResult, page_results_from_normalized_payload, parse_label_response
from labeler.models.llm_json import (
    iter_json_candidates as _iter_json_candidates,
    normalize_batch_payload as _normalize_batch_payload,
    try_load_json_like as _try_load_json_like,
    extract_json_object_text,
    parse_json_object,
)
from labeler.models.page_input import PageInput, detect_mime_type, file_to_base64
from labeler.models.page_classification_prompt import PAGE_CLASSIFICATION_SYSTEM_PROMPT

load_dotenv()

LOGGER = logging.getLogger(__name__)

SYSTEM_PROMPT = PAGE_CLASSIFICATION_SYSTEM_PROMPT
_SYSTEM_PROMPT_TEXT = SYSTEM_PROMPT.strip()
_THINKING_JSON_SUFFIX = "\n\nDo not output <think> or reasoning text. Return exactly one JSON object."
_THINKING_NO_REASONING_SUFFIX = "\n\nDo not output <think> or reasoning text."
_TRIMMED_SYSTEM_PROMPT_CACHE_LIMIT = 16


class LLMProvider(Protocol):
    def classify_batch(self, pdf_name: str, total_pages: int, pages: list[PageInput]) -> list[PageResult]:
        ...

    def generate_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        temperature: float = 0.2,
    ) -> str:
        ...

    def generate_structured_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
    ) -> str:
        ...


class OpenAICompatibleClient:
    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str = "https://api.openai.com",
        max_retries: int = 4,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._chat_completions_url = (
            f"{self.base_url}/chat/completions"
            if self.base_url.endswith("/v1")
            else f"{self.base_url}/v1/chat/completions"
        )
        self.max_retries = max_retries
        self._headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        self._model_is_qwen_thinking = self._is_qwen_thinking_model(self.model)
        self._base_url_is_openrouter = "openrouter.ai" in self.base_url.lower()
        self._trimmed_system_prompt_cache: dict[str, str] = {}

    @classmethod
    def from_env(
        cls,
        model: str | None = None,
        *,
        env_prefix: str = "OPENAI",
        default_base_url: str = "https://api.openai.com",
        default_api_key: str | None = None,
    ) -> "OpenAICompatibleClient":
        prefix = env_prefix.strip().upper().rstrip("_") or "OPENAI"
        api_key = (
            os.environ.get(f"{prefix}_KEY", "").strip()
            or os.environ.get(f"{prefix}_API_KEY", "").strip()
            or (default_api_key or "").strip()
        )
        if not api_key:
            raise RuntimeError(
                f"Missing {prefix}_KEY or {prefix}_API_KEY. Set it in .env or your environment before running label_pdf_pages."
            )
        resolved_model = (model or "").strip() or os.environ.get(f"{prefix}_MODEL", "").strip()
        if not resolved_model:
            raise RuntimeError(
                f"Missing {prefix}_MODEL. Set it in .env or pass --model before running label_pdf_pages."
            )
        base_url = os.environ.get(f"{prefix}_BASE_URL", "").strip() or default_base_url
        return cls(model=resolved_model, api_key=api_key, base_url=base_url)

    def classify_batch(self, pdf_name: str, total_pages: int, pages: list[PageInput]) -> list[PageResult]:
        expected_indices = [p.page_index for p in pages]
        parsed, raw_text, repaired_text = self._classify_batch_with_repair(
            pdf_name=pdf_name,
            total_pages=total_pages,
            pages=pages,
            expected_indices=expected_indices,
        )
        if parsed is not None:
            return parsed

        # Safety net: recover by classifying one page at a time when a multi-page batch is unrecoverable.
        if len(pages) > 1:
            LOGGER.warning(
                "Batch parse still invalid for pages %s; retrying per-page classification fallback.",
                expected_indices,
            )
            recovered: list[PageResult] = []
            for page in pages:
                single_parsed, single_raw, single_repaired = self._classify_batch_with_repair(
                    pdf_name=pdf_name,
                    total_pages=total_pages,
                    pages=[page],
                    expected_indices=[page.page_index],
                )
                if single_parsed is None:
                    raw_preview = _truncate_for_log(single_raw, max_chars=400)
                    repaired_preview = _truncate_for_log(single_repaired, max_chars=400)
                    raise RuntimeError(
                        f"Failed to parse valid JSON for page {page.page_index} after per-page fallback. "
                        f"raw_preview={raw_preview!r} repaired_preview={repaired_preview!r}"
                    )
                recovered.extend(single_parsed)
            return recovered

        raw_preview = _truncate_for_log(raw_text, max_chars=400)
        repaired_preview = _truncate_for_log(repaired_text, max_chars=400)
        raise RuntimeError(
            f"Failed to parse valid JSON for batch pages {expected_indices} after repair attempt. "
            f"raw_preview={raw_preview!r} repaired_preview={repaired_preview!r}"
        )

    def _classify_batch_with_repair(
        self,
        *,
        pdf_name: str,
        total_pages: int,
        pages: list[PageInput],
        expected_indices: list[int],
    ) -> tuple[list[PageResult] | None, str, str]:
        payload = self._build_batch_payload(pdf_name=pdf_name, total_pages=total_pages, pages=pages)
        raw_text = self._chat_completion(payload)
        parsed = self._parse_batch_response(raw_text, pdf_name, expected_indices)
        if parsed is not None:
            return parsed, raw_text, ""

        LOGGER.warning("Invalid JSON from model. Retrying once with repair prompt for pages %s", expected_indices)
        repair_payload = self._build_repair_payload(
            pdf_name=pdf_name,
            total_pages=total_pages,
            expected_indices=expected_indices,
            invalid_response=raw_text,
        )
        repaired_text = self._chat_completion(repair_payload)
        repaired = self._parse_batch_response(repaired_text, pdf_name, expected_indices)
        return repaired, raw_text, repaired_text

    def generate_structured_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
    ) -> str:
        payload = self._build_text_payload(
            system_prompt=system_prompt,
            user_prompt=f"{user_prompt}\n\nDo not output <think> or reasoning text. Return exactly one JSON object.",
            model=model,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        return self._chat_completion(payload)

    def generate_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        temperature: float = 0.2,
    ) -> str:
        payload = self._build_text_payload(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model,
            temperature=temperature,
            response_format=None,
        )
        return self._chat_completion(payload)

    def _build_batch_payload(self, pdf_name: str, total_pages: int, pages: list[PageInput]) -> dict:
        qwen_thinking = self._model_is_qwen_thinking
        instruction = (
            f"PDF name: {pdf_name}\n"
            f"Total pages in PDF: {total_pages}\n"
            f"Classify ONLY these page_index values: {', '.join(str(p.page_index) for p in pages)}\n"
            "Return STRICT JSON with this schema:\n"
            '{'
            '"schema_version":"1.0",'
            f'"pdf_name":"{pdf_name}",'
            '"pages":[{"page_index":1,"label":"email|document|spreadsheet|presentation|text","confidence":0.0,"rationale":"<=20 words"}]'
            '}\n'
            "Do not include extra keys. Include exactly one entry per requested page_index.\n"
            "Batch page map:\n"
            + "\n".join(
                f"- page_index={p.page_index}, image_file={p.image_path.name}, image_hash={p.image_hash[:12]}"
                for p in pages
            )
        )
        if qwen_thinking:
            instruction = f"{_SYSTEM_PROMPT_TEXT}\n\n{instruction}{_THINKING_JSON_SUFFIX}"
        user_content: list[dict] = [{"type": "text", "text": instruction}]
        for p in pages:
            user_content.append({"type": "text", "text": f"Page {p.page_index} image:"})
            user_content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{p.mime_type};base64,{p.image_b64}"},
                }
            )

        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": 0.6 if qwen_thinking else 0,
            "messages": (
                [{"role": "user", "content": user_content}]
                if qwen_thinking
                else [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_content}]
            ),
        }
        if not qwen_thinking:
            payload["response_format"] = {"type": "json_object"}
        if qwen_thinking:
            payload["top_p"] = 0.95
            payload["top_k"] = 20
        self._apply_openrouter_reasoning_options(payload)
        return payload

    def _build_repair_payload(
        self,
        pdf_name: str,
        total_pages: int,
        expected_indices: list[int],
        invalid_response: str,
    ) -> dict:
        qwen_thinking = self._model_is_qwen_thinking
        user_prompt = (
            "Fix the invalid output and return STRICT JSON only.\n"
            f"PDF name: {pdf_name}\n"
            f"Total pages in PDF: {total_pages}\n"
            f"Required page_index values: {expected_indices}\n"
            "Schema:\n"
            '{'
            '"schema_version":"1.0",'
            f'"pdf_name":"{pdf_name}",'
            '"pages":[{"page_index":1,"label":"email|document|spreadsheet|presentation|text","confidence":0.0,"rationale":"<=20 words"}]'
            "}\n"
            "One object per page_index. No extra text.\n"
            "Invalid output follows:\n"
            f"{invalid_response}"
        )
        if qwen_thinking:
            user_prompt = f"{_SYSTEM_PROMPT_TEXT}\n\n{user_prompt}{_THINKING_JSON_SUFFIX}"

        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": 0.6 if qwen_thinking else 0,
            "messages": (
                [{"role": "user", "content": user_prompt}]
                if qwen_thinking
                else [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_prompt}]
            ),
        }
        if not qwen_thinking:
            payload["response_format"] = {"type": "json_object"}
        if qwen_thinking:
            payload["top_p"] = 0.95
            payload["top_k"] = 20
        self._apply_openrouter_reasoning_options(payload)
        return payload

    def _chat_completion(self, payload: dict) -> str:
        url = self._chat_completions_url
        payload_to_send = payload
        response_format_fallback_used = False
        serialized_body: bytes | None = None

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                if serialized_body is None:
                    serialized_body = json.dumps(payload_to_send, separators=(",", ":")).encode("utf-8")
                req = urllib.request.Request(url=url, data=serialized_body, headers=self._headers, method="POST")
                with urllib.request.urlopen(req) as resp:
                    raw = resp.read()
                parsed = json.loads(raw)
                return self._extract_assistant_text(parsed)
            except urllib.error.HTTPError as exc:
                last_error = exc
                code = exc.code
                message = self._read_http_error_message(exc)
                if (
                    code == 400
                    and "response_format" in message.lower()
                    and "response_format" in payload_to_send
                    and not response_format_fallback_used
                ):
                    LOGGER.debug("Retrying request without response_format due to provider compatibility.")
                    payload_to_send = dict(payload_to_send)
                    payload_to_send.pop("response_format", None)
                    response_format_fallback_used = True
                    serialized_body = None
                    continue
                retryable = code in {408, 409, 429, 500, 502, 503, 504}
                if not retryable or attempt >= self.max_retries:
                    raise RuntimeError(f"LLM request failed (HTTP {code}): {message}") from exc
                self._sleep_backoff(attempt)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError) as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    raise RuntimeError(f"LLM request failed after retries: {exc}") from exc
                self._sleep_backoff(attempt)

        raise RuntimeError(f"LLM request failed: {last_error}")

    def _build_text_payload(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str | None,
        temperature: float,
        response_format: dict[str, Any] | None,
    ) -> dict[str, Any]:
        selected_model = model or self.model
        qwen_thinking = (
            self._model_is_qwen_thinking
            if selected_model == self.model
            else self._is_qwen_thinking_model(selected_model)
        )
        if qwen_thinking:
            merged_user_prompt = (
                f"{self._trim_system_prompt(system_prompt)}\n\n{user_prompt}{_THINKING_NO_REASONING_SUFFIX}"
            )
            messages: list[dict[str, Any]] = [{"role": "user", "content": merged_user_prompt}]
        else:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]

        payload: dict[str, Any] = {
            "model": selected_model,
            "temperature": 0.6 if qwen_thinking else temperature,
            "messages": messages,
        }
        if response_format is not None and not qwen_thinking:
            payload["response_format"] = response_format
        if qwen_thinking:
            payload["top_p"] = 0.95
            payload["top_k"] = 20
        self._apply_openrouter_reasoning_options(payload)
        return payload

    def _sleep_backoff(self, attempt: int) -> None:
        delay = (2**attempt) + random.uniform(0, 0.3)
        LOGGER.debug("Retrying LLM request in %.2f seconds", delay)
        time.sleep(delay)

    def _trim_system_prompt(self, system_prompt: str) -> str:
        cached = self._trimmed_system_prompt_cache.get(system_prompt)
        if cached is not None:
            return cached
        trimmed = system_prompt.strip()
        if len(self._trimmed_system_prompt_cache) < _TRIMMED_SYSTEM_PROMPT_CACHE_LIMIT:
            self._trimmed_system_prompt_cache[system_prompt] = trimmed
        return trimmed

    def _extract_assistant_text(self, response_json: dict) -> str:
        choices = response_json.get("choices")
        if not choices:
            raise KeyError("No choices in model response")
        message = choices[0].get("message", {})
        content = message.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = ""
            text_parts: io.StringIO | None = None
            json_text = ""
            json_text_parts: io.StringIO | None = None
            saw_text = False
            saw_json_text = False
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    item_text = str(item.get("text", ""))
                    item_has_json = "{" in item_text
                    if saw_text:
                        if not saw_json_text and not item_has_json:
                            if text_parts is None:
                                text_parts = io.StringIO()
                                text_parts.write(text)
                            text_parts.write("\n")
                            text_parts.write(item_text)
                    else:
                        text = item_text
                        saw_text = True
                    if item_has_json:
                        if saw_json_text:
                            if json_text_parts is None:
                                json_text_parts = io.StringIO()
                                json_text_parts.write(json_text)
                            json_text_parts.write("\n")
                            json_text_parts.write(item_text)
                        else:
                            json_text = item_text
                            saw_json_text = True
            if not saw_text:
                raise KeyError("No text content in model response")
            # Prefer the segment that contains JSON (thinking models may return e.g. "0.95" then the real answer).
            if saw_json_text:
                text = json_text_parts.getvalue() if json_text_parts is not None else json_text
            elif text_parts is not None:
                text = text_parts.getvalue()
        else:
            raise KeyError("No text content in model response")
        # If content looks like a bare number or too short, check for JSON elsewhere (e.g. reasoning excluded but answer in another field).
        if _should_check_reasoning_for_json(text):
            alt = message.get("reasoning") or message.get("reasoning_content")
            if isinstance(alt, str) and "{" in alt:
                return alt
            if isinstance(alt, list):
                for item in alt:
                    if isinstance(item, dict) and "{" in str(item.get("text", item.get("content", ""))):
                        return str(item.get("text", item.get("content", "")))
        return text

    def _is_qwen_thinking_model(self, model_name: str) -> bool:
        lowered = model_name.strip().lower()
        return "qwen" in lowered and "thinking" in lowered

    def _is_openrouter(self) -> bool:
        return self._base_url_is_openrouter

    def _apply_openrouter_reasoning_options(self, payload: dict[str, Any]) -> None:
        if not self._base_url_is_openrouter:
            return
        # Keep reasoning internal and reduce noisy reasoning traces in returned assistant content.
        payload["reasoning"] = {"exclude": True}
        payload["include_reasoning"] = False

    def _parse_batch_response(
        self, raw_text: str, pdf_name: str, expected_indices: list[int]
    ) -> list[PageResult] | None:
        for candidate in _iter_json_candidates(raw_text):
            loaded = _try_load_json_like(candidate)
            if loaded is None:
                continue

            normalized_payload = _normalize_batch_payload(
                loaded=loaded,
                pdf_name=pdf_name,
                expected_indices=expected_indices,
            )
            if normalized_payload is None:
                continue

            pages = page_results_from_normalized_payload(normalized_payload)
            if pages is None:
                try:
                    response = parse_label_response(normalized_payload)
                except ValueError:
                    continue
                pages = response.pages

            return pages

        return None

    @staticmethod
    def _read_http_error_message(exc: urllib.error.HTTPError) -> str:
        try:
            body = exc.read().decode("utf-8", errors="replace")
            if body:
                return body[:500]
        except Exception:
            return str(exc)
        return str(exc)


def _truncate_for_log(text: str, max_chars: int = 400) -> str:
    if max_chars <= 0:
        return ""
    output: list[str] = []
    pending_space = False
    for char in str(text):
        if char.isspace():
            if output:
                pending_space = True
            continue
        if pending_space and output:
            if len(output) >= max_chars:
                return _truncate_log_chars(output, max_chars)
            output.append(" ")
            pending_space = False
        if len(output) >= max_chars:
            return _truncate_log_chars(output, max_chars)
        output.append(char)
    return "".join(output)


def _truncate_log_chars(chars: list[str], max_chars: int) -> str:
    if max_chars <= 3:
        return "." * max_chars
    return "".join(chars[: max_chars - 3]) + "..."


def _should_check_reasoning_for_json(text: str) -> bool:
    if "{" in text or "[" in text:
        return False
    start, end = _non_whitespace_bounds(text)
    length = end - start
    if length < 50:
        return True

    saw_digit = False
    for index in range(start, end):
        char = text[index]
        if char == ".":
            continue
        if not char.isdigit():
            return False
        saw_digit = True
    return saw_digit


def _non_whitespace_bounds(text: str) -> tuple[int, int]:
    start = 0
    end = len(text)
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end
