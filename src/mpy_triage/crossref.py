"""Cross-reference extraction from issue/PR text and GitHub events."""

import sqlite3
from dataclasses import dataclass


@dataclass
class CrossRef:
    """A cross-reference between two items."""

    source_number: int
    source_type: str
    source_repo: str
    target_number: int
    target_type: str | None
    target_repo: str
    relationship: str  # fixes, closes, related, duplicate_of, references
    extracted_from: str  # body, comment, event


def parse_references(text: str, source_repo: str) -> list[CrossRef]:
    """Extract cross-references from text using regex patterns."""
    raise NotImplementedError


def extract_cross_references(conn: sqlite3.Connection, repo: str) -> int:
    """Scan all bodies and comments, populate cross_references table."""
    raise NotImplementedError


def build_ground_truth(conn: sqlite3.Connection, repo: str) -> int:
    """Build ground truth from duplicate state_reasons and comments."""
    raise NotImplementedError
