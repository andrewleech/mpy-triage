"""Export scan results as CSV and Markdown."""

import csv
import io
import logging
import sqlite3

from .format import github_url

logger = logging.getLogger(__name__)


def _fetch_scan_results(conn: sqlite3.Connection) -> list[dict]:
    """Fetch all scan results with titles and assessment data if available."""
    rows = conn.execute("""
        SELECT sr.query_number, sr.query_type, sr.query_repo,
               sr.candidate_number, sr.candidate_type, sr.candidate_repo,
               sr.candidate_state, sr.rerank_score, sr.value_score
        FROM scan_results sr
        ORDER BY sr.value_score DESC
    """).fetchall()

    results = []
    for r in rows:
        q_num, q_type, q_repo = r[0], r[1], r[2]
        c_num, c_type, c_repo = r[3], r[4], r[5]

        # Fetch titles
        q_title = _get_title(conn, q_num, q_type, q_repo)
        c_title = _get_title(conn, c_num, c_type, c_repo)

        # Check for Sonnet assessment in scan_assessments if it exists
        assessment = _get_assessment(conn, q_num, q_type, q_repo, c_num, c_type, c_repo)

        results.append({
            "query_number": q_num,
            "query_type": q_type,
            "query_repo": q_repo,
            "query_title": q_title,
            "query_url": github_url(q_repo, q_type, q_num),
            "candidate_number": c_num,
            "candidate_type": c_type,
            "candidate_repo": c_repo,
            "candidate_title": c_title,
            "candidate_url": github_url(c_repo, c_type, c_num),
            "candidate_state": r[6],
            "rerank_score": r[7],
            "value_score": r[8],
            "classification": assessment.get("classification", ""),
            "confidence": assessment.get("confidence", ""),
            "reasoning": assessment.get("reasoning", ""),
            "suggested_action": assessment.get("suggested_action", ""),
        })

    return results


def _get_title(conn: sqlite3.Connection, number: int, item_type: str, repo: str) -> str:
    table = "pull_requests" if item_type == "pull_request" else "issues"
    row = conn.execute(
        f"SELECT title FROM {table} WHERE number = ? AND repo = ?",
        (number, repo),
    ).fetchone()
    return row[0] if row else ""


def _get_assessment(
    conn: sqlite3.Connection,
    q_num: int, q_type: str, q_repo: str,
    c_num: int, c_type: str, c_repo: str,
) -> dict:
    """Fetch assessment for a scan result pair, if it exists."""
    try:
        row = conn.execute(
            "SELECT classification, confidence, reasoning, suggested_action "
            "FROM scan_assessments "
            "WHERE query_number=? AND query_type=? AND query_repo=? "
            "AND candidate_number=? AND candidate_type=? AND candidate_repo=?",
            (q_num, q_type, q_repo, c_num, c_type, c_repo),
        ).fetchone()
        if row:
            return {
                "classification": row[0],
                "confidence": row[1],
                "reasoning": row[2],
                "suggested_action": row[3],
            }
    except sqlite3.OperationalError:
        pass  # Table doesn't exist yet
    return {}


def export_csv(conn: sqlite3.Connection) -> str:
    """Export scan results as CSV string."""
    results = _fetch_scan_results(conn)
    if not results:
        return ""

    output = io.StringIO()
    fields = [
        "query_number", "query_title", "query_url",
        "candidate_number", "candidate_type", "candidate_title",
        "candidate_url", "candidate_state", "value_score",
        "classification", "confidence", "reasoning", "suggested_action",
    ]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(results)
    return output.getvalue()


def export_markdown(conn: sqlite3.Connection) -> str:
    """Export scan results as Markdown tables grouped by category."""
    results = _fetch_scan_results(conn)
    if not results:
        return "No scan results.\n"

    assessed = sum(1 for r in results if r["classification"])
    total = len(results)

    lines = [
        "# Scan Results",
        "",
        f"**{total} matches** across "
        f"**{len(set(r['query_number'] for r in results))} open issues**",
        f"**{assessed}/{total} assessed** by Sonnet",
        "",
    ]

    # Group by category
    merged_prs = [r for r in results if r["candidate_state"] == "merged"]
    open_issues = [
        r for r in results
        if r["candidate_state"] == "open" and r["candidate_type"] == "issue"
    ]
    other = [
        r for r in results
        if r not in merged_prs and r not in open_issues
    ]

    def _table(title: str, items: list[dict]) -> None:
        if not items:
            return
        lines.append(f"## {title} ({len(items)})")
        lines.append("")
        lines.append(
            "| Issue | Match | State | Score | Classification | Reasoning |"
        )
        lines.append(
            "|-------|-------|-------|------:|----------------|-----------|"
        )
        for r in items:
            issue_link = f"[#{r['query_number']}]({r['query_url']}) {r['query_title']}"
            kind = "PR" if r["candidate_type"] == "pull_request" else "Issue"
            match_link = (
                f"[{kind} #{r['candidate_number']}]({r['candidate_url']})"
                f" {r['candidate_title']}"
            )
            cls = r["classification"] or "-"
            reasoning = r["reasoning"][:80] + "..." if len(r["reasoning"]) > 80 else r["reasoning"]
            reasoning = reasoning or "-"
            lines.append(
                f"| {issue_link} | {match_link} | {r['candidate_state']}"
                f" | {r['value_score']:.3f} | {cls} | {reasoning} |"
            )
        lines.append("")

    _table("Open Issues matched to Merged PRs (likely already fixed)", merged_prs)
    _table("Open Issues matched to Open Issues (potential duplicates)", open_issues)
    _table("Other matches", other)

    return "\n".join(lines)
