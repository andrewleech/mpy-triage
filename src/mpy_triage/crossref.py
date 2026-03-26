"""Extract cross-references from issue/PR text."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

# Pattern to match fenced code blocks (``` ... ```)
_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)

# Reference pattern: either org/repo#N or bare #N.
# Group 1: repo (org/name), may be None for bare refs.
# Group 2: issue/PR number.
_REF = r"(?:([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)#(\d+)|#(\d+))"

# Match patterns: keyword(s) followed by a reference.
# Each tuple: (compiled regex, relationship)
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(rf"\bfix(?:es|ed)?\s+{_REF}", re.IGNORECASE), "fixes"),
    (re.compile(rf"\bclos(?:es|ed|e)\s+{_REF}", re.IGNORECASE), "closes"),
    (re.compile(rf"\bduplicate(?:s)?\s+of\s+{_REF}", re.IGNORECASE), "duplicate_of"),
    (re.compile(rf"\brelated\s+to\s+{_REF}", re.IGNORECASE), "related"),
    (re.compile(rf"\bsee\s+also\s+{_REF}", re.IGNORECASE), "related"),
    (re.compile(rf"\bsee\s+{_REF}", re.IGNORECASE), "related"),
]


@dataclass
class CrossRef:
    """A cross-reference extracted from text."""

    source_repo: str
    source_number: int
    source_type: str  # "issue", "pr", or "comment"
    target_repo: str
    target_number: int
    relationship: str  # "fixes", "closes", "duplicate_of", "related"


def _strip_code_blocks(text: str) -> str:
    """Remove fenced code blocks from text."""
    return _CODE_BLOCK_RE.sub("", text)


def _extract_ref(match: re.Match[str]) -> tuple[str | None, int]:
    """Extract (repo_or_None, number) from a pattern match.

    The reference groups are always the last 3 groups in the match:
    - group(-3): repo from org/repo#N alternative
    - group(-2): number from org/repo#N alternative
    - group(-1): number from bare #N alternative
    """
    groups = match.groups()
    repo, repo_num, bare_num = groups[-3], groups[-2], groups[-1]
    if repo is not None:
        return repo, int(repo_num)
    return None, int(bare_num)


def parse_references(text: str, source_repo: str) -> list[CrossRef]:
    """Parse cross-references from text.

    Finds patterns like "Fixes #123", "Duplicate of org/repo#456", etc.
    References inside fenced code blocks are ignored.
    Bare #N without a keyword prefix is NOT matched.

    Args:
        text: The text to scan for references.
        source_repo: Default repository for bare #N references.

    Returns:
        List of CrossRef instances (source_number and source_type are left
        as 0 and "" respectively -- the caller fills those in).
    """
    if not text:
        return []

    cleaned = _strip_code_blocks(text)
    refs: list[CrossRef] = []
    seen: set[tuple[str, int, str]] = set()

    for pattern, relationship in _PATTERNS:
        for match in pattern.finditer(cleaned):
            repo, target_number = _extract_ref(match)
            target_repo = repo if repo else source_repo

            key = (target_repo, target_number, relationship)
            if key not in seen:
                seen.add(key)
                refs.append(
                    CrossRef(
                        source_repo=source_repo,
                        source_number=0,
                        source_type="",
                        target_repo=target_repo,
                        target_number=target_number,
                        relationship=relationship,
                    )
                )

    return refs


def extract_cross_references(conn: sqlite3.Connection, repo: str) -> int:
    """Scan issue bodies, PR bodies, and comment bodies for cross-references.

    Inserts found references into the cross_references table.

    Args:
        conn: SQLite database connection.
        repo: Repository identifier (e.g. "micropython/micropython").

    Returns:
        Count of new references added.
    """
    read_cursor = conn.cursor()
    write_cursor = conn.cursor()
    count = 0

    sources = [
        ("SELECT number, body FROM issues WHERE body IS NOT NULL AND body != ''", "issue"),
        (
            "SELECT number, body FROM pull_requests WHERE body IS NOT NULL AND body != ''",
            "pr",
        ),
        (
            "SELECT issue_number, body FROM comments WHERE body IS NOT NULL AND body != ''",
            "comment",
        ),
    ]

    for query, source_type in sources:
        read_cursor.execute(query)
        for number, body in read_cursor.fetchall():
            for ref in parse_references(body, repo):
                ref.source_number = number
                ref.source_type = source_type
                count += _insert_cross_reference(write_cursor, ref)

    conn.commit()
    return count


def _insert_cross_reference(cursor: sqlite3.Cursor, ref: CrossRef) -> int:
    """Insert a cross-reference, returning 1 if inserted or 0 if duplicate."""
    cursor.execute(
        """
        INSERT OR IGNORE INTO cross_references
            (source_repo, source_number, source_type, target_repo, target_number, relationship)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            ref.source_repo,
            ref.source_number,
            ref.source_type,
            ref.target_repo,
            ref.target_number,
            ref.relationship,
        ),
    )
    return cursor.rowcount


