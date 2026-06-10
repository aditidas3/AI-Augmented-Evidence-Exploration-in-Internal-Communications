# Labeler Code Walkthrough

A step-by-step map of `labeler/` for readers who want to know **how a
PDF or text-like input turns into document text, labels, and extracted
entities, what every LLM call is for, and what knobs control the
pipeline**.

This document pairs with the source files. When you see a reference
like `pipeline/stage1.py:85`, open the file at that line — the code
is the ground truth; this walkthrough is a guide.

---

## 1. What the labeler is

The labeler is a **two-stage document processing pipeline**. PDF
inputs still go through page rendering and page-family labeling.
Text-like inputs (`txt`, `csv`, `xml`, `json`, `html`, markdown, yaml,
logs, extensionless text, and similar decodable files) skip rendering
and are treated as one document. Zip inputs are batch containers: each
supported member becomes its own document. Stage 2 extracts entities
and relationships. A PDF run produces:

```
PDF -> render -> Stage 1 classify pages -> labels.jsonl / labels.json
              -> Stage 2 OCR + entity/relationship extraction
              -> summary.json / relationship.txt / stage2_manifest.json
```

For text-like inputs the front of the flow is shorter:

```
text/csv/xml/... -> synthetic one-document labels.jsonl / labels.json
                 -> Stage 2 entity/relationship extraction
```

**Stage 1** classifies every PDF page into one of five `Label` values
— `email`, `document`, `spreadsheet`, `presentation`, `text`. For
text-like inputs, the orchestrator writes a synthetic one-row
`document` label instead of calling the page-classification model.
Output: `labels.jsonl` and `labels.json`. KG0 reads `labels.jsonl`
during `create_pages()` so Page nodes can carry a family label.

**Stage 2** runs OCR on every rendered PDF page image or reuses the
source text for text-like inputs, concatenates the text into a single
document, and extracts:
- an **entity inventory** (`entities.txt`),
- **Wikipedia enrichments** per entity (`wikipedia_enrichment.txt`),
- a **relationship list** (`relationship.txt`),
- a consolidated **`summary.json`**, and
- a **`stage2_manifest.json`** with run metadata.

Stage 2 is the terminal labeler output. Downstream ingestion into
databases or search indexes is handled outside the labeler package.

The labeler is **provider-agnostic** — the same code can talk to an
OpenAI-compatible HTTP endpoint, a local vLLM server, or a local
Qwen transformers install, selected at runtime via a flag.

---

## 2. Package layout

```
labeler/
├── cli.py                      argparse CLI entry point (~225 lines)
├── __init__.py                 (empty)
├── core/
│   └── schemas.py              Pydantic models for Stage 1 I/O (~100 lines)
├── extraction/
│   ├── render.py               PDF → PNG + optimized JPEG (~85 lines)
│   ├── ocr.py                  olmocr GPU OCR wrapper (~280 lines)
│   └── extract_layout.py       Page layout helpers (~190 lines)
├── models/
│   ├── llm.py                  LLMProvider protocol + OpenAI-compat client (~840 lines)
│   ├── llm_json.py             LLM JSON recovery + normalization helpers
│   ├── page_input.py           Image payload and hash helpers
│   ├── page_classification_prompt.py
│   └── qwen.py                 Local Qwen transformers backend (~260 lines)
├── pipeline/
│   ├── orchestrator.py         PipelineConfig + run_pipeline (~170 lines)
│   ├── stage1.py               Stage 1: classify pages (~285 lines)
│   ├── stage2.py               Stage 2: extract entities / relationships (~780 lines)
│   ├── stage2_outputs.py       Stage 2 output filename contract
│   ├── stage2_manifest.py      Stage 2 manifest payload
│   ├── stage2_text.py          Deterministic Stage 2 text helpers
│   ├── prompts.py              All LLM prompt templates (~1,080 lines)
│   └── progress.py             Progress CSV with Stage 1/2 status
└── CODE_WALKTHROUGH.md         This file
```

