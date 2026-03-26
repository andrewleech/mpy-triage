"""Stage 2: Haiku-based summarization of issues and PRs."""

import json
import logging
import sqlite3
import subprocess
from datetime import datetime, timezone

from .config import clean_env, get_config

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 300

_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "components": {"type": "array", "items": {"type": "string"}},
        "item_category": {
            "type": "string",
            "enum": [
                "bug_report",
                "feature_request",
                "refactor",
                "question",
                "ci_build",
                "documentation",
            ],
        },
        "synopsis": {"type": "string"},
        "affected_code": {"type": "array", "items": {"type": "string"}},
        "error_signatures": {"type": "string"},
        "concepts": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "components",
        "item_category",
        "synopsis",
        "affected_code",
        "error_signatures",
        "concepts",
    ],
}

_DIFF_TRUNCATE_LIMIT = 10000


def _get_json_schema() -> str:
    """Return the JSON schema string for Haiku structured output."""
    return json.dumps(_JSON_SCHEMA)


def _fetch_linked_item(
    conn: sqlite3.Connection, repo: str, number: int, item_type: str | None
) -> sqlite3.Row | None:
    """Fetch a linked item's title and body from issues or pull_requests."""
    if item_type == "issue":
        return conn.execute(
            "SELECT title, body FROM issues WHERE repo = ? AND number = ?",
            (repo, number),
        ).fetchone()
    elif item_type == "pull_request":
        return conn.execute(
            "SELECT title, body FROM pull_requests WHERE repo = ? AND number = ?",
            (repo, number),
        ).fetchone()
    # target_type unknown, try both tables
    row = conn.execute(
        "SELECT title, body FROM issues WHERE repo = ? AND number = ?",
        (repo, number),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT title, body FROM pull_requests WHERE repo = ? AND number = ?",
            (repo, number),
        ).fetchone()
    return row


def _build_context(
    conn: sqlite3.Connection, repo: str, item_number: int, item_type: str
) -> str:
    """Build the full context string for Haiku summarization.

    Fetches the item, its comments, review comments (for PRs), diff (for PRs),
    and linked items from cross_references.
    """
    parts = []

    # Fetch the item itself
    if item_type == "issue":
        row = conn.execute(
            "SELECT title, body, labels FROM issues WHERE repo = ? AND number = ?",
            (repo, item_number),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT title, body, labels FROM pull_requests WHERE repo = ? AND number = ?",
            (repo, item_number),
        ).fetchone()

    if row is None:
        return ""

    title, body, labels = row["title"], row["body"], row["labels"]
    parts.append(f"Title: {title}")
    if labels:
        parts.append(f"Labels: {labels}")
    if body:
        parts.append(f"Body:\n{body}")

    # Fetch discussion comments
    comments = conn.execute(
        "SELECT author, body FROM comments "
        "WHERE repo = ? AND item_number = ? AND item_type = ? "
        "ORDER BY created_at",
        (repo, item_number, item_type),
    ).fetchall()
    if comments:
        parts.append("\n--- Comments ---")
        for c in comments:
            parts.append(f"[{c['author']}]: {c['body']}")

    # PR-specific: review comments and diff
    if item_type == "pull_request":
        review_comments = conn.execute(
            "SELECT author, body, path, diff_hunk FROM review_comments "
            "WHERE repo = ? AND pr_number = ? ORDER BY created_at",
            (repo, item_number),
        ).fetchall()
        if review_comments:
            parts.append("\n--- Review Comments ---")
            for rc in review_comments:
                prefix = f"[{rc['author']}]"
                if rc["path"]:
                    prefix += f" ({rc['path']})"
                parts.append(f"{prefix}: {rc['body']}")

        diff_row = conn.execute(
            "SELECT diff_text FROM pr_diffs WHERE repo = ? AND pr_number = ?",
            (repo, item_number),
        ).fetchone()
        if diff_row and diff_row["diff_text"]:
            diff_text = diff_row["diff_text"]
            if len(diff_text) > _DIFF_TRUNCATE_LIMIT:
                diff_text = diff_text[:_DIFF_TRUNCATE_LIMIT] + "\n... [truncated]"
            parts.append(f"\n--- Diff ---\n{diff_text}")

    # Linked items from cross_references
    refs = conn.execute(
        "SELECT target_number, target_type, target_repo, relationship "
        "FROM cross_references "
        "WHERE source_number = ? AND source_repo = ?",
        (item_number, repo),
    ).fetchall()
    if refs:
        parts.append("\n--- Linked Items ---")
        for ref in refs:
            target_num = ref["target_number"]
            target_type = ref["target_type"]
            target_repo = ref["target_repo"]
            relationship = ref["relationship"]

            linked = _fetch_linked_item(conn, target_repo, target_num, target_type)

            label = f"[{relationship}] #{target_num}"
            if linked:
                linked_body = linked["body"] or ""
                if len(linked_body) > 500:
                    linked_body = linked_body[:500] + "..."
                parts.append(f"{label}: {linked['title']}\n{linked_body}")
            else:
                parts.append(f"{label}: (not in database)")

    return "\n".join(parts)


