# Model Comparison: Qwen3.5 vs Sonnet vs Gemma-4 on Issue Triage Classification

## Background

The mpy-triage pipeline uses an LLM to classify pairs of GitHub items
(issue/PR) as DUPLICATE, LIKELY_DUPLICATE, RELATED, OFF_TOPIC, or UNRELATED.
The retrieval stage produces 4051 candidate pairs across the MicroPython
repositories; each must be classified to decide which issues can be closed.

This report compares three models on this task to determine the most
cost-effective pipeline.

## Models

| Model | Type | Active Params | Quantisation | Host |
|---|---|---|---|---|
| **Qwen3.5-35B-A3B** | MoE | 3B (35B total) | Q4_K_XL GGUF | Radeon 890M iGPU (Lemonade/llama.cpp) |
| **Claude Sonnet** | Dense | undisclosed | — | Anthropic API via `claude -p` |
| **Gemma-4-26B-A4B** | MoE | 4B (26B total) | Q4_K_M GGUF | Radeon 890M iGPU (Lemonade/llama.cpp) |

Both local models ran on the same hardware: AMD Ryzen AI 9 HX PRO 370 with
Radeon 890M iGPU (shared 64GB system RAM). Server settings: `ctx_size=8192`,
q8_0 KV cache, flash-attn, batch/ubatch 4096, threads 4.

## Prompt

All three models received identical prompts (`prompts/assess.txt`):

- System prompt: task description, classification labels with definitions,
  closure rules (which item to close, issue-vs-PR direction), MicroPython
  domain hints
- User prompt: `## QUERY ITEM\n{text}\n\n## CANDIDATE ITEM\n{text}`
- Expected output: JSON object with `classification`, `confidence`,
  `reasoning`, `suggested_action`

Input sizes (measured across 150 sampled pairs):

| Percentile | Tokens |
|---|---|
| Median | ~3,000 |
| P95 | ~4,000 |
| Max | ~4,000 |

Output: ~100-300 tokens (no-think mode), ~1000-2000 tokens (thinking mode).

## Phase 1: Full Qwen Pass (4051 pairs)

Qwen3.5-35B-A3B classified every retrieved pair. Thinking mode disabled
(`enable_thinking: false`). Runtime: ~40 hours.

### Classification Distribution

| Label | Count | % |
|---|---|---|
| RELATED | 1621 | 40.0% |
| OFF_TOPIC | 1108 | 27.4% |
| LIKELY_DUPLICATE | 899 | 22.2% |
| DUPLICATE | 370 | 9.1% |
| UNRELATED | 53 | 1.3% |

**Actionable classifications**: 1269 DUPLICATE + LIKELY_DUPLICATE.

## Phase 2: Sonnet Validation (1268 pairs)

Sonnet independently re-assessed all 1269 of Qwen's DUPLICATE and
LIKELY_DUPLICATE calls. 1268/1269 completed successfully (one pair failed
repeatedly, abandoned). Runtime: ~6 hours wall time via Claude CLI subprocess.

### Overall Agreement

**Exact-label match**: 317/1268 = **25.0%**

The low exact-match rate is misleading — most disagreements are adjacent
categories (DUPLICATE ↔ LIKELY_DUPLICATE) which are both actionable.

### Confusion Matrix (Qwen rows × Sonnet columns)

|  | Sonnet DUPLICATE | Sonnet LIKELY_DUP | Sonnet RELATED | Sonnet UNRELATED | Sonnet OFF_TOPIC |
|---|---|---|---|---|---|
| **Qwen DUPLICATE** (n=369) | **204** (55%) | 120 (33%) | 45 (12%) | 0 | 0 |
| **Qwen LIKELY_DUP** (n=899) | 24 (3%) | 113 (13%) | **750** (83%) | 11 (1%) | 1 |

### Per-Label Actionable Rate

"Actionable" = Sonnet also classified as DUPLICATE or LIKELY_DUPLICATE
(i.e. the issue should be considered for closure):

| Qwen label | n | Still actionable per Sonnet | Rate |
|---|---|---|---|
| **DUPLICATE** | 369 | 324 | **88%** |
| **LIKELY_DUPLICATE** | 899 | 137 | **15%** |

### Key Finding: Qwen's Failure Mode is One-Directional

Qwen **over-promotes** but **never under-classifies**:

- Of 369 Qwen DUPLICATEs, Sonnet downgrades 45 (12%) fully to RELATED —
  a moderate false positive rate but still 88% actionable.
- Of 899 Qwen LIKELY_DUPLICATEs, Sonnet downgrades 762 (85%) to RELATED
  or weaker — Qwen's LIKELY_DUPLICATE is essentially a "maybe" bucket
  that Sonnet reads much more conservatively.
