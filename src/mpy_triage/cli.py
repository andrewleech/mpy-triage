"""CLI entry point for mpy-triage."""

import logging
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


def _get_repos(repo_tuple):
    """Resolve repo list from CLI option or config default."""
    from .config import get_config

    if repo_tuple:
        return list(repo_tuple)
    return get_config().repos


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
    from .db import get_connection, init_db

    config = _get_config_with_db(ctx)
    conn = get_connection(config.db_path)
    init_db(conn, config.schema_path)

    repos = _get_repos(repo)
    for r in repos:
        logger.info("Collecting from %s", r)
        counts = collect_all(conn, r)
        click.echo(f"{r}: {counts}")

    conn.close()


@main.command()
@click.option("--repo", multiple=True, help="Repository to summarize.")
@click.pass_context
def summarize(ctx, repo):
    """Run Haiku summarization on issues and PRs."""
    from .db import get_connection, init_db
    from .summarize import summarize_all

    config = _get_config_with_db(ctx)
    conn = get_connection(config.db_path)
    init_db(conn, config.schema_path)

    repos = _get_repos(repo)
    for r in repos:
        logger.info("Summarizing %s", r)
        count = summarize_all(conn, r)
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


def _triage_item(ctx, number, repo, item_type, skip_summarize, skip_assess, output_json):
    """Shared implementation for issue and pr commands."""
    from .assemble import assemble_item
    from .db import get_connection, init_db
    from .format import format_human, format_json

    config = _get_config_with_db(ctx)
    conn = get_connection(config.db_path)
    init_db(conn, config.schema_path)

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
            from .summarize import summarize_item

            logger.info("Summarizing %s #%d", item_type, number)
            summarize_item(conn, repo, number, item_type)

    # Assemble
    logger.info("Assembling %s #%d", item_type, number)
    assemble_item(conn, repo, number, item_type)

    # Search for candidates
    from .embed import Embedder
    from .search import search

    embedder = Embedder(config.embedding)
    candidates = search(
        conn,
        query_item.get("title", ""),
        embedder,
        config=config.retrieval,
        filters={"exclude_number": number, "exclude_repo": repo},
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
@click.pass_context
def issue(ctx, number, repo, skip_summarize, skip_assess, output_json):
    """Triage an issue for duplicates and related items."""
    _triage_item(ctx, number, repo, "issue", skip_summarize, skip_assess, output_json)


@main.command()
@click.argument("number", type=int)
@click.option("--repo", default="micropython/micropython", help="Repository.")
@click.option("--skip-summarize", is_flag=True, help="Skip Haiku summarization.")
@click.option("--skip-assess", is_flag=True, help="Skip Sonnet assessment.")
@click.option("--json", "output_json", is_flag=True, help="JSON output.")
@click.pass_context
def pr(ctx, number, repo, skip_summarize, skip_assess, output_json):
    """Triage a PR for duplicates and related items."""
    _triage_item(ctx, number, repo, "pull_request", skip_summarize, skip_assess, output_json)


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

    db_stats = {}
    for table, key in [
        ("issues", "issues"),
        ("pull_requests", "pull_requests"),
        ("comments", "comments"),
        ("summaries", "summaries"),
        ("assembled_xml", "assembled"),
    ]:
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
