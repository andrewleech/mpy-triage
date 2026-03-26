# mpy-triage Specification

## Overview

A standalone CLI tool for detecting duplicate, related, and off-topic issues/PRs across MicroPython repositories. Mirrors all GitHub issue, PR, comment, and diff data into a local SQLite database, builds a semantic search index using LLM-generated summaries and embeddings, and uses a multi-stage retrieval pipeline with Claude-based assessment to identify similar items.

## Goals and Objectives

- Detect duplicate and related issues/PRs across `micropython/micropython` and `micropython/micropython-lib`
- Surface relationships that humans miss due to vocabulary variation, temporal distance, or cross-repo boundaries
- Provide actionable output: classification (duplicate/related/off-topic/unrelated), reasoning, and suggested actions
- Act as a spam/off-topic detector for issues that are unrelated to MicroPython
- Maintain a complete local mirror of GitHub data for re-processing when models improve

## Target Users

MicroPython maintainers performing issue triage, initially via CLI. Internal API designed for future expansion to webhook-driven or scheduled operation.

## Architecture

### Pipeline Stages

```
1. COLLECT    Mirror raw GitHub data into SQLite
                |
2. SUMMARIZE  Haiku processes each item with linked context (optional, skippable)
                |
3. ASSEMBLE   Static script builds structured XML per item,
              merging verbatim content with Haiku output if available
                |
4. EMBED      Qwen3-Embedding-0.6B (default) encodes assembled XML
              into sqlite-vec + FTS5
                |
5. SEARCH     Hybrid KNN + BM25, RRF fusion, cross-encoder rerank
                |
6. ASSESS     Sonnet evaluates top-N candidates with MicroPython context (optional, skippable)
```

Each stage is independently re-runnable. Changing the embedding model requires re-running stages 4+. Changing the summarization model requires re-running stages 2+. The assembler (stage 3) always runs and works with or without Haiku summaries.

### CLI Flags

- Default: full pipeline (summarize + embed + assess)
- `--skip-summarize`: skip Haiku, assemble from static fields only
- `--skip-assess`: skip Sonnet, return ranked candidates with scores only
- Flags can be combined: `--skip-summarize --skip-assess` for embedding-only retrieval

### Model Agnosticism

The system is architecturally agnostic to embedding model choice. Configuration specifies:
- Model identifier
- Embedding dimensions
- Optional query instruction prefix
- Optional document instruction prefix

Qwen3-Embedding-0.6B is the default. When the model changes, the entire index is rebuilt from the assembled XML (rebuild-and-replace, no mixed indexes).

## Data Model

### SQLite Database: Raw Mirror

All raw GitHub data is mirrored for re-processing when models change.

#### `issues`
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | GitHub ID |
| number | INTEGER | Issue number |
| repo | TEXT | `micropython/micropython` or `micropython/micropython-lib` |
| title | TEXT | |
| body | TEXT | |
| author | TEXT | |
| state | TEXT | open, closed |
| state_reason | TEXT | completed, not_planned, duplicate |
| labels | TEXT | JSON array of label names |
| milestone | TEXT | |
| created_at | TEXT | ISO 8601 |
| updated_at | TEXT | ISO 8601 |
| closed_at | TEXT | ISO 8601, nullable |
| UNIQUE(repo, number) | | |

#### `pull_requests`
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | GitHub ID |
| number | INTEGER | PR number |
| repo | TEXT | |
| title | TEXT | |
| body | TEXT | |
| author | TEXT | |
| state | TEXT | open, closed, merged |
| draft | INTEGER | boolean |
| labels | TEXT | JSON array |
| created_at | TEXT | ISO 8601 |
| updated_at | TEXT | ISO 8601 |
| closed_at | TEXT | ISO 8601, nullable |
| merged_at | TEXT | ISO 8601, nullable |
| base_branch | TEXT | |
| changed_files | INTEGER | |
| additions | INTEGER | |
| deletions | INTEGER | |
| UNIQUE(repo, number) | | |

