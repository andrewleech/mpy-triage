"""Stage 3: Assemble structured XML from raw data and optional Haiku summaries."""

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
    raise NotImplementedError


def assemble_item(
    conn: sqlite3.Connection,
    repo: str,
    item_number: int,
    item_type: str,
) -> str:
    """Build XML for a single item. Works with or without Haiku summary."""
    raise NotImplementedError


def assemble_all(conn: sqlite3.Connection, repo: str) -> int:
    """Assemble XML for all items. Skip unchanged (by hash). Returns count processed."""
    raise NotImplementedError
