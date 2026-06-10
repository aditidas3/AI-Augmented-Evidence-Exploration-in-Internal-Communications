"""
map_transform.py — MAP-TRANSFORM: Evidence Transformation Classifier

Consumes sets of source and target EvidenceNode UIDs from an Evidence
Graph (EG), identifies transformation relationships between them via
embedding similarity and LLM classification, and writes the results
back into the EG according to the EG-RG schema v1.0.0.

Architecture:
    TRACE ──produces──▶ source/target UIDs
    MAP-TRANSFORM ──reads/writes──▶ EG

Phase Summary
─────────────
  Phase 0   Precondition guards: verify agent, label set, EG root
  Phase 1   Read source and target EvidenceNodes
  Phase 2   Pair generation: embed, cosine similarity, top-K
  Phase 3   Classification: LLM label, DERIVES_FROM edge, Derived node,
            REFERENCES edges, ProvenanceEvent, ReliabilityFactor
  Phase 4   Consistency filter and retraction
  Phase 5   Return

Schema Compliance
─────────────────
  - All EvidenceNodes carry required uid, type, label, createdAt
  - All evidence-to-evidence edges carry the common property set
    (uid, confidence, justification, assertedByUid, assertedAt,
     domainMetadata)
  - DERIVES_FROM direction: target -[:DERIVES_FROM]→ source.
    Reciprocal source/target candidate pairs are canonicalized before
    classification so MAP-TRANSFORM cannot introduce lineage cycles.
  - ProvenanceEvent uses timestamp / notes / domainMetadata
  - ReliabilityFactor uses factor / impact / notes
  - Retraction sets lifecycleStatus, retractedAt, retractedReason
  - Action enum uses S1-amendment value 'retracted'
  - All scores clamped to [0.0, 1.0]
  - domainMetadata passed as native dict  (Map type)
  - tags passed as native list             (String[] type)

Compatibility
─────────────
  This module's InMemoryGraphStore satisfies the GraphWriter protocol
  owned by TRACE (pipeline.trace.writers) and adds get_node and
  set_properties for read/update.  An InMemoryGraphStore instance
  can be passed directly to Trace as the ``eg`` writer and then
  reused here, sharing all nodes and edges by reference.
"""

from __future__ import annotations

import collections
import concurrent.futures
import json
import logging
import math
import os
import re
import time as _time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Protocol,
    Set,
    Tuple,
    Callable,
    runtime_checkable,
)
from urllib.parse import urlparse


_ROOT_DOTENV_LOADED = False
_DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
_DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-pro"


def _load_root_dotenv_once() -> None:
    """Load root .env for live LLM calls without overriding process env."""
    global _ROOT_DOTENV_LOADED
    if _ROOT_DOTENV_LOADED:
        return
    _ROOT_DOTENV_LOADED = True

    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(env_path, override=False)


def _first_env(*names: str) -> Optional[str]:
    for name in names:
        if not name:
            continue
        value = os.getenv(name)
        if value:
            return value
    return None


def _first_env_pair(*names: str) -> Tuple[Optional[str], Optional[str]]:
    for name in names:
        if not name:
            continue
        value = os.getenv(name)
        if value:
            return name, value
    return None, None


def _is_openrouter_base_url(base_url: str) -> bool:
    return urlparse(base_url).netloc.lower().endswith("openrouter.ai")

logger = logging.getLogger(__name__)

# ── Namespace UUID for deterministic ID generation ──────────
# Separate from trace2.TRACE_NS to prevent seed collisions.
MAP_TRANSFORM_NS = uuid.UUID("c4a8e92d-1f3b-4d7c-9e5a-2bc64f8d1a70")


# ═══════════════════════════════════════════════════════════════
#  SECTION 1 — Configuration
# ═══════════════════════════════════════════════════════════════

@dataclass
class MapTransformConfig:
    """
    All tuneable knobs for a MAP-TRANSFORM execution.

    Attributes whose names start with ``tau_`` are threshold gates;
    everything else is a structural or policy parameter.
    """

    # ── Schema constants ────────────────────────────────────
    schema_version: str = "1.0.0"

    # ── Agent identity ──────────────────────────────────────
    transform_agent_uid: str = "agent::map_transform"
    transform_agent_name: str = "MAP-TRANSFORM Classifier"

    # ── Pair selection ──────────────────────────────────────
    K_p: int = 50                       # max candidate pairs
    tau_map: float = 0.3                # min cosine similarity

    # ── Impact classification (§1.2 ClassifyImpact) ─────────
    tau_impact_positive: float = 0.7
    tau_impact_neutral: float = 0.4

    # ── Consistency filter ──────────────────────────────────
    max_mappings_per_target: int = 3    # keep top-N per target

    # ── LLM classifier backend ──────────────────────────────
    classifier_backend: str = "heuristic"  # heuristic | deepseek
    deepseek_model: str = "deepseek-v4-pro"
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_api_key_env: str = "DEEPSEEK_API_KEY"
    deepseek_reasoning_effort: str = "high"
    deepseek_thinking_enabled: bool = True
    deepseek_max_tokens: Optional[int] = None

    # ── Batch LLM execution ────────────────────────────────
    llm_batch_size: int = 8
    llm_concurrency: int = 4
    llm_max_retries: int = 2
    llm_retry_base_delay_seconds: float = 1.0


# ============================================================
# SECTION 2 — Protocol Definitions
# ============================================================
#
# These protocols define the contracts that the core algorithm
# (Section 6) programs against. The algorithm never imports a
# concrete class — it only sees these interfaces.
#
# To swap in a new provider (Anthropic, Mistral, a local model,
# a human-in-the-loop UI, etc.) implement the protocol and pass
# the instance at construction time. No other code changes.
# ============================================================


# ------------------------------------------------------------
# 2a. ClassificationResult — the value object every classifier
#     must return.
# ------------------------------------------------------------

@dataclass(frozen=True)
class ClassificationResult:
    """Structured output from a single classify() call.

    Fields
    ------
    label : str
        One of the labels from the label_set that was passed in.
        Must be an *exact* member of that set — the caller checks
        membership and will reject unknown labels.

    confidence : float
        0.0 – 1.0 inclusive.  Used by the mapping loop (Section 6)
        to decide acceptance vs. fallback:
          - >= high_threshold  → auto-accept
          - >= low_threshold   → flag for review
          - <  low_threshold   → discard / retry
        Thresholds are configured on MapTransform, not here.

    rationale : str or None
        Free-text explanation of *why* this label was chosen.
        Stored in the provenance trace so reviewers can audit
        the decision later.  May be None for stubs / tests.

    missing_elements : list[str] or None
        For labels such as OMISSION / QUALIFIER_DROP / HEDGE_DROP,
        the specific source elements that are absent from the
        target.  Used by the auto-validation gate in Phase 3.
        None when not applicable.

    abstraction_evidence : str or None
        Free-text evidence that an abstraction or information
        loss occurred.  Paired with missing_elements for the
        AUTO_VALIDATED pathway.  None when not applicable.
    """

    label: str
    confidence: float
    rationale: Optional[str] = None
    missing_elements: Optional[List[str]] = None
    abstraction_evidence: Optional[str] = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(
                f"confidence must be in [0.0, 1.0], got {self.confidence}"
            )


@dataclass(frozen=True)
class ClassificationPair:
    """One source/target excerpt pair prepared for batch classification."""

    pair_id: str
    source_excerpt: str
    target_excerpt: str


# ------------------------------------------------------------
# 2b. ClassificationError — the single exception type that
#     implementations should raise on unrecoverable failure.
# ------------------------------------------------------------

class ClassificationError(Exception):
    """Raised when a classifier hits an unrecoverable error.

    Examples: authentication failure, network timeout after all
    retries exhausted, content-policy refusal.

    Ambiguous or low-quality model responses should NOT raise.
    Return a ClassificationResult with low confidence instead,
    so the mapping loop can apply its own fallback logic.
    """


# ------------------------------------------------------------
# 2c. TransformClassifier protocol
# ------------------------------------------------------------

@runtime_checkable
class TransformClassifier(Protocol):
    """Decides which transform type links a source excerpt to a
    target excerpt.

    Semantic contract
    -----------------
    The caller provides:

      source_excerpt — a textual description or DDL snippet of
          one or more fields from the *source* schema.

      target_excerpt — the corresponding snippet from the
          *target* schema.

      label_set — the list of candidate transform names that
          are valid for this project / run.  This is NOT a fixed
          enum.  It may change across projects, or even across
          pairs within a single run.  Implementations must not
          hard-code these labels.

    The classifier's job is to answer:

      "Which single label from label_set best describes the
       structural relationship between source_excerpt and
       target_excerpt?"

    Example call
    ------------
    >>> result = classifier.classify(
    ...     source_excerpt=(
    ...         "customer_id INT PRIMARY KEY, "
    ...         "full_name VARCHAR(200)"
    ...     ),
    ...     target_excerpt=(
    ...         "cust_num BIGINT NOT NULL, "
    ...         "first_name VARCHAR(100), "
    ...         "last_name VARCHAR(100)"
    ...     ),
    ...     label_set=[
    ...         "rename", "split", "merge",
    ...         "retype", "passthrough", "custom",
    ...     ],
    ... )
    >>> result
    ClassificationResult(
        label="split",
        confidence=0.92,
        rationale="full_name in source is decomposed into "
                  "first_name and last_name in target",
    )

    Implementation degrees of freedom
    ----------------------------------
    The protocol deliberately leaves the following open:

      - Model provider and model version
        (OpenAI, Anthropic, Cohere, local GGUF, etc.)
      - Prompt template and few-shot examples
      - Temperature / sampling parameters
      - Retry and rate-limit strategy
      - Whether to batch multiple excerpt pairs per API call
        (the interface is one-pair-at-a-time; batching is an
        internal optimisation hidden behind classify())
      - How raw model output is parsed into ClassificationResult
      - Whether results are cached for identical inputs

    Error handling
    --------------
      - Raise ClassificationError for unrecoverable failures
        (auth, network, content-policy block).
      - For ambiguous or weak model answers, return a
        ClassificationResult with low confidence.  Do NOT raise.
    """

    def classify(
        self,
        source_excerpt: str,
        target_excerpt: str,
        label_set: List[str],
    ) -> ClassificationResult:
        ...


