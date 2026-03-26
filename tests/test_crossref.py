"""Tests for mpy_triage.crossref module."""

import sqlite3

import pytest

from mpy_triage.crossref import (
    build_ground_truth,
    extract_cross_references,
    parse_references,
)
from mpy_triage.db import init_db

REPO = "micropython/micropython"


# ---------------------------------------------------------------------------
# parse_references
# ---------------------------------------------------------------------------


class TestParseReferencesBasic:
    """Basic keyword patterns."""

    def test_fixes_hash(self):
        refs = parse_references("Fixes #123", REPO)
        assert len(refs) == 1
        assert refs[0].target_number == 123
        assert refs[0].relationship == "fixes"
        assert refs[0].target_repo == REPO

    def test_fixed_hash(self):
        refs = parse_references("Fixed #456", REPO)
        assert len(refs) == 1
        assert refs[0].target_number == 456
        assert refs[0].relationship == "fixes"

    def test_fix_hash(self):
        refs = parse_references("Fix #789", REPO)
        assert len(refs) == 1
        assert refs[0].target_number == 789
        assert refs[0].relationship == "fixes"

    def test_closes_hash(self):
        refs = parse_references("Closes #100", REPO)
        assert len(refs) == 1
        assert refs[0].target_number == 100
        assert refs[0].relationship == "closes"

    def test_closed_hash(self):
        refs = parse_references("Closed #101", REPO)
        assert len(refs) == 1
        assert refs[0].relationship == "closes"

    def test_close_hash(self):
        refs = parse_references("Close #102", REPO)
        assert len(refs) == 1
        assert refs[0].relationship == "closes"

    def test_duplicate_of_hash(self):
        refs = parse_references("Duplicate of #789", REPO)
        assert len(refs) == 1
        assert refs[0].target_number == 789
        assert refs[0].relationship == "duplicate_of"

    def test_duplicates_of_hash(self):
        refs = parse_references("Duplicates of #790", REPO)
        assert len(refs) == 1
        assert refs[0].target_number == 790
        assert refs[0].relationship == "duplicate_of"

    def test_related_to_hash(self):
        refs = parse_references("Related to #50", REPO)
        assert len(refs) == 1
        assert refs[0].target_number == 50
        assert refs[0].relationship == "related"

    def test_see_also_hash(self):
        refs = parse_references("See also #60", REPO)
        assert len(refs) == 1
        assert refs[0].target_number == 60
        assert refs[0].relationship == "related"

    def test_see_hash(self):
        refs = parse_references("See #70", REPO)
        assert len(refs) == 1
        assert refs[0].target_number == 70
        assert refs[0].relationship == "related"


class TestParseReferencesCaseInsensitive:
    """Case-insensitive matching."""

    def test_lowercase_fixes(self):
        refs = parse_references("fixes #123", REPO)
        assert len(refs) == 1
        assert refs[0].relationship == "fixes"

    def test_uppercase_fixes(self):
        refs = parse_references("FIXES #123", REPO)
        assert len(refs) == 1
        assert refs[0].relationship == "fixes"

    def test_mixed_case_duplicate(self):
        refs = parse_references("duplicate OF #10", REPO)
        assert len(refs) == 1
        assert refs[0].relationship == "duplicate_of"

    def test_lowercase_closes(self):
        refs = parse_references("closes #5", REPO)
        assert len(refs) == 1
        assert refs[0].relationship == "closes"


class TestParseReferencesCrossRepo:
    """Cross-repository references."""

    def test_cross_repo_fixes(self):
        refs = parse_references("Fixes micropython/micropython-lib#42", REPO)
        assert len(refs) == 1
        assert refs[0].target_repo == "micropython/micropython-lib"
        assert refs[0].target_number == 42
        assert refs[0].relationship == "fixes"

    def test_cross_repo_duplicate(self):
        refs = parse_references("Duplicate of other-org/other-repo#99", REPO)
        assert len(refs) == 1
        assert refs[0].target_repo == "other-org/other-repo"
        assert refs[0].target_number == 99

    def test_cross_repo_source_repo_unchanged(self):
        refs = parse_references("Fixes micropython/micropython-lib#42", REPO)
        assert refs[0].source_repo == REPO


