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
    from .format import (
        format_candidates_human,
        format_candidates_json,
        format_human,
        format_json,
    )

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
    if skip_assess:
        if output_json:
            click.echo(format_candidates_json(query_item, candidates))
        else:
            click.echo(format_candidates_human(query_item, candidates))
    elif output_json:
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
@click.option("--repo", multiple=True)
@click.option("--min-score", type=float, default=0.06, help="Minimum value score.")
@click.option("--top-k", type=int, default=3,
              help="Candidates per type (issues and PRs separately).")
@click.option("--skip-rerank", is_flag=True, help="Skip cross-encoder (faster).")
@click.option("--reranker", "reranker_model", type=str, default=None,
              help="Reranker model name (default: from config).")
@click.option("--top-n", type=int, default=50, help="Top discoveries to show.")
@click.option("--output", type=click.Path(), default=None, help="Save full results to JSON.")
@click.pass_context
def scan(ctx, repo, min_score, top_k, skip_rerank, reranker_model, top_n, output):
    """Scan all open issues for related/duplicate items."""
    from .db import get_connection, init_db, load_vec_extension
    from .embed import Embedder
    from .scan import format_scan_report, scan_open_issues
    from .search import Reranker

    config = _get_config_with_db(ctx)
    conn = get_connection(config.db_path)
    init_db(conn, config.schema_path)
    load_vec_extension(conn)

    model = reranker_model or config.retrieval.reranker_model
    embedder = Embedder(config.embedding)
    reranker = None if skip_rerank else Reranker(model)

    repos = _get_repos(repo)
    all_results = []
    for r in repos:
        results = scan_open_issues(
            conn, embedder, reranker,
            repo=r, min_score=min_score, top_k=top_k,
            skip_rerank=skip_rerank,
        )
        all_results.extend(results)

    all_results.sort(key=lambda x: -x["value_score"])
    click.echo(format_scan_report(all_results, top_n=top_n))

    if output:
        import json
        with open(output, "w") as f:
            json.dump(all_results, f, indent=2)
        click.echo(f"\nFull results ({len(all_results)} items) saved to {output}")

    conn.close()


@main.command("export")
@click.option("--format", "fmt",
              type=click.Choice(["csv", "markdown", "html"]), default="markdown")
@click.option("--output", "-o", type=click.Path(), default=None,
              help="Output file (default: stdout).")
@click.pass_context
def export_cmd(ctx, fmt, output):
    """Export scan results as CSV, Markdown, or HTML."""
    from .db import get_connection, init_db
    from .export import export_csv, export_html, export_markdown

    config = _get_config_with_db(ctx)
    conn = get_connection(config.db_path)
    init_db(conn, config.schema_path)

    if fmt == "csv":
        text = export_csv(conn)
    elif fmt == "html":
        text = export_html(conn)
    else:
        text = export_markdown(conn)

    if output:
        with open(output, "w") as f:
            f.write(text)
        click.echo(f"Exported to {output}")
    else:
        click.echo(text)

    conn.close()


def _resolve_display_hostname() -> str:
    """Return a shareable hostname for the serve URL.

    Tries tailscale first (so LAN/remote users can click the URL), falls back
    to the system hostname, then "localhost".
    """
    import json
    import socket as _socket
    import subprocess

    try:
        out = subprocess.check_output(
            ["tailscale", "status", "--json"],
            timeout=2,
            stderr=subprocess.DEVNULL,
        )
        name = json.loads(out).get("Self", {}).get("DNSName", "").rstrip(".")
        if name:
            return name
    except (OSError, subprocess.SubprocessError, ValueError):
        pass

    try:
        name = _socket.gethostname()
        if name and name != "localhost":
            return name
    except OSError:
        pass

    return "localhost"


@main.command("serve")
@click.option("--host", default="0.0.0.0", help="Bind address.")
@click.option("--port", type=int, default=0, help="Port (0 = random).")
@click.pass_context
def serve(ctx, host, port):
    """Serve the triage workbench — index + per-pair detail pages."""
    import http.server
    import re
    import socket
    import urllib.parse

    from .db import get_connection, init_db
    from .export import _fetch_scan_results
    from .render import (
        STYLE_CSS,
        render_detail_html,
        render_index_html,
        sort_pairs,
    )

    config = _get_config_with_db(ctx)
    conn = get_connection(config.db_path)
    init_db(conn, config.schema_path)

    pairs = _fetch_scan_results(conn)
    sort_pairs(pairs)

    # Pre-render the index (single large page, stable while server runs).
    index_bytes = render_index_html(pairs, inline_css=False).encode("utf-8")
    css_bytes = STYLE_CSS.encode("utf-8")

    # Cache rendered detail pages by 0-based index.
    detail_cache: dict[int, bytes] = {}

    def _render_detail(i: int) -> bytes:
        if i not in detail_cache:
            detail_cache[i] = render_detail_html(conn, pairs, i).encode("utf-8")
        return detail_cache[i]

    click.echo(f"Loaded {len(pairs)} pairs. Starting server...")

    pair_re = re.compile(r"^/pair/(\d+)/?$")

    class Handler(http.server.BaseHTTPRequestHandler):
        def _respond(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if content_type.startswith("text/css"):
                self.send_header("Cache-Control", "public, max-age=300")
            self.end_headers()
            self.wfile.write(body)

        def _not_found(self, msg: str = "Not found") -> None:
            body = f"<h1>404</h1><p>{msg}</p>".encode()
            self._respond(404, "text/html; charset=utf-8", body)

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path

            if path in ("/", "/index.html"):
                self._respond(200, "text/html; charset=utf-8", index_bytes)
                return

            if path == "/static/style.css":
                self._respond(200, "text/css; charset=utf-8", css_bytes)
                return

            m = pair_re.match(path)
            if m:
                n = int(m.group(1))
                if not 1 <= n <= len(pairs):
                    self._not_found(f"pair {n} out of range (1..{len(pairs)})")
                    return
                self._respond(
                    200, "text/html; charset=utf-8", _render_detail(n - 1)
                )
                return

            self._not_found()

        def log_message(self, fmt, *args):
            pass  # suppress request logs

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind((host, port))
    actual_port = sock.getsockname()[1]
    sock.close()

    server = http.server.HTTPServer((host, actual_port), Handler)
    display_host = _resolve_display_hostname()
    click.echo(f"Serving triage workbench at http://{display_host}:{actual_port}")
    if display_host != "localhost":
        click.echo(f"  (also: http://localhost:{actual_port})")
    click.echo("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        click.echo("\nStopped.")
        server.server_close()
        conn.close()


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
