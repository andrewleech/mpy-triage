"""Stage 6: Sonnet-based assessment of candidate matches."""

import sqlite3
from dataclasses import dataclass


@dataclass
class Assessment:
    """Result of Sonnet's assessment of a candidate match."""

    item_number: int
    item_type: str
    repo: str
    classification: str  # DUPLICATE, LIKELY_DUPLICATE, RELATED, OFF_TOPIC, UNRELATED
    confidence: str  # high, medium, low
    reasoning: str
    suggested_action: str


def assess_candidates(
    conn: sqlite3.Connection,
    query_item: dict,
    candidates: list[dict],
    *,
    top_k: int = 5,
) -> list[Assessment]:
    """Assess candidates using claude --model sonnet -p subprocess."""
    raise NotImplementedError
