"""Stage 2: LLM-based summarization of issues and PRs.

Supports two backends:
- "claude": Uses claude --model haiku -p subprocess (default)
- "local": Uses an OpenAI-compatible HTTP server (e.g. llama.cpp)
"""

import asyncio
import json
import logging
import sqlite3
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone

from .config import SummarizeConfig, clean_env, get_config

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 300
DEFAULT_CONCURRENCY = 8

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


def _build_prompt(
    conn: sqlite3.Connection, repo: str, item_number: int, item_type: str
) -> str | None:
    """Build the full prompt for a single item. Returns None if no context."""
    context = _build_context(conn, repo, item_number, item_type)
    if not context:
        return None

    config = get_config()
    prompt_path = config.prompts_dir / "summarize.txt"
    system_prompt = prompt_path.read_text()
    return f"{system_prompt}\n\n--- Item ---\n{context}"


def _parse_response(stdout: str) -> dict | None:
    """Parse claude subprocess stdout into a summary dict."""
    response = json.loads(stdout)
    parsed = response if isinstance(response, dict) else {}
    if "structured_output" in parsed:
        parsed = parsed["structured_output"]
    return parsed


def _store_summary(
    conn: sqlite3.Connection,
    repo: str,
    item_number: int,
    item_type: str,
    parsed: dict,
    model_id: str = "haiku",
) -> None:
    """Write a parsed summary to the database."""
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
            model_id,
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


def _summarize_via_claude(
    full_prompt: str, schema_json: str, item_type: str, item_number: int
) -> dict | None:
    """Call claude --model haiku -p subprocess. Returns parsed dict or None."""
    cmd = [
        "claude", "--model", "haiku", "-p",
        "--output-format", "json", "--json-schema", schema_json,
        "--no-session-persistence",
    ]

    try:
        result = subprocess.run(
            cmd, input=full_prompt, capture_output=True, text=True,
            timeout=TIMEOUT_SECONDS, env=clean_env(),
        )
        if result.returncode != 0:
            logger.warning(
                "claude subprocess failed for %s #%d (rc=%d): stderr=%s stdout=%s",
                item_type, item_number, result.returncode,
                result.stderr[:500] if result.stderr else "(empty)",
                result.stdout[:500] if result.stdout else "(empty)",
            )
            return None

        return _parse_response(result.stdout)
    except subprocess.TimeoutExpired:
        logger.warning("claude subprocess timed out for %s #%d", item_type, item_number)
        return None
    except json.JSONDecodeError as e:
        logger.warning("Invalid JSON from claude for %s #%d: %s", item_type, item_number, e)
        return None


def _summarize_via_local(
    full_prompt: str, config: SummarizeConfig, item_type: str, item_number: int
) -> dict | None:
    """Call a local OpenAI-compatible server. Returns parsed dict or None."""
    url = f"{config.local_url.rstrip('/')}/v1/chat/completions"
    body = json.dumps({
        "model": config.local_model,
        "messages": [{"role": "user", "content": full_prompt}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "summary", "schema": _JSON_SCHEMA},
        },
        "temperature": 0.1,
        # Disable Qwen3.5 thinking/reasoning mode for faster inference.
        "thinking": {"type": "disabled"},
    }).encode("utf-8")

    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=config.timeout) as resp:
            response = json.loads(resp.read().decode("utf-8"))
        content = response["choices"][0]["message"]["content"]
        return json.loads(content)
    except urllib.error.URLError as e:
        logger.warning(
            "Local server request failed for %s #%d: %s", item_type, item_number, e
        )
        return None
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        logger.warning(
            "Invalid response from local server for %s #%d: %s",
            item_type, item_number, e,
        )
        return None


def summarize_item(
    conn: sqlite3.Connection,
    repo: str,
    item_number: int,
    item_type: str,
    *,
    backend: str | None = None,
    summarize_config: SummarizeConfig | None = None,
) -> dict | None:
    """Summarize a single item. Dispatches to claude or local backend."""
    if summarize_config is None:
        summarize_config = get_config().summarize
    if backend is None:
        backend = summarize_config.backend

    full_prompt = _build_prompt(conn, repo, item_number, item_type)
    if full_prompt is None:
        logger.warning("No context found for %s #%d in %s", item_type, item_number, repo)
        return None

    logger.debug("Summarizing %s #%d via %s backend", item_type, item_number, backend)

    if backend == "local":
        parsed = _summarize_via_local(
            full_prompt, summarize_config, item_type, item_number
        )
        model_id = summarize_config.local_model
    else:
        schema_json = _get_json_schema()
        parsed = _summarize_via_claude(
            full_prompt, schema_json, item_type, item_number
        )
        model_id = "haiku"

    if parsed is None:
        return None

    _store_summary(conn, repo, item_number, item_type, parsed, model_id=model_id)
    return parsed