Current refactor note: the original high-level layout above is still
accurate, but several implementation details now live in smaller
modules. `models/llm.py` owns the provider protocol and
OpenAI-compatible client; `models/llm_json.py` owns JSON extraction and
normalization; `models/page_input.py` owns `PageInput`,
`file_to_base64()`, and `detect_mime_type()`; and
`models/page_classification_prompt.py` owns the Stage 1 classification
prompt. Stage 2 orchestration remains in `pipeline/stage2.py`, while
`pipeline/stage2_text.py`, `pipeline/stage2_outputs.py`, and
`pipeline/stage2_manifest.py` own deterministic text processing,
output-path contracts, and manifest payload construction.

Three layers:

1. **Control** — `cli.py` parses CLI args, builds a `PipelineConfig`,
   calls `run_pipeline`.
2. **Pipeline** - `orchestrator.py` runs Stage 1 and Stage 2.
3. **Stages** - `stage1.py` does page classification; `stage2.py` does
   entity/relationship extraction.

Everything else is utilities or prompt templates.

---

## 3. Data model at a glance

### 3.1 Stage 1 types (`core/schemas.py`)

```python
Label = Literal["email", "document", "spreadsheet", "presentation", "text"]

class PageResult(BaseModel):
    page_index: int          # 1-indexed
    label: Label             # one of the five canonical labels
    confidence: float        # [0, 1]
    rationale: str           # LLM explanation

class LabelResponse(BaseModel):
    schema_version: str = "1.0"
    pdf_name: str
    pages: list[PageResult]

class RenderedPage(BaseModel):
    page_index: int
    image_path: str          # high-res PNG
    width: int
    height: int
    dpi: int
    optimized_image_path: str | None  # JPEG (when --optimize-images)
    llm_image_path: str      # path that gets sent to the LLM

class RenderManifest(BaseModel):
    pdf_path: str
    dpi: int
    pages: list[RenderedPage]

class JsonlRow(BaseModel):
    page_index: int
    label: Label
    confidence: float
    rationale: str
    image_path: str          # the LLM image path (for traceability)
```

Three validators:

- **`validate_full_coverage(response, expected_pages)`** — asserts the
  response contains exactly `expected_pages` `PageResult`s with
  contiguous indexes.
- **`parse_label_response(data)`** — parses raw JSON from the LLM
  into `LabelResponse`.
- **`make_label_response(pdf_name, pages)`** — constructor helper.

### 3.2 Stage 2 types (`pipeline/stage2.py`)

```python
@dataclass(frozen=True)
class Stage2Config:
    output_dir: Path
    pdf_name: str
    pdf_hash: str
    schema_model: str | None = None

@dataclass(frozen=True)
class Stage2Result:
    entities_path: Path
    wikipedia_enrichment_path: Path
    relationship_path: Path
    summary_json_path: Path
    manifest_path: Path
    success_count: int
    error_count: int
    output_files: list[str]
```

Stage 2 has no Pydantic envelope — every intermediate is a plain
text file the next LLM call reads as-is.

## 4. Entry points

### 4.1 CLI

```bash
python -m labeler.cli \
  --pdf /data/docs/SOME_DOC.pdf \
  --out /data/docs \
  --id SOME_DOC \
  --dpi 200 \
  --batch-size 4 \
  --max-dim 1600 \
  --optimize-images \
  --provider endpoint \
  --model claude-3-5-sonnet \
  --extract-schema \
  --schema-model claude-3-5-sonnet \
  --max-concurrent 10
```

`cli.py` also supports directory mode: pass a directory to `--pdf`
and every `*.pdf` under it gets processed in sequence. Each doc goes
under `{out}/{id}/` — default `id` is the PDF stem.

Key flags:

| Flag | Default | What it does |
|---|---|---|
| `--pdf` | (required) | PDF file or directory of PDFs |
| `--out` | `labeler_out/` | Root output directory |
| `--id` | PDF stem | Per-document subdir name |
| `--dpi` | `200` | PNG render DPI |
| `--batch-size` | `4` | Pages per Stage 1 LLM call |
| `--max-dim` | `1600` | Max image dimension for the LLM call |
| `--optimize-images` | off | Generate side-by-side JPEGs at `--max-dim` |
| `--provider` | `endpoint` | `endpoint`, `vllm`, or `qwen-local` |
| `--model` | env/default | Stage 1 model name |
| `--extract-schema` | on | Run Stage 2 entity/relationship extraction |
| `--schema-model` | `--model` | Stage 2 model name (can differ) |
| `--max-concurrent` | `10` | Thread pool size for Stage 1 batches |
| `--dry-run` | off | Render only, skip LLM calls |
| `--skip-stage2-if-exists` | off | Idempotency for Stage 2 when `relationship.txt` is non-empty |
| `--gpu-memory-utilization` | `0.09` | vLLM GPU memory fraction |
| `--log-level` | `INFO` | Standard logging level |

