"""Stage 1: Mirror GitHub issues, PRs, comments, and diffs into SQLite."""

import sqlite3


def collect_issues(conn: sqlite3.Connection, repo: str) -> int:
    """Collect/update issues for a repository. Returns count of new/updated items."""
    raise NotImplementedError


def collect_pull_requests(conn: sqlite3.Connection, repo: str) -> int:
    """Collect/update pull requests for a repository."""
    raise NotImplementedError


def collect_pr_diffs(conn: sqlite3.Connection, repo: str) -> int:
    """Collect diffs for PRs that don't have one yet."""
    raise NotImplementedError


def collect_comments(conn: sqlite3.Connection, repo: str) -> int:
    """Collect/update issue and PR comments."""
    raise NotImplementedError


def collect_review_comments(conn: sqlite3.Connection, repo: str) -> int:
    """Collect/update inline code review comments on PRs."""
    raise NotImplementedError


def collect_all(conn: sqlite3.Connection, repo: str) -> dict[str, int]:
    """Run all collectors for a repository. Returns counts per type."""
    raise NotImplementedError
