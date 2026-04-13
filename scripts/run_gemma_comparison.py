#!/usr/bin/env python3
"""Run Gemma assessment on Qwen/Sonnet disagreement pairs for 3-way comparison.

Usage:
    python run_gemma_comparison.py [SAMPLE_SIZE] [API_URL] [--think]

Samples from each disagreement bucket proportionally, assesses with Gemma,
and reports which model Gemma agrees with.
"""

import json
import random
import sqlite3
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

sys.path.insert(0, "src")

from mpy_triage.assess import (
    _build_comparison_prompt,
    _fetch_item_text,
    _load_system_prompt,
)
from mpy_triage.config import get_config
from mpy_triage.db import get_connection, init_db

SAMPLE_SIZE = int(sys.argv[1]) if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else 50
API_URL = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("-") else "http://pilap2:13305/v1"
THINKING = "--think" in sys.argv
TIMEOUT = 600 if THINKING else 120

# Use separate table/file when thinking enabled to preserve non-thinking results
TABLE_NAME = "scan_assessments_gemma_think" if THINKING else "scan_assessments_gemma"
OUTPUT_FILE = "data/eval_gemma_think_comparison.json" if THINKING else "data/eval_gemma_comparison.json"

config = get_config()
conn = get_connection(config.db_path)
conn = sqlite3.connect(str(config.db_path), check_same_thread=False)
conn.row_factory = sqlite3.Row
init_db(conn, config.schema_path)

# Create Gemma comparison table
conn.execute(f"""
    CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
        query_number INTEGER,
        query_type TEXT,
        query_repo TEXT,
        candidate_number INTEGER,
        candidate_type TEXT,
        candidate_repo TEXT,
        classification TEXT,
        confidence TEXT,
        reasoning TEXT,
        suggested_action TEXT,
        assessed_at TEXT,
        PRIMARY KEY (query_number, query_type, query_repo,
                     candidate_number, candidate_type, candidate_repo)
    )
""")
conn.commit()

# Fetch all disagreement pairs with their classifications
disagreements = conn.execute(f"""
    SELECT sa.query_number, sa.query_type, sa.query_repo,
           sa.candidate_number, sa.candidate_type, sa.candidate_repo,
           sa.classification as qwen_cls,
           ss.classification as sonnet_cls,
           sr.value_score
    FROM scan_assessments sa
    JOIN scan_assessments_sonnet ss
        ON ss.query_number = sa.query_number AND ss.query_type = sa.query_type
        AND ss.query_repo = sa.query_repo
        AND ss.candidate_number = sa.candidate_number AND ss.candidate_type = sa.candidate_type
        AND ss.candidate_repo = sa.candidate_repo
    JOIN scan_results sr
        ON sr.query_number = sa.query_number AND sr.query_type = sa.query_type
        AND sr.query_repo = sa.query_repo
        AND sr.candidate_number = sa.candidate_number AND sr.candidate_type = sa.candidate_type
        AND sr.candidate_repo = sa.candidate_repo
    LEFT JOIN {TABLE_NAME} sg
        ON sg.query_number = sa.query_number AND sg.query_type = sa.query_type
        AND sg.query_repo = sa.query_repo
        AND sg.candidate_number = sa.candidate_number AND sg.candidate_type = sa.candidate_type
        AND sg.candidate_repo = sa.candidate_repo
    WHERE sa.classification != ss.classification
        AND sg.query_number IS NULL
    ORDER BY sr.value_score DESC
""").fetchall()

# Stratified sampling: proportional to each disagreement bucket
buckets = {}
for row in disagreements:
    key = (row["qwen_cls"], row["sonnet_cls"])
    buckets.setdefault(key, []).append(row)

total_disagreements = len(disagreements)
sample = []
for key, items in sorted(buckets.items(), key=lambda x: -len(x[1])):
    proportion = len(items) / total_disagreements
    n = max(1, round(SAMPLE_SIZE * proportion))
    # Take top by value_score (already sorted) for reproducibility
    sample.extend(items[:n])
    print(f"  Bucket {key[0]:>20s} -> {key[1]:<20s}: {len(items)} total, sampling {min(n, len(items))}")

# Cap at SAMPLE_SIZE
sample = sample[:SAMPLE_SIZE]

print(f"\nSampled {len(sample)} disagreement pairs for Gemma comparison.", flush=True)

system_prompt = _load_system_prompt()
now = datetime.now(timezone.utc).isoformat()

# Check model — prefer Gemma, fall back to first available
try:
    req = urllib.request.Request(f"{API_URL}/models")
    with urllib.request.urlopen(req, timeout=10) as resp:
        models = json.loads(resp.read())
    model_id = None
    for m in models["data"]:
        if "gemma" in m["id"].lower():
            model_id = m["id"]
            break
    if model_id is None:
        model_id = models["data"][0]["id"]
    print(f"Using model: {model_id}", flush=True)
except Exception as e:
    print(f"ERROR: Cannot reach {API_URL}: {e}")
    sys.exit(1)

results = []
total_time = 0

