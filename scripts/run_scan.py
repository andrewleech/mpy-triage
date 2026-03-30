#!/usr/bin/env python3
"""Run a fast scan (no reranker) of all open issues."""

import json
import sys

sys.path.insert(0, "src")

from mpy_triage.config import get_config
from mpy_triage.db import get_connection, init_db, load_vec_extension
from mpy_triage.embed import Embedder
from mpy_triage.scan import format_scan_report, scan_open_issues

config = get_config()
conn = get_connection(config.db_path)
init_db(conn, config.schema_path)
load_vec_extension(conn)

embedder = Embedder(config.embedding)

print("Fast scan (RRF only, no rerank)...", flush=True)
results = scan_open_issues(
    conn, embedder, None,
    repo="micropython/micropython",
    min_score=0.005,
    top_k=5,
    skip_rerank=True,
)

report = format_scan_report(results, top_n=50)
print(report, flush=True)

with open("scan_report.json", "w") as f:
    json.dump(results, f, indent=2)
print(f"\nSaved {len(results)} results to scan_report.json", flush=True)