### 4.2 Programmatic

```python
from labeler.pipeline.orchestrator import run_pipeline, PipelineConfig
from pathlib import Path

config = PipelineConfig(
    pdf_path=Path("/data/docs/SOME_DOC.pdf"),
    output_dir=Path("/data/docs"),
    dpi=200,
    batch_size=4,
    max_dim=1600,
    optimize_images=True,
    provider_name="endpoint",
    model="claude-3-5-sonnet",
    extract_schema=True,
    schema_model="claude-3-5-sonnet",
    max_concurrent=10,
)
run_pipeline(config)
```

---

## 5. Execution flow at a glance

`run_pipeline` in `pipeline/orchestrator.py` is 60 lines of glue.
High-level:

```
PipelineConfig ─► resolve stage1_model / stage2_model from env+config
              │
              ▼
              build_provider(provider_name, model) ─► LLMProvider
                 │
                 ├─ endpoint    → OpenAICompatibleClient (HTTP)
                 ├─ vllm        → OpenAICompatibleClient (local vLLM server)
                 └─ qwen-local  → QwenLocalClient (transformers in-process)
              │
              ▼
              run_stage1(Stage1Config, provider)
              │   └─► Stage1Artifacts {labels.json, labels.jsonl, manifest, …}
              │
              ▼
              if dry_run or not extract_schema: return
              │
              ▼
              OCR every rendered page via extraction/ocr.py (olmocr)
              │   └─► concatenate with page markers into a single string
              │   └─► write {doc_id}.txt
              │
              ▼
              run_stage2(Stage2Config, provider, ocr_text)
                  -> entities.txt, wikipedia_enrichment.txt,
                     relationship.txt, summary.json, stage2_manifest.json
```

Stage 1 is **always** represented in outputs: PDFs are rendered and
classified page by page, while text-like inputs get a synthetic
one-row `document` label. Stage 2 is controlled by `--extract-schema`
/ `--no-extract-schema`. A `--dry-run` prepares the input but skips
every LLM call.

---

## 6. Stage-by-stage walkthrough

### Stage 0 — Rendering
**Module**: `extraction/render.py`
**Entry**: `render_pdf_pages(pdf_path, out_dir, dpi, optimize_images,
max_dim, jpeg_quality=85)`

1. Open the PDF via PyMuPDF (`fitz.open(str(pdf_path))`).
2. Build a `zoom = dpi / 72.0` transformation matrix.
3. For each page:
   - Rasterize to a `Pixmap` at the requested DPI.
   - Save the raw PNG to `{out_dir}/pages/page_{page_index:04d}.png`.
   - If `optimize_images=True`:
     - Open the PNG via Pillow.
     - Resize to fit within `(max_dim, max_dim)` via LANCZOS.
     - Save as JPEG at `page_{page_index:04d}.opt.jpg`.
     - Point `llm_image_path` at the JPEG.
   - Otherwise `llm_image_path = image_path` (the PNG).
4. Build a `RenderManifest` with every `RenderedPage`.
5. Write `pages/manifest.json` next to the images.

The manifest carries everything Stage 1 needs to reconstruct page
metadata without re-opening the PDF.

Why render once: every downstream stage reads the rendered images.
Re-rendering on every call would dominate runtime for large PDFs.

### Stage 1 — Page classification
**Module**: `pipeline/stage1.py`
**Entry**: `run_stage1(config: Stage1Config, provider: LLMProvider)
-> Stage1Artifacts`

Six-phase pipeline:

1. **Resolve** — compute `pdf_hash = sha256_file(pdf_path)` once.
   The hash is the key into the per-image label cache.
