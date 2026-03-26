"""Tests for Haiku summarization."""

import json
import os
import subprocess
from unittest.mock import patch

from mpy_triage.summarize import (
    _build_context,
    _clean_env,
    _get_json_schema,
    summarize_item,
)

SAMPLE_HAIKU_OUTPUT = {
    "components": ["py/objstr"],
    "item_category": "bug_report",
    "synopsis": "str.split() crashes when separator is empty string.",
    "affected_code": ["py/objstr.c", "mp_obj_str_split"],
    "error_signatures": "FATAL: uncaught exception ValueError",
    "concepts": ["string splitting", "CPython compatibility", "ValueError"],
}


def _insert_issue(conn, number=42, repo="micropython/micropython"):
    conn.execute(
        "INSERT INTO issues (number, repo, title, body, labels, state, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            number,
            repo,
            "str.split crashes on empty sep",
            "Calling `'abc'.split('')` causes a crash.",
            '["bug", "py-core"]',
            "open",
            "2024-01-01T00:00:00Z",
            "2024-01-02T00:00:00Z",
        ),
    )
    conn.commit()


def _insert_pr(conn, number=100, repo="micropython/micropython"):
    conn.execute(
        "INSERT INTO pull_requests "
        "(number, repo, title, body, labels, state, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            number,
            repo,
            "Fix str.split for empty separator",
            "Fixes the crash by raising ValueError.",
            '["bugfix"]',
            "open",
            "2024-01-03T00:00:00Z",
            "2024-01-04T00:00:00Z",
        ),
    )
    conn.commit()


def _insert_comment(conn, item_number, item_type, body, repo="micropython/micropython"):
    conn.execute(
        "INSERT INTO comments (item_number, item_type, repo, author, body, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (item_number, item_type, repo, "testuser", body, "2024-01-02T00:00:00Z"),
    )
    conn.commit()


def _insert_review_comment(conn, pr_number, body, path=None, repo="micropython/micropython"):
    conn.execute(
        "INSERT INTO review_comments (pr_number, repo, author, body, path, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (pr_number, repo, "reviewer", body, path, "2024-01-04T00:00:00Z"),
    )
    conn.commit()


def _insert_diff(conn, pr_number, diff_text, repo="micropython/micropython"):
    conn.execute(
        "INSERT INTO pr_diffs (pr_number, repo, diff_text) VALUES (?, ?, ?)",
        (pr_number, repo, diff_text),
    )
    conn.commit()


class TestCleanEnv:
    def test_strips_claudecode_vars(self):
        with patch.dict(os.environ, {"CLAUDECODE_SESSION": "abc", "PATH": "/usr/bin"}, clear=True):
            env = _clean_env()
            assert "CLAUDECODE_SESSION" not in env
            assert env["PATH"] == "/usr/bin"

    def test_strips_all_claudecode_prefixed(self):
        extras = {"CLAUDECODE_FOO": "1", "CLAUDECODEBAR": "2", "HOME": "/home/test"}
        with patch.dict(os.environ, extras, clear=True):
            env = _clean_env()
            assert "CLAUDECODE_FOO" not in env
            assert "CLAUDECODEBAR" not in env
            assert env["HOME"] == "/home/test"

    def test_preserves_non_claudecode_vars(self):
        with patch.dict(os.environ, {"CLAUDE_API_KEY": "key", "TERM": "xterm"}, clear=True):
            env = _clean_env()
            assert env["CLAUDE_API_KEY"] == "key"
            assert env["TERM"] == "xterm"


class TestGetJsonSchema:
    def test_returns_valid_json(self):
        schema_str = _get_json_schema()
        schema = json.loads(schema_str)
        assert schema["type"] == "object"
        assert "components" in schema["properties"]
        assert "synopsis" in schema["properties"]
        assert set(schema["required"]) == {
            "components",
            "item_category",
            "synopsis",
            "affected_code",
            "error_signatures",
            "concepts",
        }


