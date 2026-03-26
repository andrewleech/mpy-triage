"""Stage 1: Mirror GitHub issues, PRs, comments, and diffs into SQLite."""

import json
import logging
import sqlite3
from datetime import UTC, datetime

from tqdm import tqdm

from .db import get_sync_state, set_sync_state
from .gh import gh_api, gh_diff

logger = logging.getLogger(__name__)

BATCH_SIZE = 100


def _extract_labels(item: dict) -> str:
    """Extract label names from an issue/PR item as a JSON array string."""
    labels = [lbl["name"] for lbl in (item.get("labels") or [])]
    return json.dumps(labels)


def _utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _upsert_batch(conn: sqlite3.Connection, count: int) -> None:
    """Commit if we have reached a batch boundary."""
    if count % BATCH_SIZE == 0:
        conn.commit()


def _parse_number_from_url(url: str) -> int:
    """Extract the trailing integer from a GitHub API URL."""
    return int(url.rstrip("/").rsplit("/", 1)[-1])


def collect_issues(conn: sqlite3.Connection, repo: str) -> int:
    """Collect/update issues for a repository. Returns count of new/updated items."""
    sync_key = f"{repo}:issues:since"
    since = get_sync_state(conn, sync_key)
    count = 0

    params = {"state": "all", "per_page": "100", "sort": "updated", "direction": "asc"}
    if since:
        logger.info("Incremental issue sync since %s", since)
        params["since"] = since
    else:
        logger.info("Full issue sync via list endpoint")

    query = "&".join(f"{k}={v}" for k, v in params.items())
    items = gh_api(f"/repos/{repo}/issues?{query}", paginate=True) or []
    # The issues endpoint also returns PRs; filter them out.
    items = [i for i in items if "pull_request" not in i]

    for item in items:
        conn.execute(
            """INSERT OR REPLACE INTO issues
               (id, number, repo, title, body, author, state, state_reason,
                labels, milestone, created_at, updated_at, closed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                item["id"],
                item["number"],
                repo,
                item.get("title"),
                item.get("body"),
                (item.get("user") or {}).get("login"),
                item.get("state"),
                item.get("state_reason"),
                _extract_labels(item),
                (item.get("milestone") or {}).get("title"),
                item.get("created_at"),
                item.get("updated_at"),
                item.get("closed_at"),
            ),
        )
        count += 1
        _upsert_batch(conn, count)

    conn.commit()
    set_sync_state(conn, sync_key, _utcnow_iso())
    logger.info("Collected %d issues", count)
    return count


def collect_pull_requests(conn: sqlite3.Connection, repo: str) -> int:
    """Collect/update pull requests for a repository."""
    sync_key = f"{repo}:pulls:since"
    since = get_sync_state(conn, sync_key)
    count = 0

    params = {"state": "all", "per_page": "100", "sort": "updated", "direction": "asc"}
    if since:
        logger.info("Incremental PR sync since %s", since)
        params["since"] = since
    else:
        logger.info("Full PR sync via list endpoint")

    query = "&".join(f"{k}={v}" for k, v in params.items())
    items = gh_api(f"/repos/{repo}/pulls?{query}", paginate=True) or []

    for item in items:
        state = item.get("state")
        if item.get("merged_at"):
            state = "merged"

        conn.execute(
            """INSERT OR REPLACE INTO pull_requests
               (id, number, repo, title, body, author, state, draft, labels,
                created_at, updated_at, closed_at, merged_at, base_branch,
                changed_files, additions, deletions)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                item["id"],
                item["number"],
                repo,
                item.get("title"),
                item.get("body"),
                (item.get("user") or {}).get("login"),
                state,
                1 if item.get("draft") else 0,
                _extract_labels(item),
                item.get("created_at"),
                item.get("updated_at"),
                item.get("closed_at"),
                item.get("merged_at"),
                (item.get("base") or {}).get("ref"),
                item.get("changed_files"),
                item.get("additions"),
                item.get("deletions"),
            ),
        )
        count += 1
        _upsert_batch(conn, count)

    conn.commit()
    set_sync_state(conn, sync_key, _utcnow_iso())
    logger.info("Collected %d pull requests", count)
    return count


