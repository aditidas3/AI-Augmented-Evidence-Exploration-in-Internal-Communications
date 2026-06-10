from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from labeler.models.llm import LLMProvider, _truncate_for_log
from labeler.models.llm_json import (
    iter_json_candidates as _iter_json_candidates,
    normalize_batch_payload as _normalize_batch_payload,
    try_load_json_like as _try_load_json_like,
)
from labeler.models.page_classification_prompt import PAGE_CLASSIFICATION_SYSTEM_PROMPT
from labeler.models.page_input import PageInput
from labeler.core.schemas import PageResult, page_results_from_normalized_payload, parse_label_response

LOGGER = logging.getLogger(__name__)


DEFAULT_QWEN_MODEL_ID = "Qwen/Qwen3-VL-235B-A22B-Thinking"
_PAGE_CLASSIFICATION_SYSTEM_PROMPT_TEXT = PAGE_CLASSIFICATION_SYSTEM_PROMPT.strip()
_THINKING_JSON_SUFFIX = "\n\nDo not output <think> or reasoning text. Return exactly one JSON object."
_THINKING_NO_REASONING_SUFFIX = "\n\nDo not output <think> or reasoning text."
_TRIMMED_SYSTEM_PROMPT_CACHE_LIMIT = 16


@dataclass
class _QwenModelBundle:
    model: Any
    processor: Any


_GLOBAL_BUNDLE: _QwenModelBundle | None = None


def _load_qwen_model(model_id: str) -> _QwenModelBundle:
    global _GLOBAL_BUNDLE
    if _GLOBAL_BUNDLE is not None and _GLOBAL_BUNDLE.model.name_or_path == model_id:
        return _GLOBAL_BUNDLE

    try:
        from transformers import AutoProcessor, Qwen3VLMoeForConditionalGeneration
    except ImportError as exc:
        raise RuntimeError(
            "Local Qwen provider requires optional dependencies: transformers, torch, and Pillow."
        ) from exc

    LOGGER.info("Loading Qwen3-VL model locally: %s", model_id)
    model = Qwen3VLMoeForConditionalGeneration.from_pretrained(
        model_id,
        dtype="auto",
        device_map="auto",
    )
    processor = AutoProcessor.from_pretrained(model_id)
    _GLOBAL_BUNDLE = _QwenModelBundle(model=model, processor=processor)
    return _GLOBAL_BUNDLE