@runtime_checkable
class BatchTransformClassifier(TransformClassifier, Protocol):
    """Optional batch extension used by MAP-TRANSFORM phase 3."""

    def classify_batch(
        self,
        pairs: List[ClassificationPair],
        label_set: List[str],
    ) -> Dict[str, ClassificationResult]:
        ...


# ------------------------------------------------------------
# 2d. Embedder protocol
# ------------------------------------------------------------

@runtime_checkable
class Embedder(Protocol):
    """Turns a text excerpt into a dense vector.

    Used by the candidate-selection stage (Section 5) to find
    likely target matches before the classifier is invoked.

    Semantic contract
    -----------------
    embed() must return a list of floats whose length is
    constant across all calls for a given Embedder instance.
    Vectors should be L2-normalised if the downstream similarity
    function is cosine similarity (the default in Section 5).

    Implementation degrees of freedom
    ----------------------------------
      - Model (sentence-transformers, OpenAI embeddings, etc.)
      - Dimensionality
      - Batching / caching
      - Normalisation strategy
    """

    def embed(self, text: str) -> List[float]:
        ...


# ------------------------------------------------------------
# 2e. GraphStore protocol
# ------------------------------------------------------------

@runtime_checkable
class GraphStore(Protocol):
    """Reader/writer for the Evidence Graph.

    Extends the write-only GraphWriter contract from trace2.py
    (create_node, create_edge, node_exists) with get_node for
    reads and set_properties for updates.

    Both InMemoryGraphStore and MemgraphStore satisfy this
    protocol.  An InMemoryGraphStore can be shared with
    trace2.Trace as its ``eg`` writer.
    """

    def create_node(
        self, labels: List[str], properties: Dict[str, Any],
    ) -> None:
        ...

    def create_edge(
        self, from_uid: str, to_uid: str,
        rel_type: str, properties: Dict[str, Any],
    ) -> None:
        ...

    def node_exists(self, uid: str) -> bool:
        ...

    def get_node(self, uid: str) -> Optional[Dict[str, Any]]:
        ...

    def set_properties(
        self, uid: str, properties: Dict[str, Any],
    ) -> None:
        ...


# ============================================================
# SECTION 3 — Stub / Default Implementations
# ============================================================
#
# These exist so that the test suite and local development can
# run the full pipeline with zero network calls and zero API
# keys.  They are NOT suitable for production.
#
# A production deployment wires in real implementations via
# constructor injection on MapTransform (see Section 6).
# ============================================================


class StubClassifier:
    """Test-only classifier that returns a fixed result.

    Always returns the *first* label in the label_set with a
    confidence of 1.0 and no rationale.

    Usage in tests
    --------------
    >>> clf = StubClassifier()
    >>> clf.classify("a INT", "b BIGINT", ["rename", "retype"])
    ClassificationResult(label='rename', confidence=1.0, rationale=None)

    To test low-confidence paths, override via the constructor:

    >>> clf = StubClassifier(label_index=1, confidence=0.3)
    >>> clf.classify("a INT", "b BIGINT", ["rename", "retype"])
    ClassificationResult(label='retype', confidence=0.3, rationale=None)

    A real implementation would look similar in shape but would
    build a prompt, call an LLM, parse structured JSON output,
    and map it to ClassificationResult.  See the docstring on
    TransformClassifier for a worked example.
    """

    def __init__(
        self,
        label_index: int = 0,
        confidence: float = 1.0,
        rationale: Optional[str] = None,
    ) -> None:
        self._label_index = label_index
        self._confidence = confidence
        self._rationale = rationale

    def classify(
        self,
        source_excerpt: str,
        target_excerpt: str,
        label_set: List[str],
    ) -> ClassificationResult:
        return ClassificationResult(
            label=label_set[self._label_index],
            confidence=self._confidence,
            rationale=self._rationale,
        )


class HeuristicTransformClassifier:
    """
    Deterministic local classifier for replayable TRACE/MAP integration.

    This is intentionally conservative and network-free. Production runs can
    still inject an LLM-backed classifier through the existing protocol.
    """

    _QUALIFIER_TERMS = {
        "shall", "must", "required", "requires", "require",
        "definitive", "legitimate", "outside", "training",
        "periodic", "routine", "promptly",
    }
    _HEDGE_TERMS = {
        "may", "might", "could", "consider", "suggestion",
        "possible", "possibly", "likely",
    }

    def classify(
        self,
        source_excerpt: str,
        target_excerpt: str,
        label_set: List[str],
    ) -> ClassificationResult:
        labels = set(label_set)

        def choose(label: str, confidence: float, rationale: str,
                   missing: Optional[List[str]] = None) -> ClassificationResult:
            selected = label if label in labels else (
                "OTHER" if "OTHER" in labels else label_set[0]
            )
            return ClassificationResult(
                label=selected,
                confidence=confidence,
                rationale=rationale,
                missing_elements=missing,
                abstraction_evidence=rationale if missing else None,
            )

        src_tokens = self._tokens(source_excerpt)
        tgt_tokens = self._tokens(target_excerpt)
        src_norm = " ".join(src_tokens)
        tgt_norm = " ".join(tgt_tokens)
        if not src_norm or not tgt_norm:
            return choose("OTHER", 0.2, "One side has no classifiable text.")

        if src_norm == tgt_norm:
            return choose("VERBATIM", 1.0, "Normalized source and target are identical.")

        src_set = set(src_tokens)
        tgt_set = set(tgt_tokens)
        overlap = len(src_set & tgt_set) / max(1, len(src_set | tgt_set))
        src_contains_target = tgt_norm in src_norm
        target_contains_source = src_norm in tgt_norm

        missing_qualifiers = sorted((src_set - tgt_set) & self._QUALIFIER_TERMS)
        if missing_qualifiers and overlap >= 0.35:
            return choose(
                "QUALIFIER_DROP",
                min(0.95, 0.65 + overlap / 3.0),
                "Target omits qualifier-bearing terms from the source.",
                missing_qualifiers,
            )

        missing_hedges = sorted((src_set - tgt_set) & self._HEDGE_TERMS)
        if missing_hedges and overlap >= 0.35:
            return choose(
                "HEDGE_DROP",
                min(0.9, 0.6 + overlap / 3.0),
                "Target omits hedging language from the source.",
                missing_hedges,
            )

        if src_contains_target or target_contains_source:
            shorter = min(len(src_tokens), len(tgt_tokens))
            longer = max(len(src_tokens), len(tgt_tokens))
            if shorter / max(1, longer) <= 0.75:
                return choose(
                    "COMPRESSION",
                    min(0.95, 0.7 + overlap / 4.0),
                    "One excerpt compresses the other while preserving key terms.",
                )

        if overlap >= 0.35:
            return choose(
                "PARAPHRASE",
                min(0.9, 0.55 + overlap / 3.0),
                "Source and target share enough terms to be treated as a paraphrase.",
            )

        if src_set and not (src_set & tgt_set):
            return choose(
                "OMISSION",
                0.55,
                "Target omits the source content.",
                sorted(list(src_set))[:8],
            )

        return choose("OTHER", 0.35, "No specific transformation class matched.")

    def classify_batch(
        self,
        pairs: List[ClassificationPair],
        label_set: List[str],
    ) -> Dict[str, ClassificationResult]:
        return {
            pair.pair_id: self.classify(
                source_excerpt=pair.source_excerpt,
                target_excerpt=pair.target_excerpt,
                label_set=label_set,
            )
            for pair in pairs
        }

    @staticmethod
    def _tokens(text: str) -> List[str]:
        return re.findall(r"[a-z0-9]+", (text or "").lower())


