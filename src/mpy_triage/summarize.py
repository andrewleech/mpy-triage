"""Stage 2: Haiku-based summarization of issues and PRs."""

import sqlite3


def summarize_item(
    conn: sqlite3.Connection,
    repo: str,
    item_number: int,
    item_type: str,
) -> dict | None:
    """Summarize a single item using claude --model haiku -p subprocess."""
    raise NotImplementedError


def summarize_all(conn: sqlite3.Connection, repo: str) -> int:
    """Summarize all unsummarized items. Returns count processed."""
    raise NotImplementedError
