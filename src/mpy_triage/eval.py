"""Eval framework for comparing summarization backends."""

import json
import logging
import random
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import SummarizeConfig, clean_env, get_config
from .summarize import _build_context, _summarize_via_local

logger = logging.getLogger(__name__)

JUDGE_TIMEOUT = 120

_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "scores_a": {
            "type": "object",
            "properties": {
                "accuracy": {"type": "integer"},
                "completeness": {"type": "integer"},
                "specificity": {"type": "integer"},
                "category": {"type": "integer"},
            },
            "required": ["accuracy", "completeness", "specificity", "category"],
        },
        "scores_b": {
            "type": "object",
            "properties": {
                "accuracy": {"type": "integer"},
                "completeness": {"type": "integer"},
                "specificity": {"type": "integer"},
                "category": {"type": "integer"},
            },
            "required": ["accuracy", "completeness", "specificity", "category"],
        },
        "verdict": {
            "type": "string",
            "enum": ["A_BETTER", "B_BETTER", "TIE"],
        },
        "reasoning": {"type": "string"},
    },
    "required": ["scores_a", "scores_b", "verdict", "reasoning"],
}

_SCORE_DIMENSIONS = ["accuracy", "completeness", "specificity", "category"]


@dataclass
class EvalReport:
    """Aggregated eval results."""

    sample_size: int
    haiku_wins: int
    local_wins: int
    ties: int
    haiku_avg_scores: dict[str, float] = field(default_factory=dict)
    local_avg_scores: dict[str, float] = field(default_factory=dict)
    per_item: list[dict] = field(default_factory=list)


def sample_items(
    conn: sqlite3.Connection, n: int = 50, stratify: bool = True
) -> list[dict]:
    """Sample items that have existing Haiku summaries."""
    if not stratify:
        rows = conn.execute(
            "SELECT item_number, item_type, repo FROM summaries "
            "WHERE model_id = 'haiku' ORDER BY RANDOM() LIMIT ?",
            (n,),
        ).fetchall()
        return [dict(r) for r in rows]

    # Stratified: proportional sample across item_category
    categories = conn.execute(
        "SELECT item_category, COUNT(*) as cnt FROM summaries "
        "WHERE model_id = 'haiku' GROUP BY item_category"
    ).fetchall()

    total = sum(r["cnt"] for r in categories)
    if total == 0:
        return []

    items = []
    for cat_row in categories:
        cat = cat_row["item_category"]
        cat_n = max(1, round(n * cat_row["cnt"] / total))
        rows = conn.execute(
            "SELECT item_number, item_type, repo FROM summaries "
            "WHERE model_id = 'haiku' AND item_category = ? ORDER BY RANDOM() LIMIT ?",
            (cat, cat_n),
        ).fetchall()
        items.extend(dict(r) for r in rows)

    # Trim to exact n if rounding produced extras
    if len(items) > n:
        random.shuffle(items)
        items = items[:n]

    return items


def _fetch_summary_dict(conn: sqlite3.Connection, item: dict, model_id: str,
                        table: str = "summaries") -> dict | None:
    """Fetch a summary as a dict from the given table."""
    row = conn.execute(
        f"SELECT components, item_category, synopsis, affected_code, "
        f"error_signatures, concepts FROM {table} "
        "WHERE repo = ? AND item_number = ? AND item_type = ? AND model_id = ?",
        (item["repo"], item["item_number"], item["item_type"], model_id),
    ).fetchone()
    if row is None:
        return None
    return {
        "components": json.loads(row["components"] or "[]"),
        "item_category": row["item_category"] or "",
        "synopsis": row["synopsis"] or "",
        "affected_code": json.loads(row["affected_code"] or "[]"),
        "error_signatures": row["error_signatures"] or "",
        "concepts": json.loads(row["concepts"] or "[]"),
    }


