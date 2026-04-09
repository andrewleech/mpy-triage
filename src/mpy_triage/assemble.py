"""Stage 3: Assemble structured XML from raw data and optional Haiku summaries."""

import datetime
import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Target budget for assembled XML. Prevents GPU OOM during embedding
# by keeping input text within ~2K tokens for the embedding model.
MAX_XML_CHARS = 4000


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


def _build_diff_section(diff_files: list[DiffFile], level: int) -> str:
    """Build diff_files XML at a given detail level.

    Level 0: Full (path + stats + functions)
    Level 1: Path + stats, no functions
    Level 2: Path only
    Level 3: Unique directory prefixes only
    Level 4: Empty (drop diff_files entirely)
    """
    if level >= 4 or not diff_files:
        return ""

    lines = ["<diff_files>"]

    if level == 3:
        dirs = sorted({
            df.path.rsplit("/", 1)[0] if "/" in df.path else "."
            for df in diff_files
        })
        lines.append(f"<directories>{', '.join(dirs)}</directories>")
    else:
        for df in diff_files:
            if level == 0:
                funcs = ", ".join(df.functions)
                lines.append(
                    f'<file path="{df.path}"'
                    f' additions="{df.additions}"'
                    f' deletions="{df.deletions}">'
                )
                lines.append(f"<functions>{funcs}</functions>")
                lines.append("</file>")
            elif level == 1:
                lines.append(
                    f'<file path="{df.path}"'
                    f' additions="{df.additions}"'
                    f' deletions="{df.deletions}"/>'
                )
            elif level == 2:
                lines.append(f'<file path="{df.path}"/>')

    lines.append("</diff_files>")
    return "\n".join(lines)


def _build_summary_section(
    conn: sqlite3.Connection, repo: str, item_number: int, item_type: str
) -> str:
    """Build the <summary> section from Haiku output, or empty string."""
    summary_row = conn.execute(
        "SELECT * FROM summaries"
        " WHERE repo = ? AND item_number = ? AND item_type = ?",
        (repo, item_number, item_type),
    ).fetchone()
    if not summary_row:
        return ""

    components = _parse_json_list(summary_row["components"])
    affected = _parse_json_list(summary_row["affected_code"])
    concepts = _parse_json_list(summary_row["concepts"])

    lines = [
        "<summary>",
        f"<components>{', '.join(components)}</components>",
        f"<type>{summary_row['item_category'] or ''}</type>",
        f"<synopsis>{summary_row['synopsis'] or ''}</synopsis>",
        f"<affected_code>{', '.join(affected)}</affected_code>",
        f"<error_signatures>{summary_row['error_signatures'] or ''}</error_signatures>",
        f"<concepts>{', '.join(concepts)}</concepts>",
        "</summary>",
    ]
    return "\n".join(lines)


def assemble_item(
    conn: sqlite3.Connection,
    repo: str,
    item_number: int,
    item_type: str,
) -> str:
    """Build XML for a single item, kept within MAX_XML_CHARS budget.

    Priority: title + labels + summary always full.
    diff_files progressively reduced. Description truncated last.
    """
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

    # Fixed parts: always included at full fidelity
    tag_open = f'<{tag} number="{item_number}" repo="{repo}">'
    title_xml = f"<title>{_cdata_wrap(title)}</title>"
    labels_xml = f"<labels>{labels_str}</labels>"
    summary_xml = _build_summary_section(conn, repo, item_number, item_type)
    tag_close = f"</{tag}>"

    fixed = "\n".join(
        p for p in [tag_open, title_xml, labels_xml, summary_xml, tag_close] if p
    )
    fixed_size = len(fixed)

    # Parse diff files for PRs
    diff_files: list[DiffFile] = []
    if item_type == "pull_request":
        diff_row = conn.execute(
            "SELECT diff_text FROM pr_diffs WHERE repo = ? AND pr_number = ?",
            (repo, item_number),
        ).fetchone()
        if diff_row and diff_row["diff_text"]:
            diff_files = parse_diff_files(diff_row["diff_text"])

    remaining = MAX_XML_CHARS - fixed_size

    # Find the most detailed diff level that fits alongside description
    diff_xml = ""
    desc_xml = f"<description>{_cdata_wrap(body)}</description>"

    if diff_files:
        for level in range(5):
            candidate_diff = _build_diff_section(diff_files, level)
            if len(candidate_diff) + len(desc_xml) <= remaining:
                diff_xml = candidate_diff
                break
            # If diff at this level + full description doesn't fit,
            # try truncating description with this diff level
            diff_size = len(candidate_diff)
            desc_budget = remaining - diff_size - len("<description></description>") - 20
            if desc_budget > 200:
                truncated_body = body[:desc_budget]
                desc_xml = f"<description>{_cdata_wrap(truncated_body)}</description>"
                diff_xml = candidate_diff
                break
        else:
            # All diff levels exhausted, drop diff entirely
            diff_xml = ""
    elif len(desc_xml) > remaining:
        # No diff, but description alone exceeds budget
        desc_budget = remaining - len("<description></description>") - 20
        truncated_body = body[:max(200, desc_budget)]
        desc_xml = f"<description>{_cdata_wrap(truncated_body)}</description>"

    # Assemble final XML
    parts = [tag_open, title_xml, desc_xml, labels_xml]
    if diff_xml:
        parts.append(diff_xml)
    if summary_xml:
        parts.append(summary_xml)
    parts.append(tag_close)
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
    """Assemble XML for one item, store if changed. Return True if new/updated.

    When XML changes, stale embeddings in vec_items and item_fts are deleted
    so index_all will re-embed the item.
    """
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

    # Delete stale embeddings so index_all re-embeds this item.
    if existing:
        try:
            conn.execute(
                "DELETE FROM vec_items"
                " WHERE item_number = ? AND item_type = ? AND repo = ?",
                (item_number, item_type, repo),
            )
            conn.execute(
                "DELETE FROM item_fts"
                " WHERE item_number = ? AND item_type = ? AND repo = ?",
                (str(item_number), item_type, repo),
            )
        except Exception:
            pass  # Tables may not exist yet

    conn.commit()
    return True