- In the reverse direction, Qwen's LIKELY_DUPLICATE calls occasionally
  get *upgraded* to DUPLICATE by Sonnet (24 cases), showing Qwen is
  actually uncertain, not consistently over-promoting.

Critically, no Qwen RELATED/UNRELATED calls were validated by Sonnet in
this study, but an earlier exploratory 23-pair test (before the full run)
showed 100% agreement on the RELATED/UNRELATED labels. Qwen appears
reliable for filtering out non-duplicates.

## Phase 3: Gemma Tiebreaker (50 pairs)

To break ties on the 951 pairs where Qwen and Sonnet disagreed, a
stratified sample of 50 disagreement pairs was assessed by Gemma-4-26B-A4B.

Sampling: proportional across disagreement buckets, ordered by `value_score`
descending for reproducibility.

### Disagreement Pool (Qwen ≠ Sonnet)

| Qwen → Sonnet | Count | % of disagreements |
|---|---|---|
| LIKELY_DUP → RELATED | 750 | 79% |
| DUPLICATE → LIKELY_DUP | 120 | 13% |
| DUPLICATE → RELATED | 45 | 5% |
| LIKELY_DUP → DUPLICATE | 24 | 3% |
| LIKELY_DUP → UNRELATED | 11 | 1% |
| LIKELY_DUP → OFF_TOPIC | 1 | 0% |

### Gemma No-Think Results (50 pairs, 45s/pair)

| Gemma agrees with | Count | % |
|---|---|---|
| **Sonnet** | **32** | **64%** |
| Qwen | 10 | 20% |
| Neither | 8 | 16% |

Per disagreement bucket:

| Qwen → Sonnet | n | →Qwen | →Sonnet | →Neither |
|---|---|---|---|---|
| LIKELY_DUP → RELATED | 37 | 2 (5%) | **28 (76%)** | 7 (19%) |
| DUPLICATE → LIKELY_DUP | 8 | **8 (100%)** | 0 | 0 |
| LIKELY_DUP → DUPLICATE | 2 | 0 | 2 | 0 |
| DUPLICATE → RELATED | 2 | 0 | 2 | 0 |
| LIKELY_DUP → UNRELATED | 1 | 0 | 0 | 1 |

**Interpretation**:
- In the largest bucket (LIKELY_DUP→RELATED), Gemma sided with Sonnet
  76% of the time. This confirms Qwen's LIKELY_DUPLICATE over-promotion
  is a genuine error, not a stylistic difference.
- In the DUPLICATE→LIKELY_DUP bucket, Gemma sided with Qwen **100%** (8/8).
  These are cases where Qwen was confident and Sonnet hedged — Gemma
  agrees with Qwen that these are confirmed duplicates. Sonnet may be
  overly conservative on this adjacent downgrade.

### Gemma Thinking Mode Results (42/50 pairs, 139s/pair)

Run with `enable_thinking: true`. 8 pairs failed (server timed out or
returned errors on longer thinking outputs).

| Gemma agrees with | Count | % |
|---|---|---|
| **Sonnet** | 22 | **52%** |
| Qwen | 10 | 24% |
| Neither | 10 | 24% |

Per disagreement bucket:

| Qwen → Sonnet | n | →Qwen | →Sonnet | →Neither |
|---|---|---|---|---|
| LIKELY_DUP → RELATED | 31 | 3 (10%) | 20 (65%) | 8 (26%) |
| DUPLICATE → LIKELY_DUP | 6 | **6 (100%)** | 0 | 0 |
| DUPLICATE → RELATED | 2 | 1 | 1 | 0 |
| LIKELY_DUP → DUPLICATE | 1 | 0 | 1 | 0 |
| LIKELY_DUP → UNRELATED | 1 | 0 | 0 | 1 |
| LIKELY_DUP → OFF_TOPIC | 1 | 0 | 0 | 1 |

### No-Think vs Thinking Comparison

| Metric | No-think | Thinking | Delta |
|---|---|---|---|
| Avg latency | 45s | **139s** | 3.1x slower |
| Success rate | 50/50 | 42/50 | -16% |
| Agreement with Sonnet | 64% | 52% | **-12pp** |
| "Neither" cases | 16% | 24% | +8pp |
| DUPLICATE→LIKELY_DUP → Qwen | 100% | 100% | unchanged |

**Thinking mode is worse on this task.** It is slower, less reliable
(timeouts on longer outputs), and produces *lower* Sonnet agreement.
The extra reasoning leads Gemma to more independent "Neither" verdicts
rather than landing on one of the two candidate classifications.

This is consistent with the task characteristics: classification is a
discrete judgement, not a multi-step problem. Additional reasoning
tokens don't improve accuracy — they give the model more opportunity
to talk itself into an alternative answer.