class QwenVLLocalClient(LLMProvider):
    """Local Qwen3-VL provider implementing the LLMProvider interface.

    This client keeps the same contract as OpenAICompatibleClient but runs
    inference locally using transformers, following the Hugging Face example:
    `Qwen/Qwen3-VL-235B-A22B-Thinking`.
    """

    def __init__(
        self,
        model_id: str | None = None,
        max_new_tokens_classify: int = 512,
        max_new_tokens_schema: int = 2048,
    ) -> None:
        resolved = (model_id or os.environ.get("QWEN_MODEL_ID") or "").strip() or DEFAULT_QWEN_MODEL_ID
        self.model_id = resolved
        self.max_new_tokens_classify = max_new_tokens_classify
        self.max_new_tokens_schema = max_new_tokens_schema
        self.max_concurrent_requests = 1
        self.requires_base64_page_payloads = False
        self._trimmed_system_prompt_cache: dict[str, str] = {}

    @property
    def _bundle(self) -> _QwenModelBundle:
        return _load_qwen_model(self.model_id)

    def classify_batch(self, pdf_name: str, total_pages: int, pages: list[PageInput]) -> list[PageResult]:
        expected_indices = [p.page_index for p in pages]
        raw_text = self._run_classification_chat(pdf_name=pdf_name, total_pages=total_pages, pages=pages)

        saw_candidate = False
        for candidate in _iter_json_candidates(raw_text):
            saw_candidate = True
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

            pages_result = page_results_from_normalized_payload(normalized_payload)
            if pages_result is None:
                try:
                    response = parse_label_response(normalized_payload)
                except ValueError:
                    continue
                pages_result = response.pages

            return pages_result

        raw_preview = _truncate_for_log(raw_text, max_chars=400)
        if not saw_candidate:
            raise RuntimeError(f"Qwen local classification returned no JSON candidates. preview={raw_preview!r}")
        raise RuntimeError(
            f"Failed to parse valid JSON for Qwen local batch pages {expected_indices}. raw_preview={raw_preview!r}"
        )

    def generate_structured_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
    ) -> str:
        """Generate a JSON object for Stage 2 using text-only prompts.

        The `model` argument is accepted for interface compatibility but ignored;
        this client always uses the configured local Qwen model.
        """
        merged_prompt = (
            f"{self._trim_system_prompt(system_prompt)}\n\n"
            f"{user_prompt}\n\n"
            "Do not output <think> or reasoning text. Return exactly one JSON object."
        )
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": merged_prompt,
                    }
                ],
            }
        ]
        return self._run_chat(messages, max_new_tokens=self.max_new_tokens_schema)

    def generate_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        temperature: float = 0.2,
    ) -> str:
        del model, temperature
        merged_prompt = (
            f"{self._trim_system_prompt(system_prompt)}\n\n"
            f"{user_prompt}{_THINKING_NO_REASONING_SUFFIX}"
        )
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": merged_prompt,
                    }
                ],
            }
        ]
        return self._run_chat(messages, max_new_tokens=self.max_new_tokens_schema)

    # Internal helpers

    def _trim_system_prompt(self, system_prompt: str) -> str:
        cached = self._trimmed_system_prompt_cache.get(system_prompt)
        if cached is not None:
            return cached
        trimmed = system_prompt.strip()
        if len(self._trimmed_system_prompt_cache) < _TRIMMED_SYSTEM_PROMPT_CACHE_LIMIT:
            self._trimmed_system_prompt_cache[system_prompt] = trimmed
        return trimmed

    def _run_classification_chat(
        self,
        *,
        pdf_name: str,
        total_pages: int,
        pages: list[PageInput],
    ) -> str:
        try:
            from PIL import Image
        except ImportError as exc:
            raise RuntimeError("Local Qwen provider requires Pillow.") from exc

        instruction = (
            f"{_PAGE_CLASSIFICATION_SYSTEM_PROMPT_TEXT}\n\n"
            f"PDF name: {pdf_name}\n"
            f"Total pages in PDF: {total_pages}\n"
            f"Classify ONLY these page_index values: {', '.join(str(p.page_index) for p in pages)}\n"
            "Return STRICT JSON with this schema:\n"
            '{'
            '"schema_version":"1.0",'
            f'"pdf_name":"{pdf_name}",'
            '"pages":[{"page_index":1,"label":"email|document|spreadsheet|presentation|text","confidence":0.0,"rationale":"<=20 words"}]'
            "}\n"
            "Do not include extra keys. Include exactly one entry per requested page_index.\n"
            "Batch page map:\n"
            + "\n".join(
                f"- page_index={p.page_index}, image_file={p.image_path.name}, image_hash={p.image_hash[:12]}"
                for p in pages
            )
            + _THINKING_JSON_SUFFIX
        )

        contents: list[dict[str, Any]] = [{"type": "text", "text": instruction}]
        converted_images: list[Any] = []
        try:
            for page in pages:
                with Image.open(page.image_path) as source:
                    img = source.convert("RGB")
                converted_images.append(img)
                contents.append({"type": "text", "text": f"Page {page.page_index} image:"})
                contents.append({"type": "image", "image": img})

            messages = [{"role": "user", "content": contents}]
            return self._run_chat(messages, max_new_tokens=self.max_new_tokens_classify)
        finally:
            for image in converted_images:
                close = getattr(image, "close", None)
                if callable(close):
                    close()

    def _run_chat(self, messages: list[dict[str, Any]], max_new_tokens: int) -> str:
        try:
            import torch
        except ImportError as exc:
            raise RuntimeError("Local Qwen provider requires torch.") from exc

        bundle = self._bundle
        model = bundle.model
        processor = bundle.processor

        inputs = processor.apply_chat_template(
            messages,
            tokenize=True,
            add_generation_prompt=True,
            return_tensors="pt",
        )

        device = model.device
        prepared_inputs: dict[str, Any] = {}
        for key, value in inputs.items():
            if hasattr(value, "to"):
                prepared_inputs[key] = value.to(device)
            else:
                prepared_inputs[key] = value

        with torch.no_grad():
            generated_ids = model.generate(
                **prepared_inputs,
                max_new_tokens=max_new_tokens,
            )

        input_len = prepared_inputs["input_ids"].shape[1]
        generated_ids_trimmed = generated_ids[:, input_len:]
        output_texts = processor.batch_decode(
            generated_ids_trimmed,
            skip_special_tokens=True,
            clean_up_tokenization_spaces=False,
        )
        return output_texts[0] if output_texts else ""
