"""Export scan results as CSV, Markdown, and HTML."""

import csv
import io
import logging
import sqlite3

from .format import github_url
from .render import _CLASSIFICATION_ORDER, sort_pairs  # noqa: F401 re-export

logger = logging.getLogger(__name__)


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _fetch_scan_results(
    conn: sqlite3.Connection, exclude_unrelated: bool = True
) -> list[dict]:
    """Fetch scan results with titles and assessment data in a single query.

    Joins scan_results with issues/pull_requests for titles and state,
    and with the assessment tables (preferring Sonnet over Qwen).
    """
    has_sonnet = _has_table(conn, "scan_assessments_sonnet")

    # Build a single query that joins everything we need.
    # Query-side title/state comes from a UNION of issues + pull_requests
    # keyed by (number, type, repo). Same for the candidate side.
    sql = """
        SELECT
            sr.query_number, sr.query_type, sr.query_repo,
            sr.candidate_number, sr.candidate_type, sr.candidate_repo,
            sr.candidate_state, sr.rerank_score, sr.value_score,
            qi.title  AS q_title,
            qi.state  AS q_state,
            ci.title  AS c_title,
    """
    if has_sonnet:
        sql += """
            COALESCE(ss.classification, sa.classification) AS classification,
            COALESCE(ss.confidence,     sa.confidence)     AS confidence,
            COALESCE(ss.reasoning,      sa.reasoning)      AS reasoning,
            COALESCE(ss.suggested_action, sa.suggested_action) AS suggested_action,
            CASE WHEN ss.classification IS NOT NULL THEN 'sonnet'
                 WHEN sa.classification IS NOT NULL THEN 'qwen'
                 ELSE '' END AS assessment_source
        """
    else:
        sql += """
            sa.classification,
            sa.confidence,
            sa.reasoning,
            sa.suggested_action,
            CASE WHEN sa.classification IS NOT NULL THEN 'qwen'
                 ELSE '' END AS assessment_source
        """
    sql += """
        FROM scan_results sr
        -- query item title + state
        LEFT JOIN (
            SELECT number, 'issue' AS item_type, repo, title, state
              FROM issues
            UNION ALL
            SELECT number, 'pull_request' AS item_type, repo, title, state
              FROM pull_requests
        ) qi ON qi.number = sr.query_number
            AND qi.item_type = sr.query_type
            AND qi.repo = sr.query_repo
        -- candidate item title
        LEFT JOIN (
            SELECT number, 'issue' AS item_type, repo, title, state
              FROM issues
            UNION ALL
            SELECT number, 'pull_request' AS item_type, repo, title, state
              FROM pull_requests
        ) ci ON ci.number = sr.candidate_number
            AND ci.item_type = sr.candidate_type
            AND ci.repo = sr.candidate_repo
        -- Qwen assessment (always present)
        LEFT JOIN scan_assessments sa
            ON sa.query_number = sr.query_number
            AND sa.query_type = sr.query_type
            AND sa.query_repo = sr.query_repo
            AND sa.candidate_number = sr.candidate_number
            AND sa.candidate_type = sr.candidate_type
            AND sa.candidate_repo = sr.candidate_repo
    """
    if has_sonnet:
        sql += """
        -- Sonnet assessment (preferred when present)
        LEFT JOIN scan_assessments_sonnet ss
            ON ss.query_number = sr.query_number
            AND ss.query_type = sr.query_type
            AND ss.query_repo = sr.query_repo
            AND ss.candidate_number = sr.candidate_number
            AND ss.candidate_type = sr.candidate_type
            AND ss.candidate_repo = sr.candidate_repo
        """
    sql += "ORDER BY sr.value_score DESC"

    rows = conn.execute(sql).fetchall()

    results = []
    for r in rows:
        cls = r["classification"] or ""
        if exclude_unrelated and cls == "UNRELATED":
            continue
        results.append({
            "query_number": r["query_number"],
            "query_type": r["query_type"],
            "query_repo": r["query_repo"],
            "query_title": r["q_title"] or "",
            "query_url": github_url(r["query_repo"], r["query_type"], r["query_number"]),
            "query_state": r["q_state"] or "unknown",
            "candidate_number": r["candidate_number"],
            "candidate_type": r["candidate_type"],
            "candidate_repo": r["candidate_repo"],
            "candidate_title": r["c_title"] or "",
            "candidate_url": github_url(
                r["candidate_repo"], r["candidate_type"], r["candidate_number"]
            ),
            "candidate_state": r["candidate_state"] or "",
            "rerank_score": r["rerank_score"],
            "value_score": r["value_score"],
            "classification": cls,
            "confidence": r["confidence"] or "",
            "reasoning": r["reasoning"] or "",
            "suggested_action": r["suggested_action"] or "",
            "assessment_source": r["assessment_source"] or "",
        })

    return results


def export_csv(conn: sqlite3.Connection) -> str:
    """Export scan results as CSV string, ordered by classification priority."""
    results = _fetch_scan_results(conn)
    if not results:
        return ""

    sort_pairs(results)

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
