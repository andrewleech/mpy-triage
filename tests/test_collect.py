"""Tests for GitHub data collector."""

import json
from unittest.mock import patch

from mpy_triage.collect import (
    collect_all,
    collect_comments,
    collect_issues,
    collect_pr_diffs,
    collect_pull_requests,
    collect_review_comments,
)

REPO = "micropython/micropython"


def _make_issue(number, **overrides):
    item = {
        "id": 1000 + number,
        "number": number,
        "title": f"Issue #{number}",
        "body": f"Body of issue #{number}",
        "user": {"login": "testuser"},
        "state": "open",
        "state_reason": None,
        "labels": [{"name": "bug"}, {"name": "stm32"}],
        "milestone": {"title": "v1.20"},
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-06-01T00:00:00Z",
        "closed_at": None,
    }
    item.update(overrides)
    return item


def _make_pr(number, **overrides):
    item = {
        "id": 2000 + number,
        "number": number,
        "title": f"PR #{number}",
        "body": f"Body of PR #{number}",
        "user": {"login": "contributor"},
        "state": "open",
        "draft": False,
        "labels": [{"name": "enhancement"}],
        "created_at": "2024-02-01T00:00:00Z",
        "updated_at": "2024-06-01T00:00:00Z",
        "closed_at": None,
        "merged_at": None,
        "base": {"ref": "master"},
        "changed_files": 3,
        "additions": 50,
        "deletions": 10,
    }
    item.update(overrides)
    return item


def _make_comment(comment_id, item_number, **overrides):
    item = {
        "id": comment_id,
        "body": f"Comment {comment_id}",
        "user": {"login": "reviewer"},
        "created_at": "2024-03-01T00:00:00Z",
        "updated_at": "2024-03-01T00:00:00Z",
        "issue_url": f"https://api.github.com/repos/{REPO}/issues/{item_number}",
    }
    item.update(overrides)
    return item


def _make_review_comment(comment_id, pr_number, **overrides):
    item = {
        "id": comment_id,
        "body": f"Review comment {comment_id}",
        "user": {"login": "reviewer"},
        "path": "py/obj.c",
        "diff_hunk": "@@ -1,3 +1,4 @@",
        "created_at": "2024-04-01T00:00:00Z",
        "pull_request_url": f"https://api.github.com/repos/{REPO}/pulls/{pr_number}",
    }
    item.update(overrides)
    return item


class TestCollectIssues:
    @patch("mpy_triage.collect.gh_search")
    @patch("mpy_triage.collect.get_sync_state", return_value=None)
    @patch("mpy_triage.collect.set_sync_state")
    def test_full_sync_inserts_rows(self, mock_set, mock_get, mock_search, tmp_db):
        issues = [_make_issue(1), _make_issue(2)]
        mock_search.return_value = issues

        count = collect_issues(tmp_db, REPO)

        rows = tmp_db.execute("SELECT * FROM issues WHERE repo = ?", (REPO,)).fetchall()
        assert len(rows) == 2
        assert count == 2

    @patch("mpy_triage.collect.gh_api")
    @patch("mpy_triage.collect.get_sync_state", return_value="2024-05-01T00:00:00Z")
    @patch("mpy_triage.collect.set_sync_state")
    def test_incremental_sync_uses_since(self, mock_set, mock_get, mock_api, tmp_db):
        mock_api.return_value = [_make_issue(10)]

        count = collect_issues(tmp_db, REPO)

        assert count == 1
        call_args = mock_api.call_args
        assert "since=2024-05-01T00:00:00Z" in call_args[0][0]

    @patch("mpy_triage.collect.gh_api")
    @patch("mpy_triage.collect.get_sync_state", return_value="2024-05-01T00:00:00Z")
    @patch("mpy_triage.collect.set_sync_state")
    def test_incremental_filters_out_prs(self, mock_set, mock_get, mock_api, tmp_db):
        """The /issues endpoint returns PRs too; they should be filtered out."""
        issue = _make_issue(10)
        pr_as_issue = _make_issue(11, pull_request={"url": "..."})
        mock_api.return_value = [issue, pr_as_issue]

        count = collect_issues(tmp_db, REPO)

        assert count == 1

    @patch("mpy_triage.collect.gh_search")
    @patch("mpy_triage.collect.get_sync_state", return_value=None)
    @patch("mpy_triage.collect.set_sync_state")
    def test_labels_stored_as_json(self, mock_set, mock_get, mock_search, tmp_db):
        issue = _make_issue(5, labels=[{"name": "bug"}, {"name": "rp2"}])
        mock_search.return_value = [issue]

        collect_issues(tmp_db, REPO)

        row = tmp_db.execute(
            "SELECT labels FROM issues WHERE number = ? AND repo = ?", (5, REPO)
        ).fetchone()
        assert row is not None
        labels = json.loads(row["labels"])
        assert labels == ["bug", "rp2"]