for i, row in enumerate(sample):
    q_num, q_type, q_repo = row["query_number"], row["query_type"], row["query_repo"]
    c_num, c_type, c_repo = row["candidate_number"], row["candidate_type"], row["candidate_repo"]
    qwen_cls = row["qwen_cls"]
    sonnet_cls = row["sonnet_cls"]
    score = row["value_score"]

    q_text = _fetch_item_text(
        conn, {"item_number": q_num, "item_type": q_type, "repo": q_repo}
    )
    c_text = _fetch_item_text(
        conn, {"item_number": c_num, "item_type": c_type, "repo": c_repo}
    )
    user_prompt = _build_comparison_prompt(q_text, c_text)

    payload = json.dumps({
        "model": model_id,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": 4000 if THINKING else 500,
        "temperature": 1.0,
        "top_p": 0.95,
        "top_k": 64,
        "samplers": ["temperature", "top_p", "top_k"],
        "chat_template_kwargs": {"enable_thinking": THINKING},
        "response_format": {"type": "json_object"},
    }).encode()

    print(
        f"  [{i+1}/{len(sample)}] #{q_num} -> {c_type} #{c_num} "
        f"(Q:{qwen_cls} S:{sonnet_cls})...",
        end="", flush=True,
    )

    try:
        req = urllib.request.Request(
            f"{API_URL}/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read())
        elapsed = time.time() - t0
        total_time += elapsed

        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})

        # Strip markdown code fences if present
        text = content.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        response = json.loads(text)
        gemma_cls = response.get("classification", "UNRELATED")
        gemma_conf = response.get("confidence", "low")

        conn.execute(f"""
            INSERT OR REPLACE INTO {TABLE_NAME}
            (query_number, query_type, query_repo,
             candidate_number, candidate_type, candidate_repo,
             classification, confidence, reasoning, suggested_action,
             assessed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            q_num, q_type, q_repo, c_num, c_type, c_repo,
            gemma_cls, gemma_conf,
            response.get("reasoning", ""),
            response.get("suggested_action", ""),
            now,
        ))
        conn.commit()

        agrees_qwen = gemma_cls == qwen_cls
        agrees_sonnet = gemma_cls == sonnet_cls
        if agrees_qwen and agrees_sonnet:
            verdict = "BOTH"  # all three agree (shouldn't happen - these are disagreements)
        elif agrees_qwen:
            verdict = "→Qwen"
        elif agrees_sonnet:
            verdict = "→Sonnet"
        else:
            verdict = "→NEITHER"

        print(f" G:{gemma_cls} [{verdict}] [{elapsed:.1f}s]")

        results.append({
            "query": f"#{q_num}",
            "candidate": f"{c_type} #{c_num}",
            "qwen": qwen_cls,
            "sonnet": sonnet_cls,
            "gemma": gemma_cls,
            "gemma_conf": gemma_conf,
            "agrees_qwen": agrees_qwen,
            "agrees_sonnet": agrees_sonnet,
            "elapsed": elapsed,
        })

    except urllib.error.URLError as e:
        print(f" NETWORK ERROR: {e}")
    except json.JSONDecodeError as e:
        print(f" BAD JSON: {e}")
    except Exception as e:
        print(f" ERROR: {e}")

# Summary
print("\n" + "=" * 70)
total = len(results)
agrees_qwen = sum(1 for r in results if r["agrees_qwen"])
agrees_sonnet = sum(1 for r in results if r["agrees_sonnet"])
agrees_neither = sum(1 for r in results if not r["agrees_qwen"] and not r["agrees_sonnet"])
avg_time = total_time / max(total, 1)

print(f"Gemma assessed {total} disagreement pairs in {total_time:.0f}s ({avg_time:.1f}s/pair)")
print(f"\nGemma agrees with:")
print(f"  Qwen:    {agrees_qwen}/{total} ({100*agrees_qwen/max(total,1):.0f}%)")
print(f"  Sonnet:  {agrees_sonnet}/{total} ({100*agrees_sonnet/max(total,1):.0f}%)")
print(f"  Neither: {agrees_neither}/{total} ({100*agrees_neither/max(total,1):.0f}%)")

# Per-bucket breakdown
print(f"\nPer disagreement type:")
print(f"{'Qwen':>20s} → {'Sonnet':<20s} | n  | →Qwen | →Sonnet | →Neither")
print("-" * 80)
bucket_results = {}
for r in results:
    key = (r["qwen"], r["sonnet"])
    bucket_results.setdefault(key, []).append(r)
for key in sorted(bucket_results.keys(), key=lambda k: -len(bucket_results[k])):
    items = bucket_results[key]
    n = len(items)
    aq = sum(1 for r in items if r["agrees_qwen"])
    as_ = sum(1 for r in items if r["agrees_sonnet"])
    an = sum(1 for r in items if not r["agrees_qwen"] and not r["agrees_sonnet"])
    print(f"{key[0]:>20s} → {key[1]:<20s} | {n:>2} | {aq:>5} | {as_:>7} | {an:>8}")

# Save results
with open(OUTPUT_FILE, "w") as f:
    json.dump(results, f, indent=2)
print(f"\nSaved to {OUTPUT_FILE}")
