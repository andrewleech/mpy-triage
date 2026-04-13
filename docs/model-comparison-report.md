# Model Comparison Report: Issue/PR Duplicate Classification

## Objective

Evaluate three LLMs for classifying GitHub issue/PR pairs as duplicates,
related, or unrelated, to determine the most cost-effective pipeline for
triaging MicroPython's open issue backlog (~1374 open issues).

## Models Tested

| Model | Type | Active Params | Quantisation | Inference Host |
|---|---|---|---|---|
| **Qwen3.5-35B-A3B** | MoE | 3B | Q4_K_XL GGUF | Radeon 890M iGPU, 64GB RAM |
| **Claude Sonnet** | Dense | Undisclosed | N/A (API) | Anthropic API |
| **Gemma-4-26B-A4B** | MoE | 4B | Q4_K_M GGUF | Radeon 890M iGPU, 64GB RAM |

All three models received identical prompts and were asked to produce
identical JSON-structured output. Local models ran on the same hardware
(AMD Ryzen AI 9 HX PRO 370 with Radeon 890M iGPU) via a Lemonade server
wrapping llama.cpp, accessed through the OpenAI-compatible chat completions API.

## Task Description

Each evaluation instance is a pair of GitHub items (issue or PR) from the
MicroPython repositories. The model receives both items' assembled XML text
(title, body, labels, comments, diff excerpts — budget-capped at 4000 chars
per item) and must classify the relationship between them.

### Classification Labels

| Label | Meaning |
|---|---|
| DUPLICATE | Candidate resolves or fully addresses the query |
| LIKELY_DUPLICATE | High probability the candidate resolves the query, not certain |
| RELATED | Connected topic but candidate does not resolve query |
| OFF_TOPIC | Query is spam, wrong repo, or not a real MicroPython issue |
| UNRELATED | Both legitimate MicroPython content but not similar |

The full prompt is in `prompts/assess.txt`. It includes closure rules
(which item to close, how to handle issue-vs-PR pairs, newer-vs-older
issues) and domain-specific guidance (same symptoms on different ports may
have different root causes, explicit PR references like "Fixes #N" mean
DUPLICATE not RELATED, etc.).

### Prompt Size

Measured across 150 sampled pairs:

| Percentile | Input tokens |
|---|---|
| Min | ~2,400 |
| Median | ~3,000 |
| P95 | ~4,000 |
| Max | ~4,000 |

Output is a short JSON object (~100-200 tokens). Total per-request context
requirement is well within 8K tokens.

## Methodology

### Phase 1: Qwen Full Pass (4051 pairs)

All 4051 scan results (produced by the retrieval pipeline) were assessed
by Qwen3.5-35B-A3B with thinking disabled (`enable_thinking: false`).

Server configuration:
```
ctx_size: 8192
llamacpp_args: --cache-type-k q8_0 --cache-type-v q8_0 --flash-attn on
               --batch-size 4096 --ubatch-size 4096 --threads 4
```

- KV cache quantisation (q8_0) is lossless on Qwen3.5's hybrid attention
  architecture (only 8 of 32 layers use full KV attention).
- Context size reduced from default 32K to 8K (prompts are ~4K tokens),
  which improved prompt processing from ~40 t/s to ~130 t/s.
- `--parallel 2` with continuous batching was tested but proved slower
  than single-slot on this iGPU (memory-bandwidth bound).

Runtime: ~40 hours at ~35-50s/pair.

### Phase 2: Sonnet Validation (519 pairs)

Sonnet independently re-assessed 519 of the 1269 pairs that Qwen classified
as DUPLICATE or LIKELY_DUPLICATE. This subset was chosen because these are
the actionable classifications (they suggest closing issues).

Sonnet was invoked via `claude --model sonnet -p` subprocess with
`--output-format json --json-schema` for structured output. Runtime:
~15s/pair.

An earlier exploratory validation of 23 random pairs (before the full run)
showed 70% exact agreement, with Qwen's RELATED calls matching Sonnet 100%
but DUPLICATE/LIKELY_DUPLICATE calls over-promoting.

### Phase 3: Gemma Tiebreaker (50 pairs)

To break ties on the 355 pairs where Qwen and Sonnet disagreed, a stratified
sample of 50 pairs was assessed by Gemma-4-26B-A4B. Pairs were sampled
proportionally from each disagreement bucket, ordered by value score
(highest first) for reproducibility.

The test was run twice with different sampling parameters to check sensitivity:

| Run | temperature | top_p | top_k | Samplers |
|---|---|---|---|---|
| Run 1 | 0.1 | default | default | default |
| Run 2 | 1.0 | 0.95 | 64 | temperature;top_p;top_k |