2. **Render** — call `render_pdf_pages` (Stage 0).
3. **Prepare inputs** — `prepare_page_inputs(manifest)` reads each
   rendered page, base64-encodes it via `file_to_base64`, computes
   its `image_hash = sha256`, and builds a list of `PageInput`
   objects. These are what the provider's `generate` method
   actually accepts.
4. **Cache lookup** — for every `PageInput`, check
   `cache/{pdf_hash}/page_{image_hash}.json`. Cache hits produce a
   ready-made `PageResult`; only the misses go to the LLM.
5. **Batch the cache misses** — chunk by `batch_size` and send each
   chunk to the LLM via a `ThreadPoolExecutor(max_concurrent)`.
   Each chunk becomes one LLM call with a multi-page image input
   and a system prompt demanding structured JSON back.
6. **Merge and validate** — concatenate cached + fresh results,
   call `validate_full_coverage(response, expected=len(pages))` to
   catch any missing pages, then write the outputs:
   - `labels.json` — the full `LabelResponse` pretty-printed
   - `labels.jsonl` — one `JsonlRow` per line (what KG0 reads)
   - Populate the cache for new results via `save_label_to_cache`.

```
Stage1Artifacts
├── pdf_name
├── pdf_hash
├── manifest            RenderManifest
├── page_inputs         list[PageInput]
├── labels_json_path    Path to labels.json
└── labels_jsonl_path   Path to labels.jsonl
```

**Caching strategy**: the cache key is `image_hash`, not
`page_index` — so two different PDFs that happen to render to the
same byte-identical page image (e.g. the same boilerplate page)
share a cache hit. This makes scheduled re-runs of a growing corpus
much cheaper over time.

**Concurrency**: `max_concurrent=10` is the default because vision
LLM endpoints usually rate-limit at ~10 concurrent image requests.
Bump it to 20+ if your endpoint allows.

### Stage 2 — Entity and relationship extraction
**Module**: `pipeline/stage2.py`
**Entry**: `run_stage2(*, provider, config, document_ocr_text)`

Runs **after** Stage 1 has labeled every page. Inputs:

- `provider: LLMProvider` — same provider Stage 1 used.
- `config: Stage2Config` — output dir, PDF name, PDF hash, optional
  stage-2 model override.
- `document_ocr_text: str` — the full concatenated OCR output.

Seven-phase pipeline:

1. **Entity extraction** — call the provider with
   `ENTITY_EXTRACTION_SYSTEM` (from `pipeline/prompts.py`) and the
   full OCR text. Returns a newline-delimited inventory like:

   ```
   Person: John Doe
   Organization: Acme Corp
   Drug: Oxycodone
   ...
   ```

   Write raw output to `entities.txt`, then sanitize via
   `_sanitize_entity_inventory_text()` to drop stray bullet marks
   and metadata lines.

2. **Wikipedia enrichment** — call the provider with
   `WIKIPEDIA_ENRICHMENT_SYSTEM` and the sanitized entity list.
   Returns `Entity | Wikipedia URL | Wikipedia Category` rows.
   Sanitize via `_sanitize_wikipedia_enrichment_text()` and write
   to `wikipedia_enrichment.txt`.

3. **Summary JSON** — build `summary.json` via
   `build_entity_summary_payload(document_name, entities_text,
   wikipedia_enrichment_text)`. This is a consolidated JSON that
   downstream tooling can parse without re-running the LLM.

4. **Relationship extraction** — call the provider with
   `RELATIONSHIP_EXTRACTION_SYSTEM`, passing both the OCR text and
   the sanitized entity list. Returns rows like:

   ```
   Person: John Doe | employed_by | Organization: Acme Corp
   Drug: Oxycodone | manufactured_by | Organization: Acme Corp
   ```

5. **Backfill missing entities** — `_backfill_missing_entities`
   parses the relationship output and identifies entities
   referenced in relationships but absent from the entity
   inventory. If any are missing, it runs a follow-up LLM call
   (`ENTITY_BACKFILL_SYSTEM`, `build_entity_backfill_user_prompt`)
   to add them back. Without this step, the relationship table
   would reference dangling entities.

6. **Relationship repair** — if the relationship output violates
   formatting, a second call with `RELATIONSHIP_REPAIR_SYSTEM`
   and `build_relationship_repair_user_prompt()` coerces it into
   the expected shape. Uses `_parse_relationship_line` as a
   validator.

