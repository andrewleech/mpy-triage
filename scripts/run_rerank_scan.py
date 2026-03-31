#!/usr/bin/env python3
"""Re-rank existing scan results using cross-encoder on GPU."""

import sys

sys.path.insert(0, "src")

import sqlite3
from datetime import datetime, timezone

from mpy_triage.config import get_config
from mpy_triage.db import get_connection, init_db, load_vec_extension
from mpy_triage.search import Reranker, _fetch_content

config = get_config()
conn = get_connection(config.db_path)
init_db(conn, config.schema_path)
load_vec_extension(conn)

# Fetch all scan result pairs
pairs = conn.execute("""
    SELECT sr.query_number, sr.query_type, sr.query_repo,
           sr.candidate_number, sr.candidate_type, sr.candidate_repo,
           sr.candidate_state
    FROM scan_results sr
    ORDER BY sr.value_score DESC
""").fetchall()

print(f"Re-ranking {len(pairs)} scan result pairs...", flush=True)

# Load reranker (trigger lazy load)
reranker = Reranker(config.retrieval.reranker_model)
reranker._load_model()

# State multipliers
STATE_MULT = {"merged": 2.0, "open": 1.5, "closed": 1.0}

try:
    from tqdm import tqdm
    iterator = tqdm(pairs, desc="Re-ranking")
except ImportError:
    iterator = pairs

now = datetime.now(timezone.utc).isoformat()

for row in iterator:
    q_num, q_type, q_repo = row[0], row[1], row[2]
    c_num, c_type, c_repo = row[3], row[4], row[5]
    c_state = row[6]

    # Fetch content for query and candidate
    q_content = _fetch_content(conn, {
        "item_number": q_num, "item_type": q_type, "repo": q_repo
    })
    c_content = _fetch_content(conn, {
        "item_number": c_num, "item_type": c_type, "repo": c_repo
    })

    if not q_content or not c_content:
        continue

    # Score with cross-encoder
    scores = reranker._model.predict([(q_content, c_content)], batch_size=1)
    rerank_score = float(scores[0])

    multiplier = STATE_MULT.get(c_state, 1.0)
    value_score = rerank_score * multiplier

    # Update scan_results
    conn.execute("""
        UPDATE scan_results
        SET rerank_score = ?, value_score = ?, scanned_at = ?
        WHERE query_number = ? AND query_type = ? AND query_repo = ?
        AND candidate_number = ? AND candidate_type = ? AND candidate_repo = ?
    """, (rerank_score, value_score, now,
          q_num, q_type, q_repo, c_num, c_type, c_repo))
    conn.commit()

print("Done. Purging low-score results...", flush=True)
conn.execute("DELETE FROM scan_results WHERE value_score < 0.06")
conn.commit()

remaining = conn.execute("SELECT COUNT(*) FROM scan_results").fetchone()[0]
print(f"{remaining} results remaining after threshold filter.", flush=True)