#### `pr_diffs` (separate table for large data)
| Column | Type | Notes |
|--------|------|-------|
| pr_number | INTEGER | |
| repo | TEXT | |
| diff_text | TEXT | Full unified diff |
| UNIQUE(repo, pr_number) | | |

#### `comments` (issue and PR discussion comments)
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | GitHub ID |
| item_number | INTEGER | Issue or PR number |
| item_type | TEXT | `issue` or `pull_request` |
| repo | TEXT | |
| author | TEXT | |
| body | TEXT | |
| created_at | TEXT | ISO 8601 |
| updated_at | TEXT | ISO 8601 |

#### `review_comments` (inline code review comments on PRs)
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | GitHub ID |
| pr_number | INTEGER | |
| repo | TEXT | |
| author | TEXT | |
| body | TEXT | |
| path | TEXT | File path |
| diff_hunk | TEXT | |
| created_at | TEXT | ISO 8601 |

#### `cross_references` (extracted from text and GitHub events)
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | Auto-increment |
| source_number | INTEGER | |
| source_type | TEXT | `issue` or `pull_request` |
| source_repo | TEXT | |
| target_number | INTEGER | |
| target_type | TEXT | |
| target_repo | TEXT | |
| relationship | TEXT | `fixes`, `closes`, `related`, `duplicate_of`, `references` |
| extracted_from | TEXT | `body`, `comment`, `event` |

#### `ground_truth` (known duplicate/related pairs from GitHub)
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | Auto-increment |
| item_a_number | INTEGER | |
| item_a_repo | TEXT | |
| item_b_number | INTEGER | |
| item_b_repo | TEXT | |
| relationship | TEXT | `duplicate`, `related` |
| source | TEXT | How this was determined: `state_reason`, `comment`, `event` |
| discovered_at | TEXT | ISO 8601 |

Ground truth is collected for reference and sanity-checking but is not stripped from Haiku context. Evaluation of new detections uses a separate multi-agent review process.

#### `sync_state`
| Column | Type | Notes |
|--------|------|-------|
| key | TEXT PK | e.g. `micropython/micropython:issues:last_updated` |
| value | TEXT | ISO 8601 timestamp or cursor |

### SQLite Database: Processed Data

#### `summaries` (Haiku output)
| Column | Type | Notes |
|--------|------|-------|
| item_number | INTEGER | |
| item_type | TEXT | `issue` or `pull_request` |
| repo | TEXT | |
| model_id | TEXT | e.g. `claude-haiku-4-5-20251001` |
| components | TEXT | JSON array, e.g. `["stm32/spi", "stm32/dma"]` |
| item_category | TEXT | `bug_report`, `feature_request`, `refactor`, `question`, `ci_build`, `documentation` |
| synopsis | TEXT | 1-2 sentence distillation |
| affected_code | TEXT | JSON array of file paths, function names |
| error_signatures | TEXT | Specific error messages, tracebacks |
| concepts | TEXT | JSON array of technical terms |
| created_at | TEXT | ISO 8601 |
| UNIQUE(repo, item_number, item_type) | | |

#### `assembled_xml`
| Column | Type | Notes |
|--------|------|-------|
| item_number | INTEGER | |
| item_type | TEXT | |
| repo | TEXT | |
| xml_text | TEXT | Full assembled XML for embedding |
| xml_hash | TEXT | SHA-256 of xml_text, used to detect changes |
| has_summary | INTEGER | Whether Haiku summary was included |
| created_at | TEXT | ISO 8601 |
| UNIQUE(repo, item_number, item_type) | | |

#### `vec_items` (sqlite-vec virtual table)
| Column | Type | Notes |
|--------|------|-------|
| item_number | INTEGER | Metadata (filterable in KNN WHERE) |
| item_type | TEXT | |
| repo | TEXT | |
| embedding | FLOAT[N] | Vector, dimension depends on model config |

