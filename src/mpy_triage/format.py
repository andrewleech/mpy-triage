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
        classification = a.classification.upper()
        confidence = a.confidence

        lines.append(f"#{a.item_number} [{classification} - {confidence} confidence]")
        lines.append(f"  {candidate_url}")
        lines.append(f"  Reasoning: {a.reasoning}")
        lines.append(f"  Suggested action: {a.suggested_action}")
        lines.append("")

    return "\n".join(lines)


def format_candidates_human(query_item: dict, candidates: list[dict]) -> str:
    """Format raw search candidates (no assessment) for terminal output."""
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

    if not candidates:
        lines.append("No similar items found.")
        return "\n".join(lines)

    lines.append(f"Found {len(candidates)} candidates (unassessed):")
    lines.append("")

    for c in candidates:
        c_number = c.get("item_number", c.get("number", "?"))
        c_type = c.get("item_type", "issue")
        c_repo = c.get("repo", repo)
        c_url = github_url(c_repo, c_type, c_number)
        score = c.get("rerank_score") or c.get("rrf_score") or c.get("score", 0)

        lines.append(f"#{c_number} [score: {score:.4f}]")
        lines.append(f"  {c_url}")
        lines.append("")

    return "\n".join(lines)


def format_candidates_json(query_item: dict, candidates: list[dict]) -> str:
    """Format raw search candidates as JSON."""
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
        "candidates": [
            {
                "item_number": c.get("item_number", c.get("number")),
                "item_type": c.get("item_type", "issue"),
                "repo": c.get("repo", repo),
                "score": c.get("rerank_score") or c.get("rrf_score") or c.get("score", 0),
                "url": github_url(
                    c.get("repo", repo),
                    c.get("item_type", "issue"),
                    c.get("item_number", c.get("number", 0)),
                ),
            }
            for c in candidates
        ],
    }
    return json.dumps(result, indent=2)


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