def _store_eval_summary(
    conn: sqlite3.Connection, item: dict, parsed: dict, model_id: str
) -> None:
    """Store a summary in the eval_summaries table."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO eval_summaries "
        "(item_number, item_type, repo, model_id, components, item_category, "
        "synopsis, affected_code, error_signatures, concepts, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            item["item_number"],
            item["item_type"],
            item["repo"],
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


def generate_comparison_set(
    conn: sqlite3.Connection, items: list[dict], local_config: SummarizeConfig
) -> list[dict]:
    """Generate local summaries for items and pair with existing Haiku summaries."""
    try:
        from tqdm import tqdm
    except ImportError:
        tqdm = None

    comparisons = []
    iterator = tqdm(items, desc="Generating local summaries") if tqdm else items

    for item in iterator:
        haiku = _fetch_summary_dict(conn, item, "haiku")
        if haiku is None:
            continue

        # Check if we already have a local eval summary
        local = _fetch_summary_dict(
            conn, item, local_config.local_model, table="eval_summaries"
        )
        if local is None:
            # Generate via local backend
            context = _build_context(
                conn, item["repo"], item["item_number"], item["item_type"]
            )
            if not context:
                continue

            config = get_config()
            prompt_path = config.prompts_dir / "summarize.txt"
            system_prompt = prompt_path.read_text()
            full_prompt = f"{system_prompt}\n\n--- Item ---\n{context}"

            local = _summarize_via_local(
                full_prompt, local_config,
                item["item_type"], item["item_number"],
            )
            if local is None:
                continue

            _store_eval_summary(conn, item, local, local_config.local_model)

        source_context = _build_context(
            conn, item["repo"], item["item_number"], item["item_type"]
        )

        comparisons.append({
            **item,
            "source_context": source_context,
            "haiku_summary": haiku,
            "local_summary": local,
        })

    return comparisons


def _format_summary_for_judge(summary: dict) -> str:
    """Format a summary dict as readable text for the judge."""
    lines = [
        f"Components: {', '.join(summary.get('components', []))}",
        f"Category: {summary.get('item_category', '')}",
        f"Synopsis: {summary.get('synopsis', '')}",
        f"Affected code: {', '.join(summary.get('affected_code', []))}",
        f"Error signatures: {summary.get('error_signatures', '')}",
        f"Concepts: {', '.join(summary.get('concepts', []))}",
    ]
    return "\n".join(lines)


def judge_pair(
    source_context: str, summary_a: dict, summary_b: dict
) -> dict | None:
    """Call Opus to judge a pair of summaries. Returns parsed verdict or None."""
    config = get_config()
    prompt_path = config.prompts_dir / "eval_judge.txt"
    system_prompt = prompt_path.read_text()

    text_a = _format_summary_for_judge(summary_a)
    text_b = _format_summary_for_judge(summary_b)

    user_prompt = (
        f"## SOURCE\n{source_context}\n\n"
        f"## SUMMARY A\n{text_a}\n\n"
        f"## SUMMARY B\n{text_b}"
    )
    full_prompt = f"{system_prompt}\n\n{user_prompt}"
    schema_json = json.dumps(_JUDGE_SCHEMA)

    cmd = [
        "claude", "--model", "opus", "-p",
        "--output-format", "json", "--json-schema", schema_json,
        "--no-session-persistence",
    ]

    try:
        result = subprocess.run(
            cmd, input=full_prompt, capture_output=True, text=True,
            timeout=JUDGE_TIMEOUT, env=clean_env(),
        )
        if result.returncode != 0:
            logger.warning("Opus judge failed (rc=%d): %s",
                           result.returncode, result.stderr[:300])
            return None

        response = json.loads(result.stdout)
        if isinstance(response, dict) and "structured_output" in response:
            response = response["structured_output"]
        return response

    except subprocess.TimeoutExpired:
        logger.warning("Opus judge timed out")
        return None
    except json.JSONDecodeError as e:
        logger.warning("Invalid JSON from Opus judge: %s", e)
        return None


def run_eval(
    conn: sqlite3.Connection,
    sample_size: int,
    local_config: SummarizeConfig,
) -> EvalReport:
    """Run full eval: sample, generate comparisons, judge, aggregate."""
    try:
        from tqdm import tqdm
    except ImportError:
        tqdm = None

    logger.info("Sampling %d items for eval", sample_size)
    items = sample_items(conn, n=sample_size)
    if not items:
        logger.warning("No Haiku summaries found for sampling")
        return EvalReport(sample_size=0, haiku_wins=0, local_wins=0, ties=0)

    logger.info("Generating comparison set (%d items)", len(items))
    comparisons = generate_comparison_set(conn, items, local_config)
    logger.info("Generated %d comparison pairs", len(comparisons))

    haiku_wins = 0
    local_wins = 0
    ties = 0
    haiku_scores = {d: [] for d in _SCORE_DIMENSIONS}
    local_scores = {d: [] for d in _SCORE_DIMENSIONS}
    per_item = []

    iterator = tqdm(comparisons, desc="Judging") if tqdm else comparisons

    for comp in iterator:
        # Randomize A/B assignment
        haiku_is_a = random.random() < 0.5
        if haiku_is_a:
            summary_a = comp["haiku_summary"]
            summary_b = comp["local_summary"]
        else:
            summary_a = comp["local_summary"]
            summary_b = comp["haiku_summary"]

        verdict_raw = judge_pair(comp["source_context"], summary_a, summary_b)
        if verdict_raw is None:
            continue

        # Reverse the A/B mapping to get haiku/local scores
        if haiku_is_a:
            h_scores = verdict_raw.get("scores_a", {})
            l_scores = verdict_raw.get("scores_b", {})
            raw_verdict = verdict_raw.get("verdict", "TIE")
            if raw_verdict == "A_BETTER":
                verdict = "haiku"
            elif raw_verdict == "B_BETTER":
                verdict = "local"
            else:
                verdict = "tie"
        else:
            h_scores = verdict_raw.get("scores_b", {})
            l_scores = verdict_raw.get("scores_a", {})
            raw_verdict = verdict_raw.get("verdict", "TIE")
            if raw_verdict == "A_BETTER":
                verdict = "local"
            elif raw_verdict == "B_BETTER":
                verdict = "haiku"
            else:
                verdict = "tie"

        if verdict == "haiku":
            haiku_wins += 1
        elif verdict == "local":
            local_wins += 1
        else:
            ties += 1

        for d in _SCORE_DIMENSIONS:
            if d in h_scores:
                haiku_scores[d].append(h_scores[d])
            if d in l_scores:
                local_scores[d].append(l_scores[d])

        per_item.append({
            "item_number": comp["item_number"],
            "item_type": comp["item_type"],
            "repo": comp["repo"],
            "verdict": verdict,
            "haiku_scores": h_scores,
            "local_scores": l_scores,
            "reasoning": verdict_raw.get("reasoning", ""),
        })

    def avg(lst):
        return round(sum(lst) / len(lst), 2) if lst else 0.0

    return EvalReport(
        sample_size=len(per_item),
        haiku_wins=haiku_wins,
        local_wins=local_wins,
        ties=ties,
        haiku_avg_scores={d: avg(haiku_scores[d]) for d in _SCORE_DIMENSIONS},
        local_avg_scores={d: avg(local_scores[d]) for d in _SCORE_DIMENSIONS},
        per_item=per_item,
    )


def format_eval_report(report: EvalReport) -> str:
    """Format an EvalReport as human-readable text."""
    total = report.haiku_wins + report.local_wins + report.ties
    if total == 0:
        return "No eval results."

    def pct(n):
        return f"{100 * n / total:.0f}%"

    lines = [
        f"Eval Results ({report.sample_size} items):",
        f"  Haiku wins:  {report.haiku_wins} ({pct(report.haiku_wins)})",
        f"  Local wins:  {report.local_wins} ({pct(report.local_wins)})",
        f"  Ties:        {report.ties} ({pct(report.ties)})",
        "",
        f"  {'Avg Scores:':<20}{'Haiku':>8}{'Local':>8}",
    ]
    for d in _SCORE_DIMENSIONS:
        h_score = report.haiku_avg_scores.get(d, 0.0)
        l_score = report.local_avg_scores.get(d, 0.0)
        lines.append(f"  {d.capitalize() + ':':<20}{h_score:>8.2f}{l_score:>8.2f}")

    return "\n".join(lines)
