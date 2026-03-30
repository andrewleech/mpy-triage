# Usage

## Installation

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone <repo-url>
cd mpy-github-triage
uv sync
```

External tool requirements:
- `gh` CLI — authenticated with access to `micropython/micropython` and `micropython/micropython-lib`
- `claude` CLI — with OAuth authentication (needed for `summarize` and `assess` stages)

Verify installation:

```bash
uv run mpy-triage --help
```

## Initial Data Collection

The `collect` command mirrors GitHub issues, PRs, comments, diffs, and metadata into a local SQLite database.

```bash
uv run mpy-triage collect
```

This collects from both `micropython/micropython` and `micropython/micropython-lib` by default. To collect from a single repo:

```bash
uv run mpy-triage collect --repo micropython/micropython
```

After fetching raw data, `collect` also extracts cross-references (e.g., "Fixes #N", "Duplicate of #N") from issue/PR bodies and comments, and builds a ground truth table of known duplicate pairs.

### Expected Runtime

Initial full collection takes several hours. The two repos contain roughly 7k PRs, 5k issues, and 50k+ comments combined. Each PR diff requires a separate API call.

### Rate Limiting

The collector pauses between requests at 0.72s intervals (5000 requests/hour GitHub limit). On HTTP 403/429 responses, it reads the `X-RateLimit-Reset` header, pauses until that time, and resumes automatically.

## Pipeline Stages

After collection, run each stage in order. Each stage is independently re-runnable and resumes where it left off.

### Summarize

Runs an LLM over each issue/PR to extract structured metadata: components, category, synopsis, affected code paths, error signatures, and concepts.

```bash
uv run mpy-triage summarize
```

Options:
- `--backend claude` (default) — uses `claude --model haiku` subprocess
- `--backend local --local-url http://host:8080` — uses a local llama.cpp server
- `-j N` / `--concurrency N` — concurrent subprocess calls, default 8 (claude backend only)
- `--repo` — limit to a specific repository

This stage is optional. The `assemble` stage works without summaries, using only static fields (title, body, labels, diff file paths).

### Assemble

Builds structured XML per item by combining raw GitHub data with summary fields (if available).

```bash
uv run mpy-triage assemble
```

Options:
- `--repo` — limit to a specific repository

Uses SHA-256 hashing to skip items whose content has not changed since the last run.

### Embed

Encodes assembled XML into vector embeddings (sqlite-vec) and a keyword index (FTS5).

```bash
uv run mpy-triage embed
```

Options:
- `--force` — rebuild the index from scratch (required when changing embedding models)
- `--batch-size N` — embedding batch size, default 4

The default embedding model is Qwen3-Embedding-0.6B (1024 dimensions). Runs on CUDA if available, CPU otherwise.

## Triaging an Issue or PR

```bash
uv run mpy-triage issue 12345
uv run mpy-triage pr 6789
```

This runs the full pipeline for a single item: summarize (if no summary exists), assemble, search for candidates, and assess with Sonnet.

### Options

| Flag | Effect |
|------|--------|
| `--repo REPO` | Repository (default: `micropython/micropython`) |
| `--skip-summarize` | Skip Haiku summarization, use static fields only |
| `--skip-assess` | Skip Sonnet assessment, return ranked candidates with scores only |
| `--json` | Machine-readable JSON output |
| `--backend claude\|local` | Summarization backend |
| `--local-url URL` | URL of local llama.cpp server |

Flags can be combined. `--skip-summarize --skip-assess` gives embedding-only retrieval with no LLM calls.

### Output Format

Human-readable output (default) shows each candidate with classification, confidence, reasoning, and suggested action:

```
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
```

Use `--json` for structured output.

### Classification Categories

- `DUPLICATE` — same root cause or identical request
- `LIKELY_DUPLICATE` — high probability but not certain
- `RELATED` — connected topic, shared component or concept
- `OFF_TOPIC` — unrelated to MicroPython (spam, wrong repo, support question)
- `UNRELATED` — legitimate MicroPython content but not similar to the query

## Checking Database Status

```bash
uv run mpy-triage stats
```

Shows counts for issues, PRs, comments, summaries, assembled items, and embedded items, plus the current embedding model ID.

## Incremental Updates

Re-running `collect` fetches only items updated since the last sync. The `sync_state` table tracks the last-updated timestamp per repo per content type. GitHub's `since` parameter filters by `updated_at`.

```bash
uv run mpy-triage collect
uv run mpy-triage summarize
uv run mpy-triage assemble
uv run mpy-triage embed
```

The `summarize`, `assemble`, and `embed` stages each skip items that have already been processed. Only new or changed items are handled.

## Custom Database Path

All commands accept a `--db` flag to override the default database location (`data/triage.db`):

```bash
uv run mpy-triage --db /path/to/custom.db collect
```

## Logging

Logs are written to stderr and to a rotating log file at `$TMPDIR/mpy-triage/mpy-triage.log` (5 MB, 3 rotations). Use `-v` / `--verbose` for debug-level output.
