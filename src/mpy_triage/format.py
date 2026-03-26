"""Output formatting for triage results."""

from .assess import Assessment


def github_url(repo: str, item_type: str, number: int) -> str:
    """Build a GitHub URL for an issue or PR."""
    kind = "issues" if item_type == "issue" else "pull"
    return f"https://github.com/{repo}/{kind}/{number}"


def format_human(query_item: dict, assessments: list[Assessment]) -> str:
    """Format triage results for human-readable terminal output."""
    raise NotImplementedError


def format_json(query_item: dict, assessments: list[Assessment]) -> str:
    """Format triage results as JSON."""
    raise NotImplementedError


def format_stats(db_stats: dict) -> str:
    """Format database statistics for display."""
    raise NotImplementedError