class TestBuildContext:
    def test_issue_with_comments(self, tmp_db):
        _insert_issue(tmp_db)
        _insert_comment(tmp_db, 42, "issue", "I can reproduce this on PYBV10.")

        ctx = _build_context(tmp_db, "micropython/micropython", 42, "issue")
        assert "str.split crashes on empty sep" in ctx
        assert "bug" in ctx
        assert "I can reproduce this on PYBV10" in ctx

    def test_pr_with_diff_and_review_comments(self, tmp_db):
        _insert_pr(tmp_db)
        _insert_review_comment(tmp_db, 100, "Use mp_raise_ValueError here.", "py/objstr.c")
        _insert_diff(tmp_db, 100, "diff --git a/py/objstr.c\n+    mp_raise(ValueError);")

        ctx = _build_context(tmp_db, "micropython/micropython", 100, "pull_request")
        assert "Fix str.split for empty separator" in ctx
        assert "Review Comments" in ctx
        assert "mp_raise_ValueError" in ctx
        assert "Diff" in ctx
        assert "mp_raise(ValueError)" in ctx

    def test_pr_diff_truncation(self, tmp_db):
        _insert_pr(tmp_db)
        long_diff = "x" * 20000
        _insert_diff(tmp_db, 100, long_diff)

        ctx = _build_context(tmp_db, "micropython/micropython", 100, "pull_request")
        assert "[truncated]" in ctx

    def test_missing_item_returns_empty(self, tmp_db):
        ctx = _build_context(tmp_db, "micropython/micropython", 999, "issue")
        assert ctx == ""

    def test_linked_items(self, tmp_db):
        _insert_issue(tmp_db, number=42)
        _insert_issue(tmp_db, number=10)
        tmp_db.execute(
            "UPDATE issues SET title = 'Original split bug' WHERE number = 10"
        )
        tmp_db.execute(
            "INSERT INTO cross_references "
            "(source_number, source_type, source_repo, target_number, target_type, "
            "target_repo, relationship, extracted_from) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                42,
                "issue",
                "micropython/micropython",
                10,
                "issue",
                "micropython/micropython",
                "duplicate_of",
                "body",
            ),
        )
        tmp_db.commit()

        ctx = _build_context(tmp_db, "micropython/micropython", 42, "issue")
        assert "Linked Items" in ctx
        assert "Original split bug" in ctx
        assert "duplicate_of" in ctx


class TestSummarizeItem:
    def _mock_subprocess_result(self, output_dict):
        """Create a mock CompletedProcess returning the given dict as JSON."""
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(output_dict),
            stderr="",
        )

    def test_successful_summarization(self, tmp_db):
        _insert_issue(tmp_db)
        mock_result = self._mock_subprocess_result(SAMPLE_HAIKU_OUTPUT)

        with patch("mpy_triage.summarize.subprocess.run", return_value=mock_result):
            result = summarize_item(tmp_db, "micropython/micropython", 42, "issue")

        assert result is not None
        assert result["synopsis"] == SAMPLE_HAIKU_OUTPUT["synopsis"]
        assert result["components"] == ["py/objstr"]

        # Verify DB insertion
        row = tmp_db.execute(
            "SELECT * FROM summaries WHERE item_number = 42 AND item_type = 'issue'"
        ).fetchone()
        assert row is not None
        assert row["model_id"] == "haiku"
        assert row["synopsis"] == SAMPLE_HAIKU_OUTPUT["synopsis"]
        assert json.loads(row["components"]) == ["py/objstr"]

    def test_structured_output_wrapper(self, tmp_db):
        """Test handling of Claude CLI's structured_output wrapper."""
        _insert_issue(tmp_db)
        wrapped = {"structured_output": SAMPLE_HAIKU_OUTPUT}
        mock_result = self._mock_subprocess_result(wrapped)

        with patch("mpy_triage.summarize.subprocess.run", return_value=mock_result):
            result = summarize_item(tmp_db, "micropython/micropython", 42, "issue")

        assert result is not None
        assert result["synopsis"] == SAMPLE_HAIKU_OUTPUT["synopsis"]

    def test_timeout_returns_none(self, tmp_db):
        _insert_issue(tmp_db)

        with patch(
            "mpy_triage.summarize.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=300),
        ):
            result = summarize_item(tmp_db, "micropython/micropython", 42, "issue")

        assert result is None
        row = tmp_db.execute("SELECT * FROM summaries WHERE item_number = 42").fetchone()
        assert row is None

    def test_invalid_json_returns_none(self, tmp_db):
        _insert_issue(tmp_db)
        bad_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="not json at all", stderr=""
        )

        with patch("mpy_triage.summarize.subprocess.run", return_value=bad_result):
            result = summarize_item(tmp_db, "micropython/micropython", 42, "issue")

        assert result is None

    def test_nonzero_returncode_returns_none(self, tmp_db):
        _insert_issue(tmp_db)
        fail_result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="error"
        )

        with patch("mpy_triage.summarize.subprocess.run", return_value=fail_result):
            result = summarize_item(tmp_db, "micropython/micropython", 42, "issue")

        assert result is None

    def test_missing_item_returns_none(self, tmp_db):
        result = summarize_item(tmp_db, "micropython/micropython", 999, "issue")
        assert result is None
