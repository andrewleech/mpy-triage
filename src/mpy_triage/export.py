"""Export scan results as CSV, Markdown, and HTML."""

import csv
import io
import logging
import sqlite3

from .format import github_url

logger = logging.getLogger(__name__)

_CLASSIFICATION_ORDER = {
    "DUPLICATE": 0,
    "LIKELY_DUPLICATE": 1,
    "RELATED": 2,
    "OFF_TOPIC": 3,
    "UNRELATED": 4,
    "": 5,
}


def _fetch_scan_results(
    conn: sqlite3.Connection, exclude_unrelated: bool = True
) -> list[dict]:
    """Fetch scan results with titles and assessment data.

    By default, filters out UNRELATED classifications since they represent
    search false positives with no actionable value.
    """
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
            "assessment_source": assessment.get("assessment_source", ""),
        })

    if exclude_unrelated:
        results = [r for r in results if r["classification"] != "UNRELATED"]

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
    """Fetch assessment for a scan result pair, preferring Sonnet over Qwen."""
    key = (q_num, q_type, q_repo, c_num, c_type, c_repo)
    where = (
        "WHERE query_number=? AND query_type=? AND query_repo=? "
        "AND candidate_number=? AND candidate_type=? AND candidate_repo=?"
    )
    # Prefer Sonnet validation if available, fall back to Qwen first-pass
    for table, source in [
        ("scan_assessments_sonnet", "sonnet"),
        ("scan_assessments", "qwen"),
    ]:
        try:
            row = conn.execute(
                f"SELECT classification, confidence, reasoning, suggested_action "
                f"FROM {table} {where}",
                key,
            ).fetchone()
            if row:
                return {
                    "classification": row[0],
                    "confidence": row[1],
                    "reasoning": row[2],
                    "suggested_action": row[3],
                    "assessment_source": source,
                }
        except sqlite3.OperationalError:
            pass  # Table doesn't exist yet
    return {}


def export_csv(conn: sqlite3.Connection) -> str:
    """Export scan results as CSV string, ordered by classification priority."""
    results = _fetch_scan_results(conn)
    if not results:
        return ""

    results.sort(key=lambda r: (
        _CLASSIFICATION_ORDER.get(r["classification"], 5),
        -r["value_score"],
    ))

    output = io.StringIO()
    fields = [
        "query_number", "query_title", "query_url",
        "candidate_number", "candidate_type", "candidate_title",
        "candidate_url", "candidate_state", "value_score",
        "classification", "confidence", "assessment_source",
        "reasoning", "suggested_action",
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
    sonnet_n = sum(1 for r in results if r.get("assessment_source") == "sonnet")
    qwen_n = sum(1 for r in results if r.get("assessment_source") == "qwen")
    total = len(results)

    lines = [
        "# Scan Results",
        "",
        f"**{total} matches** across "
        f"**{len(set(r['query_number'] for r in results))} open issues**",
        f"**{assessed}/{total} assessed** — Sonnet: {sonnet_n}, Qwen: {qwen_n}",
        "",
    ]

    # Group by classification
    groups = {
        "DUPLICATE": [],
        "LIKELY_DUPLICATE": [],
        "RELATED": [],
        "OFF_TOPIC": [],
        "": [],
    }
    for r in results:
        cls = r["classification"]
        groups.setdefault(cls, []).append(r)

    group_titles = {
        "DUPLICATE": "Duplicates — issues to close",
        "LIKELY_DUPLICATE": "Likely Duplicates — need confirmation",
        "RELATED": "Related",
        "OFF_TOPIC": "Off-topic (spam / wrong repo)",
        "": "Pending assessment",
    }

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
            issue_link = (
                f"[#{r['query_number']}]({r['query_url']}) {r['query_title']}"
            )
            kind = "PR" if r["candidate_type"] == "pull_request" else "Issue"
            match_link = (
                f"[{kind} #{r['candidate_number']}]({r['candidate_url']})"
                f" {r['candidate_title']}"
            )
            cls = r["classification"] or "-"
            reasoning = (
                r["reasoning"][:80] + "..."
                if len(r["reasoning"]) > 80
                else r["reasoning"]
            )
            reasoning = reasoning or "-"
            lines.append(
                f"| {issue_link} | {match_link} | {r['candidate_state']}"
                f" | {r['value_score']:.3f} | {cls} | {reasoning} |"
            )
        lines.append("")

    for cls in ["DUPLICATE", "LIKELY_DUPLICATE", "RELATED", "OFF_TOPIC", ""]:
        _table(group_titles.get(cls, cls), groups.get(cls, []))

    return "\n".join(lines)


def export_html(conn: sqlite3.Connection) -> str:
    """Export scan results as a self-contained HTML index page."""
    from .render import render_index_html, sort_pairs

    results = _fetch_scan_results(conn)
    if not results:
        return "<html><body><p>No scan results.</p></body></html>"

    sort_pairs(results)
    return render_index_html(results, inline_css=True)
