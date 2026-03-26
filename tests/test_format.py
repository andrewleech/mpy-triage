"""Tests for output formatting."""

import json

from mpy_triage.assess import Assessment
from mpy_triage.format import format_human, format_json, format_stats, github_url


def _make_query_item():
    return {
        "repo": "micropython/micropython",
        "number": 100,
        "title": "Bug in UART driver",
        "item_type": "issue",
    }


def _make_assessments():
    return [
        Assessment(
            item_number=50,
            item_type="issue",
            repo="micropython/micropython",
            classification="DUPLICATE",
            confidence="high",
            reasoning="Same UART crash on STM32.",
            suggested_action="Close as duplicate of #50.",
        ),
        Assessment(
            item_number=200,
            item_type="pull_request",
            repo="micropython/micropython",
            classification="RELATED",
            confidence="medium",
            reasoning="Fixes a different UART bug but touches same code.",
            suggested_action="Review for overlap.",
        ),
    ]


class TestGithubUrl:
    def test_issue_url(self):
        url = github_url("micropython/micropython", "issue", 42)
        assert url == "https://github.com/micropython/micropython/issues/42"

    def test_pr_url(self):
        url = github_url("micropython/micropython", "pull_request", 99)
        assert url == "https://github.com/micropython/micropython/pull/99"


class TestFormatHuman:
    def test_with_assessments(self):
        query = _make_query_item()
        assessments = _make_assessments()
        output = format_human(query, assessments)

        assert "micropython/micropython#100" in output
        assert '"Bug in UART driver"' in output
        assert "Found 2 candidates:" in output
        assert "#50 [DUPLICATE - high confidence]" in output
        assert "#200 [RELATED - medium confidence]" in output
        assert "Same UART crash on STM32." in output
        assert "Close as duplicate of #50." in output
        # PR should use /pull/ URL
        assert "/pull/200" in output
        # Issue should use /issues/ URL
        assert "/issues/50" in output

    def test_empty_assessments(self):
        query = _make_query_item()
        output = format_human(query, [])

        assert "micropython/micropython#100" in output
        assert "No similar items found." in output
        assert "candidates" not in output


class TestFormatJson:
    def test_produces_valid_json(self):
        query = _make_query_item()
        assessments = _make_assessments()
        output = format_json(query, assessments)

        data = json.loads(output)
        assert data["query"]["number"] == 100
        assert data["query"]["repo"] == "micropython/micropython"
        assert len(data["assessments"]) == 2
        assert data["assessments"][0]["classification"] == "DUPLICATE"
        assert data["assessments"][1]["item_type"] == "pull_request"

    def test_empty_assessments(self):
        query = _make_query_item()
        output = format_json(query, [])

        data = json.loads(output)
        assert data["assessments"] == []

    def test_urls_in_json(self):
        query = _make_query_item()
        assessments = _make_assessments()
        output = format_json(query, assessments)

        data = json.loads(output)
        assert data["query"]["url"] == "https://github.com/micropython/micropython/issues/100"
        assert data["assessments"][1]["url"] == (
            "https://github.com/micropython/micropython/pull/200"
        )


class TestFormatStats:
    def test_basic_stats(self):
        stats = {
            "issues": 1234,
            "pull_requests": 5678,
            "comments": 9999,
            "summaries": 500,
            "assembled": 400,
            "embedded": 350,
            "model_id": "Qwen/Qwen3-Embedding-0.6B",
        }
        output = format_stats(stats)

        assert "Database Statistics:" in output
        assert "Issues:" in output
        assert "1234" in output
        assert "Pull Requests:" in output
        assert "5678" in output
        assert "Model ID:" in output
        assert "Qwen/Qwen3-Embedding-0.6B" in output

    def test_partial_stats(self):
        stats = {"issues": 10, "pull_requests": 20}
        output = format_stats(stats)

        assert "Issues:" in output
        assert "10" in output
        # Keys not present should not appear
        assert "Model ID:" not in output
