#!/usr/bin/env python3
"""Re-assess Qwen-classified pairs with Sonnet and compare."""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, "src")

from mpy_triage.assess import (
    _build_comparison_prompt,
    _fetch_item_text,
    _get_json_schema,
    _load_system_prompt,
)
from mpy_triage.config import clean_env, get_config
from mpy_triage.db import get_connection, init_db

TIMEOUT = 300
CLAUDE_BIN = shutil.which("claude") or os.path.expanduser(
    "~/.local/share/claude/versions/2.1.81"
)
if not os.path.isfile(CLAUDE_BIN):
    print(f"ERROR: claude not found at {CLAUDE_BIN}")
    sys.exit(1)

config = get_config()
conn = get_connection(config.db_path)
init_db(conn, config.schema_path)

# Fetch all Qwen-assessed pairs (assessed after 2026-04-11)
qwen_rows = conn.execute("""
    SELECT query_number, query_type, query_repo,
           candidate_number, candidate_type, candidate_repo,
           classification, confidence, reasoning
    FROM scan_assessments
    WHERE assessed_at > '2026-04-11'
    ORDER BY rowid
""").fetchall()

print(f"Validating {len(qwen_rows)} Qwen assessments with Sonnet...", flush=True)

system_prompt = _load_system_prompt()
schema_json = _get_json_schema()
env = clean_env()

results = []
for i, row in enumerate(qwen_rows):
    q_num, q_type, q_repo = row[0], row[1], row[2]
    c_num, c_type, c_repo = row[3], row[4], row[5]
    qwen_cls, qwen_conf = row[6], row[7]

    q_text = _fetch_item_text(
        conn, {"item_number": q_num, "item_type": q_type, "repo": q_repo}
    )
    c_text = _fetch_item_text(
        conn, {"item_number": c_num, "item_type": c_type, "repo": c_repo}
    )
    user_prompt = _build_comparison_prompt(q_text, c_text)
    full_prompt = f"{system_prompt}\n\n{user_prompt}"

    cmd = [
        CLAUDE_BIN, "--model", "sonnet", "-p",
        "--output-format", "json", "--json-schema", schema_json,
        "--no-session-persistence",
    ]

    print(
        f"  [{i+1}/{len(qwen_rows)}] #{q_num} -> {c_type} #{c_num} "
        f"(Qwen: {qwen_cls})...",
        end="", flush=True,
    )

    try:
        result = subprocess.run(
            cmd, input=full_prompt, capture_output=True, text=True,
            timeout=TIMEOUT, env=env,
        )
        if result.returncode != 0:
            print(f" FAILED (rc={result.returncode})")
            continue

        response = json.loads(result.stdout)
        if "structured_output" in response:
            response = response["structured_output"]

        sonnet_cls = response.get("classification", "?")
        sonnet_conf = response.get("confidence", "?")
        match = "MATCH" if qwen_cls == sonnet_cls else "DIFFER"
        print(f" Sonnet: {sonnet_cls} ({sonnet_conf}) [{match}]")

        results.append({
            "query": f"#{q_num}",
            "candidate": f"{c_type} #{c_num}",
            "qwen": qwen_cls,
            "qwen_conf": qwen_conf,
            "sonnet": sonnet_cls,
            "sonnet_conf": sonnet_conf,
            "match": qwen_cls == sonnet_cls,
        })

    except subprocess.TimeoutExpired:
        print(" TIMEOUT")
    except json.JSONDecodeError:
        print(" BAD JSON")

# Summary
print("\n" + "=" * 60)
total = len(results)
matches = sum(1 for r in results if r["match"])
print(f"Agreement: {matches}/{total} ({100*matches/max(total,1):.0f}%)")

# Confusion-style breakdown
print("\nQwen \\ Sonnet  | " + " | ".join(
    ["DUP", "LIKELY", "REL", "UNREL", "OFFTOP"]
))
cls_order = ["DUPLICATE", "LIKELY_DUPLICATE", "RELATED", "UNRELATED", "OFF_TOPIC"]
cls_short = {"DUPLICATE": "DUP", "LIKELY_DUPLICATE": "LIKELY", "RELATED": "REL",
             "UNRELATED": "UNREL", "OFF_TOPIC": "OFFTOP"}
for qc in cls_order:
    counts = []
    for sc in cls_order:
        n = sum(1 for r in results if r["qwen"] == qc and r["sonnet"] == sc)
        counts.append(f"{n:>5}")
    label = cls_short.get(qc, qc)
    print(f"{label:<14} | " + " | ".join(counts))

# Show disagreements
disagree = [r for r in results if not r["match"]]
if disagree:
    print(f"\nDisagreements ({len(disagree)}):")
    for r in disagree:
        print(f"  {r['query']} -> {r['candidate']}: "
              f"Qwen={r['qwen']} vs Sonnet={r['sonnet']}")

# Save to JSON
out_path = "data/eval_qwen_vs_sonnet.json"
with open(out_path, "w") as f:
    json.dump(results, f, indent=2)
print(f"\nSaved to {out_path}")
