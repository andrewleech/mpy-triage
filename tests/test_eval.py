"""Tests for eval framework."""

from mpy_triage.eval import (
    EvalReport,
    _format_summary_for_judge,
    format_eval_report,
    sample_items,
)

SAMPLE_SUMMARY = {
    "components": ["py/objstr"],
    "item_category": "bug_report",
    "synopsis": "str.split crashes on empty sep.",
    "affected_code": ["py/objstr.c"],
    "error_signatures": "ValueError",
    "concepts": ["string", "split"],
}


def _insert_haiku_summary(conn, number, item_type="issue",
                          repo="micropython/micropython",
                          category="bug_report"):
    conn.execute(
        "INSERT INTO summaries "
        "(item_number, item_type, repo, model_id, components, item_category, "
        "synopsis, affected_code, error_signatures, concepts, created_at) "
        "VALUES (?, ?, ?, 'haiku', '[]', ?, 'Test synopsis', '[]', '', '[]', '')",
        (number, item_type, repo, category),
    )
    conn.commit()


class TestSampleItems:
    def test_basic_sample(self, tmp_db):
        for i in range(10):
            _insert_haiku_summary(tmp_db, i + 1)

        items = sample_items(tmp_db, n=5, stratify=False)
        assert len(items) == 5
        assert all("item_number" in it for it in items)

    def test_stratified_sample(self, tmp_db):
        for i in range(6):
            _insert_haiku_summary(tmp_db, i + 1, category="bug_report")
        for i in range(4):
            _insert_haiku_summary(tmp_db, i + 100, category="feature_request")

        items = sample_items(tmp_db, n=5, stratify=True)
        assert len(items) == 5

    def test_empty_db(self, tmp_db):
        items = sample_items(tmp_db, n=10)
        assert items == []

    def test_sample_larger_than_available(self, tmp_db):
        _insert_haiku_summary(tmp_db, 1)
        _insert_haiku_summary(tmp_db, 2)

        items = sample_items(tmp_db, n=100, stratify=False)
        assert len(items) == 2


class TestFormatSummaryForJudge:
    def test_format(self):
        text = _format_summary_for_judge(SAMPLE_SUMMARY)
        assert "py/objstr" in text
        assert "bug_report" in text
        assert "str.split crashes" in text
        assert "ValueError" in text


class TestFormatEvalReport:
    def test_basic_report(self):
        report = EvalReport(
            sample_size=10,
            haiku_wins=5,
            local_wins=3,
            ties=2,
            haiku_avg_scores={
                "accuracy": 4.2, "completeness": 3.8,
                "specificity": 4.0, "category": 4.5,
            },
            local_avg_scores={
                "accuracy": 3.9, "completeness": 3.7,
                "specificity": 3.5, "category": 4.3,
            },
        )
        text = format_eval_report(report)
        assert "Haiku wins:  5 (50%)" in text
        assert "Local wins:  3 (30%)" in text
        assert "Ties:        2 (20%)" in text
        assert "4.20" in text
        assert "3.90" in text

    def test_empty_report(self):
        report = EvalReport(sample_size=0, haiku_wins=0, local_wins=0, ties=0)
        text = format_eval_report(report)
        assert "No eval results" in text
