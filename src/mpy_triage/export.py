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
            "assessment_source": assessment.get("assessment_source", ""),
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
        "UNRELATED": [],
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
        "UNRELATED": "Unrelated (false positives)",
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

    for cls in ["DUPLICATE", "LIKELY_DUPLICATE", "RELATED", "UNRELATED", "OFF_TOPIC", ""]:
        _table(group_titles.get(cls, cls), groups.get(cls, []))

    return "\n".join(lines)


_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>mpy-triage Scan Results</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica,
        Arial, sans-serif; margin: 2em; color: #24292f; }}
h1 {{ font-size: 1.5em; }}
h2 {{ font-size: 1.2em; margin-top: 2em; border-bottom: 1px solid #d0d7de;
      padding-bottom: 0.3em; }}
.stats {{ color: #57606a; margin-bottom: 1.5em; }}
table {{ border-collapse: collapse; width: 100%; font-size: 0.9em; }}
th, td {{ border: 1px solid #d0d7de; padding: 6px 10px; text-align: left;
          vertical-align: top; }}
th {{ background: #f6f8fa; position: sticky; top: 0; cursor: pointer; }}
th:hover {{ background: #eaeef2; }}
td.score {{ text-align: right; font-variant-numeric: tabular-nums; }}
a {{ color: #0969da; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
.cls {{ font-weight: 600; font-size: 0.85em; padding: 2px 6px;
        border-radius: 3px; white-space: nowrap; }}
.cls-DUPLICATE {{ background: #dafbe1; color: #116329; }}
.cls-LIKELY_DUPLICATE {{ background: #fff8c5; color: #6a5300; }}
.cls-RELATED {{ background: #ddf4ff; color: #0550ae; }}
.cls-UNRELATED {{ background: #f6f8fa; color: #57606a; }}
.cls-OFF_TOPIC {{ background: #ffebe9; color: #82071e; }}
.cls- {{ background: #f6f8fa; color: #8b949e; }}
.src {{ display: inline-block; font-size: 0.7em; font-weight: 700;
        padding: 1px 5px; border-radius: 3px; margin-left: 4px;
        vertical-align: middle; }}
.src-sonnet {{ background: #d7aefb; color: #24292f; }}
.src-qwen {{ background: #a5d6a7; color: #24292f; }}
details {{ cursor: pointer; max-width: 400px; }}
details summary {{ white-space: nowrap; overflow: hidden;
                   text-overflow: ellipsis; }}
</style>
</head>
<body>
<h1>mpy-triage Scan Results</h1>
<div class="stats">{stats}</div>
{sections}
<script>
document.querySelectorAll("th").forEach(th => {{
  th.addEventListener("click", () => {{
    const table = th.closest("table");
    const idx = Array.from(th.parentNode.children).indexOf(th);
    const tbody = table.querySelector("tbody");
    const rows = Array.from(tbody.querySelectorAll("tr"));
    const dir = th.dataset.dir === "asc" ? "desc" : "asc";
    th.dataset.dir = dir;
    rows.sort((a, b) => {{
      let av = a.children[idx].dataset.sort || a.children[idx].textContent;
      let bv = b.children[idx].dataset.sort || b.children[idx].textContent;
      let na = parseFloat(av), nb = parseFloat(bv);
      if (!isNaN(na) && !isNaN(nb)) return dir === "asc" ? na - nb : nb - na;
      return dir === "asc" ? av.localeCompare(bv) : bv.localeCompare(av);
    }});
    rows.forEach(r => tbody.appendChild(r));
  }});
}});
</script>
</body>
</html>
"""


def _html_escape(text: str) -> str:
    """Escape HTML special characters."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _html_section(title: str, items: list[dict]) -> str:
    """Build one HTML table section."""
    if not items:
        return ""

    rows_html = []
    for r in items:
        q_title = _html_escape(r["query_title"])
        c_title = _html_escape(r["candidate_title"])
        kind = "PR" if r["candidate_type"] == "pull_request" else "Issue"
        cls = r["classification"] or ""
        cls_label = cls or "pending"
        reasoning = _html_escape(r["reasoning"]) if r["reasoning"] else ""
        action = _html_escape(r["suggested_action"]) if r["suggested_action"] else ""

        reasoning_cell = ""
        if reasoning:
            short = reasoning[:60] + "..." if len(reasoning) > 60 else reasoning
            reasoning_cell = (
                f"<details><summary>{short}</summary>"
                f"<p>{reasoning}</p>"
                f"{'<p><b>Action:</b> ' + action + '</p>' if action else ''}"
                f"</details>"
            )
        else:
            reasoning_cell = "-"

        source = r.get("assessment_source", "")
        source_label = {
            "sonnet": '<span class="src src-sonnet">S</span>',
            "qwen": '<span class="src src-qwen">Q</span>',
        }.get(source, "")

        rows_html.append(
            f"<tr>"
            f'<td><a href="{r["query_url"]}">#{r["query_number"]}</a> {q_title}</td>'
            f'<td><a href="{r["candidate_url"]}">{kind} #{r["candidate_number"]}</a>'
            f" {c_title}</td>"
            f'<td>{r["candidate_state"]}</td>'
            f'<td class="score" data-sort="{r["value_score"]:.4f}">'
            f'{r["value_score"]:.3f}</td>'
            f'<td><span class="cls cls-{cls}">{cls_label}</span> {source_label}</td>'
            f"<td>{reasoning_cell}</td>"
            f"</tr>"
        )

    return (
        f"<h2>{_html_escape(title)} ({len(items)})</h2>\n"
        f"<table>\n<thead><tr>"
        f"<th>Issue</th><th>Match</th><th>State</th>"
        f"<th>Score</th><th>Classification</th><th>Reasoning</th>"
        f"</tr></thead>\n<tbody>\n"
        + "\n".join(rows_html)
        + "\n</tbody></table>\n"
    )


def export_html(conn: sqlite3.Connection) -> str:
    """Export scan results as a self-contained HTML page."""
    results = _fetch_scan_results(conn)
    if not results:
        return "<html><body><p>No scan results.</p></body></html>"

    assessed = sum(1 for r in results if r["classification"])
    sonnet_n = sum(1 for r in results if r.get("assessment_source") == "sonnet")
    qwen_n = sum(1 for r in results if r.get("assessment_source") == "qwen")
    total = len(results)
    unique_issues = len(set(r["query_number"] for r in results))

    stats = (
        f"<b>{total}</b> matches across <b>{unique_issues}</b> open issues "
        f"&mdash; <b>{assessed}/{total}</b> assessed "
        f'(<span class="src src-sonnet">S</span> Sonnet: <b>{sonnet_n}</b>, '
        f'<span class="src src-qwen">Q</span> Qwen: <b>{qwen_n}</b>)'
    )

    groups = {}
    for r in results:
        cls = r["classification"]
        groups.setdefault(cls, []).append(r)

    group_titles = {
        "DUPLICATE": "Duplicates — issues to close",
        "LIKELY_DUPLICATE": "Likely Duplicates — need confirmation",
        "RELATED": "Related",
        "UNRELATED": "Unrelated (false positives)",
        "OFF_TOPIC": "Off-topic (spam / wrong repo)",
        "": "Pending assessment",
    }

    sections = ""
    for cls in ["DUPLICATE", "LIKELY_DUPLICATE", "RELATED", "UNRELATED", "OFF_TOPIC", ""]:
        sections += _html_section(
            group_titles.get(cls, cls), groups.get(cls, [])
        )

    return _HTML_TEMPLATE.format(stats=stats, sections=sections)
