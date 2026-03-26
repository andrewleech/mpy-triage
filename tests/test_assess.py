"""Tests for Sonnet assessment."""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from mpy_triage.assess import (
    Assessment,
    _build_comparison_prompt,
    _get_json_schema,
    _load_system_prompt,
    assess_candidates,
)
from mpy_triage.config import clean_env as _clean_env

# --- Helpers ---


def _make_subprocess_result(
    classification="RELATED",
    confidence="medium",
    reasoning="Shared component.",
    suggested_action="link as related",
    returncode=0,
):
    """Build a mock CompletedProcess with canned JSON output."""
    payload = {
        "classification": classification,
        "confidence": confidence,
        "reasoning": reasoning,
        "suggested_action": suggested_action,
    }
    result = MagicMock(spec=subprocess.CompletedProcess)
    result.returncode = returncode
    result.stdout = json.dumps(payload)
    result.stderr = ""
    return result


def _insert_issue(conn, repo, number, title, body):
    conn.execute(
        "INSERT INTO issues (number, repo, title, body) VALUES (?, ?, ?, ?)",
        (number, repo, title, body),
    )
    conn.commit()


def _insert_assembled_xml(conn, repo, number, item_type, xml_text):
    conn.execute(
        "INSERT INTO assembled_xml (item_number, item_type, repo, xml_text) "
        "VALUES (?, ?, ?, ?)",
        (number, item_type, repo, xml_text),
    )
    conn.commit()


# --- _clean_env ---


def test_clean_env_strips_claudecode_vars():
    with patch.dict(
        os.environ,
        {
            "PATH": "/usr/bin",
            "HOME": "/home/test",
            "CLAUDECODE_SESSION": "abc123",
            "CLAUDECODE_PIPE": "/tmp/pipe",
            "NORMAL_VAR": "value",
        },
        clear=True,
    ):
        env = _clean_env()
        assert "PATH" in env
        assert "HOME" in env
        assert "NORMAL_VAR" in env
        assert "CLAUDECODE_SESSION" not in env
        assert "CLAUDECODE_PIPE" not in env


def test_clean_env_preserves_non_claudecode():
    with patch.dict(os.environ, {"FOO": "bar", "CLAUDE_KEY": "ok"}, clear=True):
        env = _clean_env()
        assert env == {"FOO": "bar", "CLAUDE_KEY": "ok"}


# --- _get_json_schema ---


def test_get_json_schema_returns_valid_json():
    schema_str = _get_json_schema()
    schema = json.loads(schema_str)
    assert schema["type"] == "object"
    assert "classification" in schema["properties"]
    assert "confidence" in schema["properties"]
    assert "reasoning" in schema["properties"]
    assert "suggested_action" in schema["properties"]
    assert set(schema["required"]) == {
        "classification",
        "confidence",
        "reasoning",
        "suggested_action",
    }


# --- _load_system_prompt ---


def test_load_system_prompt_reads_file(tmp_path):
    prompt_text = "You are an assessment expert."
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "assess.txt").write_text(prompt_text)

    from mpy_triage import config

    original = config._config
    try:
        config._config = config.TriageConfig(prompts_dir=prompts_dir)
        with patch.object(Path, "home", return_value=tmp_path / "nonexistent"):
            result = _load_system_prompt()
        assert result == prompt_text
    finally:
        config._config = original


def test_load_system_prompt_appends_mpy_rules(tmp_path):
    prompt_text = "Base prompt."
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "assess.txt").write_text(prompt_text)

    mpy_rules_dir = tmp_path / ".claude" / "mpy-rules"
    mpy_rules_dir.mkdir(parents=True)
    (mpy_rules_dir / "core.md").write_text("# MicroPython Rules")

    from mpy_triage import config

    original = config._config
    try:
        config._config = config.TriageConfig(prompts_dir=prompts_dir)
        with patch.object(Path, "home", return_value=tmp_path):
            result = _load_system_prompt()
        assert "Base prompt." in result
        assert "# MicroPython Rules" in result
    finally:
        config._config = original


