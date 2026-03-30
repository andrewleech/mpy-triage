"""CLI entry point for mpy-triage."""

import logging
import re
import tempfile
from logging.handlers import RotatingFileHandler
from pathlib import Path

import click

from . import __version__

logger = logging.getLogger(__name__)

LOG_DIR = Path(tempfile.gettempdir()) / "mpy-triage"
LOG_FILE = LOG_DIR / "mpy-triage.log"
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
LOG_BACKUP_COUNT = 3


def _setup_logging(verbose: bool) -> None:
    """Configure logging with stderr + rotating file handler."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    # stderr handler
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    root.addHandler(console)

    # Rotating file handler
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(name)s %(levelname)s: %(message)s")
    )
    root.addHandler(file_handler)
    logger.debug("Log file: %s", LOG_FILE)


@click.group()
@click.version_option(version=__version__)
@click.option("--db", type=click.Path(), default=None, help="Path to SQLite database.")
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose output.")
@click.pass_context
def main(ctx, db, verbose):
    """MicroPython issue/PR triage - duplicate and related item detection."""
    ctx.ensure_object(dict)
    ctx.obj["db"] = db
    ctx.obj["verbose"] = verbose
    _setup_logging(verbose)


_REPO_RE = re.compile(r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$")


def _get_repos(repo_tuple):
    """Resolve repo list from CLI option or config default."""
    from .config import get_config

    repos = list(repo_tuple) if repo_tuple else get_config().repos
    for r in repos:
        if not _REPO_RE.match(r):
            raise click.BadParameter(f"Invalid repo format: {r!r} (expected 'owner/name')")
    return repos


def _get_config_with_db(ctx):
    """Get config, overriding db_path if --db was passed."""
    import copy
    from pathlib import Path

    from .config import get_config

    config = get_config()
    db = ctx.obj.get("db")
    if db:
        config = copy.copy(config)
        config.db_path = Path(db)
    return config


@main.command()
@click.option("--repo", multiple=True, help="Repository to collect from.")
@click.pass_context
def collect(ctx, repo):
    """Mirror GitHub issues, PRs, comments, and diffs into SQLite."""
    from .collect import collect_all
    from .crossref import build_ground_truth, extract_cross_references
    from .db import get_connection, init_db

    config = _get_config_with_db(ctx)
    conn = get_connection(config.db_path)
    init_db(conn, config.schema_path)

    repos = _get_repos(repo)
    for r in repos:
        logger.info("Collecting from %s", r)
        counts = collect_all(conn, r)
        click.echo(f"{r}: {counts}")

        logger.info("Extracting cross-references for %s", r)
        xref_count = extract_cross_references(conn, r)
        gt_count = build_ground_truth(conn, r)
        click.echo(f"{r}: {xref_count} cross-refs, {gt_count} ground truth entries")

    conn.close()


@main.command()
@click.option("--repo", multiple=True, help="Repository to summarize.")
@click.option("--concurrency", "-j", type=int, default=8,
              help="Concurrent subprocess calls (claude backend only).")
@click.option("--backend", type=click.Choice(["claude", "local"]), default=None,
              help="Summarization backend (default: from config).")
@click.option("--local-url", default=None,
              help="URL of local llama.cpp server (e.g. http://step:8080).")
@click.pass_context
def summarize(ctx, repo, concurrency, backend, local_url):
    """Run LLM summarization on issues and PRs."""
    from .config import SummarizeConfig
    from .db import get_connection, init_db
    from .summarize import summarize_all

    config = _get_config_with_db(ctx)
    conn = get_connection(config.db_path)
    init_db(conn, config.schema_path)

    sum_config = config.summarize
    if backend:
        sum_config = SummarizeConfig(
            backend=backend,
            local_url=local_url or sum_config.local_url,
            local_model=sum_config.local_model,
            timeout=sum_config.timeout,
        )
    elif local_url:
        sum_config = SummarizeConfig(
            backend="local",
            local_url=local_url,
            local_model=sum_config.local_model,
            timeout=sum_config.timeout,
        )

    repos = _get_repos(repo)
    for r in repos:
        logger.info("Summarizing %s via %s backend", r, sum_config.backend)
        count = summarize_all(
            conn, r, concurrency=concurrency,
            backend=sum_config.backend, summarize_config=sum_config,
        )
        click.echo(f"{r}: summarized {count} items")

    conn.close()


@main.command()
@click.option("--repo", multiple=True, help="Repository to assemble.")
@click.pass_context
def assemble(ctx, repo):
    """Build structured XML from raw data and summaries."""
    from .assemble import assemble_all
    from .db import get_connection, init_db

    config = _get_config_with_db(ctx)
    conn = get_connection(config.db_path)
    init_db(conn, config.schema_path)

    repos = _get_repos(repo)
    for r in repos:
        logger.info("Assembling %s", r)
        count = assemble_all(conn, r)
        click.echo(f"{r}: assembled {count} items")

    conn.close()


@main.command()
@click.option("--force", is_flag=True, help="Rebuild index from scratch.")
@click.option("--batch-size", type=int, default=4, help="Embedding batch size.")
@click.pass_context
def embed(ctx, force, batch_size):
    """Build or rebuild the embedding index."""
    from .db import get_connection, init_db
    from .embed import Embedder, index_all, rebuild_index

    config = _get_config_with_db(ctx)
    conn = get_connection(config.db_path)
    init_db(conn, config.schema_path)

    embedder = Embedder(config.embedding)
    if force:
        count = rebuild_index(conn, embedder)
        click.echo(f"Rebuilt index: {count} items")
    else:
        count = index_all(conn, embedder, batch_size=batch_size)
        click.echo(f"Indexed {count} items")

    conn.close()


def _triage_item(
    ctx, number, repo, item_type, skip_summarize, skip_assess, output_json,
    backend=None, local_url=None,
):
    """Shared implementation for issue and pr commands."""
    from .assemble import _assemble_and_store
    from .db import get_connection, init_db, load_vec_extension
    from .format import format_human, format_json

    config = _get_config_with_db(ctx)
    conn = get_connection(config.db_path)
    init_db(conn, config.schema_path)
    load_vec_extension(conn)

    # Fetch the target item
    if item_type == "issue":
        row = conn.execute(
            "SELECT * FROM issues WHERE repo = ? AND number = ?", (repo, number)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM pull_requests WHERE repo = ? AND number = ?", (repo, number)
        ).fetchone()

    if row is None:
        click.echo(f"Error: {item_type} #{number} not found in database for {repo}.")
        raise SystemExit(1)

    query_item = dict(row)
    query_item["item_type"] = item_type

    # Summarize if needed
    if not skip_summarize:
        existing = conn.execute(
            "SELECT 1 FROM summaries WHERE repo = ? AND item_number = ? AND item_type = ?",
            (repo, number, item_type),
        ).fetchone()
        if not existing:
            from .config import SummarizeConfig
            from .summarize import summarize_item

            sum_config = config.summarize
            if backend or local_url:
                sum_config = SummarizeConfig(
                    backend=backend or ("local" if local_url else sum_config.backend),
                    local_url=local_url or sum_config.local_url,
                    local_model=sum_config.local_model,
                    timeout=sum_config.timeout,
                )
            logger.info("Summarizing %s #%d via %s", item_type, number, sum_config.backend)
            summarize_item(
                conn, repo, number, item_type, summarize_config=sum_config,
            )

    # Assemble and persist
    logger.info("Assembling %s #%d", item_type, number)
    _assemble_and_store(conn, repo, number, item_type)

    # Search for candidates
    from .embed import Embedder
    from .search import search

    query_text = query_item.get("title", "")
    body = query_item.get("body") or ""
    if body:
        query_text = f"{query_text}\n{body[:2000]}"

    embedder = Embedder(config.embedding)
    candidates = search(
        conn,
        query_text,
        embedder,
        config=config.retrieval,
        exclude=(number, repo),
    )

    # Assess candidates
    assessments = []
    if not skip_assess and candidates:
        from .assess import assess_candidates

        assessments = assess_candidates(
            conn,
            query_item,
            candidates[: config.retrieval.top_k_assess],
        )

    # Format output
    if output_json:
        click.echo(format_json(query_item, assessments))
    else:
        click.echo(format_human(query_item, assessments))

    conn.close()


@main.command()
@click.argument("number", type=int)
@click.option("--repo", default="micropython/micropython", help="Repository.")
@click.option("--skip-summarize", is_flag=True, help="Skip Haiku summarization.")
@click.option("--skip-assess", is_flag=True, help="Skip Sonnet assessment.")
@click.option("--json", "output_json", is_flag=True, help="JSON output.")
@click.option("--backend", type=click.Choice(["claude", "local"]), default=None,
              help="Summarization backend.")
@click.option("--local-url", default=None, help="URL of local llama.cpp server.")
@click.pass_context
def issue(ctx, number, repo, skip_summarize, skip_assess, output_json, backend, local_url):
    """Triage an issue for duplicates and related items."""
    _triage_item(
        ctx, number, repo, "issue", skip_summarize, skip_assess, output_json,
        backend=backend, local_url=local_url,
    )


@main.command()
@click.argument("number", type=int)
@click.option("--repo", default="micropython/micropython", help="Repository.")
@click.option("--skip-summarize", is_flag=True, help="Skip Haiku summarization.")
@click.option("--skip-assess", is_flag=True, help="Skip Sonnet assessment.")
@click.option("--json", "output_json", is_flag=True, help="JSON output.")
@click.option("--backend", type=click.Choice(["claude", "local"]), default=None,
              help="Summarization backend.")
@click.option("--local-url", default=None, help="URL of local llama.cpp server.")
@click.pass_context
def pr(ctx, number, repo, skip_summarize, skip_assess, output_json, backend, local_url):
    """Triage a PR for duplicates and related items."""
    _triage_item(
        ctx, number, repo, "pull_request", skip_summarize, skip_assess, output_json,
        backend=backend, local_url=local_url,
    )


@main.command()
@click.pass_context
def stats(ctx):
    """Show database and index statistics."""
    from .db import get_connection, init_db
    from .format import format_stats

    config = _get_config_with_db(ctx)
    try:
        conn = get_connection(config.db_path)
        init_db(conn, config.schema_path)
    except Exception as e:
        click.echo(f"Could not open database at {config.db_path}: {e}")
        raise SystemExit(1)

    _table_keys = [
        ("issues", "issues"),
        ("pull_requests", "pull_requests"),
        ("comments", "comments"),
        ("summaries", "summaries"),
        ("assembled_xml", "assembled"),
    ]
    db_stats = {}
    for table, key in _table_keys:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        db_stats[key] = row[0]

    # vec_items may not exist if embed hasn't been run
    try:
        row = conn.execute("SELECT COUNT(*) FROM vec_items").fetchone()
        db_stats["embedded"] = row[0]
    except Exception:
        db_stats["embedded"] = 0

    # Check embedding model
    try:
        row = conn.execute(
            "SELECT value FROM embedding_meta WHERE key = 'model_id'"
        ).fetchone()
        db_stats["model_id"] = row[0] if row else "N/A"
    except Exception:
        db_stats["model_id"] = "N/A"

    click.echo(format_stats(db_stats))
    conn.close()


@main.group("eval")
@click.pass_context
def eval_group(ctx):
    """Evaluate summarization quality."""
    pass


@eval_group.command("compare")
@click.option("--sample-size", "-n", type=int, default=50,
              help="Number of items to compare.")
@click.option("--local-url", required=True,
              help="URL of local llama.cpp server.")
@click.option("--local-model", default="qwen3.5-4b",
              help="Model name for the local backend.")
@click.option("--output", type=click.Path(), default=None,
              help="Save detailed results to JSON file.")
@click.pass_context
def eval_compare(ctx, sample_size, local_url, local_model, output):
    """Run pairwise Haiku vs local comparison with Opus judge."""
    from .config import SummarizeConfig
    from .db import get_connection, init_db
    from .eval import format_eval_report, run_eval

    config = _get_config_with_db(ctx)
    conn = get_connection(config.db_path)
    init_db(conn, config.schema_path)

    local_config = SummarizeConfig(
        backend="local", local_url=local_url,
        local_model=local_model,
    )

    report = run_eval(conn, sample_size, local_config)

    click.echo(format_eval_report(report))

    if output:
        import json
        with open(output, "w") as f:
            json.dump({
                "sample_size": report.sample_size,
                "haiku_wins": report.haiku_wins,
                "local_wins": report.local_wins,
                "ties": report.ties,
                "haiku_avg_scores": report.haiku_avg_scores,
                "local_avg_scores": report.local_avg_scores,
                "per_item": report.per_item,
            }, f, indent=2)
        click.echo(f"\nDetailed results saved to {output}")

    conn.close()