async def _summarize_item_async(
    repo: str,
    item_number: int,
    item_type: str,
    full_prompt: str,
    schema_json: str,
    env: dict,
) -> tuple[int, str, dict | None]:
    """Run a single claude subprocess asynchronously.

    Returns (item_number, item_type, parsed_dict_or_None).
    """
    cmd = [
        "claude", "--model", "haiku", "-p",
        "--output-format", "json", "--json-schema", schema_json,
        "--no-session-persistence",
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(input=full_prompt.encode("utf-8")),
            timeout=TIMEOUT_SECONDS,
        )
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            logger.warning(
                "claude subprocess failed for %s #%d (rc=%d): stderr=%s stdout=%s",
                item_type, item_number, proc.returncode,
                stderr[:500] if stderr else "(empty)",
                stdout[:500] if stdout else "(empty)",
            )
            return (item_number, item_type, None)

        parsed = _parse_response(stdout)
        return (item_number, item_type, parsed)

    except asyncio.TimeoutError:
        logger.warning("claude subprocess timed out for %s #%d", item_type, item_number)
        return (item_number, item_type, None)
    except json.JSONDecodeError as e:
        logger.warning("Invalid JSON from claude for %s #%d: %s", item_type, item_number, e)
        return (item_number, item_type, None)
    except Exception as e:
        logger.warning("Error summarizing %s #%d: %s", item_type, item_number, e)
        return (item_number, item_type, None)


async def _summarize_all_async(
    conn: sqlite3.Connection,
    repo: str,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> int:
    """Summarize unsummarized items with concurrent subprocess calls."""
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

    logger.info(
        "Summarizing %d items with concurrency=%d", len(items), concurrency
    )

    # Pre-build all prompts (DB reads are fast and must be on main thread).
    schema_json = _get_json_schema()
    env = clean_env()
    work: list[tuple[int, str, str]] = []  # (item_number, item_type, prompt)
    for item_number, item_type in items:
        prompt = _build_prompt(conn, repo, item_number, item_type)
        if prompt is not None:
            work.append((item_number, item_type, prompt))
        else:
            logger.warning("No context for %s #%d, skipping", item_type, item_number)

    semaphore = asyncio.Semaphore(concurrency)
    count = 0
    pbar = tqdm(total=len(work), desc="Summarizing") if tqdm else None

    async def _run_one(item_number: int, item_type: str, prompt: str):
        async with semaphore:
            return await _summarize_item_async(
                repo, item_number, item_type, prompt, schema_json, env
            )

    # Process in batches to avoid unbounded task creation and allow periodic DB writes.
    batch_size = concurrency * 4
    for batch_start in range(0, len(work), batch_size):
        batch = work[batch_start:batch_start + batch_size]
        tasks = [_run_one(n, t, p) for n, t, p in batch]
        results = await asyncio.gather(*tasks)

        for item_number, item_type, parsed in results:
            if parsed is not None:
                _store_summary(conn, repo, item_number, item_type, parsed)
                count += 1
            if pbar:
                pbar.update(1)

        set_sync_state(
            conn, "summarize_checkpoint",
            f"{batch[-1][1]}:{batch[-1][0]}"
        )

    if pbar:
        pbar.close()

    return count


def summarize_all(
    conn: sqlite3.Connection,
    repo: str,
    *,
    concurrency: int = DEFAULT_CONCURRENCY,
    backend: str | None = None,
    summarize_config: SummarizeConfig | None = None,
) -> int:
    """Summarize all unsummarized items.

    For claude backend, uses concurrent subprocess calls.
    For local backend, uses sequential HTTP calls (single GPU).
    """
    if summarize_config is None:
        summarize_config = get_config().summarize
    if backend is None:
        backend = summarize_config.backend

    if backend == "local":
        return _summarize_all_local(conn, repo, summarize_config)
    return asyncio.run(_summarize_all_async(conn, repo, concurrency=concurrency))


def _summarize_all_local(
    conn: sqlite3.Connection, repo: str, config: SummarizeConfig
) -> int:
    """Sequential summarization via local HTTP server."""
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
    iterator = tqdm(items, desc="Summarizing (local)") if tqdm else items
    for item_number, item_type in iterator:
        try:
            result = summarize_item(
                conn, repo, item_number, item_type,
                backend="local", summarize_config=config,
            )
            if result is not None:
                count += 1
        except Exception:
            logger.warning(
                "Error summarizing %s #%d, skipping",
                item_type, item_number, exc_info=True,
            )
        set_sync_state(
            conn, "summarize_checkpoint", f"{item_type}:{item_number}"
        )

    return count
