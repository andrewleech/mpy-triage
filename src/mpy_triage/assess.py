"""Stage 6: Sonnet-based assessment of candidate matches."""

import json
import logging
import os
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import get_config

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 120

_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "classification": {
            "type": "string",
            "enum": ["DUPLICATE", "LIKELY_DUPLICATE", "RELATED", "OFF_TOPIC", "UNRELATED"],
        },
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "reasoning": {"type": "string"},
        "suggested_action": {"type": "string"},
    },
    "required": ["classification", "confidence", "reasoning", "suggested_action"],
}


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


def _clean_env() -> dict:
    """Copy os.environ, removing keys starting with CLAUDECODE to prevent recursion."""
    return {k: v for k, v in os.environ.items() if not k.startswith("CLAUDECODE")}


def _get_json_schema() -> str:
    """Return the JSON schema string for Assessment structured output."""
    return json.dumps(_JSON_SCHEMA)


def _load_system_prompt() -> str:
    """Read the assessment system prompt, optionally appending MicroPython project context."""
    config = get_config()
    prompt_path = config.prompts_dir / "assess.txt"
    prompt = prompt_path.read_text()

    mpy_rules_path = Path.home() / ".claude" / "mpy-rules" / "core.md"
    if mpy_rules_path.is_file():
        try:
            context = mpy_rules_path.read_text()
            prompt += "\n\n--- MicroPython Project Context ---\n" + context
        except OSError:
            pass

    return prompt


def _fetch_item_text(conn: sqlite3.Connection, item: dict) -> str:
    """Fetch assembled_xml for an item, falling back to title+description."""
    repo = item.get("repo", "")
    item_number = item.get("number") or item.get("item_number")
    item_type = item.get("item_type", item.get("type", "issue"))

    # Try assembled_xml first
    row = conn.execute(
        "SELECT xml_text FROM assembled_xml "
        "WHERE repo = ? AND item_number = ? AND item_type = ?",
        (repo, item_number, item_type),
    ).fetchone()
    if row and row["xml_text"]:
        return row["xml_text"]

    # Fall back to title + body from the source table
    if item_type == "pull_request":
        row = conn.execute(
            "SELECT title, body FROM pull_requests WHERE repo = ? AND number = ?",
            (repo, item_number),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT title, body FROM issues WHERE repo = ? AND number = ?",
            (repo, item_number),
        ).fetchone()

    if row:
        title = row["title"] or ""
        body = row["body"] or ""
        return f"Title: {title}\n\n{body}"

    # Last resort: use whatever is in the dict
    title = item.get("title", "")
    body = item.get("body", item.get("description", ""))
    return f"Title: {title}\n\n{body}"


def _build_comparison_prompt(query_text: str, candidate_text: str) -> str:
    """Build the user prompt comparing a query item to a candidate."""
    return f"## QUERY ITEM\n{query_text}\n\n## CANDIDATE ITEM\n{candidate_text}"


def _make_fallback(item_number: int, item_type: str, repo: str, error: str) -> Assessment:
    """Create a fallback Assessment for error cases."""
    return Assessment(
        item_number=item_number,
        item_type=item_type,
        repo=repo,
        classification="UNRELATED",
        confidence="low",
        reasoning=f"Assessment failed: {error}",
        suggested_action="no action",
    )


def assess_candidates(
    conn: sqlite3.Connection,
    query_item: dict,
    candidates: list[dict],
    *,
    top_k: int = 5,
) -> list[Assessment]:
    """Assess candidates using claude --model sonnet -p subprocess."""
    system_prompt = _load_system_prompt()
    schema_json = _get_json_schema()

    selected = candidates[:top_k]
    query_text = _fetch_item_text(conn, query_item)

    assessments: list[Assessment] = []
    for candidate in selected:
        cand_number = candidate.get("number") or candidate.get("item_number")
        cand_type = candidate.get("item_type", candidate.get("type", "issue"))
        cand_repo = candidate.get("repo", "")

        cand_text = _fetch_item_text(conn, candidate)
        user_prompt = _build_comparison_prompt(query_text, cand_text)
        full_prompt = f"{system_prompt}\n\n{user_prompt}"

        cmd = [
            "claude",
            "--model",
            "sonnet",
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            schema_json,
            "--no-session-persistence",
        ]

        logger.info("Assessing candidate %s #%s against query", cand_type, cand_number)

        try:
            result = subprocess.run(
                cmd,
                input=full_prompt,
                capture_output=True,
                text=True,
                timeout=TIMEOUT_SECONDS,
                env=_clean_env(),
            )
            if result.returncode != 0:
                logger.warning(
                    "claude subprocess failed for %s #%s: %s",
                    cand_type,
                    cand_number,
                    result.stderr,
                )
                assessments.append(
                    _make_fallback(
                        cand_number,
                        cand_type,
                        cand_repo,
                        f"subprocess returned {result.returncode}",
                    )
                )
                continue

            response = json.loads(result.stdout)
            parsed = response if isinstance(response, dict) else {}
            if "structured_output" in parsed:
                parsed = parsed["structured_output"]

            assessments.append(
                Assessment(
                    item_number=cand_number,
                    item_type=cand_type,
                    repo=cand_repo,
                    classification=parsed.get("classification", "UNRELATED"),
                    confidence=parsed.get("confidence", "low"),
                    reasoning=parsed.get("reasoning", ""),
                    suggested_action=parsed.get("suggested_action", "no action"),
                )
            )

        except subprocess.TimeoutExpired:
            logger.warning(
                "claude subprocess timed out for %s #%s", cand_type, cand_number
            )
            assessments.append(_make_fallback(cand_number, cand_type, cand_repo, "timeout"))

        except json.JSONDecodeError as e:
            logger.warning(
                "Invalid JSON from claude for %s #%s: %s", cand_type, cand_number, e
            )
            assessments.append(
                _make_fallback(cand_number, cand_type, cand_repo, f"invalid JSON: {e}")
            )

    return assessments