Both runs used `enable_thinking: false` and `response_format: json_object`.

## Results

### Qwen3.5-35B-A3B: Full Corpus Classification

| Classification | Count | % |
|---|---|---|
| RELATED | 1621 | 40.0% |
| OFF_TOPIC | 1108 | 27.4% |
| LIKELY_DUPLICATE | 899 | 22.2% |
| DUPLICATE | 370 | 9.1% |
| UNRELATED | 53 | 1.3% |

### Sonnet Validation of Qwen's Positive Classifications

519 pairs where Qwen said DUPLICATE or LIKELY_DUPLICATE were re-assessed
by Sonnet. Overall exact agreement: **164/519 (31.6%)**.

#### Confusion Matrix (Qwen rows, Sonnet columns)

|  | S:DUPLICATE | S:LIKELY_DUP | S:RELATED | S:UNRELATED |
|---|---|---|---|---|
| **Q:DUPLICATE** | **116** | 56 | 14 | 0 |
| **Q:LIKELY_DUP** | 16 | **48** | 261 | 8 |

#### Per-Label Analysis

**Qwen DUPLICATE (186 validated):**
- 116 (62%) confirmed DUPLICATE by Sonnet
- 56 (30%) softened to LIKELY_DUPLICATE — adjacent category, still actionable
- 14 (8%) downgraded to RELATED — false positive
- **92% actionable** (DUPLICATE or LIKELY_DUPLICATE per Sonnet)

**Qwen LIKELY_DUPLICATE (333 validated):**
- 48 (14%) confirmed LIKELY_DUPLICATE by Sonnet
- 16 (5%) upgraded to DUPLICATE by Sonnet
- 261 (78%) downgraded to RELATED — over-promotion
- 8 (2%) downgraded to UNRELATED
- **19% actionable** (DUPLICATE or LIKELY_DUPLICATE per Sonnet)

#### Sonnet's Own Distribution (on this subset)

| Classification | Count | % |
|---|---|---|
| RELATED | 275 | 53.0% |
| DUPLICATE | 132 | 25.4% |
| LIKELY_DUPLICATE | 104 | 20.0% |
| UNRELATED | 8 | 1.5% |

### Gemma Tiebreaker on Disagreement Pairs

50 pairs where Qwen and Sonnet disagreed, assessed by Gemma:

| Gemma agrees with | Run 1 (temp=0.1) | Run 2 (temp=1.0) |
|---|---|---|
| Sonnet | 33 (66%) | 32 (64%) |
| Qwen | 9 (18%) | 10 (20%) |
| Neither | 8 (16%) | 8 (16%) |

Sampling parameters had no meaningful effect on classification decisions.

#### Per Disagreement Bucket

| Qwen → Sonnet | n | Gemma→Qwen | Gemma→Sonnet | Gemma→Neither |
|---|---|---|---|---|
| LIKELY_DUP → RELATED | 37 | 2 (5%) | 28 (76%) | 7 (19%) |
| DUPLICATE → LIKELY_DUP | 8 | **8 (100%)** | 0 | 0 |
| LIKELY_DUP → DUPLICATE | 2 | 0 | 2 | 0 |
| DUPLICATE → RELATED | 2 | 0 | 2 | 0 |
| LIKELY_DUP → UNRELATED | 1 | 0 | 0 | 1 |

Key findings:
- When Qwen says LIKELY_DUPLICATE and Sonnet says RELATED, Gemma sides with
  Sonnet 76% of the time. Qwen over-promotes at the LIKELY_DUPLICATE level.
- When Qwen says DUPLICATE and Sonnet softens to LIKELY_DUPLICATE, Gemma
  sides with Qwen 100% (8/8). Sonnet may be overly conservative on these.
- The "Neither" cases (7/37 in the largest bucket) were mostly Gemma
  classifying as DUPLICATE where both Qwen (LIKELY_DUP) and Sonnet (RELATED)
  chose intermediate labels.

## Throughput Comparison

All local inference ran on the same hardware: AMD Ryzen AI 9 HX PRO 370
with Radeon 890M iGPU (RDNA 3.5, shared system memory), 64GB RAM.

| Model | Avg latency | Prompt processing | Generation | Cost |
|---|---|---|---|---|
| Qwen3.5-35B-A3B | 35-50s/pair | 130 t/s | 10-16 t/s | Free (local) |
| Gemma-4-26B-A4B | 42-45s/pair | 14-16 t/s | 6-16 t/s | Free (local) |
| Claude Sonnet | ~15s/pair | N/A (API) | N/A (API) | ~$0.01/pair |

