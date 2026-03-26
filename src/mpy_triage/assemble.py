"""Stage 3: Assemble structured XML from raw data and optional Haiku summaries."""

import datetime
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass


@dataclass
class DiffFile:
    """A file entry parsed from a unified diff."""

    path: str
    additions: int
    deletions: int
    functions: list[str]


def parse_diff_files(diff_text: str) -> list[DiffFile]:
    """Parse unified diff to extract file paths, stats, and function names from @@ headers."""
    files: list[DiffFile] = []
    current: DiffFile | None = None

    for line in diff_text.splitlines():
        # New file header
        m = re.match(r"^diff --git a/.+ b/(.+)$", line)
        if m:
            if current is not None:
                files.append(current)
            current = DiffFile(
                path=m.group(1), additions=0, deletions=0, functions=[]
            )
            continue

        if current is None:
            continue

        # Hunk header with optional function name
        m = re.match(
            r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@(?: (.+))?$", line
        )
        if m:
            func_name = m.group(1)
            if func_name:
                func_name = func_name.strip()
                if func_name and func_name not in current.functions:
                    current.functions.append(func_name)
            continue

        # Count additions/deletions (exclude --- and +++ header lines)
        if line.startswith("---") or line.startswith("+++"):
            continue
        if line.startswith("+"):
            current.additions += 1
        elif line.startswith("-"):
            current.deletions += 1

    if current is not None:
        files.append(current)

    return files


def _cdata_wrap(text: str | None) -> str:
    """Wrap text in a CDATA section, handling the ]]> edge case."""
    if text is None:
        text = ""
    escaped = text.replace("]]>", "]]]]><![CDATA[>")
    return f"<![CDATA[{escaped}]]>"


def _parse_json_list(raw: str | None) -> list[str]:
    """Parse a JSON array string, returning empty list on failure."""
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []


def assemble_item(
    conn: sqlite3.Connection,
    repo: str,
    item_number: int,
    item_type: str,
) -> str:
    """Build XML for a single item. Works with or without Haiku summary."""
    if item_type == "issue":
        table = "issues"
        tag = "issue"
    else:
        table = "pull_requests"
        tag = "pull_request"
    assert table in ("issues", "pull_requests")

    row = conn.execute(
        f"SELECT * FROM {table} WHERE repo = ? AND number = ?",
        (repo, item_number),
    ).fetchone()
    if row is None:
        raise ValueError(
            f"{item_type} #{item_number} not found in {repo}"
        )

    title = row["title"] or ""
    body = row["body"] or ""
    labels_str = ", ".join(_parse_json_list(row["labels"]))

    parts = [
        f'<{tag} number="{item_number}" repo="{repo}">',
        f"<title>{_cdata_wrap(title)}</title>",
        f"<description>{_cdata_wrap(body)}</description>",
        f"<labels>{labels_str}</labels>",
    ]

    # For PRs, include diff_files section
    if item_type == "pull_request":
        diff_row = conn.execute(
            "SELECT diff_text FROM pr_diffs"
            " WHERE repo = ? AND pr_number = ?",
            (repo, item_number),
        ).fetchone()
        if diff_row and diff_row["diff_text"]:
            diff_files = parse_diff_files(diff_row["diff_text"])
            if diff_files:
                parts.append("<diff_files>")
                for df in diff_files:
                    funcs_str = ", ".join(df.functions)
                    parts.append(
                        f'<file path="{df.path}"'
                        f' additions="{df.additions}"'
                        f' deletions="{df.deletions}">'
                    )
                    parts.append(
                        f"<functions>{funcs_str}</functions>"
                    )
                    parts.append("</file>")
                parts.append("</diff_files>")

    # Check for Haiku summary
    summary_row = conn.execute(
        "SELECT * FROM summaries"
        " WHERE repo = ? AND item_number = ? AND item_type = ?",
        (repo, item_number, item_type),
    ).fetchone()
    if summary_row:
        components = _parse_json_list(summary_row["components"])
        affected = _parse_json_list(summary_row["affected_code"])
        concepts = _parse_json_list(summary_row["concepts"])

        parts.append("<summary>")
        parts.append(
            f"<components>{', '.join(components)}</components>"
        )
        parts.append(
            f"<type>{summary_row['item_category'] or ''}</type>"
        )
        parts.append(
            f"<synopsis>{summary_row['synopsis'] or ''}</synopsis>"
        )
        parts.append(
            f"<affected_code>{', '.join(affected)}</affected_code>"
        )
        parts.append(
            "<error_signatures>"
            f"{summary_row['error_signatures'] or ''}"
            "</error_signatures>"
        )
        parts.append(
            f"<concepts>{', '.join(concepts)}</concepts>"
        )
        parts.append("</summary>")

    parts.append(f"</{tag}>")
    return "\n".join(parts)


def assemble_all(conn: sqlite3.Connection, repo: str) -> int:
    """Assemble XML for all items. Skip unchanged (by hash). Return count."""
    count = 0

    issue_rows = conn.execute(
        "SELECT number FROM issues WHERE repo = ?", (repo,)
    ).fetchall()
    for row in issue_rows:
        if _assemble_and_store(conn, repo, row["number"], "issue"):
            count += 1

    pr_rows = conn.execute(
        "SELECT number FROM pull_requests WHERE repo = ?", (repo,)
    ).fetchall()
    for row in pr_rows:
        if _assemble_and_store(
            conn, repo, row["number"], "pull_request"
        ):
            count += 1

    return count


def _assemble_and_store(
    conn: sqlite3.Connection,
    repo: str,
    item_number: int,
    item_type: str,
) -> bool:
    """Assemble XML for one item, store if changed. Return True if new/updated."""
    xml_text = assemble_item(conn, repo, item_number, item_type)
    xml_hash = hashlib.sha256(xml_text.encode("utf-8")).hexdigest()

    existing = conn.execute(
        "SELECT xml_hash FROM assembled_xml"
        " WHERE repo = ? AND item_number = ? AND item_type = ?",
        (repo, item_number, item_type),
    ).fetchone()

    if existing and existing["xml_hash"] == xml_hash:
        return False

    has_summary = 1 if "<summary>" in xml_text else 0
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    conn.execute(
        "INSERT OR REPLACE INTO assembled_xml"
        " (item_number, item_type, repo, xml_text, xml_hash,"
        " has_summary, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            item_number,
            item_type,
            repo,
            xml_text,
            xml_hash,
            has_summary,
            now,
        ),
    )
    conn.commit()
    return True
