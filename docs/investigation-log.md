# Investigation Log: MicroPython Issue Triage Pipeline

## Overview

Automated pipeline to detect duplicate/related issues and PRs across the
MicroPython GitHub repositories. Uses embedding-based retrieval, cross-encoder
reranking, and LLM-based assessment to surface actionable findings.

## Pipeline Architecture

```
collect → summarize → assemble → embed → scan → assess → export
```

1. **collect** — Mirror GitHub issues/PRs into SQLite via GitHub API
2. **summarize** — Optional Haiku/local LLM summarization
3. **assemble** — Build structured XML per item (budget-capped at 4K chars)
4. **embed** — Encode XML into sqlite-vec (Qwen3-Embedding-0.6B) + FTS5
5. **scan** — For each open issue: hybrid KNN + BM25 retrieval, RRF fusion, cross-encoder reranking
6. **assess** — LLM classifies each (query, candidate) pair
7. **export** — CSV, Markdown, HTML with sortable tables

## Scan Configuration

### Retrieval
- Dense: sqlite-vec KNN with Qwen3-Embedding-0.6B (1024-dim)
- Sparse: FTS5 BM25 keyword matching
- Fusion: Reciprocal Rank Fusion (k=60)
- Reranker: cross-encoder/ms-marco-MiniLM-L-6-v2 (22.7M params, fp16)
- Previous reranker: BAAI/bge-reranker-large (560M params) — switched for speed

### Per-type budgets
Top-k candidates are selected separately per candidate type (issues and PRs).
This prevents merged PRs (which get a 2x value score multiplier) from crowding
out issue-to-issue duplicate matches.

- `top_k=3` per type → up to 6 candidates per query issue
- `min_score=0.06` value score threshold

### Value scoring
```
value_score = rerank_score × state_multiplier
```
| Candidate state | Multiplier |
|----------------|------------|
| merged PR      | 2.0        |
| open item      | 1.5        |
| closed item    | 1.0        |

## Scan Execution

Scan ran on SSH host `step` (GTX 1650 SUPER, 4GB VRAM):
- 1374 open issues scanned
- 4051 results across 1173 issues (201 issues had no matches above threshold)
- 2490 issue→issue pairs, 1561 issue→PR pairs
- Embedding + reranking at ~2.2s/issue with MiniLM (was 559s/issue with bge-reranker-large)
- Required `HF_HUB_OFFLINE=1` — host has no internet access
- Models cached in `~/.cache/huggingface/hub/`

## Assessment

### Phase 1: Sonnet baseline (213 pairs)
First 213 pairs assessed via `claude --model sonnet -p` subprocess.
Established classification quality baseline.

### Phase 2: Qwen3.5-35B-A3B local assessment (4051 pairs)
Switched to local Qwen3.5-35B-A3B (MoE, 3B active) running on
Lemonade server (llama.cpp backend) on AMD Ryzen AI 9 HX PRO 370
with Radeon 890M iGPU, 64GB RAM.

Server configuration (via Lemonade recipe_options):
```json
{
  "ctx_size": 8192,
  "llamacpp_args": "--cache-type-k q8_0 --cache-type-v q8_0 --flash-attn on --batch-size 4096 --ubatch-size 4096 --threads 4"
}
```

Key findings on server tuning:
- `ctx_size` 8192 vs 32768: prompt processing 130 t/s vs 40 t/s (3x faster)
- `--cache-type-k q8_0 --cache-type-v q8_0`: lossless on Qwen3.5 hybrid attention
- `--parallel 2` with `--cont-batching`: *slower* than single slot on iGPU (memory bandwidth bound)
- Thinking mode (`enable_thinking: true`): 4x slower, JSON parsing issues with markdown wrapping
- `--reasoning-budget 0` server flag unnecessary — per-request `chat_template_kwargs` controls it

Assessment throughput: ~35-50s/pair depending on thermal conditions.
Total runtime: ~40 hours for 4051 pairs.

### Validation: Qwen vs Sonnet agreement
23 pairs assessed by both models. Results:

| | Agreement |
|---|---|
| Overall | 70% exact match |
| RELATED/UNRELATED | 100% — Qwen matches Sonnet perfectly |
| DUPLICATE/LIKELY_DUPLICATE | Qwen over-promotes — calls things DUPLICATE that Sonnet calls RELATED |
| Direction | Qwen never under-classifies |

This makes Qwen viable as a first-pass filter: trust RELATED/UNRELATED calls,
escalate DUPLICATE/LIKELY_DUPLICATE to Sonnet for confirmation.

### Final classification (Qwen first pass)

| Classification | Count | % |
|---|---|---|
| RELATED | 1621 | 40% |
| OFF_TOPIC | 1108 | 27% |
| LIKELY_DUPLICATE | 899 | 22% |
| DUPLICATE | 370 | 9% |
| UNRELATED | 53 | 1% |

### Phase 3: Sonnet validation (pending)
1269 DUPLICATE + LIKELY_DUPLICATE pairs to be re-assessed with Sonnet.
Expected to reduce false positives significantly based on validation results.

## Key Scripts

| Script | Purpose |
|---|---|
| `scripts/run_assess_local.py` | Batch assessment via OpenAI-compatible API |
| `scripts/run_assess_scan.py` | Batch assessment via Claude CLI subprocess |
| `scripts/validate_qwen_vs_sonnet.py` | Compare Qwen and Sonnet classifications |

## Infrastructure

| Host | Role | Hardware |
|---|---|---|
| `step` (SSH) | Embedding + reranking | GTX 1650 SUPER, no internet |
| `pilap2` (LAN) | Qwen3.5 inference | Ryzen AI 9 HX PRO 370, Radeon 890M, 64GB RAM |
| local | Scripts, DB, Claude CLI | No GPU |

## Reranker Model Selection

Evaluated multiple cross-encoder reranker models for the scan stage:

| Model | Params | Speed (per issue) | Notes |
|---|---|---|---|
| BAAI/bge-reranker-large | 560M | 559s | Original, too slow for full scan |
| BAAI/bge-reranker-base | 278M | ~same on GPU | Memory-bandwidth bound, not compute |
| cross-encoder/ms-marco-MiniLM-L-6-v2 | 22.7M | 2.2s | Selected — 250x faster |
| Alibaba-NLP/gte-reranker-modernbert-base | 149M | ~10-15s est. | Higher quality but slower, not tested |

On GPU, bge-reranker-base and bge-reranker-large take nearly identical time
because inference is memory-bandwidth bound. The architecture change to MiniLM
(6 layers, 384 hidden) is what delivered the speedup.
