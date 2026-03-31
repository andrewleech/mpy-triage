#!/usr/bin/env python3
"""Run Sonnet assessment on top N scan results."""

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, "src")

from mpy_triage.assess import _build_comparison_prompt, _fetch_item_text, _get_json_schema, _load_system_prompt
from mpy_triage.config import clean_env, get_config
from mpy_triage.db import get_connection, init_db

TOP_N = int(sys.argv[1]) if len(sys.argv) > 1 else 20
TIMEOUT = 120

config = get_config()
conn = get_connection(config.db_path)
init_db(conn, config.schema_path)

# Fetch top N unassessed scan results
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

print(f"Assessing {len(pairs)} pairs with Sonnet...", flush=True)

system_prompt = _load_system_prompt()
schema_json = _get_json_schema()
env = clean_env()
now = datetime.now(timezone.utc).isoformat()

for i, row in enumerate(pairs):
    q_num, q_type, q_repo = row[0], row[1], row[2]
    c_num, c_type, c_repo = row[3], row[4], row[5]
    score = row[6]

    q_text = _fetch_item_text(conn, {
        "item_number": q_num, "item_type": q_type, "repo": q_repo
    })
    c_text = _fetch_item_text(conn, {
        "item_number": c_num, "item_type": c_type, "repo": c_repo
    })

    user_prompt = _build_comparison_prompt(q_text, c_text)
    full_prompt = f"{system_prompt}\n\n{user_prompt}"

    cmd = [
        "claude", "--model", "sonnet", "-p",
        "--output-format", "json", "--json-schema", schema_json,
        "--no-session-persistence",
    ]

    print(f"  [{i+1}/{len(pairs)}] #{q_num} -> {c_type} #{c_num} (score {score:.3f})...",
          end="", flush=True)

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

        conn.execute("""
            INSERT OR REPLACE INTO scan_assessments
            (query_number, query_type, query_repo,
             candidate_number, candidate_type, candidate_repo,
             classification, confidence, reasoning, suggested_action,
             assessed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            q_num, q_type, q_repo, c_num, c_type, c_repo,
            response.get("classification", "UNRELATED"),
            response.get("confidence", "low"),
            response.get("reasoning", ""),
            response.get("suggested_action", ""),
            now,
        ))
        conn.commit()

        cls = response.get("classification", "?")
        conf = response.get("confidence", "?")
        print(f" {cls} ({conf})")

    except subprocess.TimeoutExpired:
        print(" TIMEOUT")
    except json.JSONDecodeError:
        print(" BAD JSON")

assessed = conn.execute("SELECT COUNT(*) FROM scan_assessments").fetchone()[0]
print(f"\nDone. {assessed} total assessments in database.", flush=True)