class DeepSeekTransformClassifier:
    """DeepSeek Pro backed transformation classifier.

    Uses DeepSeek's OpenAI-compatible chat completions API directly, or an
    OpenAI-compatible proxy such as OpenRouter when root .env provides one.
    Official DeepSeek requests use ``thinking`` plus ``reasoning_effort``;
    OpenRouter requests use ``reasoning.effort``. Sampling parameters are
    intentionally omitted because thinking/reasoning mode rejects them.
    """

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        api_key_env: str = "DEEPSEEK_API_KEY",
        base_url: str = _DEFAULT_DEEPSEEK_BASE_URL,
        model: str = _DEFAULT_DEEPSEEK_MODEL,
        reasoning_effort: str = "high",
        thinking_enabled: bool = True,
        max_tokens: Optional[int] = None,
    ) -> None:
        _load_root_dotenv_once()
        key_env_name, env_key = _first_env_pair(
            api_key_env,
            "DEEPSEEK_API_KEY",
            "OPENAI_KEY",
            "OPENAI_API_KEY",
        )
        resolved_key = api_key or env_key
        if not resolved_key:
            raise ClassificationError(
                "Missing DeepSeek API key. Set DEEPSEEK_API_KEY or "
                "OPENAI_KEY in the process environment or root .env."
            )
        use_openai_alias = bool(key_env_name and key_env_name.startswith("OPENAI_"))
        if base_url == _DEFAULT_DEEPSEEK_BASE_URL:
            if use_openai_alias:
                base_url = (
                    _first_env("OPENAI_BASE_URL", "DEEPSEEK_BASE_URL")
                    or _DEFAULT_DEEPSEEK_BASE_URL
                )
            else:
                base_url = _first_env("DEEPSEEK_BASE_URL") or _DEFAULT_DEEPSEEK_BASE_URL
        if model == _DEFAULT_DEEPSEEK_MODEL:
            if use_openai_alias:
                model = (
                    _first_env("OPENAI_MODEL", "DEEPSEEK_MODEL")
                    or _DEFAULT_DEEPSEEK_MODEL
                )
            else:
                model = _first_env("DEEPSEEK_MODEL") or _DEFAULT_DEEPSEEK_MODEL

        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ClassificationError(
                "The openai package is required for DeepSeek API calls. "
                "Install it with: pip install openai"
            ) from exc

        self.client = OpenAI(api_key=resolved_key, base_url=base_url)
        self.base_url = base_url
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.thinking_enabled = thinking_enabled
        self.max_tokens = max_tokens

    def classify(
        self,
        source_excerpt: str,
        target_excerpt: str,
        label_set: List[str],
    ) -> ClassificationResult:
        if not label_set:
            raise ClassificationError("label_set is empty")

        system = (
            "You classify evidence transformations for an auditable evidence "
            "pipeline. Return only valid JSON. Do not include markdown."
        )
        user = {
            "task": "Choose exactly one transformation label from label_set.",
            "label_set": label_set,
            "label_definitions": {
                "VERBATIM": "Target repeats the source with no material change.",
                "PARAPHRASE": "Target restates the same meaning in different wording.",
                "COMPRESSION": "Target preserves the main claim but compresses detail.",
                "OMISSION": "Target omits the source content or claim.",
                "QUALIFIER_DROP": "Target drops an important constraint, condition, or qualifier.",
                "HEDGE_DROP": "Target removes uncertainty or hedging language.",
                "OTHER": "No listed label fits.",
            },
            "source_excerpt": source_excerpt,
            "target_excerpt": target_excerpt,
            "required_json_schema": {
                "label": "one label from label_set",
                "confidence": "number between 0 and 1",
                "rationale": "short audit explanation, no hidden reasoning",
                "missing_elements": "array of source elements missing in target, empty if none",
                "abstraction_evidence": "short evidence for loss/drop/abstraction, empty if none",
            },
        }
        payload = self._chat_json(system, user)
        return self._payload_to_result(payload, label_set)

    def classify_batch(
        self,
        pairs: List[ClassificationPair],
        label_set: List[str],
    ) -> Dict[str, ClassificationResult]:
        if not pairs:
            return {}
        if not label_set:
            raise ClassificationError("label_set is empty")

        system = (
            "You classify evidence transformations for an auditable evidence "
            "pipeline. Return only valid JSON. Do not include markdown."
        )
        user = {
            "task": (
                "Classify each source/target pair. Return exactly one result "
                "for each pair_id."
            ),
            "label_set": label_set,
            "label_definitions": {
                "VERBATIM": "Target repeats the source with no material change.",
                "PARAPHRASE": "Target restates the same meaning in different wording.",
                "COMPRESSION": "Target preserves the main claim but compresses detail.",
                "OMISSION": "Target omits the source content or claim.",
                "QUALIFIER_DROP": "Target drops an important constraint, condition, or qualifier.",
                "HEDGE_DROP": "Target removes uncertainty or hedging language.",
                "OTHER": "No listed label fits.",
            },
            "pairs": [
                {
                    "pair_id": pair.pair_id,
                    "source_excerpt": pair.source_excerpt,
                    "target_excerpt": pair.target_excerpt,
                }
                for pair in pairs
            ],
            "required_json_schema": {
                "results": [
                    {
                        "pair_id": "the pair_id from input",
                        "label": "one label from label_set",
                        "confidence": "number between 0 and 1",
                        "rationale": "short audit explanation, no hidden reasoning",
                        "missing_elements": "array of source elements missing in target, empty if none",
                        "abstraction_evidence": "short evidence for loss/drop/abstraction, empty if none",
                    }
                ]
            },
        }
        payload = self._chat_json(system, user)
        rows = payload.get("results")
        if not isinstance(rows, list):
            raise ClassificationError("DeepSeek batch response missing results array")

        results: Dict[str, ClassificationResult] = {}
        valid_ids = {pair.pair_id for pair in pairs}
        for row in rows:
            if not isinstance(row, dict):
                continue
            pair_id = str(row.get("pair_id", "")).strip()
            if pair_id not in valid_ids:
                continue
            results[pair_id] = self._payload_to_result(row, label_set)
        return results

    def _chat_json(self, system: str, user: Dict[str, Any]) -> Dict[str, Any]:
        extra_body = {}
        is_openrouter = _is_openrouter_base_url(self.base_url)
        if is_openrouter:
            if self.thinking_enabled:
                extra_body["reasoning"] = {
                    "effort": self.reasoning_effort,
                    "exclude": True,
                }
        else:
            extra_body["thinking"] = {
                "type": "enabled" if self.thinking_enabled else "disabled"
            }

        try:
            request: Dict[str, Any] = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
                ],
                "response_format": {"type": "json_object"},
            }
            if self.max_tokens is not None:
                request["max_tokens"] = self.max_tokens
            if not is_openrouter:
                request["reasoning_effort"] = self.reasoning_effort
            if extra_body:
                request["extra_body"] = extra_body
            completion = self.client.chat.completions.create(**request)
        except Exception as exc:
            raise ClassificationError(f"DeepSeek classification failed: {exc}") from exc

        content = completion.choices[0].message.content or ""
        return self._parse_json_object(content)

    @staticmethod
    def _payload_to_result(
        payload: Dict[str, Any],
        label_set: List[str],
    ) -> ClassificationResult:
        label = str(payload.get("label", "")).strip().upper()
        if label not in label_set:
            label = "OTHER" if "OTHER" in label_set else label_set[0]
        confidence = _clamp01(float(payload.get("confidence", 0.0) or 0.0))
        rationale = str(payload.get("rationale", "") or "")
        missing = payload.get("missing_elements", [])
        if not isinstance(missing, list):
            missing = [str(missing)] if missing else []
        missing = [str(item) for item in missing if str(item).strip()]
        abstraction = str(payload.get("abstraction_evidence", "") or "")
        return ClassificationResult(
            label=label,
            confidence=confidence,
            rationale=rationale,
            missing_elements=missing or None,
            abstraction_evidence=abstraction or None,
        )

    @staticmethod
    def _parse_json_object(content: str) -> Dict[str, Any]:
        text = (content or "").strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start < 0 or end <= start:
                raise ClassificationError(
                    f"DeepSeek returned non-JSON content: {text[:200]}"
                )
            parsed = json.loads(text[start : end + 1])
        if not isinstance(parsed, dict):
            raise ClassificationError("DeepSeek JSON response is not an object")
        return parsed


class IdentityEmbedder:
    """Test-only embedder that hashes input to a fixed-dim vector.

    Produces deterministic, roughly-uniform vectors so that
    candidate selection runs end-to-end without a real model.
    NOT suitable for production — the vectors carry no semantic
    information.

    Parameters
    ----------
    dims : int
        Length of the returned vector.  Must match whatever the
        candidate-selection index in Section 5 expects.

    A production replacement (e.g. SentenceTransformerEmbedder,
    OpenAIEmbedder) should return L2-normalised vectors of the
    same fixed dimensionality.
    """

    def __init__(self, dims: int = 128) -> None:
        self._dims = dims

    def embed(self, text: str) -> List[float]:
        import hashlib
        import struct

        h = hashlib.sha512(text.encode()).digest()
        # Stretch hash to cover self._dims floats
        while len(h) < self._dims * 4:
            h += hashlib.sha512(h).digest()
        raw = struct.unpack(f"{self._dims}f", h[: self._dims * 4])
        # Rough L2 normalisation
        norm = max(sum(x * x for x in raw) ** 0.5, 1e-9)
        return [x / norm for x in raw]


# ============================================================
# SECTION 3b — InMemoryGraphStore
# ============================================================
#
# A zero-dependency, dict-backed implementation of GraphStore.
# Suitable for tests, notebooks, and single-process batch jobs.
#
# Can be shared with trace2.Trace as the ``eg`` writer — it
# satisfies the GraphWriter protocol (create_node, create_edge,
# node_exists) and adds get_node / set_properties for
# MAP-TRANSFORM's read/update needs.
# ============================================================