7. **Write final outputs** — `relationship.txt`, update
   `summary.json`, write `stage2_manifest.json` with run metadata
   (model, token counts, success/error counts, output paths).

`Stage2Result` is returned with all the output paths and counters.

**Why so many LLM calls**: the split between entity extraction,
enrichment, relationship extraction, backfill, and repair is
deliberate — a single monolithic prompt is **less** reliable at
producing structured output than a chain of narrow prompts each
doing one thing. The chain also lets Stage 2 be partially cached
(if you only change the relationship prompt, entity extraction can
be skipped when the OCR text is unchanged).

**Allowed-entity filtering**: `_build_allowed_entity_keys` and
`_entity_is_allowed` implement a post-hoc check that every relationship
endpoint resolves to a known entity. Unmatched endpoints get dropped
rather than flagged — the LLM tends to hallucinate plausible-looking
references that do not appear in the document.

**Occurrence counting**: `_count_occurrences` and
`_build_entity_occurrence_info` tally how many times each entity
appears in the OCR text, so downstream tools can use the statistics
without re-running extraction.

---

## 7. Supporting modules

### 7.1 `models/llm.py` — LLM provider abstraction

The largest file in `labeler/`. Core surface:

```python
class LLMProvider(Protocol):
    def generate(self, *, system_prompt: str, user_prompt: str,
                 images: list[PageInput] | None = None,
                 model: str | None = None, temperature: float = 0.2,
                 response_format: str = "json") -> str: ...

    def generate_text(self, *, system_prompt: str, user_prompt: str,
                      model: str | None = None,
                      temperature: float = 0.2) -> str: ...
```

Concrete implementation: `OpenAICompatibleClient`. It speaks the
OpenAI chat-completions format and works against any server that
implements that interface (real OpenAI, Anthropic via proxy, local
vLLM, Ollama with the OpenAI adapter, etc.).

Supporting utilities are now split by responsibility:

- **`models/page_input.py`** — `PageInput`, `file_to_base64()`,
  and `detect_mime_type()`. `models/llm.py` re-exports these names for
  older imports.
- **`models/llm_json.py`** — JSON extraction, JSON-like recovery,
  label/confidence normalization, and batch payload normalization.
- **`models/page_classification_prompt.py`** — the Stage 1
  page-classification system prompt. `models/llm.py:SYSTEM_PROMPT`
  remains a compatibility alias.

Previously, these utilities lived in `models/llm.py`:

- **`PageInput`** — `{page_index, image_path, base64, mime_type,
  image_hash}`. The unit every provider accepts for image input.
- **`file_to_base64(path)`** — reads a file and returns a data URI.
- **`detect_mime_type(path)`** — sniffs `.png` / `.jpg` / `.jpeg`.
- **`_sha256(bytes)`** — image-hash helper used for caching.
- **JSON extraction** — the LLM-response parser is a small
  state-machine of helpers (`_extract_json`, `_iter_json_candidates`,
  `_extract_balanced_snippets`, `_remove_trailing_commas_json`,
  `_try_load_json_like`). LLMs frequently wrap their JSON in prose,
  trailing commas, Markdown fences, or partial snippets — these
  functions strip all of that and return the first plausible JSON
  object.
- **`_normalize_label`** / **`_normalize_confidence`** /
  **`_extract_page_index`** / **`_extract_pages_list`** — single-
  field normalizers that make the Pydantic validators happy when
  the LLM returns `"PRESENTATION"` instead of `"presentation"` or
  a string confidence instead of a float.
- **`_normalize_batch_payload`** — given a raw LLM batch response,
  coerce it into the `{pages: [...]}` shape Stage 1 expects.
- **`parse_json_object(text)`** — public helper that runs the full
  extraction + normalization chain; also exported as
  `extract_json_object_text`.

### 7.2 `models/qwen.py` — Local Qwen backend

An alternative `LLMProvider` impl that loads a Qwen2-VL or Qwen2.5-VL
model directly via `transformers` and `accelerate`. Used when you
want zero-network inference (airgapped environments, dev without a
backend). Same interface as `OpenAICompatibleClient` so it slots in
transparently.