# --- _build_comparison_prompt ---


def test_build_comparison_prompt_format():
    result = _build_comparison_prompt("<issue>Query content</issue>", "<issue>Candidate</issue>")
    assert "## QUERY ITEM" in result
    assert "## CANDIDATE ITEM" in result
    assert "<issue>Query content</issue>" in result
    assert "<issue>Candidate</issue>" in result


def test_build_comparison_prompt_ordering():
    result = _build_comparison_prompt("FIRST", "SECOND")
    assert result.index("FIRST") < result.index("SECOND")


# --- assess_candidates ---


@patch("mpy_triage.assess._load_system_prompt", return_value="System prompt.")
@patch("mpy_triage.assess.subprocess.run")
def test_assess_candidates_basic(mock_run, mock_prompt, tmp_db):
    mock_run.return_value = _make_subprocess_result(
        classification="DUPLICATE",
        confidence="high",
        reasoning="Same root cause.",
        suggested_action="close as duplicate of #100",
    )

    _insert_issue(tmp_db, "micropython/micropython", 100, "SPI bug", "SPI fails on STM32")
    _insert_issue(tmp_db, "micropython/micropython", 200, "SPI crash", "SPI crash on STM32")

    query = {"number": 200, "repo": "micropython/micropython", "item_type": "issue"}
    candidates = [{"number": 100, "repo": "micropython/micropython", "item_type": "issue"}]

    results = assess_candidates(tmp_db, query, candidates)
    assert len(results) == 1
    a = results[0]
    assert isinstance(a, Assessment)
    assert a.classification == "DUPLICATE"
    assert a.confidence == "high"
    assert a.item_number == 100
    assert a.repo == "micropython/micropython"


@patch("mpy_triage.assess._load_system_prompt", return_value="System prompt.")
@patch("mpy_triage.assess.subprocess.run")
def test_assess_candidates_uses_assembled_xml(mock_run, mock_prompt, tmp_db):
    mock_run.return_value = _make_subprocess_result()

    _insert_issue(tmp_db, "micropython/micropython", 1, "Title A", "Body A")
    _insert_issue(tmp_db, "micropython/micropython", 2, "Title B", "Body B")
    _insert_assembled_xml(tmp_db, "micropython/micropython", 1, "issue", "<xml>A assembled</xml>")
    _insert_assembled_xml(tmp_db, "micropython/micropython", 2, "issue", "<xml>B assembled</xml>")

    query = {"number": 2, "repo": "micropython/micropython", "item_type": "issue"}
    candidates = [{"number": 1, "repo": "micropython/micropython", "item_type": "issue"}]

    assess_candidates(tmp_db, query, candidates)

    call_args = mock_run.call_args
    prompt_input = call_args.kwargs.get("input") or call_args[1].get("input", "")
    assert "<xml>A assembled</xml>" in prompt_input
    assert "<xml>B assembled</xml>" in prompt_input


@patch("mpy_triage.assess._load_system_prompt", return_value="System prompt.")
@patch("mpy_triage.assess.subprocess.run")
def test_assess_candidates_top_k(mock_run, mock_prompt, tmp_db):
    mock_run.return_value = _make_subprocess_result()

    for i in range(10):
        _insert_issue(tmp_db, "repo", i, f"Title {i}", f"Body {i}")

    query = {"number": 99, "repo": "repo", "item_type": "issue", "title": "Q", "body": "Q body"}
    candidates = [{"number": i, "repo": "repo", "item_type": "issue"} for i in range(10)]

    results = assess_candidates(tmp_db, query, candidates, top_k=3)
    assert len(results) == 3
    assert mock_run.call_count == 3


