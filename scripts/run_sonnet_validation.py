#!/usr/bin/env python3
"""Re-assess Qwen DUPLICATE/LIKELY_DUPLICATE pairs with Sonnet.

Stores Sonnet results in scan_assessments_sonnet table to preserve
the original Qwen assessments for comparison.
"""

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

TOP_N = int(sys.argv[1]) if len(sys.argv) > 1 else 9999
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

# Create Sonnet validation table if needed
conn.execute("""
    CREATE TABLE IF NOT EXISTS scan_assessments_sonnet (
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

# Fetch Qwen DUPLICATE/LIKELY_DUPLICATE pairs not yet validated by Sonnet
pairs = conn.execute("""
    SELECT sa.query_number, sa.query_type, sa.query_repo,
           sa.candidate_number, sa.candidate_type, sa.candidate_repo,
           sa.classification as qwen_cls,
           sr.value_score
    FROM scan_assessments sa
    JOIN scan_results sr
        ON sr.query_number = sa.query_number
        AND sr.query_type = sa.query_type
        AND sr.query_repo = sa.query_repo
        AND sr.candidate_number = sa.candidate_number
        AND sr.candidate_type = sa.candidate_type
        AND sr.candidate_repo = sa.candidate_repo
    LEFT JOIN scan_assessments_sonnet ss
        ON ss.query_number = sa.query_number
        AND ss.query_type = sa.query_type
        AND ss.query_repo = sa.query_repo
        AND ss.candidate_number = sa.candidate_number
        AND ss.candidate_type = sa.candidate_type
        AND ss.candidate_repo = sa.candidate_repo
    WHERE sa.classification IN ('DUPLICATE', 'LIKELY_DUPLICATE')
        AND ss.query_number IS NULL
    ORDER BY sr.value_score DESC
    LIMIT ?
""", (TOP_N,)).fetchall()

print(f"Validating {len(pairs)} Qwen DUPLICATE/LIKELY_DUPLICATE pairs with Sonnet...",
      flush=True)

system_prompt = _load_system_prompt()
schema_json = _get_json_schema()
env = clean_env()
now = datetime.now(timezone.utc).isoformat()

agree = 0
disagree = 0

for i, row in enumerate(pairs):
    q_num, q_type, q_repo = row[0], row[1], row[2]
    c_num, c_type, c_repo = row[3], row[4], row[5]
    qwen_cls = row[6]
    score = row[7]

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
        f"  [{i+1}/{len(pairs)}] #{q_num} -> {c_type} #{c_num} "
        f"(Qwen: {qwen_cls}, score {score:.3f})...",
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

        conn.execute("""
            INSERT OR REPLACE INTO scan_assessments_sonnet
            (query_number, query_type, query_repo,
             candidate_number, candidate_type, candidate_repo,
             classification, confidence, reasoning, suggested_action,
             assessed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            q_num, q_type, q_repo, c_num, c_type, c_repo,
            sonnet_cls, sonnet_conf,
            response.get("reasoning", ""),
            response.get("suggested_action", ""),
            now,
        ))
        conn.commit()

        match = "AGREE" if qwen_cls == sonnet_cls else "DIFFER"
        if qwen_cls == sonnet_cls:
            agree += 1
        else:
            disagree += 1

        print(f" Sonnet: {sonnet_cls} ({sonnet_conf}) [{match}]")

    except subprocess.TimeoutExpired:
        print(" TIMEOUT")
    except json.JSONDecodeError:
        print(" BAD JSON")

total = agree + disagree
validated = conn.execute("SELECT COUNT(*) FROM scan_assessments_sonnet").fetchone()[0]
print(f"\nDone. {validated} Sonnet validations in database.", flush=True)
if total > 0:
    print(f"Agreement: {agree}/{total} ({100*agree/total:.0f}%)", flush=True)

# Show disagreement summary
print("\nDisagreement breakdown:")
rows = conn.execute("""
    SELECT sa.classification as qwen, ss.classification as sonnet, COUNT(*) as cnt
    FROM scan_assessments sa
    JOIN scan_assessments_sonnet ss
        ON ss.query_number = sa.query_number
        AND ss.query_type = sa.query_type
        AND ss.query_repo = sa.query_repo
        AND ss.candidate_number = sa.candidate_number
        AND ss.candidate_type = sa.candidate_type
        AND ss.candidate_repo = sa.candidate_repo
    GROUP BY sa.classification, ss.classification
    ORDER BY cnt DESC
""").fetchall()
for row in rows:
    print(f"  Qwen {row[0]:>20s} -> Sonnet {row[1]:<20s}: {row[2]}")