Not the default because transformers-based inference is 10-50x
slower than a dedicated vLLM server for multi-page batches.

### 7.3 `pipeline/prompts.py` — prompt templates

~1,080 lines of prompt strings. Exported constants:

- Stage 1 page classification prompt now lives in
  `models/page_classification_prompt.py` as
  `PAGE_CLASSIFICATION_SYSTEM_PROMPT`.
- `ENTITY_EXTRACTION_SYSTEM` — Stage 2 entity extraction system.
- `ENTITY_BACKFILL_SYSTEM` — Stage 2 missing-entity backfill.
- `WIKIPEDIA_ENRICHMENT_SYSTEM` — Stage 2 enrichment system.
- `RELATIONSHIP_EXTRACTION_SYSTEM` — Stage 2 relationship system.
- `RELATIONSHIP_REPAIR_SYSTEM` — Stage 2 relationship repair system.

Plus user-prompt builders:

- `build_wikipedia_enrichment_user_prompt(entity_list_text)`
- `build_relationship_user_prompt(document_text, entity_list_text)`
- `build_entity_backfill_user_prompt(...)`
- `build_relationship_repair_user_prompt(...)`

Prompts are versioned by edit — if you change one, bump the cache
key (see §8) so stale cache hits don't bleed through.

### 7.4 `extraction/ocr.py` — olmocr wrapper

OCR runs via `olmocr`, a vision-language model optimized for
scanned PDFs and image-based documents. The wrapper:

1. Normalizes input paths (`_normalize_input_paths`) — requires
   unique filename stems within a batch because olmocr uses the
   stem as the workspace key.
2. Builds the olmocr subprocess command
   (`_build_olmocr_command`) — runs `python -m olmocr.pipeline`
   with `--markdown`, `--pdfs`, `--model`, `--workers`.
3. Sets `VLLM_GPU_MEMORY_UTILIZATION` on the subprocess environment
   (default `0.09` for low-free-GPU setups; override via
   `--gpu-memory-utilization` on the CLI).
4. Runs olmocr, captures stdout/stderr, checks return code.
5. Reads the resulting Markdown files back into strings.

Why a subprocess instead of an in-process call: olmocr starts its
own vLLM instance, and keeping it in-process would conflict with
the provider's LLM session if they share a GPU.

### 7.5 `extraction/extract_layout.py`

Layout helpers for structural page information such as table
boundaries and reading order. Not called directly in the current
Stage 2 pipeline but retained for future structured-extraction paths.

### 7.6 `pipeline/progress.py`

A small CSV progress tracker for batch runs. Columns:

- `document_name`
- `is_segmented`
- `is_ocred`
- `is_entity_extracted`
- `is_relationship_extracted`

The flags are inferred from Stage 1/2 output files.

---

## 8. Determinism and caching

Stage 1 is deterministic in two layers:

1. **Image hash cache** — every `PageInput.image_hash` maps to a
   cached `PageResult` at
   `cache/{pdf_hash}/page_{image_hash}.json`. Re-running Stage 1
   with the same PDF is a pure cache hit and makes no LLM calls.
2. **LLM seed** — when the provider supports a temperature of 0 /
   fixed seed, Stage 1 gets the same classification every run.

Stage 2 is **not** deterministic today because the per-call
temperature is `0.2` (not `0.0`), and the LLM provider may not
respect seeds across runs. The cache exists but is less
forgiving — Stage 2 re-runs cost real tokens unless
`--skip-stage2-if-exists` is set.

**Cache invalidation**: changing any prompt in `pipeline/prompts.py`
means every cache hit is now stale. The canonical way to invalidate
is to delete `cache/`. There is no automatic versioning — if you
edit a prompt, delete the cache yourself.

---

## 9. Configuration

`PipelineConfig` (`pipeline/orchestrator.py:16`) is the single
source of truth:

```python
@dataclass(frozen=True)
class PipelineConfig:
    pdf_path: Path
    output_dir: Path
    dpi: int = 200
    batch_size: int = 4
    max_dim: int = 1600
    optimize_images: bool = True
    provider_name: str = "endpoint"        # endpoint | vllm | qwen-local
    model: str | None = None                # Stage 1 model
    dry_run: bool = False
    extract_schema: bool = True             # Run Stage 2?
    schema_model: str | None = None         # Stage 2 model override
    max_concurrent: int = 10                # Stage 1 ThreadPool size
    gpu_memory_utilization: float | None = None
    skip_stage2_if_output_exists: bool = False
```

