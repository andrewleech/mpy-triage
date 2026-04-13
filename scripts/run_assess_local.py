#!/usr/bin/env python3
"""Run assessment on scan results using a local OpenAI-compatible LLM.

Usage:
    python run_assess_local.py [TOP_N] [API_URL] [--think] [--workers N]
"""

import json
import sqlite3
import sys
import threading
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

sys.path.insert(0, "src")

from mpy_triage.assess import (
    _build_comparison_prompt,
    _fetch_item_text,
    _get_json_schema,
    _load_system_prompt,
)
from mpy_triage.config import get_config
from mpy_triage.db import get_connection, init_db


def _parse_arg(flag, default):
    """Parse --flag N from sys.argv."""
    for i, a in enumerate(sys.argv):
        if a == flag and i + 1 < len(sys.argv):
            return int(sys.argv[i + 1])
    return default


TOP_N = int(sys.argv[1]) if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else 100
API_URL = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("-") else "http://pilap2:13305/v1"
THINKING = "--think" in sys.argv
WORKERS = _parse_arg("--workers", 4)
TIMEOUT = 300 if THINKING else 120

config = get_config()
conn = get_connection(config.db_path)
init_db(conn, config.schema_path)

# Re-open with check_same_thread=False for ThreadPoolExecutor
conn.close()
conn = sqlite3.connect(str(config.db_path), check_same_thread=False)
conn.row_factory = sqlite3.Row

# Fetch unassessed scan results
pairs = conn.execute("""
    SELECT sr.query_number, sr.query_type, sr.query_repo,
           sr.candidate_number, sr.candidate_type, sr.candidate_repo,
           sr.value_score
    FROM scan_results sr
    LEFT JOIN scan_assessments sa
        ON sa.query_number = sr.query_number
        AND sa.query_type = sr.query_type
        AND sa.query_repo = sr.query_repo
        AND sa.candidate_number = sr.candidate_number
        AND sa.candidate_type = sr.candidate_type
        AND sa.candidate_repo = sr.candidate_repo
    WHERE sa.query_number IS NULL
    ORDER BY sr.value_score DESC
    LIMIT ?
""", (TOP_N,)).fetchall()

mode = "thinking" if THINKING else "no_think"
print(f"Assessing {len(pairs)} pairs via {API_URL} ({mode}, {WORKERS} workers)...",
      flush=True)

system_prompt = _load_system_prompt()
now = datetime.now(timezone.utc).isoformat()

# Check which model is available
try:
    req = urllib.request.Request(f"{API_URL}/models")
    with urllib.request.urlopen(req, timeout=10) as resp:
        models = json.loads(resp.read())
    model_id = models["data"][0]["id"]
    print(f"Using model: {model_id}", flush=True)
except Exception as e:
    print(f"ERROR: Cannot reach {API_URL}: {e}")
    sys.exit(1)

# Pre-fetch all item texts (avoid DB contention in threads)
print("Loading item texts...", end="", flush=True)
item_texts = {}
for row in pairs:
    for num, typ, repo in [(row[0], row[1], row[2]), (row[3], row[4], row[5])]:
        key = (num, typ, repo)
        if key not in item_texts:
            item_texts[key] = _fetch_item_text(
                conn, {"item_number": num, "item_type": typ, "repo": repo}
            )
print(f" {len(item_texts)} items cached.", flush=True)

# Thread-safe DB writes and progress tracking
db_lock = threading.Lock()
progress = {"done": 0, "total": len(pairs), "total_time": 0.0, "total_tokens": 0}
print_lock = threading.Lock()


def assess_pair(idx, row):
    """Assess a single pair via the API. Returns result dict or None."""
    q_num, q_type, q_repo = row[0], row[1], row[2]
    c_num, c_type, c_repo = row[3], row[4], row[5]
    score = row[6]

    q_text = item_texts[(q_num, q_type, q_repo)]
    c_text = item_texts[(c_num, c_type, c_repo)]
    user_prompt = _build_comparison_prompt(q_text, c_text)

    payload = json.dumps({
        "model": model_id,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": 4000 if THINKING else 500,
        "temperature": 0.1,
        "chat_template_kwargs": {"enable_thinking": THINKING},
        "response_format": {"type": "json_object"},
    }).encode()

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

        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})

        # Strip markdown code fences if present (thinking mode wraps JSON)
        text = content.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        response = json.loads(text)

        cls = response.get("classification", "UNRELATED")
        conf = response.get("confidence", "low")

        # Write to DB under lock
        with db_lock:
            conn.execute("""
                INSERT OR REPLACE INTO scan_assessments
                (query_number, query_type, query_repo,
                 candidate_number, candidate_type, candidate_repo,
                 classification, confidence, reasoning, suggested_action,
                 assessed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                q_num, q_type, q_repo, c_num, c_type, c_repo,
                cls, conf,
                response.get("reasoning", ""),
                response.get("suggested_action", ""),
                now,
            ))
            conn.commit()
            progress["done"] += 1
            progress["total_time"] += elapsed
            progress["total_tokens"] += usage.get("total_tokens", 0)
            done = progress["done"]

        with print_lock:
            print(
                f"  [{done}/{progress['total']}] #{q_num} -> {c_type} #{c_num}"
                f" (score {score:.3f})... {cls} ({conf}) [{elapsed:.1f}s]",
                flush=True,
            )

        return cls

    except urllib.error.URLError as e:
        with print_lock:
            print(f"  #{q_num} -> #{c_num}: NETWORK ERROR: {e}", flush=True)
    except json.JSONDecodeError as e:
        with print_lock:
            print(f"  #{q_num} -> #{c_num}: BAD JSON: {e}", flush=True)
    except Exception as e:
        with print_lock:
            print(f"  #{q_num} -> #{c_num}: ERROR: {e}", flush=True)
    return None


# Run with thread pool
t_start = time.time()

with ThreadPoolExecutor(max_workers=WORKERS) as pool:
    futures = {pool.submit(assess_pair, i, row): i for i, row in enumerate(pairs)}
    for future in as_completed(futures):
        future.result()  # propagate exceptions

wall_time = time.time() - t_start
assessed = conn.execute("SELECT COUNT(*) FROM scan_assessments").fetchone()[0]
avg = progress["total_time"] / max(progress["done"], 1)
throughput = progress["done"] / max(wall_time, 1)

print(f"\nDone. {assessed} total assessments in database.", flush=True)
print(f"Wall time: {wall_time:.0f}s ({wall_time/3600:.1f}h)", flush=True)
print(f"Avg latency: {avg:.1f}s/pair, throughput: {throughput:.2f} pairs/s", flush=True)
print(f"Total tokens: {progress['total_tokens']:,}", flush=True)