class InMemoryGraphStore:
    """Dict-backed graph store for testing and local development.

    Nodes are stored as ``{uid: properties_dict}`` and edges as a
    flat list of ``(from_uid, to_uid, rel_type, props)`` tuples.
    All operations are O(1) for node lookups by UID.

    Thread safety: not provided.  Use a lock if sharing across
    threads.
    """

    def __init__(self) -> None:
        self._nodes: Dict[str, Dict[str, Any]] = {}
        self._edges: List[Tuple[str, str, str, Dict[str, Any]]] = []

    # ── GraphStore / GraphWriter implementation ──────────────

    def create_node(
        self, labels: List[str], properties: Dict[str, Any],
    ) -> None:
        uid = properties.get("uid", "")
        if uid in self._nodes:
            # MERGE semantics: update existing properties
            self._nodes[uid].update(properties)
            return
        self._nodes[uid] = dict(properties)

    def create_edge(
        self, from_uid: str, to_uid: str,
        rel_type: str, properties: Dict[str, Any],
    ) -> None:
        self._edges.append(
            (from_uid, to_uid, rel_type, dict(properties or {})))

    def node_exists(self, uid: str) -> bool:
        return uid in self._nodes

    def get_node(self, uid: str) -> Optional[Dict[str, Any]]:
        props = self._nodes.get(uid)
        if props is None:
            return None
        return dict(props)

    def set_properties(
        self, uid: str, properties: Dict[str, Any],
    ) -> None:
        if uid in self._nodes:
            self._nodes[uid].update(properties)


# ============================================================
# SECTION 3c — Caching Wrappers
# ============================================================
#
# These are transparent decorators that sit between the caller
# (Section 6) and any concrete classifier or embedder.  They
# satisfy the same protocols, so the mapping loop doesn't know
# caching is happening.
#
# Usage:
#
#   raw_clf  = OpenAITransformClassifier(model="gpt-4o")
#   clf      = CachedClassifier(raw_clf, max_size=4096)
#
#   raw_emb  = SentenceTransformerEmbedder()
#   emb      = CachedEmbedder(raw_emb, max_size=8192)
#
#   mt = MapTransform(
#       eg=store,
#       embedder=emb,
#       classifier=clf,
#   )
#
# Why cache?
# ----------
# LLM calls and embedding calls are the two dominant costs —
# both in latency and in spend.  Schema mapping workloads
# contain heavy repetition:
#
#   - The same source field may be compared against dozens of
#     candidate targets.  The source embedding is identical
#     every time.
#   - Re-runs after threshold tuning re-classify many pairs
#     that haven't changed.
#   - Interactive / notebook usage often replays the same
#     pipeline while tweaking config.
#
# A simple in-memory LRU cache eliminates the duplicates with
# zero infrastructure.
#
# Design decisions
# ----------------
#   - The cache key is built from the raw inputs, not from
#     object identity.  Two calls with identical strings and
#     label sets will hit the cache even if they come from
#     different parts of the pipeline.
#
#   - The cache is per-instance, not global.  This avoids
#     cross-contamination when multiple MapTransform instances
#     run in the same process with different configs.
#
#   - Eviction is LRU via OrderedDict.  No time-based TTL by
#     default — schema mapping is typically a bounded batch
#     job, not a long-running service.  If TTL is needed (e.g.
#     for a service deployment), set ttl_seconds in the
#     constructor.
#
#   - Cache misses that raise ClassificationError are NOT
#     cached.  Only successful results are stored.
#
#   - Thread safety: not provided.  The mapping loop in
#     Section 6 is single-threaded.  If you parallelise it,
#     wrap with a lock or use a thread-safe cache backend.
# ============================================================