Environment variables the orchestrator reads:

- `LABELER_STAGE1_MODEL` — default Stage 1 model
- `LABELER_STAGE2_MODEL` — default Stage 2 model
- `LABELER_ENDPOINT_URL` — OpenAI-compat base URL
- `LABELER_ENDPOINT_API_KEY` — auth
- `VLLM_GPU_MEMORY_UTILIZATION` — set on olmocr subprocess

Everything else is a CLI flag or a constant inside the module.

---

## 10. How to run

### 10.1 Single PDF, full pipeline

```bash
python -m labeler.cli \
  --pdf /data/docs/ACME_0001.pdf \
  --out /data/docs \
  --id ACME_0001 \
  --provider endpoint \
  --model claude-3-5-sonnet \
  --extract-schema \
  --schema-model claude-3-5-sonnet \
  --batch-size 4 \
  --max-concurrent 10
```

Output:

```
/data/docs/ACME_0001/
├── pages/
│   ├── manifest.json
│   ├── page_0001.png
│   ├── page_0001.opt.jpg
│   ├── page_0002.png
│   └── ...
├── labels.json
├── labels.jsonl                ◄── consumed by KG0
├── ACME_0001.txt                (full OCR text, if Stage 2 ran)
├── entities.txt
├── wikipedia_enrichment.txt
├── relationship.txt
├── summary.json
└── stage2_manifest.json
```

### 10.2 Directory of documents

```bash
python -m labeler.cli \
  --pdf /data/inputs \
  --out /data/docs \
  --provider endpoint \
  --model claude-3-5-sonnet \
  --extract-schema \
  --skip-stage2-if-exists
```

Every supported file under `/data/inputs` gets processed. This includes
PDFs, text-like files, and zip batches. `--id` is derived from each
file stem or zip member path. `--skip-stage2-if-exists` makes the Stage
2 part idempotent after a non-empty `relationship.txt` exists.

### 10.3 Dry run (render + Stage 1 preview)

```bash
python -m labeler.cli \
  --pdf /data/docs/SOME_DOC.pdf \
  --out /tmp/labeler_preview \
  --dry-run
```

Renders the pages, writes a dry-run summary, skips every LLM call.
Useful for verifying a PDF can be opened and rendered before
spending tokens.

### 10.4 Local Qwen (no network)

```bash
python -m labeler.cli \
  --pdf /data/docs/SOME_DOC.pdf \
  --out /data/docs \
  --provider qwen-local \
  --model Qwen/Qwen2-VL-7B-Instruct
```

Requires `transformers`, `accelerate`, and enough GPU to load the
Qwen weights.

### 10.5 Verifying Stage 1 matches KG0

```bash
head /data/docs/ACME_0001/labels.jsonl

# {"page_index": 1, "label": "email", "confidence": 0.94, "rationale": "...", "image_path": "/data/docs/ACME_0001/pages/page_0001.opt.jpg"}
# {"page_index": 2, "label": "email", "confidence": 0.91, "rationale": "...", ...}
# ...

# Then re-run KG0 to pick up the new labels
python -m pipeline.kg0.kg0_from_db \
  --docs-dir /data/docs \
  --neo4j-uri bolt://127.0.0.1:7687
```

KG0's `create_pages()` reads `labels.jsonl` directly; no intermediate
format.

---

## 11. Editing gotchas

1. **The five labels are closed.** Adding a sixth `Label` literal
   requires coordinated changes in: `core/schemas.py`,
   `models/page_classification_prompt.py`, and KG0's downstream
   consumers. Worth a design doc before doing.

2. **Stage 1 cache keys off `image_hash`, not `page_index`.**
   Changing `models/page_input.py:file_to_base64` invalidates every cache
   entry. Do not "normalize" image bytes without bumping a cache
   generation number.