Metadata columns in vec0 are filterable at query time without post-hoc filtering.

#### `item_fts` (FTS5 table)
Full-text search index over assembled XML text for BM25 keyword retrieval.

#### `embedding_meta`
| Column | Type | Notes |
|--------|------|-------|
| key | TEXT PK | |
| value | TEXT | Model ID, dimensions, record count, build timestamp |

## Assembly Format

The static assembler builds XML per item. Title and description are included verbatim in CDATA sections. Haiku summary fields are merged in when available.

### Issue Example

```xml
<issue number="12345" repo="micropython/micropython">
<title><![CDATA[stm32: SPI DMA transfers fail on F4 series]]></title>
<description><![CDATA[
When using SPI with DMA enabled on STM32F405, transfers larger than
64 bytes trigger a HardFault...
]]></description>
<labels>bug, stm32</labels>
<summary>
<components>stm32/spi, stm32/dma</components>
<type>bug report</type>
<synopsis>SPI transfers using DMA fail with HardFault on STM32F4 when transfer size exceeds 64 bytes.</synopsis>
<affected_code>ports/stm32/machine_spi.c, ports/stm32/dma.c</affected_code>
<error_signatures>HardFault at 0x0800xxxx in spi_transfer_dma()</error_signatures>
<concepts>DMA, SPI, HardFault, F4, transfer size</concepts>
</summary>
</issue>
```

### PR Example

```xml
<pull_request number="12346" repo="micropython/micropython">
<title><![CDATA[stm32: Fix SPI DMA transfer size limit on F4.]]></title>
<description><![CDATA[
Fixes the HardFault when DMA transfers exceed 64 bytes on STM32F4
by splitting into multiple DMA transactions...
]]></description>
<labels>stm32</labels>
<diff_files>
<file path="ports/stm32/machine_spi.c" additions="15" deletions="8">
<functions>spi_transfer_dma, machine_spi_transfer</functions>
</file>
<file path="ports/stm32/dma.c" additions="3" deletions="1">
<functions>dma_configure_transfer</functions>
</file>
</diff_files>
<summary>
<components>stm32/spi, stm32/dma</components>
<type>bug fix</type>
<synopsis>Splits DMA transfers exceeding 64 bytes into multiple transactions to avoid HardFault on F4 series.</synopsis>
<affected_code>ports/stm32/machine_spi.c:spi_transfer_dma, ports/stm32/dma.c:dma_configure_transfer</affected_code>
<concepts>DMA, SPI, HardFault, F4, transfer size, transaction splitting</concepts>
</summary>
</pull_request>
```

### Static Fields (always populated)

Extracted without LLM:
- Title, description (verbatim from GitHub)
- Labels
- `diff_files`: file paths, addition/deletion counts, enclosing function names from `@@` hunk headers
- Linked item numbers (from cross-reference parsing)

### Haiku Fields (populated when summarization is enabled)

- `components`: list of MicroPython components (may be multiple)
- `type`: bug report, feature request, refactor, question, CI/build, documentation
- `synopsis`: 1-2 sentence distillation
- `affected_code`: file paths and function names mentioned or inferred
- `error_signatures`: specific error messages and tracebacks
- `concepts`: technical terms and domain concepts

## Haiku Summarization

### Invocation

All Haiku calls use `claude --model haiku -p <prompt>` subprocess, leveraging existing OAuth setup. Never direct API calls.

### Input Context

Haiku receives:
- The item's title, body, and labels
- All comments on the item (issue comments, review comments)
- PR diff (if applicable)
- Content of linked items (resolved from cross-references: `Fixes #X`, `Related to #Y`, `Duplicate of #Z`)

### Linked Content Resolution

Cross-references are parsed during collection. The summarization step resolves these to include linked item content as context, enabling Haiku to produce richer summaries that understand the relationship between linked items.

