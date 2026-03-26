"""Output formatting for triage results."""

import json

from .assess import Assessment


def github_url(repo: str, item_type: str, number: int) -> str:
    """Build a GitHub URL for an issue or PR."""
    kind = "issues" if item_type == "issue" else "pull"
    return f"https://github.com/{repo}/{kind}/{number}"


def format_human(query_item: dict, assessments: list[Assessment]) -> str:
    """Format triage results for human-readable terminal output."""
    repo = query_item["repo"]
    number = query_item["number"]
    title = query_item["title"]
    item_type = query_item.get("item_type", "issue")
    url = github_url(repo, item_type, number)

    lines = [
        f"Searching for similar items to: {repo}#{number}",
        f'  "{title}"',
        f"  {url}",
        "",
    ]

    if not assessments:
        lines.append("No similar items found.")
        return "\n".join(lines)

    lines.append(f"Found {len(assessments)} candidates:")
    lines.append("")

    for a in assessments:
        candidate_url = github_url(a.repo, a.item_type, a.item_number)
        candidate_title = getattr(a, "title", "") or ""
        created_at = getattr(a, "created_at", "") or ""
        if created_at and len(created_at) >= 10:
            created_at = created_at[:10]

        classification = a.classification.upper()
        confidence = a.confidence

        lines.append(f"#{a.item_number} [{classification} - {confidence} confidence]")
        if candidate_title:
            lines.append(f'  "{candidate_title}"')
        lines.append(f"  {candidate_url}")
        status_parts = []
        state = getattr(a, "state", None)
        if state:
            status_parts.append(f"Status: {state}")
        if created_at:
            status_parts.append(f"Created: {created_at}")
        if status_parts:
            lines.append(f"  {' | '.join(status_parts)}")
        lines.append(f"  Reasoning: {a.reasoning}")
        lines.append(f"  Suggested action: {a.suggested_action}")
        lines.append("")

    return "\n".join(lines)


def format_json(query_item: dict, assessments: list[Assessment]) -> str:
    """Format triage results as JSON."""
    repo = query_item["repo"]
    number = query_item["number"]
    title = query_item["title"]
    item_type = query_item.get("item_type", "issue")

    result = {
        "query": {
            "repo": repo,
            "number": number,
            "title": title,
            "item_type": item_type,
            "url": github_url(repo, item_type, number),
        },
        "assessments": [
            {
                "item_number": a.item_number,
                "item_type": a.item_type,
                "repo": a.repo,
                "classification": a.classification,
                "confidence": a.confidence,
                "reasoning": a.reasoning,
                "suggested_action": a.suggested_action,
                "url": github_url(a.repo, a.item_type, a.item_number),
            }
            for a in assessments
        ],
    }
    return json.dumps(result, indent=2)


def format_stats(db_stats: dict) -> str:
    """Format database statistics for display."""
    labels = {
        "issues": "Issues",
        "pull_requests": "Pull Requests",
        "comments": "Comments",
        "summaries": "Summaries",
        "assembled": "Assembled",
        "embedded": "Embedded",
        "model_id": "Model ID",
    }

    lines = ["Database Statistics:"]
    for key, label in labels.items():
        if key in db_stats:
            value = db_stats[key]
            lines.append(f"  {label + ':':<19}{value}")

    return "\n".join(lines)