class TestParseReferencesMultiple:
    """Multiple references in one text."""

    def test_multiple_different_relationships(self):
        text = "Fixes #1 and related to #2"
        refs = parse_references(text, REPO)
        assert len(refs) == 2
        numbers = {r.target_number for r in refs}
        assert numbers == {1, 2}
        rels = {r.relationship for r in refs}
        assert "fixes" in rels
        assert "related" in rels

    def test_multiple_same_relationship(self):
        text = "Fixes #10, also fixes #20"
        refs = parse_references(text, REPO)
        assert len(refs) == 2

    def test_deduplication(self):
        text = "Fixes #1. Also fixes #1."
        refs = parse_references(text, REPO)
        assert len(refs) == 1


class TestParseReferencesCodeBlocks:
    """References inside code blocks should be ignored."""

    def test_ref_inside_code_block(self):
        text = "Some text\n```\nFixes #999\n```\nMore text"
        refs = parse_references(text, REPO)
        assert len(refs) == 0

    def test_ref_outside_code_block(self):
        text = "Fixes #100\n```\nsome code\n```\nFixes #200"
        refs = parse_references(text, REPO)
        numbers = {r.target_number for r in refs}
        assert 100 in numbers
        assert 200 in numbers

    def test_ref_only_inside_code_block(self):
        text = "```python\nFixes #123\n```"
        refs = parse_references(text, REPO)
        assert len(refs) == 0


class TestParseReferencesEdgeCases:
    """Edge cases and non-matches."""

    def test_bare_hash_no_match(self):
        refs = parse_references("#123", REPO)
        assert len(refs) == 0

    def test_pr_hash_no_match(self):
        refs = parse_references("PR #123", REPO)
        assert len(refs) == 0

    def test_issue_hash_no_match(self):
        refs = parse_references("Issue #123", REPO)
        assert len(refs) == 0

    def test_empty_text(self):
        refs = parse_references("", REPO)
        assert len(refs) == 0

    def test_none_text(self):
        refs = parse_references(None, REPO)
        assert len(refs) == 0

    def test_no_references(self):
        refs = parse_references("This is just normal text.", REPO)
        assert len(refs) == 0

    def test_source_fields_default(self):
        refs = parse_references("Fixes #1", REPO)
        assert refs[0].source_number == 0
        assert refs[0].source_type == ""


# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db():
    """Create an in-memory SQLite database with schema."""
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# extract_cross_references
# ---------------------------------------------------------------------------


class TestExtractCrossReferences:
    """Test extraction from database records."""

    def test_extract_from_issues(self, tmp_db):
        tmp_db.execute(
            "INSERT INTO issues (number, title, body, state) VALUES (?, ?, ?, ?)",
            (1, "Bug report", "Fixes #100", "open"),
        )
        tmp_db.commit()

        count = extract_cross_references(tmp_db, REPO)
        assert count == 1

        rows = tmp_db.execute("SELECT * FROM cross_references").fetchall()
        assert len(rows) == 1
        # id, source_repo, source_number, source_type, target_repo, target_number, relationship
        assert rows[0][1] == REPO
        assert rows[0][2] == 1  # source_number
        assert rows[0][3] == "issue"  # source_type
        assert rows[0][4] == REPO  # target_repo
        assert rows[0][5] == 100  # target_number
        assert rows[0][6] == "fixes"  # relationship

    def test_extract_from_pull_requests(self, tmp_db):
        tmp_db.execute(
            "INSERT INTO pull_requests (number, title, body, state) VALUES (?, ?, ?, ?)",
            (50, "Fix something", "Closes #200", "open"),
        )
        tmp_db.commit()

        count = extract_cross_references(tmp_db, REPO)
        assert count == 1

        rows = tmp_db.execute("SELECT * FROM cross_references").fetchall()
        assert rows[0][3] == "pr"

    def test_extract_from_comments(self, tmp_db):
        tmp_db.execute(
            "INSERT INTO issues (number, title, body, state) VALUES (?, ?, ?, ?)",
            (10, "An issue", "", "open"),
        )
        tmp_db.execute(
            "INSERT INTO comments (id, issue_number, body) VALUES (?, ?, ?)",
            (1, 10, "Duplicate of #300"),
        )
        tmp_db.commit()

        count = extract_cross_references(tmp_db, REPO)
        assert count == 1

        rows = tmp_db.execute("SELECT * FROM cross_references").fetchall()
        assert rows[0][3] == "comment"
        assert rows[0][6] == "duplicate_of"

    def test_no_duplicates_on_rerun(self, tmp_db):
        tmp_db.execute(
            "INSERT INTO issues (number, title, body, state) VALUES (?, ?, ?, ?)",
            (1, "Bug", "Fixes #100", "open"),
        )
        tmp_db.commit()

        count1 = extract_cross_references(tmp_db, REPO)
        count2 = extract_cross_references(tmp_db, REPO)
        assert count1 == 1
        assert count2 == 0

    def test_null_body_skipped(self, tmp_db):
        tmp_db.execute(
            "INSERT INTO issues (number, title, body, state) VALUES (?, ?, ?, ?)",
            (1, "Bug", None, "open"),
        )
        tmp_db.commit()

        count = extract_cross_references(tmp_db, REPO)
        assert count == 0

    def test_multiple_sources(self, tmp_db):
        tmp_db.execute(
            "INSERT INTO issues (number, title, body, state) VALUES (?, ?, ?, ?)",
            (1, "Issue A", "Fixes #100", "open"),
        )
        tmp_db.execute(
            "INSERT INTO pull_requests (number, title, body, state) VALUES (?, ?, ?, ?)",
            (2, "PR B", "Related to #200", "open"),
        )
        tmp_db.execute(
            "INSERT INTO comments (id, issue_number, body) VALUES (?, ?, ?)",
            (1, 1, "See also #300"),
        )
        tmp_db.commit()

        count = extract_cross_references(tmp_db, REPO)
        assert count == 3