@patch("mpy_triage.assess._load_system_prompt", return_value="System prompt.")
@patch("mpy_triage.assess.subprocess.run")
def test_assess_candidates_timeout_produces_fallback(mock_run, mock_prompt, tmp_db):
    mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=120)

    _insert_issue(tmp_db, "repo", 1, "Title", "Body")

    query = {"number": 2, "repo": "repo", "item_type": "issue", "title": "Q", "body": "B"}
    candidates = [{"number": 1, "repo": "repo", "item_type": "issue"}]

    results = assess_candidates(tmp_db, query, candidates)
    assert len(results) == 1
    a = results[0]
    assert a.classification == "UNRELATED"
    assert a.confidence == "low"
    assert "timeout" in a.reasoning.lower()


@patch("mpy_triage.assess._load_system_prompt", return_value="System prompt.")
@patch("mpy_triage.assess.subprocess.run")
def test_assess_candidates_invalid_json_produces_fallback(mock_run, mock_prompt, tmp_db):
    result = MagicMock(spec=subprocess.CompletedProcess)
    result.returncode = 0
    result.stdout = "not valid json {{"
    result.stderr = ""
    mock_run.return_value = result

    _insert_issue(tmp_db, "repo", 1, "Title", "Body")

    query = {"number": 2, "repo": "repo", "item_type": "issue", "title": "Q", "body": "B"}
    candidates = [{"number": 1, "repo": "repo", "item_type": "issue"}]

    results = assess_candidates(tmp_db, query, candidates)
    assert len(results) == 1
    a = results[0]
    assert a.classification == "UNRELATED"
    assert a.confidence == "low"
    assert "invalid json" in a.reasoning.lower()


@patch("mpy_triage.assess._load_system_prompt", return_value="System prompt.")
@patch("mpy_triage.assess.subprocess.run")
def test_assess_candidates_nonzero_exit_produces_fallback(mock_run, mock_prompt, tmp_db):
    result = MagicMock(spec=subprocess.CompletedProcess)
    result.returncode = 1
    result.stdout = ""
    result.stderr = "error occurred"
    mock_run.return_value = result

    _insert_issue(tmp_db, "repo", 1, "Title", "Body")

    query = {"number": 2, "repo": "repo", "item_type": "issue", "title": "Q", "body": "B"}
    candidates = [{"number": 1, "repo": "repo", "item_type": "issue"}]

    results = assess_candidates(tmp_db, query, candidates)
    assert len(results) == 1
    a = results[0]
    assert a.classification == "UNRELATED"
    assert a.confidence == "low"
    assert "failed" in a.reasoning.lower()


@patch("mpy_triage.assess._load_system_prompt", return_value="System prompt.")
@patch("mpy_triage.assess.subprocess.run")
def test_assess_candidates_empty_list(mock_run, mock_prompt, tmp_db):
    query = {"number": 1, "repo": "repo", "item_type": "issue", "title": "Q", "body": "B"}
    results = assess_candidates(tmp_db, query, [])
    assert results == []
    mock_run.assert_not_called()


@patch("mpy_triage.assess._load_system_prompt", return_value="System prompt.")
@patch("mpy_triage.assess.subprocess.run")
def test_assess_candidates_structured_output_wrapper(mock_run, mock_prompt, tmp_db):
    """Test that structured_output wrapper from Claude CLI is unwrapped."""
    payload = {
        "structured_output": {
            "classification": "LIKELY_DUPLICATE",
            "confidence": "medium",
            "reasoning": "Wrapped response.",
            "suggested_action": "investigate further",
        }
    }
    result = MagicMock(spec=subprocess.CompletedProcess)
    result.returncode = 0
    result.stdout = json.dumps(payload)
    result.stderr = ""
    mock_run.return_value = result

    _insert_issue(tmp_db, "repo", 1, "Title", "Body")

    query = {"number": 2, "repo": "repo", "item_type": "issue", "title": "Q", "body": "B"}
    candidates = [{"number": 1, "repo": "repo", "item_type": "issue"}]

    results = assess_candidates(tmp_db, query, candidates)
    assert results[0].classification == "LIKELY_DUPLICATE"
    assert results[0].reasoning == "Wrapped response."