def build_ground_truth(conn: sqlite3.Connection, repo: str) -> int:
    """Build ground truth entries from duplicate issues.

    Finds issues with state_reason='duplicate' and looks for "Duplicate of #N"
    references in their body or comments. Also picks up duplicate relationships
    already in cross_references.

    Args:
        conn: SQLite database connection.
        repo: Repository identifier.

    Returns:
        Count of new ground truth entries added.
    """
    read_cursor = conn.cursor()
    write_cursor = conn.cursor()
    count = 0

    # Find issues marked as duplicate
    read_cursor.execute("SELECT number, body FROM issues WHERE state_reason = 'duplicate'")
    for number, body in read_cursor.fetchall():
        targets = _find_duplicate_targets(conn, repo, number, body)
        for target_repo, target_number in targets:
            count += _insert_ground_truth(write_cursor, repo, number, target_repo, target_number)

    # Also pull in any "duplicate_of" cross_references not yet in ground_truth
    read_cursor.execute(
        """
        SELECT source_repo, source_number, target_repo, target_number
        FROM cross_references
        WHERE relationship = 'duplicate_of' AND source_repo = ?
        """,
        (repo,),
    )
    for source_repo, source_number, target_repo, target_number in read_cursor.fetchall():
        count += _insert_ground_truth(
            write_cursor, source_repo, source_number, target_repo, target_number
        )

    conn.commit()
    return count


def _find_duplicate_targets(
    conn: sqlite3.Connection,
    repo: str,
    issue_number: int,
    body: str | None,
) -> set[tuple[str, int]]:
    """Find duplicate targets from issue body and its comments."""
    targets: set[tuple[str, int]] = set()

    # Check issue body
    if body:
        for ref in parse_references(body, repo):
            if ref.relationship == "duplicate_of":
                targets.add((ref.target_repo, ref.target_number))

    # Check comments on this issue
    cursor = conn.cursor()
    cursor.execute(
        "SELECT body FROM comments WHERE issue_number = ? AND body IS NOT NULL AND body != ''",
        (issue_number,),
    )
    for (comment_body,) in cursor.fetchall():
        for ref in parse_references(comment_body, repo):
            if ref.relationship == "duplicate_of":
                targets.add((ref.target_repo, ref.target_number))

    return targets


def _insert_ground_truth(
    cursor: sqlite3.Cursor,
    source_repo: str,
    source_number: int,
    target_repo: str,
    target_number: int,
) -> int:
    """Insert a ground truth entry, returning 1 if inserted or 0 if duplicate."""
    cursor.execute(
        """
        INSERT OR IGNORE INTO ground_truth
            (source_repo, source_number, target_repo, target_number, relationship)
        VALUES (?, ?, ?, ?, 'duplicate')
        """,
        (source_repo, source_number, target_repo, target_number),
    )
    return cursor.rowcount