# ---------------------------------------------------------------------------
# build_ground_truth
# ---------------------------------------------------------------------------


class TestBuildGroundTruth:
    """Test ground truth construction from duplicate issues."""

    def test_duplicate_issue_with_body_ref(self, tmp_db):
        tmp_db.execute(
            "INSERT INTO issues (number, title, body, state, state_reason) VALUES (?, ?, ?, ?, ?)",
            (10, "Dup issue", "Duplicate of #5", "closed", "duplicate"),
        )
        tmp_db.commit()

        count = build_ground_truth(tmp_db, REPO)
        assert count == 1

        rows = tmp_db.execute("SELECT * FROM ground_truth").fetchall()
        assert len(rows) == 1
        assert rows[0][1] == REPO  # source_repo
        assert rows[0][2] == 10  # source_number
        assert rows[0][3] == REPO  # target_repo
        assert rows[0][4] == 5  # target_number
        assert rows[0][5] == "duplicate"  # relationship

    def test_duplicate_issue_with_comment_ref(self, tmp_db):
        tmp_db.execute(
            "INSERT INTO issues (number, title, body, state, state_reason) VALUES (?, ?, ?, ?, ?)",
            (20, "Dup issue", "This is a duplicate", "closed", "duplicate"),
        )
        tmp_db.execute(
            "INSERT INTO comments (id, issue_number, body) VALUES (?, ?, ?)",
            (1, 20, "Duplicate of #15"),
        )
        tmp_db.commit()

        count = build_ground_truth(tmp_db, REPO)
        assert count == 1

        rows = tmp_db.execute("SELECT * FROM ground_truth").fetchall()
        assert rows[0][2] == 20
        assert rows[0][4] == 15

    def test_duplicate_from_cross_references(self, tmp_db):
        # Pre-populate cross_references with a duplicate_of entry
        tmp_db.execute(
            """INSERT INTO cross_references
            (source_repo, source_number, source_type, target_repo, target_number, relationship)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (REPO, 30, "issue", REPO, 25, "duplicate_of"),
        )
        tmp_db.commit()

        count = build_ground_truth(tmp_db, REPO)
        assert count == 1

    def test_no_duplicates_on_rerun(self, tmp_db):
        tmp_db.execute(
            "INSERT INTO issues (number, title, body, state, state_reason) VALUES (?, ?, ?, ?, ?)",
            (10, "Dup", "Duplicate of #5", "closed", "duplicate"),
        )
        tmp_db.commit()

        count1 = build_ground_truth(tmp_db, REPO)
        count2 = build_ground_truth(tmp_db, REPO)
        assert count1 == 1
        assert count2 == 0

    def test_non_duplicate_issue_ignored(self, tmp_db):
        tmp_db.execute(
            "INSERT INTO issues (number, title, body, state, state_reason) VALUES (?, ?, ?, ?, ?)",
            (10, "Normal", "Fixes #5", "closed", "completed"),
        )
        tmp_db.commit()

        count = build_ground_truth(tmp_db, REPO)
        assert count == 0

    def test_duplicate_issue_without_ref(self, tmp_db):
        """Duplicate issue with no 'Duplicate of' reference yields no ground truth."""
        tmp_db.execute(
            "INSERT INTO issues (number, title, body, state, state_reason) VALUES (?, ?, ?, ?, ?)",
            (10, "Dup", "This is a duplicate somehow", "closed", "duplicate"),
        )
        tmp_db.commit()

        count = build_ground_truth(tmp_db, REPO)
        assert count == 0