Collection must complete before summarization starts.

### Output

Structured JSON matching the `summaries` table schema, which the assembler merges into XML.

### Batch Processing

For initial backlog (~13k items across both repos):
- Sequential processing via subprocess
- Rate limiting handled by claude CLI
- Checkpoint tracking for resume on interruption

## Search Pipeline

### Stage 1: Dense Retrieval
- Embed query using configured model (Qwen3-Embedding-0.6B default)
- KNN search in sqlite-vec, top-K initial candidates (K=100)
- Optional metadata filters (repo, item_type)

### Stage 2: Keyword Retrieval
- FTS5 BM25 search on assembled XML text
- Top-K initial candidates (K=100)

### Stage 3: Fusion
- Reciprocal Rank Fusion (RRF) combining dense + sparse results
- Deduplication by (item_number, item_type, repo) tuple

### Stage 4: Cross-Encoder Rerank
- Rerank fused candidates with cross-encoder model
- Configurable reranker (default: BAAI/bge-reranker-large)
- Output: top-N candidates with reranker scores

### Stage 5: Sonnet Assessment (unless `--skip-assess`)
- Top-N candidates (default 5) sent to Sonnet via `claude --model sonnet -p`
- System prompt includes:
  - Static triage instructions (maintained in a prompt file)
  - MicroPython project context from mpy-rules
- Sonnet receives both the query item and each candidate's assembled XML

### Cross-Repo Search
Searches span both `micropython/micropython` and `micropython/micropython-lib`. Items from either repo can match.

## Sonnet Assessment Output

### Classification Categories
- `DUPLICATE` — same root cause or identical request
- `LIKELY_DUPLICATE` — high probability but not certain
- `RELATED` — connected topic, shared component or concept, not the same issue
- `OFF_TOPIC` — item appears unrelated to MicroPython (spam, wrong repo, AI noise, support question)
- `UNRELATED` — legitimate MicroPython content but not similar to the query

### Output Fields Per Candidate
- Classification (as above)
- Confidence: high, medium, low
- Reasoning: brief explanation, may include specific aspect of duplication/relation
- Suggested action: "close as duplicate of #X", "link as related to #Y", "no action", "flag as spam/off-topic"

### Off-Topic / Spam Detection
Falls out naturally from the pipeline. If an issue's top similarity scores against the entire corpus are uniformly low, and Sonnet judges the content unrelated to MicroPython, it is classified as `OFF_TOPIC`.

## CLI Interface

### Primary Command

```
mpy-triage issue <NUMBER> [--repo REPO] [OPTIONS]
mpy-triage pr <NUMBER> [--repo REPO] [OPTIONS]
```

### Pipeline Control Flags
- `--skip-summarize` — skip Haiku, assemble from static fields only
- `--skip-assess` — skip Sonnet, return ranked candidates with scores only
- `--json` — machine-readable JSON output

### Data Management Commands

```
mpy-triage collect [--repo REPO]       # Mirror GitHub data into SQLite
mpy-triage summarize [--repo REPO]     # Run Haiku on all/new items
mpy-triage assemble [--repo REPO]      # Build XML from raw + summary data
mpy-triage embed [--repo REPO]         # Build/rebuild embedding index
mpy-triage stats                       # Show database and index statistics
```

### Output Format