class CacheStats:
    """Observable counters for cache hit/miss behaviour.

    Read these after a run to decide whether to resize the cache
    or to verify that caching is actually helping.

    >>> clf = CachedClassifier(inner, max_size=1024)
    >>> # ... run pipeline ...
    >>> print(clf.stats)
    CacheStats(hits=312, misses=87, evictions=0, hit_rate=78.20%)
    """

    __slots__ = ("hits", "misses", "evictions")

    def __init__(self) -> None:
        self.hits: int = 0
        self.misses: int = 0
        self.evictions: int = 0

    @property
    def total(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        """Returns 0.0 when no calls have been made."""
        return self.hits / self.total if self.total > 0 else 0.0

    def __repr__(self) -> str:
        return (
            f"CacheStats(hits={self.hits}, misses={self.misses}, "
            f"evictions={self.evictions}, hit_rate={self.hit_rate:.2%})"
        )


class CachedClassifier:
    """LRU-caching wrapper around any TransformClassifier.

    Satisfies the TransformClassifier protocol — drop it in
    wherever the inner classifier would go.

    Parameters
    ----------
    inner : TransformClassifier
        The real classifier to delegate to on cache misses.

    max_size : int
        Maximum number of results to keep.  When exceeded the
        least-recently-used entry is evicted.  Set generously —
        each entry is tiny (a short string + float + optional
        rationale string).

    ttl_seconds : float or None
        If set, entries older than this are treated as misses.
        Leave as None for batch jobs.  Set to e.g. 3600.0 for
        a long-running service where upstream schemas may have
        been updated.

    Cache key
    ---------
    (source_excerpt, target_excerpt, frozenset(label_set))

    The label_set is frozen as a set, NOT a tuple, because the
    *order* of labels must not affect the cache key — the
    classifier's answer should be the same regardless of list
    ordering.  If your classifier is order-sensitive (unlikely
    but possible with prompt-based approaches), override
    _make_key() in a subclass to use tuple() instead.

    Error handling
    --------------
    If the inner classifier raises ClassificationError, the
    exception propagates and nothing is cached.  Only successful
    ClassificationResult values are stored.
    """

    def __init__(
        self,
        inner: TransformClassifier,
        max_size: int = 2048,
        ttl_seconds: Optional[float] = None,
    ) -> None:
        self._inner = inner
        self._max_size = max_size
        self._ttl_seconds = ttl_seconds
        # OrderedDict gives us O(1) move-to-end for LRU
        self._cache: collections.OrderedDict[
            Tuple, Tuple[ClassificationResult, float]
        ] = collections.OrderedDict()
        self.stats = CacheStats()

    # -- key construction (override in subclass if needed) ----

    @staticmethod
    def _make_key(
        source_excerpt: str,
        target_excerpt: str,
        label_set: List[str],
    ) -> Tuple:
        """Build a hashable, order-insensitive cache key.

        Uses frozenset for label_set so that
        ["rename", "split"] and ["split", "rename"] map to the
        same key.
        """
        return (source_excerpt, target_excerpt, frozenset(label_set))

    # -- protocol method --------------------------------------

    def classify(
        self,
        source_excerpt: str,
        target_excerpt: str,
        label_set: List[str],
    ) -> ClassificationResult:
        key = self._make_key(source_excerpt, target_excerpt, label_set)
        now = _time.monotonic()

        # Check cache
        if key in self._cache:
            result, timestamp = self._cache[key]
            if (self._ttl_seconds is None
                    or (now - timestamp) < self._ttl_seconds):
                # Move to end (most recently used)
                self._cache.move_to_end(key)
                self.stats.hits += 1
                return result
            else:
                # Expired — remove stale entry
                del self._cache[key]

        # Cache miss — call inner classifier
        # ClassificationError is allowed to propagate uncaught
        self.stats.misses += 1
        result = self._inner.classify(
            source_excerpt=source_excerpt,
            target_excerpt=target_excerpt,
            label_set=label_set,
        )

        # Store result
        self._cache[key] = (result, now)

        # Evict LRU if over capacity
        while len(self._cache) > self._max_size:
            self._cache.popitem(last=False)
            self.stats.evictions += 1

        return result

    # -- convenience ------------------------------------------

    def clear(self) -> None:
        """Drop all cached entries.  Stats are preserved."""
        self._cache.clear()

    @property
    def size(self) -> int:
        return len(self._cache)


class CachedEmbedder:
    """LRU-caching wrapper around any Embedder.

    Satisfies the Embedder protocol.  Especially valuable
    because the *same* source field is often embedded many
    times during candidate selection against a large target
    schema.

    Parameters
    ----------
    inner : Embedder
        The real embedder to delegate to on cache misses.

    max_size : int
        Maximum entries.  Embedding vectors are larger than
        classifier results (~512 floats × 8 bytes ≈ 4 KB each),
        so size this with memory in mind.  8192 entries ≈ 32 MB.

    ttl_seconds : float or None
        Same semantics as CachedClassifier.

    Cache key
    ---------
    The raw input string.  Two identical strings always produce
    the same embedding, regardless of where in the pipeline they
    originate.

    Error handling
    --------------
    If the inner embedder raises, the exception propagates and
    nothing is cached.
    """

    def __init__(
        self,
        inner: Embedder,
        max_size: int = 8192,
        ttl_seconds: Optional[float] = None,
    ) -> None:
        self._inner = inner
        self._max_size = max_size
        self._ttl_seconds = ttl_seconds
        self._cache: collections.OrderedDict[
            str, Tuple[List[float], float]
        ] = collections.OrderedDict()
        self.stats = CacheStats()

    def embed(self, text: str) -> List[float]:
        now = _time.monotonic()

        if text in self._cache:
            vector, timestamp = self._cache[text]
            if (self._ttl_seconds is None
                    or (now - timestamp) < self._ttl_seconds):
                self._cache.move_to_end(text)
                self.stats.hits += 1
                return vector
            else:
                del self._cache[text]

        self.stats.misses += 1
        vector = self._inner.embed(text)

        self._cache[text] = (vector, now)

        while len(self._cache) > self._max_size:
            self._cache.popitem(last=False)
            self.stats.evictions += 1

        return vector

    def clear(self) -> None:
        """Drop all cached entries.  Stats are preserved."""
        self._cache.clear()

    @property
    def size(self) -> int:
        return len(self._cache)


# ═══════════════════════════════════════════════════════════════
#  SECTION 4 — Result containers
# ═══════════════════════════════════════════════════════════════

@dataclass
class MappingRecord:
    """One classified transformation pair."""

    derived_uid: str          # Derived EvidenceNode UID
    df_uid: str               # DERIVES_FROM edge UID
    s_uid: str                # source EvidenceNode UID
    t_uid: str                # target EvidenceNode UID
    label: str                # classification label
    confidence: float
    justification: str
    validation_state: str     # UNVERIFIED | AUTO_VALIDATED | NEEDS_REVIEW


@dataclass
class MapTransformResult:
    """Immutable output of a MAP-TRANSFORM execution."""

    retained: List[MappingRecord]
    dropped_uids: Set[str]
    all_derived_uids: List[str]
    diagnostics: List[Dict[str, Any]]
    stats: Dict[str, int]


# ═══════════════════════════════════════════════════════════════
#  SECTION 5 — Utility functions
# ═══════════════════════════════════════════════════════════════

def classify_impact(
    score: float,
    tau_positive: float = 0.7,
    tau_neutral: float = 0.4,
) -> str:
    """
    §1.2 ClassifyImpact — bridge a numeric confidence score to
    the schema's three-valued ``ReliabilityFactor.impact`` enum.
    """
    if score >= tau_positive:
        return "positive"
    if score >= tau_neutral:
        return "neutral"
    return "negative"


def cosine_similarity(a: List[float], b: List[float]) -> float:
    """Cosine similarity between two dense vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0
    return dot / (mag_a * mag_b)


def _clamp01(val: float) -> float:
    """Clamp to the schema-required [0.0, 1.0] range."""
    if val < 0.0:
        return 0.0
    if val > 1.0:
        return 1.0
    return val


# ═══════════════════════════════════════════════════════════════
#  SECTION 6 helpers are defined below. The final implementation follows.
# ═══════════════════════════════════════════════════════════════

def _deterministic_uid(seed: str) -> str:
    """Deterministic UUID5 from a seed string."""
    return str(uuid.uuid5(MAP_TRANSFORM_NS, seed))


def _edge_uid(from_uid: str, to_uid: str, rel_type: str) -> str:
    """Deterministic edge UID from the (from, to, type) triple."""
    return str(uuid.uuid5(
        MAP_TRANSFORM_NS, f"{from_uid}|{to_uid}|{rel_type}"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ═══════════════════════════════════════════════════════════════
#  SECTION 6 — The MAP-TRANSFORM algorithm
# ═══════════════════════════════════════════════════════════════

# Labels that trigger the AUTO_VALIDATED / NEEDS_REVIEW pathway
_VALIDATION_LABELS: Set[str] = {
    "OMISSION", "QUALIFIER_DROP", "HEDGE_DROP",
}


class MapTransform:
    """
    Stateful, single-use classifier.
    Instantiate → ``execute()`` → discard.

    Parameters
    ----------
    eg : GraphStore
        Reader/writer for the Evidence Graph.
    embedder : Embedder
        Produces dense vectors from text excerpts.
    classifier : TransformClassifier
        LLM-backed transformation classifier.
    cfg : MapTransformConfig, optional
    """

    # ─────────────────────────────────────────────────────────
    #  Construction
    # ─────────────────────────────────────────────────────────

    def __init__(
        self,
        eg: GraphStore,
        embedder: Embedder,
        classifier: TransformClassifier,
        cfg: MapTransformConfig | None = None,
    ) -> None:
        self.eg = eg
        self.embedder = embedder
        self.classifier = classifier
        self.cfg = cfg or MapTransformConfig()

        self._diags: List[Dict[str, Any]] = []
        self._seq_counters: Dict[str, int] = {}   # uid → next seqIdx
        self._embed_cache: Dict[str, List[float]] = {}

    # ─────────────────────────────────────────────────────────
    #  Public entry point
    # ─────────────────────────────────────────────────────────

    def execute(
        self,
        source_uids: List[str],
        target_uids: List[str],
        label_set: List[str],
        eg_root_uid: str,
    ) -> MapTransformResult:
        """
        Run all MAP-TRANSFORM phases.

        Parameters
        ----------
        source_uids : list[str]
            UIDs of source EvidenceNodes (typically
            ``traceResult.evidence_node_uids``).
        target_uids : list[str]
            UIDs of target EvidenceNodes to compare against.
        label_set : list[str]
            Transformation labels the classifier may assign
            (e.g. ``['VERBATIM', 'PARAPHRASE', 'OMISSION', ...]``).
        eg_root_uid : str
            UID of the EG ``GraphRoot``.  Required so that new
            Derived nodes receive ``CONTAINS_NODE`` edges.

        Returns
        -------
        MapTransformResult
        """
        # ── Phase 0 ────────────────────────────────────────
        ok = self._phase0_preconditions(label_set, eg_root_uid)
        if not ok:
            return self._finalise([], set(), [])

        # ── Phase 1 ────────────────────────────────────────
        source_nodes = self._phase1_read_nodes(
            source_uids, "source")
        target_nodes = self._phase1_read_nodes(
            target_uids, "target")

        if not source_nodes or not target_nodes:
            self._diag(
                "WARNING", "MAP_EMPTY_NODES",
                f"source={len(source_nodes)}, "
                f"target={len(target_nodes)} — nothing to compare")
            return self._finalise([], set(), [])

        # ── Phase 2 ────────────────────────────────────────
        top_pairs = self._phase2_pairs(source_nodes, target_nodes)

        if not top_pairs:
            self._diag("INFO", "MAP_NO_PAIRS",
                       "No candidate pairs above similarity threshold")
            return self._finalise([], set(), [])

        # ── Phase 3 ────────────────────────────────────────
        mappings = self._phase3_classify(
            top_pairs, label_set, eg_root_uid)

        # ── Phase 4 ────────────────────────────────────────
        retained, dropped = self._phase4_filter(mappings)

        all_derived = [m.derived_uid for m in mappings]
        dropped_uids = {m.derived_uid for m in dropped}

        return self._finalise(retained, dropped_uids, all_derived)

    # ═════════════════════════════════════════════════════════
    #  Phase 0 — Precondition guards
    # ═════════════════════════════════════════════════════════

    def _phase0_preconditions(
        self,
        label_set: List[str],
        eg_root_uid: str,
    ) -> bool:
        """
        Verify agent existence, label set non-emptiness, and
        EG root reachability.  Returns True if all guards pass.
        """
        # ── Ensure transform agent node exists ──────────────
        if not self.eg.node_exists(self.cfg.transform_agent_uid):
            self.eg.create_node(["Agent"], {
                "uid":  self.cfg.transform_agent_uid,
                "type": "system",
                "name": self.cfg.transform_agent_name,
                "role": "transform_classifier",
            })

        # ── Guard: empty label set ──────────────────────────
        if not label_set:
            self._diag(
                "ERROR", "MAP_EMPTY_LABEL_SET",
                "Transformation label set is empty; "
                "cannot classify.")
            return False

        # ── Guard: EG root ──────────────────────────────────
        if not eg_root_uid:
            self._diag(
                "ERROR", "MAP_NO_EG_ROOT",
                "No EG root UID provided.")
            return False

        root_props = self.eg.get_node(eg_root_uid)
        if root_props is None:
            self._diag(
                "ERROR", "MAP_EG_ROOT_MISSING",
                f"EG root {eg_root_uid} not found in store.")
            return False

        if root_props.get("graphType") != "EvidenceGraph":
            self._diag(
                "ERROR", "MAP_EG_ROOT_WRONG_TYPE",
                f"Node {eg_root_uid} has graphType="
                f"'{root_props.get('graphType')}', "
                f"expected 'EvidenceGraph'.")
            return False

        self._diag("INFO", "MAP_PHASE0",
                   f"Preconditions OK — {len(label_set)} labels, "
                   f"EG root {eg_root_uid}")
        return True

    # ═════════════════════════════════════════════════════════
    #  Phase 1 — Read source and target nodes
    # ═════════════════════════════════════════════════════════

    def _phase1_read_nodes(
        self,
        uids: List[str],
        role: str,
    ) -> List[Dict[str, Any]]:
        """
        Load EvidenceNodes by UID.  Nodes that are missing or
        lack ``contentExcerpt`` are skipped with a diagnostic.
        """
        nodes: List[Dict[str, Any]] = []
        for uid in uids:
            props = self.eg.get_node(uid)
            if props is None:
                self._diag(
                    "WARNING", "MAP_NODE_MISSING",
                    f"{role} node {uid} not found in EG")
                continue
            excerpt = props.get("contentExcerpt")
            if not excerpt:
                self._diag(
                    "WARNING", "MAP_NO_EXCERPT",
                    f"{role} node {uid} has no contentExcerpt; "
                    f"skipping")
                continue
            nodes.append(props)

        self._diag(
            "INFO", f"MAP_PHASE1_{role.upper()}",
            f"{len(nodes)} {role} nodes loaded "
            f"({len(uids) - len(nodes)} skipped)")
        return nodes

    # ═════════════════════════════════════════════════════════
    #  Phase 2 — Pair generation with self-loop / reciprocal guard
    # ═════════════════════════════════════════════════════════

    def _phase2_pairs(
        self,
        source_nodes: List[Dict[str, Any]],
        target_nodes: List[Dict[str, Any]],
    ) -> List[Tuple[str, str, float]]:
        """
        Generate ``(source_uid, target_uid, similarity)`` triples,
        excluding self-loops and reciprocal duplicate pairs.  Returns the top ``K_p`` pairs
        ranked by cosine similarity.
        """
        pairs: List[Tuple[str, str, float]] = []
        source_uid_set = {node["uid"] for node in source_nodes}
        target_uid_set = {node["uid"] for node in target_nodes}
        skipped_reciprocal = 0

        for s_node in source_nodes:
            s_uid = s_node["uid"]
            s_emb = self._get_embedding(
                s_uid, s_node["contentExcerpt"])

            for t_node in target_nodes:
                t_uid = t_node["uid"]

                # ── Self-loop guard ─────────────────────────
                if s_uid == t_uid:
                    continue

                # If the caller supplied overlapping source/target sets,
                # both (A,B) and (B,A) are possible. Since phase 3 writes
                # target -[:DERIVES_FROM]-> source, keeping both would create
                # an immediate lineage cycle. A deterministic UID total order
                # keeps one direction while preserving idempotency.
                if (
                    s_uid in target_uid_set
                    and t_uid in source_uid_set
                    and s_uid > t_uid
                ):
                    skipped_reciprocal += 1
                    continue

                t_emb = self._get_embedding(
                    t_uid, t_node["contentExcerpt"])
                sim = cosine_similarity(s_emb, t_emb)
                pairs.append((s_uid, t_uid, sim))

        # Top-K by similarity, descending
        pairs.sort(key=lambda p: p[2], reverse=True)
        top = pairs[: self.cfg.K_p]

        self._diag(
            "INFO", "MAP_PHASE2",
            f"{len(pairs)} candidate pairs generated, "
            f"{skipped_reciprocal} reciprocal duplicates skipped, "
            f"top {len(top)} selected (K_p={self.cfg.K_p})")
        return top

    # ═════════════════════════════════════════════════════════
    #  Phase 3 helpers — Batch / fallback classification
    # ═════════════════════════════════════════════════════════

    def _classify_pairs(
        self,
        top_pairs: List[Tuple[str, str, float]],
        label_set: List[str],
    ) -> List[Dict[str, Any]]:
        prepared: List[Dict[str, Any]] = []
        for idx, (s_uid, t_uid, sim) in enumerate(top_pairs):
            if sim < self.cfg.tau_map:
                continue

            s_node = self.eg.get_node(s_uid)
            t_node = self.eg.get_node(t_uid)
            if s_node is None or t_node is None:
                continue

            prepared.append({
                "pair_id": f"pair-{idx:04d}",
                "s_uid": s_uid,
                "t_uid": t_uid,
                "sim": sim,
                "s_node": s_node,
                "t_node": t_node,
            })

        if not prepared:
            return []

        batch_fn = getattr(self.classifier, "classify_batch", None)
        batch_size = max(1, int(self.cfg.llm_batch_size or 1))
        if not callable(batch_fn) or batch_size <= 1:
            results = self._classify_pairs_serial(prepared, label_set)
            return self._attach_classification_results(prepared, results)

        chunks = [
            prepared[i:i + batch_size]
            for i in range(0, len(prepared), batch_size)
        ]
        max_workers = max(1, int(self.cfg.llm_concurrency or 1))
        max_workers = min(max_workers, len(chunks))
        results: Dict[str, ClassificationResult] = {}

        if max_workers <= 1:
            for chunk in chunks:
                results.update(self._classify_batch_chunk(chunk, label_set, batch_fn))
        else:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=max_workers
            ) as executor:
                future_to_chunk = {
                    executor.submit(
                        self._classify_batch_chunk,
                        chunk,
                        label_set,
                        batch_fn,
                    ): chunk
                    for chunk in chunks
                }
                for future in concurrent.futures.as_completed(future_to_chunk):
                    chunk = future_to_chunk[future]
                    try:
                        results.update(future.result())
                    except Exception as exc:  # defensive final fallback
                        logger.error("Batch worker failed: %s", exc)
                        results.update(self._classify_pairs_serial(chunk, label_set))

        self._diag(
            "INFO",
            "MAP_BATCH_CLASSIFY",
            f"{len(prepared)} pair(s) classified through {len(chunks)} "
            f"batch(es), batch_size={batch_size}, concurrency={max_workers}",
        )
        return self._attach_classification_results(prepared, results)

    def _classify_batch_chunk(
        self,
        chunk: List[Dict[str, Any]],
        label_set: List[str],
        batch_fn: Callable[
            [List[ClassificationPair], List[str]],
            Dict[str, ClassificationResult],
        ],
    ) -> Dict[str, ClassificationResult]:
        pairs = [
            ClassificationPair(
                pair_id=item["pair_id"],
                source_excerpt=item["s_node"].get("contentExcerpt", ""),
                target_excerpt=item["t_node"].get("contentExcerpt", ""),
            )
            for item in chunk
        ]
        max_retries = max(0, int(self.cfg.llm_max_retries or 0))
        last_exc: Optional[BaseException] = None

        for attempt in range(max_retries + 1):
            try:
                raw = batch_fn(pairs, label_set)
                if not isinstance(raw, dict):
                    raise ClassificationError(
                        "Batch classifier returned non-dict result"
                    )
                results = {
                    pair.pair_id: result
                    for pair in pairs
                    if isinstance((result := raw.get(pair.pair_id)),
                                  ClassificationResult)
                }
                missing = [
                    item for item in chunk
                    if item["pair_id"] not in results
                ]
                if missing:
                    self._diag(
                        "WARNING",
                        "MAP_BATCH_MISSING_RESULT",
                        f"Batch classifier missed {len(missing)} pair(s); "
                        "falling back to single classification.",
                    )
                    results.update(self._classify_pairs_serial(missing, label_set))
                return results
            except Exception as exc:
                last_exc = exc
                if attempt < max_retries:
                    delay = self.cfg.llm_retry_base_delay_seconds * (2 ** attempt)
                    _time.sleep(max(0.0, delay))

        self._diag(
            "WARNING",
            "MAP_BATCH_FALLBACK",
            f"Batch classification failed after {max_retries + 1} attempt(s): "
            f"{last_exc}. Falling back to single classification.",
        )
        return self._classify_pairs_serial(chunk, label_set)

    def _classify_pairs_serial(
        self,
        prepared: List[Dict[str, Any]],
        label_set: List[str],
    ) -> Dict[str, ClassificationResult]:
        results: Dict[str, ClassificationResult] = {}
        for item in prepared:
            s_uid = item["s_uid"]
            t_uid = item["t_uid"]
            try:
                results[item["pair_id"]] = self.classifier.classify(
                    source_excerpt=item["s_node"].get("contentExcerpt", ""),
                    target_excerpt=item["t_node"].get("contentExcerpt", ""),
                    label_set=label_set,
                )
            except ClassificationError as exc:
                logger.error(
                    "Classifier failed for pair (%s, %s): %s",
                    s_uid, t_uid, exc)
        return results

    @staticmethod
    def _attach_classification_results(
        prepared: List[Dict[str, Any]],
        results: Dict[str, ClassificationResult],
    ) -> List[Dict[str, Any]]:
        classified: List[Dict[str, Any]] = []
        for item in prepared:
            result = results.get(item["pair_id"])
            if result is None:
                continue
            row = dict(item)
            row["result"] = result
            classified.append(row)
        return classified

    # ═════════════════════════════════════════════════════════
    #  Phase 3 — Classification
    # ═════════════════════════════════════════════════════════

    def _phase3_classify(
        self,
        top_pairs: List[Tuple[str, str, float]],
        label_set: List[str],
        eg_root: str,
    ) -> List[MappingRecord]:
        """
        For each candidate pair whose similarity ≥ τ_map:

        1. Call the LLM classifier.
        2. Create a DERIVES_FROM edge  (target → source).
        3. Create a Derived EvidenceNode summarising the
           classification.
        4. Create REFERENCES edges from the Derived node to
           both source and target.
        5. Create a ProvenanceEvent (``action = 'created'``).
        6. Create a ReliabilityFactor.
        """
        mappings: List[MappingRecord] = []
        now = _now()
        classified_pairs = self._classify_pairs(top_pairs, label_set)

        for item in classified_pairs:
            s_uid = item["s_uid"]
            t_uid = item["t_uid"]
            sim = item["sim"]
            s_node = item["s_node"]
            t_node = item["t_node"]
            result = item["result"]

            label = result.label
            conf = _clamp01(result.confidence)
            justif = result.rationale or ""
            missing = result.missing_elements
            abs_ev = result.abstraction_evidence

            # ── Null-check justification ────────────────────
            if not justif:
                justif = (
                    "[No justification returned by classifier]")
                self._diag(
                    "WARNING", "MAP_NULL_JUSTIFICATION",
                    f"Classifier returned empty justification "
                    f"for ({s_uid}, {t_uid})")

            # ── Validation state ────────────────────────────
            val_state = "UNVERIFIED"
            if label in _VALIDATION_LABELS:
                if missing and abs_ev:
                    val_state = "AUTO_VALIDATED"
                else:
                    val_state = "NEEDS_REVIEW"

            # ─────────────────────────────────────────────────
            #  3a. DERIVES_FROM edge: target → source
            # ─────────────────────────────────────────────────
            df_uid = _deterministic_uid(
                f"derives_from::{t_uid}::{s_uid}::{label}")

            self.eg.create_edge(t_uid, s_uid, "DERIVES_FROM", {
                "uid":           df_uid,
                "confidence":    conf,
                "justification": justif,
                "assertedByUid": self.cfg.transform_agent_uid,
                "assertedAt":    now,
                "domainMetadata": {
                    "transformLabel":   label,
                    "validationState":  val_state,
                    "cosineSimilarity": round(sim, 4),
                    "mapping_reason": (
                        "MAP-TRANSFORM: target DERIVES_FROM source. "
                        f"label='{label}', "
                        f"sim={sim:.4f}, conf={conf:.4f}."
                    ),
                },
            })

            # ─────────────────────────────────────────────────
            #  3b. Derived EvidenceNode
            # ─────────────────────────────────────────────────
            d_uid = _deterministic_uid(
                f"derived::{s_uid}::{t_uid}::{label}")

            s_label = s_node.get("label", s_uid)
            t_label = t_node.get("label", t_uid)

            self.eg.create_node(["EvidenceNode"], {
                "uid":             d_uid,
                "type":            "Derived",
                "label":           (
                    f"{label}: {s_label} → {t_label}"),
                "description":     justif,
                "contentExcerpt":  justif,
                "createdAt":       now,
                "lifecycleStatus": "draft",
                "reliabilityScore": conf,
                "sourceReference": f"MAP_TRANSFORM:{df_uid}",
                "domainMetadata":  {
                    "transformLabel":      label,
                    "validationState":     val_state,
                    "sourceUID":           s_uid,
                    "targetUID":           t_uid,
                    "cosineSimilarity":    round(sim, 4),
                    "missingElements":     missing,
                    "abstractionEvidence": abs_ev,
                    "mapping_reason": (
                        "MAP-TRANSFORM Derived node: captures the "
                        f"classified transformation '{label}' "
                        f"between source {s_uid} and "
                        f"target {t_uid}."
                    ),
                },
            })

            # ── CONTAINS_NODE ────────────────────────────────
            self.eg.create_edge(
                eg_root, d_uid, "CONTAINS_NODE", {
                    "uid": _edge_uid(
                        eg_root, d_uid, "CONTAINS_NODE"),
                })

            # ─────────────────────────────────────────────────
            #  3c. REFERENCES edges
            # ─────────────────────────────────────────────────
            self.eg.create_edge(
                d_uid, s_uid, "REFERENCES", {
                    "uid":           _edge_uid(
                        d_uid, s_uid, "REFERENCES"),
                    "confidence":    conf,
                    "justification": (
                        f"Source evidence for {label} "
                        f"classification"),
                    "assertedByUid": self.cfg.transform_agent_uid,
                    "assertedAt":    now,
                    "domainMetadata": {
                        "role": "source",
                        "mapping_reason":
                            "MAP-TRANSFORM: derived REFERENCES "
                            "source.",
                    },
                })
            self.eg.create_edge(
                d_uid, t_uid, "REFERENCES", {
                    "uid":           _edge_uid(
                        d_uid, t_uid, "REFERENCES"),
                    "confidence":    conf,
                    "justification": (
                        f"Target evidence for {label} "
                        f"classification"),
                    "assertedByUid": self.cfg.transform_agent_uid,
                    "assertedAt":    now,
                    "domainMetadata": {
                        "role": "target",
                        "mapping_reason":
                            "MAP-TRANSFORM: derived REFERENCES "
                            "target.",
                    },
                })

            # ─────────────────────────────────────────────────
            #  3d. ProvenanceEvent (created)
            # ─────────────────────────────────────────────────
            pe_uid = _deterministic_uid(
                f"pe::created::{d_uid}")

            self.eg.create_node(["ProvenanceEvent"], {
                "uid":       pe_uid,
                "action":    "created",
                "timestamp": now,
                "notes":     (
                    f"Classified by MAP-TRANSFORM, "
                    f"label={label}"),
                "domainMetadata": {
                    "mapping_reason": (
                        "MAP-TRANSFORM: ProvenanceEvent for "
                        f"Derived node creation, "
                        f"label='{label}'."
                    ),
                },
            })
            self.eg.create_edge(
                d_uid, pe_uid, "HAS_PROVENANCE_EVENT", {
                    "uid": _edge_uid(
                        d_uid, pe_uid,
                        "HAS_PROVENANCE_EVENT"),
                    "sequenceIndex": 0,
                })
            self.eg.create_edge(
                pe_uid, self.cfg.transform_agent_uid,
                "PERFORMED_BY", {
                    "uid": _edge_uid(
                        pe_uid,
                        self.cfg.transform_agent_uid,
                        "PERFORMED_BY"),
                })
            self._seq_counters[d_uid] = 1

            # ─────────────────────────────────────────────────
            #  3e. ReliabilityFactor
            # ─────────────────────────────────────────────────
            rf_uid = _deterministic_uid(f"rf::{d_uid}")

            self.eg.create_node(["ReliabilityFactor"], {
                "uid":    rf_uid,
                "factor": "classification_confidence",
                "impact": classify_impact(
                    conf,
                    self.cfg.tau_impact_positive,
                    self.cfg.tau_impact_neutral,
                ),
                "notes":  (
                    f"score={conf:.4f}, "
                    f"assessedAt={now}"),
            })
            self.eg.create_edge(
                d_uid, rf_uid, "HAS_RELIABILITY_FACTOR", {
                    "uid": _edge_uid(
                        d_uid, rf_uid,
                        "HAS_RELIABILITY_FACTOR"),
                })

            # ── Accumulate mapping record ────────────────────
            mappings.append(MappingRecord(
                derived_uid=d_uid,
                df_uid=df_uid,
                s_uid=s_uid,
                t_uid=t_uid,
                label=label,
                confidence=conf,
                justification=justif,
                validation_state=val_state,
            ))

        self._diag(
            "INFO", "MAP_PHASE3",
            f"{len(mappings)} mappings classified from "
            f"{len(top_pairs)} candidate pairs")
        return mappings

    # ═════════════════════════════════════════════════════════
    #  Phase 4 — Consistency filter and retraction
    # ═════════════════════════════════════════════════════════

    def _phase4_filter(
        self,
        mappings: List[MappingRecord],
    ) -> Tuple[List[MappingRecord], List[MappingRecord]]:
        """
        Select a consistent subset of mappings.

        Default strategy: for each **target** node, retain at
        most ``cfg.max_mappings_per_target`` mappings (highest
        confidence first).  The rest are retracted.

        Retraction writes:

        * ``lifecycleStatus = 'retracted'``
        * ``retractedAt     = NOW()``
        * ``retractedReason = 'Dropped by …'``
        * A ``ProvenanceEvent`` with ``action = 'retracted'``
          (S1 amendment).
        """
        # ── Group by target, keep top-N per target ──────────
        by_target: Dict[str, List[MappingRecord]] = {}
        for m in mappings:
            by_target.setdefault(m.t_uid, []).append(m)

        retained_set: Set[str] = set()
        for t_uid, group in by_target.items():
            group.sort(key=lambda m: m.confidence, reverse=True)
            for m in group[: self.cfg.max_mappings_per_target]:
                retained_set.add(m.derived_uid)

        retained: List[MappingRecord] = []
        dropped: List[MappingRecord] = []
        for m in mappings:
            if m.derived_uid in retained_set:
                retained.append(m)
            else:
                dropped.append(m)

        # ── Retract dropped Derived nodes ────────────────────
        now = _now()
        for m in dropped:
            # Schema: retracted_requires_timestamp
            self.eg.set_properties(m.derived_uid, {
                "lifecycleStatus": "retracted",
                "retractedAt":     now,
                "retractedReason": (
                    "Dropped by MAP-TRANSFORM "
                    "consistency filter"),
            })

            # ProvenanceEvent — S1 action 'retracted'
            pe_uid = _deterministic_uid(
                f"pe::retracted::{m.derived_uid}")
            seq_idx = self._seq_counters.get(
                m.derived_uid, 1)

            self.eg.create_node(["ProvenanceEvent"], {
                "uid":       pe_uid,
                "action":    "retracted",
                "timestamp": now,
                "notes":     (
                    "Dropped by MAP-TRANSFORM "
                    "consistency filter"),
                "domainMetadata": {
                    "mapping_reason": (
                        "MAP-TRANSFORM retraction: Derived "
                        f"node {m.derived_uid} removed by "
                        f"consistency filter."
                    ),
                },
            })
            self.eg.create_edge(
                m.derived_uid, pe_uid,
                "HAS_PROVENANCE_EVENT", {
                    "uid": _edge_uid(
                        m.derived_uid, pe_uid,
                        "HAS_PROVENANCE_EVENT"),
                    "sequenceIndex": seq_idx,
                })
            self.eg.create_edge(
                pe_uid, self.cfg.transform_agent_uid,
                "PERFORMED_BY", {
                    "uid": _edge_uid(
                        pe_uid,
                        self.cfg.transform_agent_uid,
                        "PERFORMED_BY"),
                })
            self._seq_counters[m.derived_uid] = seq_idx + 1

        self._diag(
            "INFO", "MAP_PHASE4",
            f"{len(retained)} retained, "
            f"{len(dropped)} retracted")
        return retained, dropped

    # ═════════════════════════════════════════════════════════
    #  Helpers
    # ═════════════════════════════════════════════════════════

    def _get_embedding(
        self, uid: str, text: str,
    ) -> List[float]:
        """Embed with per-UID caching."""
        if uid not in self._embed_cache:
            self._embed_cache[uid] = self.embedder.embed(text)
        return self._embed_cache[uid]

    def _diag(
        self,
        severity: str,
        code: str,
        message: str,
        context: Dict[str, Any] | None = None,
    ) -> None:
        entry = {
            "severity": severity,
            "code":     code,
            "message":  message,
            "context":  context or {},
            "timestamp": _now(),
        }
        self._diags.append(entry)
        getattr(logger, severity.lower(), logger.info)(
            f"[{code}] {message}")

    def _finalise(
        self,
        retained: List[MappingRecord],
        dropped_uids: Set[str],
        all_derived: List[str],
    ) -> MapTransformResult:
        return MapTransformResult(
            retained=retained,
            dropped_uids=dropped_uids,
            all_derived_uids=all_derived,
            diagnostics=list(self._diags),
            stats={
                "retained":    len(retained),
                "dropped":     len(dropped_uids),
                "total":       len(all_derived),
                "diagnostics": len(self._diags),
            },
        )


# ═══════════════════════════════════════════════════════════════
#  SECTION 7 — Memgraph driver adapter
# ═══════════════════════════════════════════════════════════════

class MemgraphStore:
    """
    Production adapter wrapping ``mgclient``.

    Extends the write-only pattern from trace2.MemgraphWriter
    with ``get_node`` (read) and ``set_properties`` (update).

    Nested dicts are JSON-serialised; lists are passed through
    natively (Memgraph supports list-typed properties).
    """

    def __init__(
        self, host: str = "127.0.0.1", port: int = 7687,
    ):
        try:
            import mgclient                         # type: ignore
        except ImportError as exc:
            raise ImportError(
                "pip install mgclient  "
                "(or use InMemoryGraphStore)"
            ) from exc
        self._conn = mgclient.connect(
            host=host, port=port)
        self._uids: Set[str] = set()

    # ── internal helpers ─────────────────────────────────────

    def _run(
        self, cypher: str, params: Dict[str, Any],
    ) -> None:
        cur = self._conn.cursor()
        cur.execute(cypher, params)
        self._conn.commit()

    def _query(
        self, cypher: str, params: Dict[str, Any],
    ) -> List[Tuple]:
        cur = self._conn.cursor()
        cur.execute(cypher, params)
        rows = cur.fetchall()
        self._conn.commit()
        return rows

    @staticmethod
    def _flat(props: Dict[str, Any]) -> Dict[str, Any]:
        """
        Prepare properties for Cypher parameter binding.

        Only dicts (Map) are JSON-serialised; lists are preserved
        as native Memgraph list properties (String[]).
        """
        out: Dict[str, Any] = {}
        for k, v in props.items():
            if v is None:
                continue
            if isinstance(v, dict):
                out[k] = json.dumps(v)
            else:
                out[k] = v
        return out

    # ── GraphStore implementation ────────────────────────────

    def create_node(
        self, labels: List[str], properties: Dict[str, Any],
    ) -> None:
        uid = properties.get("uid", "")
        if uid in self._uids:
            return
        self._uids.add(uid)
        lbl = ":".join(labels)
        flat = self._flat(properties)
        set_parts = ", ".join(f"n.{k} = ${k}" for k in flat)
        cypher = (
            f"MERGE (n:{lbl} {{uid: $uid}}) "
            f"ON CREATE SET {set_parts} "
            f"ON MATCH SET {set_parts}")
        self._run(cypher, flat)

    def create_edge(
        self, from_uid: str, to_uid: str,
        rel_type: str, properties: Dict[str, Any],
    ) -> None:
        flat = self._flat(properties or {})
        edge_uid = flat.get("uid", "")
        if edge_uid:
            other = {k: v for k, v in flat.items()
                     if k != "uid"}
            if other:
                sp = ", ".join(
                    f"r.{k} = ${k}" for k in other)
                sc = (f"ON CREATE SET {sp} "
                      f"ON MATCH SET {sp}")
            else:
                sc = ""
            cypher = (
                f"MATCH (a {{uid: $from_uid}}), "
                f"(b {{uid: $to_uid}}) "
                f"MERGE (a)-[r:{rel_type} "
                f"{{uid: $uid}}]->(b) {sc}")
        else:
            if flat:
                sp = ", ".join(
                    f"r.{k} = ${k}" for k in flat)
                sc = f"SET {sp}"
            else:
                sc = ""
            cypher = (
                f"MATCH (a {{uid: $from_uid}}), "
                f"(b {{uid: $to_uid}}) "
                f"CREATE (a)-[r:{rel_type}]->(b) {sc}")
        self._run(cypher, {
            "from_uid": from_uid,
            "to_uid": to_uid,
            **flat,
        })

    def node_exists(self, uid: str) -> bool:
        if uid in self._uids:
            return True
        rows = self._query(
            "MATCH (n {uid: $uid}) RETURN n.uid LIMIT 1",
            {"uid": uid})
        if rows:
            self._uids.add(uid)
            return True
        return False

    def get_node(
        self, uid: str,
    ) -> Optional[Dict[str, Any]]:
        rows = self._query(
            "MATCH (n {uid: $uid}) "
            "RETURN properties(n) AS props LIMIT 1",
            {"uid": uid})
        if rows:
            self._uids.add(uid)
            return rows[0][0]
        return None

    def set_properties(
        self, uid: str, properties: Dict[str, Any],
    ) -> None:
        flat = self._flat(properties)
        if not flat:
            return
        set_parts = ", ".join(
            f"n.{k} = ${k}" for k in flat)
        self._run(
            f"MATCH (n {{uid: $uid}}) SET {set_parts}",
            {"uid": uid, **flat})


# ═══════════════════════════════════════════════════════════════
#  SECTION 8 — Convenience entry-point and CLI
# ═══════════════════════════════════════════════════════════════

def run_map_transform(
    eg: GraphStore,
    source_uids: List[str],
    target_uids: List[str],
    label_set: List[str],
    eg_root_uid: str,
    embedder: Embedder | None = None,
    classifier: TransformClassifier | None = None,
    cfg: MapTransformConfig | None = None,
) -> MapTransformResult:
    """
    One-liner to execute MAP-TRANSFORM.

    Parameters
    ----------
    eg : GraphStore
        Evidence Graph reader/writer.
    source_uids, target_uids : list[str]
        Evidence node UIDs to compare.
    label_set : list[str]
        Allowed transformation labels.
    eg_root_uid : str
        UID of the EG GraphRoot.
    embedder : Embedder, optional
        Defaults to ``IdentityEmbedder`` (testing only).
    classifier : TransformClassifier, optional
        Defaults to ``StubClassifier`` (testing only).
    cfg : MapTransformConfig, optional

    Returns
    -------
    MapTransformResult
    """
    emb = embedder or IdentityEmbedder()
    clf = classifier or StubClassifier()
    mt = MapTransform(
        eg=eg,
        embedder=emb,
        classifier=clf,
        cfg=cfg,
    )
    return mt.execute(
        source_uids, target_uids, label_set, eg_root_uid)


# ── CLI ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
    )

    print(
        "map_transform.py — standalone execution requires "
        "an EG populated by trace2.py.\n"
        "\n"
        "Example integration:\n"
        "\n"
        "    from trace2 import Trace, TraceConfig\n"
        "    from map_transform import (\n"
        "        InMemoryGraphStore, MapTransform,\n"
        "        MapTransformConfig,\n"
        "        IdentityEmbedder, StubClassifier,\n"
        "    )\n"
        "\n"
        "    store = InMemoryGraphStore()\n"
        "    rg    = InMemoryGraphStore()\n"
        "    trace = Trace(eg=store, rg=rg)\n"
        "    tr    = trace.execute(bundle)\n"
        "\n"
        "    mt = MapTransform(\n"
        "        eg=store,\n"
        "        embedder=IdentityEmbedder(),\n"
        "        classifier=StubClassifier(),\n"
        "    )\n"
        "    result = mt.execute(\n"
        "        source_uids=tr.evidence_node_uids,\n"
        "        target_uids=tr.evidence_node_uids,\n"
        "        label_set=['VERBATIM', 'PARAPHRASE',\n"
        "                   'OMISSION', 'QUALIFIER_DROP',\n"
        "                   'HEDGE_DROP', 'OTHER'],\n"
        "        eg_root_uid=tr.eg_root_uid,\n"
        "    )\n"
        "\n"
        "    print(f'Retained: {result.stats[\"retained\"]}')\n"
        "    print(f'Dropped:  {result.stats[\"dropped\"]}')\n"
    )