3. **Stage 2's chain of calls is deliberate.** Collapsing entity
   extraction + enrichment + relationship extraction into a single
   prompt degrades accuracy by ~30% in tests. Keep the split.

4. **`_backfill_missing_entities` and `_build_allowed_entity_keys`
   are load-bearing filters.** Without them, the relationship
   output references hallucinated entities that do not appear in
   the document. Downstream KG0 ingestion would then build edges
   to dangling nodes.

5. **`validate_full_coverage` is not optional.** If the LLM returns
   fewer pages than expected (usually a batch-size overflow), the
   validator raises. Do not downgrade to a warning — the missing
   pages would silently default to `UNKNOWN_PAGE` in KG0.

6. **`render_pdf_pages` always rasterizes.** Text-extraction is
   intentionally not used, because the input corpus is mostly
   scanned PDFs where `page.get_text()` returns empty strings.
   If you add a fast-path for text-born PDFs, route it around
   the olmocr OCR in Stage 2 as well.

7. **`optimize_images=True` is not a cost-saver — it is a quality
   lever.** The high-res PNG gets rasterized regardless; the
   optimized JPEG just shrinks the image sent to the LLM so
   token counts stay manageable. Disabling it on high-DPI scans
   makes Stage 1 slower and more expensive.

8. **The provider's `generate` method blocks on each image.**
   Stage 1 parallelism comes from the `ThreadPoolExecutor(max_concurrent)`
   around `generate`, not from provider-native async. If you swap
   in an async provider, rewire Stage 1 accordingly.

9. **`olmocr` requires the olmocr package and poppler-utils to be
   installed.** Stage 2 will fail at OCR time with a clear error
   message if either is missing. Stage 1 does not depend on olmocr
   — you can run Stage 1 alone without it.

10. **`extract_schema=False` is the fastest way to bulk-label a
    corpus.** Stage 1 is cheap (one vision LLM call per batch);
    Stage 2 is expensive (four to six LLM calls per document plus
    OCR). For KG0 purposes you only need Stage 1 — Stage 2 is
    consumed by a separate upstream pipeline.

11. **Cache keys do not include the model name.** Two runs with
    different models against the same `image_hash` will see each
    other's cache hits. If you need per-model caches, add the
    model name into `save_label_to_cache` / `load_label_from_cache`.

---

## 12. Where to look next

- **`pipeline/kg0/CODE_WALKTHROUGH.md`** — how `labels.jsonl`
  becomes `:Page` nodes in Neo4j. Reading this next shows the full
  labeler → KG0 handoff.
- **`labeler/pipeline/prompts.py`** — every prompt string. If
  Stage 1 or Stage 2 is misclassifying something, the fix is almost
  always in this file, not in the code.
- **`labeler/models/llm.py:_extract_json`** and the helpers below
  it — the JSON parser is where 90% of "LLM returned something
  weird" bugs get fixed.
- **`labeler/pipeline/stage1.py:load_label_from_cache`** — the
  caching rules. Start here when cache behavior is surprising.
- **`labeler/core/schemas.py:validate_full_coverage`** — the
  contract every Stage 1 response must satisfy.
- **`labeler/extraction/ocr.py`** — the olmocr command builder. If
  OCR is slow or failing, check the subprocess command and the
  `VLLM_GPU_MEMORY_UTILIZATION` environment variable.

When Stage 1 misclassifies a page, the recovery path is:
1. Look at the exact image sent to the LLM
   (`pages/page_XXXX.opt.jpg` if `--optimize-images` was set).
2. Manually run the Stage 1 prompt against the image via your
   provider's console to see what it says.
3. If the model is wrong, adjust the prompt in
   `models/page_classification_prompt.py:PAGE_CLASSIFICATION_SYSTEM_PROMPT`.
4. Delete the `cache/{pdf_hash}/page_{image_hash}.json` entry.
5. Re-run Stage 1.

When Stage 2 produces bad relationships, start at
`_backfill_missing_entities` and walk backward through the chain —
the problem is almost always that entity extraction missed a
reference the relationship extractor then hallucinated.

Refactor map update: JSON parsing now lives in `models/llm_json.py`;
page input hashing and MIME helpers live in `models/page_input.py`;
and the Stage 1 classifier prompt lives in
`models/page_classification_prompt.py`.
