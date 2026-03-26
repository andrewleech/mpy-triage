# mpy-triage

MicroPython issue/PR triage tool for detecting duplicates, related items, and spam.

## Build & Test

```bash
uv sync                          # Install dependencies
uv run mpy-triage --help         # Show CLI commands
uv run pytest                    # Run tests
uv run ruff check src/ tests/    # Lint
```

## Project Structure

- `src/mpy_triage/` - Python package (src-layout)
- `schema.sql` - SQLite database schema
- `prompts/` - Prompt templates for Haiku/Sonnet
- `micropython/` - Git submodule for agent research
- `data/` - SQLite database (git-lfs tracked)

## Pipeline Stages

1. `collect` - Mirror GitHub data into SQLite
2. `summarize` - Haiku processes items (optional, skippable)
3. `assemble` - Build structured XML per item
4. `embed` - Encode XML into sqlite-vec + FTS5
5. `search` - Hybrid KNN + BM25, RRF fusion, rerank (internal, called by issue/pr)
6. `assess` - Sonnet evaluates candidates (optional, skippable)

## Claude Subprocess Invocation

All Claude calls use `claude --model <model> -p` subprocess with `--output-format json --json-schema` for structured output. Never use direct API calls.