class TestCollectPullRequests:
    @patch("mpy_triage.collect.gh_search")
    @patch("mpy_triage.collect.get_sync_state", return_value=None)
    @patch("mpy_triage.collect.set_sync_state")
    def test_full_sync_inserts_prs(self, mock_set, mock_get, mock_search, tmp_db):
        prs = [_make_pr(100)]
        mock_search.return_value = prs

        collect_pull_requests(tmp_db, REPO)

        rows = tmp_db.execute(
            "SELECT * FROM pull_requests WHERE repo = ?", (REPO,)
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["number"] == 100

    @patch("mpy_triage.collect.gh_search")
    @patch("mpy_triage.collect.get_sync_state", return_value=None)
    @patch("mpy_triage.collect.set_sync_state")
    def test_merged_state(self, mock_set, mock_get, mock_search, tmp_db):
        pr = _make_pr(101, state="closed", merged_at="2024-05-01T00:00:00Z")
        mock_search.return_value = [pr]

        collect_pull_requests(tmp_db, REPO)

        row = tmp_db.execute(
            "SELECT state FROM pull_requests WHERE number = ? AND repo = ?", (101, REPO)
        ).fetchone()
        assert row["state"] == "merged"


class TestCollectPrDiffs:
    @patch("mpy_triage.collect.gh_diff")
    def test_collects_missing_diffs(self, mock_diff, tmp_db):
        tmp_db.execute(
            """INSERT INTO pull_requests
               (id, number, repo, title, state, created_at)
               VALUES (1, 50, ?, 'Test PR', 'open', '2024-01-01')""",
            (REPO,),
        )
        tmp_db.commit()

        mock_diff.return_value = "diff --git a/file.c b/file.c\n+new line"

        count = collect_pr_diffs(tmp_db, REPO)

        assert count == 1
        row = tmp_db.execute(
            "SELECT diff_text FROM pr_diffs WHERE pr_number = 50 AND repo = ?", (REPO,)
        ).fetchone()
        assert row is not None
        assert "new line" in row["diff_text"]

    @patch("mpy_triage.collect.gh_diff")
    def test_skips_existing_diffs(self, mock_diff, tmp_db):
        tmp_db.execute(
            """INSERT INTO pull_requests
               (id, number, repo, title, state, created_at)
               VALUES (1, 50, ?, 'Test PR', 'open', '2024-01-01')""",
            (REPO,),
        )
        tmp_db.execute(
            "INSERT INTO pr_diffs (pr_number, repo, diff_text) VALUES (50, ?, 'old diff')",
            (REPO,),
        )
        tmp_db.commit()

        count = collect_pr_diffs(tmp_db, REPO)

        assert count == 0
        mock_diff.assert_not_called()


class TestCollectComments:
    @patch("mpy_triage.collect.gh_api")
    @patch("mpy_triage.collect.get_sync_state", return_value=None)
    @patch("mpy_triage.collect.set_sync_state")
    def test_classifies_pr_comments(self, mock_set, mock_get, mock_api, tmp_db):
        tmp_db.execute(
            """INSERT INTO pull_requests
               (id, number, repo, title, state, created_at)
               VALUES (1, 42, ?, 'Some PR', 'open', '2024-01-01')""",
            (REPO,),
        )
        tmp_db.commit()

        comment_on_pr = _make_comment(500, 42)
        comment_on_issue = _make_comment(501, 99)
        mock_api.return_value = [comment_on_pr, comment_on_issue]

        count = collect_comments(tmp_db, REPO)

        assert count == 2
        pr_comment = tmp_db.execute(
            "SELECT item_type FROM comments WHERE id = 500"
        ).fetchone()
        assert pr_comment["item_type"] == "pull_request"

        issue_comment = tmp_db.execute(
            "SELECT item_type FROM comments WHERE id = 501"
        ).fetchone()
        assert issue_comment["item_type"] == "issue"


class TestCollectReviewComments:
    @patch("mpy_triage.collect.gh_api")
    @patch("mpy_triage.collect.get_sync_state", return_value=None)
    @patch("mpy_triage.collect.set_sync_state")
    def test_inserts_review_comments(self, mock_set, mock_get, mock_api, tmp_db):
        rc = _make_review_comment(600, 42)
        mock_api.return_value = [rc]

        count = collect_review_comments(tmp_db, REPO)

        assert count == 1
        row = tmp_db.execute("SELECT * FROM review_comments WHERE id = 600").fetchone()
        assert row["pr_number"] == 42
        assert row["path"] == "py/obj.c"


class TestCollectAll:
    @patch("mpy_triage.collect.collect_review_comments", return_value=5)
    @patch("mpy_triage.collect.collect_comments", return_value=10)
    @patch("mpy_triage.collect.collect_pr_diffs", return_value=3)
    @patch("mpy_triage.collect.collect_pull_requests", return_value=20)
    @patch("mpy_triage.collect.collect_issues", return_value=50)
    def test_calls_all_subcollectors(self, m_iss, m_pr, m_diff, m_com, m_rc, tmp_db):
        result = collect_all(tmp_db, REPO)

        assert result == {
            "issues": 50,
            "pull_requests": 20,
            "pr_diffs": 3,
            "comments": 10,
            "review_comments": 5,
        }
        m_iss.assert_called_once_with(tmp_db, REPO)
        m_pr.assert_called_once_with(tmp_db, REPO)
        m_diff.assert_called_once_with(tmp_db, REPO)
        m_com.assert_called_once_with(tmp_db, REPO)
        m_rc.assert_called_once_with(tmp_db, REPO)
