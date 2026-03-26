"""CLI entry point for mpy-triage."""

import click

from . import __version__


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


@main.command()
@click.option("--repo", multiple=True, help="Repository to collect from.")
@click.pass_context
def collect(ctx, repo):
    """Mirror GitHub issues, PRs, comments, and diffs into SQLite."""
    click.echo("collect: not yet implemented")


@main.command()
@click.option("--repo", multiple=True, help="Repository to summarize.")
@click.pass_context
def summarize(ctx, repo):
    """Run Haiku summarization on issues and PRs."""
    click.echo("summarize: not yet implemented")


@main.command()
@click.option("--repo", multiple=True, help="Repository to assemble.")
@click.pass_context
def assemble(ctx, repo):
    """Build structured XML from raw data and summaries."""
    click.echo("assemble: not yet implemented")


@main.command()
@click.option("--force", is_flag=True, help="Rebuild index from scratch.")
@click.option("--batch-size", type=int, default=4, help="Embedding batch size.")
@click.pass_context
def embed(ctx, force, batch_size):
    """Build or rebuild the embedding index."""
    click.echo("embed: not yet implemented")


@main.command()
@click.argument("number", type=int)
@click.option("--repo", default="micropython/micropython", help="Repository.")
@click.option("--skip-summarize", is_flag=True, help="Skip Haiku summarization.")
@click.option("--skip-assess", is_flag=True, help="Skip Sonnet assessment.")
@click.option("--json", "output_json", is_flag=True, help="JSON output.")
@click.pass_context
def issue(ctx, number, repo, skip_summarize, skip_assess, output_json):
    """Triage an issue for duplicates and related items."""
    click.echo(f"issue {number}: not yet implemented")


@main.command()
@click.argument("number", type=int)
@click.option("--repo", default="micropython/micropython", help="Repository.")
@click.option("--skip-summarize", is_flag=True, help="Skip Haiku summarization.")
@click.option("--skip-assess", is_flag=True, help="Skip Sonnet assessment.")
@click.option("--json", "output_json", is_flag=True, help="JSON output.")
@click.pass_context
def pr(ctx, number, repo, skip_summarize, skip_assess, output_json):
    """Triage a PR for duplicates and related items."""
    click.echo(f"pr {number}: not yet implemented")


@main.command()
@click.pass_context
def stats(ctx):
    """Show database and index statistics."""
    click.echo("stats: not yet implemented")
