# Pipeline Architecture

mpy-triage detects duplicate, related, and off-topic issues/PRs across MicroPython repositories using a six-stage pipeline that combines structured data mirroring, LLM-generated summaries, semantic embeddings, hybrid search, and LLM-based assessment.

## Pipeline Overview

```
GitHub API
    |
    v
[1. Collect] --> SQLite (raw mirror)
    |
    v
[2. Summarize] --> summaries table (Haiku LLM)
    |
    v
[3. Assemble] --> assembled_xml table (structured XML)
    |
    v
[4. Embed] --> sqlite-vec (vectors) + FTS5 (keywords)
    |
    v
[5. Search] --> hybrid KNN + BM25, RRF fusion, cross-encoder rerank
    |
    v
[6. Assess] --> Sonnet LLM classification + reasoning
    |
    v
Advisory output (DUPLICATE / RELATED / OFF_TOPIC / UNRELATED)
```

Each stage is independently re-runnable. Changing the embedding model requires re-running from stage 4. Changing the summarization model requires re-running from stage 2.

## Stage 1: Collect

Mirrors all GitHub data into SQLite via the `gh` CLI.

**Data sources** (both `micropython/micropython` and `micropython/micropython-lib`):
- Issues: title, body, author, state, state_reason, labels, milestone
- Pull requests: title, body, author, state, draft, labels, diff stats
- PR diffs: full unified diff text (separate table for large data)
- Discussion comments: issue and PR comments with author, timestamps
- Review comments: inline code review comments with file path and diff hunk
- Cross-references: extracted from text (Fixes #N, Duplicate of #N, etc.)
- Ground truth: known duplicate pairs from GitHub state_reason and comments

**Sync strategy**: List endpoints with `--paginate` for full initial sync, `since` parameter for incremental updates. Rate limiting at 0.72s between requests (5000/hour). Automatic pause and retry on 403/429 with `X-RateLimit-Reset` header parsing.

**Tables**: `issues`, `pull_requests`, `pr_diffs`, `comments`, `review_comments`, `cross_references`, `ground_truth`, `sync_state`

## Stage 2: Summarize

An LLM processes each issue/PR to extract structured metadata for embedding. This normalizes vocabulary variance across differently-worded reports about the same problem and compresses large diffs into semantic descriptions.

**Input context per item**:
- Title, body, labels
- All discussion comments
- Review comments (PRs only)
- Diff text, truncated to 10K chars (PRs only)
- Linked item content from cross-references

**Output schema**:
```json
{
  "components": ["stm32/spi", "stm32/dma"],
  "item_category": "bug_report",
  "synopsis": "SPI transfers using DMA fail with HardFault on STM32F4 when transfer size exceeds 64 bytes.",
  "affected_code": ["ports/stm32/machine_spi.c", "ports/stm32/dma.c"],
  "error_signatures": "HardFault at 0x0800xxxx in spi_transfer_dma()",
  "concepts": ["DMA", "SPI", "HardFault", "F4", "transfer size"]
}
```

**Backend options**: See [Summarization Backends](#summarization-backends) below.

**Table**: `summaries` (keyed on repo + item_number + item_type, stores model_id)

## Stage 3: Assemble

A static script builds structured XML per item by combining verbatim content with optional LLM summary fields. Works with or without stage 2 output.

**XML format** (issue example):
```xml
<issue number="12345" repo="micropython/micropython">
<title><![CDATA[stm32: SPI DMA transfers fail on F4 series]]></title>
<description><![CDATA[verbatim issue body...]]></description>
<labels>bug, stm32</labels>
<summary>
  <components>stm32/spi, stm32/dma</components>
  <type>bug report</type>
  <synopsis>SPI transfers using DMA fail with HardFault...</synopsis>
  <affected_code>ports/stm32/machine_spi.c, ports/stm32/dma.c</affected_code>
  <error_signatures>HardFault at 0x0800xxxx</error_signatures>
  <concepts>DMA, SPI, HardFault, F4</concepts>
</summary>
</issue>
```

For PRs, a `<diff_files>` section is added with file paths, addition/deletion counts, and function names extracted from `@@` hunk headers.

**Static fields** (always populated without LLM): title, description, labels, diff file paths, function names from hunk headers.

**Hash-based skip**: SHA-256 of assembled XML prevents redundant re-processing when nothing changed.

**Table**: `assembled_xml`

## Stage 4: Embed

Encodes assembled XML into vector embeddings for semantic search and builds a keyword index for BM25 retrieval.

**Embedding model**: Qwen3-Embedding-0.6B (default). Model-agnostic architecture — configurable model ID, dimensions, and query/document prefix. Rebuild-and-replace strategy when changing models (no mixed indexes).

**Vector index**: sqlite-vec `vec0` virtual table with cosine distance metric. Metadata columns (item_number, item_type, repo) are filterable in KNN WHERE clauses.

**Keyword index**: FTS5 table over assembled XML text for BM25 ranking.

**Resume-capable**: Tracks already-indexed items, skips on re-run. Periodic `gc.collect()` for memory management during batch processing.

**Tables**: `vec_items` (sqlite-vec), `item_fts` (FTS5), `embedding_meta`

## Stage 5: Search

Hybrid retrieval combining dense vector search and sparse keyword search, merged via Reciprocal Rank Fusion.

```
Query text
    |
    +--> [Encode with embedding model] --> KNN top-100 (dense)
    |                                          |
    +--> [FTS5 BM25 match] ----------------> top-100 (sparse)
                                               |
                                         [RRF fusion (k=60)]
                                               |
                                         [Exclude self-match]
                                               |
                                         [Truncate to top-20]
                                               |
                                         [Fetch content for candidates]
                                               |
                                         [Cross-encoder rerank (bge-reranker-large)]
                                               |
                                         top-20 ranked candidates
```

**RRF formula**: `score = sum(1/(k + rank))` across dense and sparse result lists. Deduplication by (item_number, item_type, repo) tuple.

**Self-exclusion**: The query item is filtered out of results before reranking to prevent self-match.

**Cross-encoder**: BAAI/bge-reranker-large scores (query, candidate) pairs for final ranking. Lazy-loaded, cacheable across calls.

## Stage 6: Assess

A Sonnet-class LLM evaluates the top candidates with MicroPython project context.

**Input per candidate**: Query item's assembled XML + candidate's assembled XML + system prompt with triage instructions and MicroPython project rules.

**Output**:
- Classification: DUPLICATE, LIKELY_DUPLICATE, RELATED, OFF_TOPIC, UNRELATED
- Confidence: high, medium, low
- Reasoning: brief explanation
- Suggested action: e.g. "close as duplicate of #X", "link as related"

**Invocation**: `claude --model sonnet -p` subprocess with `--output-format json --json-schema` for structured output.

## Summarization Backends

The summarize stage supports two backends, selectable via `--backend` CLI flag.

### Claude Haiku (default, recommended)

```bash
mpy-triage summarize
mpy-triage summarize --backend claude
```

Uses `claude --model haiku -p` subprocess with JSON schema enforcement. Supports concurrent subprocess calls (`--concurrency` flag, default 8) for throughput.

**Characteristics**:
- High-quality structured extraction
- Preserves specific error messages, file paths, and technical details
- ~$0.001 per item, ~$15 for full corpus (~15K items)
- Requires Claude CLI with OAuth authentication

### Local LLM via llama.cpp (alternative)

```bash
mpy-triage summarize --backend local --local-url http://host:8080
```

Uses an OpenAI-compatible HTTP server (e.g. llama.cpp `llama-server`) with JSON schema enforcement via GBNF grammars. Sequential processing (single GPU processes one item at a time).

**Setup**: See `scripts/setup-llama-server.sh` for automated GPU host provisioning (CUDA toolkit, model download, llama.cpp build).

**Server flags**: `--reasoning-budget 0` is required for Qwen3.5 models to disable the chain-of-thought mode that otherwise generates 2000+ internal reasoning tokens per request.

**Characteristics**:
- Zero API cost after initial setup
- Requires a CUDA GPU (tested on GTX 1650 Super, 4GB VRAM)
- ~30-40 tokens/sec on GTX 1650 Super
- Lower quality than Haiku on specificity and completeness (see evaluation below)

### Backend Evaluation

Automated pairwise comparison using Opus as a blind A/B judge. 49 items sampled with stratification across item categories. Each summary pair scored on four dimensions (1-5 scale), with randomized position assignment to prevent judge bias.

**Results**:

| Outcome | Count | Percentage |
|---------|-------|-----------|
| Haiku wins | 37 | 76% |
| Local wins | 3 | 6% |
| Ties | 9 | 18% |

| Dimension | Haiku | Qwen3.5-4B | Delta |
|-----------|-------|-----------|-------|
| Accuracy | 4.65 | 3.86 | +0.79 |
| Completeness | 4.08 | 3.41 | +0.67 |
| Specificity | 4.16 | 3.12 | **+1.04** |
| Category | 4.59 | 4.61 | -0.02 |

**Key finding**: Qwen3.5-4B matches Haiku on coarse category classification (tied at ~4.6/5) but falls behind on specificity (-1.04) — it loses technical details like error messages, register names, and function names that are critical for embedding-based similarity detection. The accuracy and completeness gaps are also meaningful.

**Recommendation**: Use Haiku for production summarization. The local backend is viable for experimentation, cost-sensitive bulk processing, or as a fallback when Claude API access is unavailable.

## Database Schema

Single SQLite database at `data/triage.db` with git-lfs tracking.

**Raw mirror tables**: `issues`, `pull_requests`, `pr_diffs`, `comments`, `review_comments`
**Derived tables**: `cross_references`, `ground_truth`, `summaries`, `assembled_xml`
**Index tables**: `vec_items` (sqlite-vec), `item_fts` (FTS5), `embedding_meta`
**Eval tables**: `eval_summaries` (for backend comparison, separate from production)
**State**: `sync_state` (checkpoint tracking for incremental updates)

WAL mode enabled for concurrent read access during long-running operations.

## Data Flow Dependencies

```
collect --> crossref --> summarize --> assemble --> embed --> search --> assess
              |                          ^
              |                          |
              +----- ground_truth        |
                     (eval only)         |
                                         |
                              (works without summarize,
                               static fields only)
```

The `assemble` stage works with or without `summarize` output. When summaries are missing, only static fields (title, description, labels, diff file paths) are included in the XML. The `--skip-summarize` flag on triage commands uses this path.