def summarize_item(
    conn: sqlite3.Connection,
    repo: str,
    item_number: int,
    item_type: str,
) -> dict | None:
    """Summarize a single item using claude --model haiku -p subprocess."""
    context = _build_context(conn, repo, item_number, item_type)
    if not context:
        logger.warning("No context found for %s #%d in %s", item_type, item_number, repo)
        return None

    config = get_config()
    prompt_path = config.prompts_dir / "summarize.txt"
    system_prompt = prompt_path.read_text()

    full_prompt = f"{system_prompt}\n\n--- Item ---\n{context}"
    schema_json = _get_json_schema()

    cmd = [
        "claude",
        "--model",
        "haiku",
        "-p",
        "--output-format",
        "json",
        "--json-schema",
        schema_json,
        "--no-session-persistence",
    ]

    logger.debug("Invoking claude subprocess for %s #%d", item_type, item_number)

    try:
        result = subprocess.run(
            cmd,
            input=full_prompt,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            env=clean_env(),
        )
        if result.returncode != 0:
            logger.warning(
                "claude subprocess failed for %s #%d: %s", item_type, item_number, result.stderr
            )
            return None

        response = json.loads(result.stdout)
        parsed = response if isinstance(response, dict) else {}
        if "structured_output" in parsed:
            parsed = parsed["structured_output"]

    except subprocess.TimeoutExpired:
        logger.warning("claude subprocess timed out for %s #%d", item_type, item_number)
        return None
    except json.JSONDecodeError as e:
        logger.warning("Invalid JSON from claude for %s #%d: %s", item_type, item_number, e)
        return None

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO summaries "
        "(item_number, item_type, repo, model_id, components, item_category, "
        "synopsis, affected_code, error_signatures, concepts, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            item_number,
            item_type,
            repo,
            "haiku",
            json.dumps(parsed.get("components", [])),
            parsed.get("item_category", ""),
            parsed.get("synopsis", ""),
            json.dumps(parsed.get("affected_code", [])),
            parsed.get("error_signatures", ""),
            json.dumps(parsed.get("concepts", [])),
            now,
        ),
    )
    conn.commit()

    return parsed


def summarize_all(conn: sqlite3.Connection, repo: str) -> int:
    """Summarize all unsummarized items. Returns count processed."""
    from .db import set_sync_state

    try:
        from tqdm import tqdm
    except ImportError:
        tqdm = None

    unsummarized_issues = conn.execute(
        "SELECT i.number FROM issues i "
        "LEFT JOIN summaries s ON s.repo = i.repo AND s.item_number = i.number "
        "AND s.item_type = 'issue' "
        "WHERE i.repo = ? AND s.item_number IS NULL",
        (repo,),
    ).fetchall()

    unsummarized_prs = conn.execute(
        "SELECT p.number FROM pull_requests p "
        "LEFT JOIN summaries s ON s.repo = p.repo AND s.item_number = p.number "
        "AND s.item_type = 'pull_request' "
        "WHERE p.repo = ? AND s.item_number IS NULL",
        (repo,),
    ).fetchall()

    items = [(row["number"], "issue") for row in unsummarized_issues] + [
        (row["number"], "pull_request") for row in unsummarized_prs
    ]

    if not items:
        return 0

    count = 0
    iterator = tqdm(items, desc="Summarizing") if tqdm else items
    for item_number, item_type in iterator:
        try:
            result = summarize_item(conn, repo, item_number, item_type)
            if result is not None:
                count += 1
        except Exception:
            logger.warning(
                "Error summarizing %s #%d, skipping", item_type, item_number, exc_info=True
            )
        set_sync_state(conn, "summarize_checkpoint", f"{item_type}:{item_number}")

    return count