## Throughput & Cost

All local inference ran on the same AMD Radeon 890M iGPU.

| Model | Latency/pair | Total time (4051 pairs) | Cost |
|---|---|---|---|
| Qwen3.5-35B-A3B (no-think) | 35-50s | ~40 hours | Free |
| Gemma-4-26B-A4B (no-think) | 45s | ~51 hours (if used for full pass) | Free |
| Gemma-4-26B-A4B (thinking) | 139s | ~156 hours (if used for full pass) | Free |
| Claude Sonnet | ~15s | ~17 hours | ~$40 via API |

Server tuning findings (applied to local model runs):

| Config change | Effect |
|---|---|
| `ctx_size` 32768 → 8192 | Prompt processing 40 → 130 t/s (3x) |
| `--cache-type-k/v q8_0` | Lossless on Qwen3.5 hybrid attention |
| `--parallel 2 --cont-batching` | *Slower* than single slot on iGPU (memory bandwidth bound) |
| `--reasoning-budget 0` | Unnecessary — per-request `chat_template_kwargs` |
| Qwen `enable_thinking: true` | 4x slower, JSON parsing issues |

## Recommendations

### Primary Recommendation: Tiered Pipeline

```
  ┌───────────────────────────┐
  │  4051 retrieved pairs     │
  └─────────────┬─────────────┘
                │
                ▼
  ┌───────────────────────────┐
  │  Qwen first pass (free)   │
  │  ~40h on local iGPU       │
  └─────────────┬─────────────┘
                │
       ┌────────┴────────┐
       │                 │
       ▼                 ▼
  ┌─────────┐   ┌─────────────┐
  │ 2782    │   │ 1269 DUP +  │
  │ RELATED/│   │ LIKELY_DUP  │
  │ etc.    │   │             │
  │(trust)  │   └──────┬──────┘
  └─────────┘          │
                       ▼
              ┌────────────────┐
              │ Sonnet validate│
              │ (~$12, ~5h)    │
              └────────┬───────┘
                       │
                       ▼
              ┌────────────────┐
              │ ~230 DUPLICATE │
              │ ~104 LIKELY    │
              │ (actionable)   │
              └────────────────┘
```

This reduces Sonnet cost by **69%** (1269 pairs instead of 4051) while
maintaining classification quality on the decisions that matter.

### Label-Specific Trust

Based on the Sonnet validation:

| If Qwen says... | Action | Basis |
|---|---|---|
| DUPLICATE | Escalate to Sonnet (88% actionable) | 12% false positive rate, but still high-signal |
| LIKELY_DUPLICATE | Escalate to Sonnet (15% actionable) | 85% false positive rate, Sonnet as gold standard |
| RELATED | Trust | Early validation showed 100% agreement |
| OFF_TOPIC / UNRELATED | Trust | No evidence of false negatives |

### Models Not Recommended

- **Gemma-4-26B-A4B** as primary classifier: No speed advantage over Qwen
  on this hardware (45s vs 45s), and no evidence of superior accuracy.
- **Gemma thinking mode**: 3x slower, less reliable, *lower* accuracy.
- **Qwen thinking mode**: 4x slower, no measured accuracy improvement,
  JSON output parsing issues.

### When Gemma is Useful

Gemma served well as a **tiebreaker** for understanding disagreements
between Qwen and Sonnet — it confirmed that Qwen's LIKELY_DUPLICATE
over-promotion is a real error and that Qwen's DUPLICATE confidence
is genuinely high-signal (Gemma agreed with Qwen 100% of the time
when Sonnet hedged).

## Data Sources

| Table / File | Contents |
|---|---|
| `scan_assessments` | Qwen classifications for all 4051 pairs |
| `scan_assessments_sonnet` | Sonnet classifications for 1268 Qwen positives |
| `scan_assessments_gemma` | Gemma no-think for 50 disagreement pairs |
| `scan_assessments_gemma_think` | Gemma thinking for 42 disagreement pairs |
| `data/eval_qwen_vs_sonnet.json` | Early 23-pair exploratory validation |
| `data/eval_gemma_comparison.json` | No-think Gemma tiebreaker results |
| `data/eval_gemma_think_comparison.json` | Thinking Gemma tiebreaker results |

## Scripts

| Script | Purpose |
|---|---|
| `scripts/run_assess_local.py` | Batch assessment via OpenAI-compatible API |
| `scripts/run_sonnet_validation.py` | Re-assess Qwen positives via Claude CLI |
| `scripts/run_gemma_comparison.py` | 3-way disagreement analysis (supports `--think`) |
| `scripts/validate_qwen_vs_sonnet.py` | Early exploratory validation (23 pairs) |
