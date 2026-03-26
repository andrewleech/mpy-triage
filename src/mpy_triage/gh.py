"""GitHub CLI (gh) wrapper with rate limiting, pagination, and error handling."""


def gh_api(
    endpoint: str,
    *,
    paginate: bool = False,
    accept: str | None = None,
    method: str = "GET",
) -> dict | list | None:
    """Call gh api as a subprocess with optional pagination and JSON multiparse."""
    raise NotImplementedError


def gh_search(query: str, *, date_range: tuple[str, str] | None = None) -> list[dict]:
    """Search GitHub issues/PRs with year-range pagination to bypass 1000-result limit."""
    raise NotImplementedError


def gh_diff(repo: str, pr_number: int) -> str | None:
    """Fetch a PR's diff via the GitHub API."""
    raise NotImplementedError