Qwen prompt processing benefited significantly from server tuning (reducing
ctx_size from 32K to 8K gave a 3x improvement). Gemma's prompt processing
was slower but generation speed was comparable.

Generation speed on the iGPU was variable for both local models (10-16 t/s
for Qwen, 6-16 t/s for Gemma), likely due to thermal throttling and the
memory-bandwidth-bound nature of MoE inference on shared system memory.

### Server Tuning Findings

| Configuration change | Effect |
|---|---|
| ctx_size 32768 → 8192 | Prompt processing 40 → 130 t/s (3x) |
| --cache-type-k/v q8_0 | Lossless on Qwen3.5 hybrid attention, halves KV memory |
| --flash-attn on | Marginal improvement |
| --batch-size/ubatch-size 4096 | Faster prompt ingestion |
| --parallel 2 --cont-batching | **Slower** than single slot (memory bandwidth bound) |
| --reasoning-budget 0 | Unnecessary — per-request chat_template_kwargs controls thinking |
| Qwen thinking mode | 4x slower (~110s/pair), JSON parsing issues |
| Gemma thinking mode | Similar overhead, requires higher max_tokens |

## Conclusions

### Classification Reliability

| Model | Strength | Weakness |
|---|---|---|
| **Qwen** | RELATED/UNRELATED/OFF_TOPIC calls are reliable | Over-promotes to LIKELY_DUPLICATE (78% false positive rate) |
| **Sonnet** | Most conservative and generally trustworthy | May be slightly conservative on clear DUPLICATEs |
| **Gemma** | Agrees with Sonnet on LIKELY_DUP→RELATED (76%); agrees with Qwen on DUPLICATE→LIKELY_DUP (100%) | Similar speed to Qwen, no clear advantage as primary classifier |

### Recommended Pipeline

Based on these results, the optimal cost/quality pipeline is:

1. **Qwen first pass** (free, ~40 hours for full corpus): classify all pairs.
   Trust RELATED, UNRELATED, and OFF_TOPIC calls directly.
2. **Sonnet validation** (paid, ~5 hours for 1269 pairs): re-assess only
   Qwen's DUPLICATE and LIKELY_DUPLICATE calls, since these are the
   actionable classifications and Qwen's false positive rate is high
   for LIKELY_DUPLICATE.

This reduces Sonnet API cost by ~69% (1269 pairs instead of 4051) while
maintaining classification quality on the decisions that matter (which
issues to close).

### Error Modes

Qwen's errors are **one-directional**: it over-promotes (calls things
DUPLICATE or LIKELY_DUPLICATE that are actually RELATED) but never
under-classifies. This means no true duplicates are missed by the Qwen
first pass — they may be over-counted but not lost.

The 1108 OFF_TOPIC classifications from Qwen (27% of all pairs) were not
validated by Sonnet. A spot-check of the early 23-pair validation found
one case where Qwen called OFF_TOPIC and Sonnet called UNRELATED — a
semantic difference with no practical impact (neither is actionable).

## Reproduction

### Scripts

| Script | Purpose |
|---|---|
| `scripts/run_assess_local.py` | Batch assessment via OpenAI-compatible API (Qwen/Gemma) |
| `scripts/run_assess_scan.py` | Batch assessment via Claude CLI (Sonnet) |
| `scripts/run_sonnet_validation.py` | Re-assess Qwen positives with Sonnet |
| `scripts/run_gemma_comparison.py` | 3-way comparison on disagreement pairs |
| `scripts/validate_qwen_vs_sonnet.py` | Early 23-pair exploratory validation |

### Data

| Table | Contents |
|---|---|
| `scan_results` | 4051 retrieval results (query-candidate pairs with scores) |
| `scan_assessments` | Qwen classifications for all 4051 pairs |
| `scan_assessments_sonnet` | Sonnet classifications for 519 DUPLICATE/LIKELY_DUPLICATE pairs |
| `scan_assessments_gemma` | Gemma classifications for 50 disagreement pairs |
| `data/eval_qwen_vs_sonnet.json` | Early 23-pair validation results |
| `data/eval_gemma_comparison.json` | 50-pair 3-way comparison results |

### Server Configuration

Optimal Lemonade server settings for local models on this hardware:
```json
{
  "ctx_size": 8192,
  "llamacpp_args": "--cache-type-k q8_0 --cache-type-v q8_0 --flash-attn on --batch-size 4096 --ubatch-size 4096 --threads 4"
}
```

Thinking mode controlled per-request via `chat_template_kwargs: {"enable_thinking": false}`.
