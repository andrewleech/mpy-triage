"""Batch scan for undetected related/duplicate items across the corpus."""

import logging
import sqlite3
from datetime import datetime, timezone

from .config import get_config
from .embed import Embedder
from .format import github_url
from .search import Reranker, search

logger = logging.getLogger(__name__)

# State multipliers for value scoring.
# Higher = more valuable to surface.
_STATE_MULTIPLIERS = {
    "merged": 2.0,    # Open issue → merged PR (likely already fixed)
    "open": 1.5,      # Open issue → open issue (potential duplicate)
    "closed": 1.0,    # Open issue → closed item
}


def _get_candidate_state(
    conn: sqlite3.Connection, number: int, item_type: str, repo: str
) -> str:
    """Look up the state of a candidate item."""
    table = "pull_requests" if item_type == "pull_request" else "issues"
    row = conn.execute(
        f"SELECT state FROM {table} WHERE repo = ? AND number = ?",
        (repo, number),
    ).fetchone()
    return row["state"] if row else "unknown"


def _get_title(
    conn: sqlite3.Connection, number: int, item_type: str, repo: str
) -> str:
    """Look up the title of an item."""
    table = "pull_requests" if item_type == "pull_request" else "issues"
    row = conn.execute(
        f"SELECT title FROM {table} WHERE repo = ? AND number = ?",
        (repo, number),
    ).fetchone()
    return row["title"] if row else ""


def _top_k_per_type(candidates: list[dict], top_k: int) -> list[dict]:
    """Select top_k candidates per item_type (issue vs pull_request).

    This prevents merged PRs (with their 2x value multiplier) from
    crowding out issue-to-issue duplicate matches.
    """
    buckets: dict[str, list[dict]] = {}
    for c in candidates:
        buckets.setdefault(c["item_type"], []).append(c)
    result = []
    for items in buckets.values():
        result.extend(items[:top_k])
    return result


def scan_open_issues(
    conn: sqlite3.Connection,
    embedder: Embedder,
    reranker: Reranker | None = None,
    *,
    repo: str,
    min_score: float = 0.01,
    top_k: int = 3,
    skip_rerank: bool = False,
) -> list[dict]:
    """Search for matches for every open issue. Returns ranked discoveries.

    Keeps top_k candidates per candidate type (issues and PRs separately)
    so that high-scoring PRs don't crowd out issue-to-issue duplicates.
    """
    try:
        from tqdm import tqdm
    except ImportError:
        tqdm = None

    config = get_config().retrieval

    open_issues = conn.execute(
        "SELECT number, title, body FROM issues WHERE repo = ? AND state = 'open'",
        (repo,),
    ).fetchall()

    logger.info("Scanning %d open issues in %s", len(open_issues), repo)

    if reranker is None and not skip_rerank:
        reranker = Reranker(config.reranker_model)

    now = datetime.now(timezone.utc).isoformat()
    all_results = []

    iterator = tqdm(open_issues, desc="Scanning") if tqdm else open_issues
    for issue_row in iterator:
        number = issue_row["number"]
        title = issue_row["title"] or ""
        body = issue_row["body"] or ""
        query_text = f"{title}\n{body[:2000]}"

        candidates = search(
            conn, query_text, embedder,
            config=config,
            exclude=(number, repo),
            reranker=reranker,
            skip_rerank=skip_rerank,
        )

        selected = _top_k_per_type(candidates, top_k)

        for c in selected:
            c_number = c["item_number"]
            c_type = c["item_type"]
            c_repo = c["repo"]
            c_state = _get_candidate_state(conn, c_number, c_type, c_repo)

            score = c.get("rerank_score") or c.get("rrf_score", 0)
            multiplier = _STATE_MULTIPLIERS.get(c_state, 1.0)
            value_score = score * multiplier

            if value_score < min_score:
                continue

            conn.execute(
                "INSERT OR REPLACE INTO scan_results "
                "(query_number, query_type, query_repo, "
                "candidate_number, candidate_type, candidate_repo, "
                "candidate_state, rerank_score, value_score, scanned_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    number, "issue", repo,
                    c_number, c_type, c_repo,
                    c_state, score, value_score, now,
                ),
            )

            all_results.append({
                "query_number": number,
                "query_title": title,
                "query_repo": repo,
                "candidate_number": c_number,
                "candidate_type": c_type,
                "candidate_repo": c_repo,
                "candidate_state": c_state,
                "candidate_title": _get_title(conn, c_number, c_type, c_repo),
                "rerank_score": score,
                "value_score": value_score,
            })

        conn.commit()

    all_results.sort(key=lambda r: -r["value_score"])
    logger.info("Scan complete: %d total discoveries", len(all_results))
    return all_results


def format_scan_report(results: list[dict], top_n: int = 50) -> str:
    """Format scan results as a human-readable report."""
    if not results:
        return "No discoveries."

    # Group by category
    open_to_merged = [r for r in results if r["candidate_state"] == "merged"]
    open_to_open = [
        r for r in results
        if r["candidate_state"] == "open" and r["candidate_type"] == "issue"
    ]
    other = [
        r for r in results
        if r not in open_to_merged and r not in open_to_open
    ]

    lines = [f"Scan Results: {len(results)} total discoveries", ""]

    def _format_group(title: str, items: list[dict], n: int) -> None:
        if not items:
            return
        lines.append(f"  {title} ({len(items)} total, showing top {min(n, len(items))}):")
        for r in items[:n]:
            q_url = github_url(r["query_repo"], "issue", r["query_number"])
            c_kind = "PR" if r["candidate_type"] == "pull_request" else "issue"
            c_url = github_url(
                r["candidate_repo"], r["candidate_type"], r["candidate_number"]
            )
            lines.append(
                f"    #{r['query_number']} -> {c_kind} #{r['candidate_number']}"
                f" [value: {r['value_score']:.3f}]"
            )
            lines.append(f"      Issue: {r['query_title']}")
            lines.append(f"        {q_url}")
            lines.append(f"      Match: {r['candidate_title']}")
            lines.append(f"        {c_url}")
            lines.append("")

    _format_group("Open issues -> Merged PRs (likely already fixed)", open_to_merged, top_n)
    _format_group("Open issues -> Open issues (potential duplicates)", open_to_open, top_n)
    _format_group("Other matches", other, top_n // 2)

    return "\n".join(lines)