def collect_pr_diffs(conn: sqlite3.Connection, repo: str) -> int:
    """Collect diffs for PRs that don't have one yet."""
    cursor = conn.execute(
        """SELECT p.number FROM pull_requests p
           LEFT JOIN pr_diffs d ON p.number = d.pr_number AND p.repo = d.repo
           WHERE p.repo = ? AND d.pr_number IS NULL""",
        (repo,),
    )
    pr_numbers = [row[0] for row in cursor.fetchall()]

    if not pr_numbers:
        logger.info("No PR diffs to collect")
        return 0

    count = 0
    for pr_number in tqdm(pr_numbers, desc="Collecting PR diffs"):
        diff_text = gh_diff(repo, pr_number)
        if diff_text is not None:
            conn.execute(
                "INSERT INTO pr_diffs (pr_number, repo, diff_text) VALUES (?, ?, ?)",
                (pr_number, repo, diff_text),
            )
            count += 1
            _upsert_batch(conn, count)

    conn.commit()
    logger.info("Collected %d PR diffs", count)
    return count


def collect_comments(conn: sqlite3.Connection, repo: str) -> int:
    """Collect/update issue and PR comments."""
    sync_key = f"{repo}:comments:since"
    since = get_sync_state(conn, sync_key)
    count = 0

    params = {"per_page": "100"}
    if since:
        params["since"] = since
        logger.info("Incremental comment sync since %s", since)
    else:
        logger.info("Full comment sync")

    query = "&".join(f"{k}={v}" for k, v in params.items())
    items = gh_api(f"/repos/{repo}/issues/comments?{query}", paginate=True) or []

    # Build a set of PR numbers for item_type classification.
    cursor = conn.execute("SELECT number FROM pull_requests WHERE repo = ?", (repo,))
    pr_numbers = {row[0] for row in cursor.fetchall()}

    for item in items:
        issue_url = item.get("issue_url", "")
        try:
            item_number = _parse_number_from_url(issue_url)
        except (ValueError, IndexError):
            continue

        item_type = "pull_request" if item_number in pr_numbers else "issue"

        conn.execute(
            """INSERT OR REPLACE INTO comments
               (id, item_number, item_type, repo, author, body, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                item["id"],
                item_number,
                item_type,
                repo,
                (item.get("user") or {}).get("login"),
                item.get("body"),
                item.get("created_at"),
                item.get("updated_at"),
            ),
        )
        count += 1
        _upsert_batch(conn, count)

    conn.commit()
    set_sync_state(conn, sync_key, _utcnow_iso())
    logger.info("Collected %d comments", count)
    return count


def collect_review_comments(conn: sqlite3.Connection, repo: str) -> int:
    """Collect/update inline code review comments on PRs."""
    sync_key = f"{repo}:review_comments:since"
    since = get_sync_state(conn, sync_key)
    count = 0

    params = {"per_page": "100"}
    if since:
        params["since"] = since
        logger.info("Incremental review comment sync since %s", since)
    else:
        logger.info("Full review comment sync")

    query = "&".join(f"{k}={v}" for k, v in params.items())
    items = gh_api(f"/repos/{repo}/pulls/comments?{query}", paginate=True) or []

    for item in items:
        pr_url = item.get("pull_request_url", "")
        try:
            pr_number = _parse_number_from_url(pr_url)
        except (ValueError, IndexError):
            continue

        conn.execute(
            """INSERT OR REPLACE INTO review_comments
               (id, pr_number, repo, author, body, path, diff_hunk, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                item["id"],
                pr_number,
                repo,
                (item.get("user") or {}).get("login"),
                item.get("body"),
                item.get("path"),
                item.get("diff_hunk"),
                item.get("created_at"),
            ),
        )
        count += 1
        _upsert_batch(conn, count)

    conn.commit()
    set_sync_state(conn, sync_key, _utcnow_iso())
    logger.info("Collected %d review comments", count)
    return count


def collect_all(conn: sqlite3.Connection, repo: str) -> dict[str, int]:
    """Run all collectors for a repository. Returns counts per type."""
    logger.info("Starting full collection for %s", repo)
    counts = {
        "issues": collect_issues(conn, repo),
        "pull_requests": collect_pull_requests(conn, repo),
        "pr_diffs": collect_pr_diffs(conn, repo),
        "comments": collect_comments(conn, repo),
        "review_comments": collect_review_comments(conn, repo),
    }
    logger.info("Collection complete: %s", counts)
    return counts