```
$ mpy-triage issue 12345

Searching for similar items to: micropython/micropython#12345
  "stm32: SPI DMA transfers fail on F4 series"
  https://github.com/micropython/micropython/issues/12345

Found 3 candidates:

#8901 [DUPLICATE - high confidence]
  "stm32: HardFault in SPI when using DMA on STM32F405"
  https://github.com/micropython/micropython/issues/8901
  Status: open | Created: 2024-03-15
  Reasoning: Same root cause - DMA transfer size exceeding 64 bytes
  triggers HardFault in spi_transfer_dma(). Identical port and subsystem.
  Suggested action: Close #12345 as duplicate of #8901

#7234 [RELATED - medium confidence]
  "stm32: DMA issues with I2C on F4"
  https://github.com/micropython/micropython/issues/7234
  Status: closed (fixed) | Created: 2023-11-02
  Reasoning: Same DMA peripheral on same chip family, different bus.
  Fix in #7234 may inform solution.
  Suggested action: Link as related

#11002 [RELATED - low confidence]
  "esp32: SPI transfers drop bytes above 32 bytes"
  https://github.com/micropython/micropython/issues/11002
  Status: open | Created: 2024-08-20
  Reasoning: Similar symptom (SPI failure at transfer size threshold)
  but different port and likely different root cause.
  Suggested action: No action
```

## Collection

### GitHub API Strategy

Authenticated via `gh` CLI. Targets:
- `micropython/micropython`: ~7k PRs, ~5k issues, ~50k+ comments
- `micropython/micropython-lib`: ~500 PRs, ~300 issues, ~5k comments

### Rate Limiting

- Detect HTTP 403/429 rate limit responses
- Log the rate limit event
- Pause until the `X-RateLimit-Reset` timestamp
- Resume automatically
- Initial full collection may take several hours

### Incremental Updates

- `sync_state` table tracks last-updated timestamp per repo per content type
- GitHub `since` parameter filters by `updated_at`
- Only fetches changed items on subsequent runs

### Diff Collection

- Collected for all PRs (not opt-in)
- Stored in separate `pr_diffs` table
- Each PR diff requires a separate API call
- Included in initial collection pass

### Cross-Reference Extraction

During collection, parse issue/PR bodies and comments for:
- `Fixes #N`, `Closes #N` patterns
- `Duplicate of #N` patterns
- `Related to #N`, `See also #N` patterns
- GitHub timeline events (duplicate marking, cross-references)

Store in `cross_references` table. Populate `ground_truth` table from duplicate close reasons and explicit "Duplicate of" comments.

## Evaluation

### Sanity Check

Compare system detections against `ground_truth` table — did the system find the duplicates that humans already identified?

### New Detection Validation

A separate multi-agent review process for validating newly detected duplicates/related items:
- Multiple differently-targeted Opus agents independently review the candidate pair
- Each agent examines both items and assesses whether the detection is valid
- Cross-review pattern modeled on the `/review-branch` skill
- Produces a consensus assessment with supporting reasoning

This process is run independently of the main triage pipeline.

## Configuration

### Embedding Model Config
```
model_id: str           # e.g. "Qwen/Qwen3-Embedding-0.6B"
embedding_dim: int      # e.g. 1024
query_prefix: str       # e.g. "Instruct: Find duplicate GitHub issues\nQuery: "
document_prefix: str    # e.g. "" (empty for Qwen3)
max_seq_length: int     # e.g. 32768
```

### Retrieval Config
```
top_k_initial: int      # Dense + FTS5 retrieval count (default: 100)
top_k_rerank: int       # After RRF fusion (default: 20)
top_k_assess: int       # Candidates sent to Sonnet (default: 5)
reranker_model: str     # e.g. "BAAI/bge-reranker-large"
```

### Database Config
```
db_path: Path           # Path to SQLite database file
```

## Dependencies

- Python >= 3.10
- sqlite-vec
- sentence-transformers (or transformers >= 4.51.0 for Qwen3)
- numpy
- click (CLI framework)
- tqdm (progress bars)
- torch (CPU or CUDA)
- `gh` CLI (GitHub data collection)
- `claude` CLI with OAuth (Haiku summarization, Sonnet assessment)

## Future Considerations

- Webhook-driven operation via GitHub App (internal API designed to support this)
- Scheduled batch processing for new issues
- Auto-posting advisory comments on GitHub issues
- Integration with `mpy-reviewer` bot infrastructure
- Fine-tuning embedding model on MicroPython issue pairs
- Recency weighting in similarity scores
