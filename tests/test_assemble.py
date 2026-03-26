"""Tests for XML assembly."""

import json

from mpy_triage.assemble import (
    _cdata_wrap,
    assemble_all,
    assemble_item,
    parse_diff_files,
)

# fmt: off
SAMPLE_DIFF = (
    "diff --git a/ports/stm32/machine_spi.c b/ports/stm32/machine_spi.c\n"
    "index abc123..def456 100644\n"
    "--- a/ports/stm32/machine_spi.c\n"
    "+++ b/ports/stm32/machine_spi.c\n"
    "@@ -372,21 +372,12 @@ static void spi_transfer_dma(mp_obj_t *self) {\n"
    "-    old line\n"
    "+    new line\n"
    "+    another new line\n"
    "diff --git a/ports/stm32/dma.c b/ports/stm32/dma.c\n"
    "index 111222..333444 100644\n"
    "--- a/ports/stm32/dma.c\n"
    "+++ b/ports/stm32/dma.c\n"
    "@@ -100,5 +100,8 @@ void dma_configure(DMA_HandleTypeDef *hdma) {\n"
    "+    added line 1\n"
    "+    added line 2\n"
    "+    added line 3\n"
)

MULTI_HUNK_DIFF = (
    "diff --git a/py/obj.c b/py/obj.c\n"
    "index aaa..bbb 100644\n"
    "--- a/py/obj.c\n"
    "+++ b/py/obj.c\n"
    "@@ -10,3 +10,4 @@ void mp_obj_print_helper(mp_obj_t o_in) {\n"
    "+    added in first hunk\n"
    "@@ -50,6 +51,5 @@ mp_obj_t mp_obj_new_int(mp_int_t value) {\n"
    "-    removed in second hunk\n"
)
# fmt: on


def test_parse_diff_files():
    files = parse_diff_files(SAMPLE_DIFF)
    assert len(files) == 2

    f0 = files[0]
    assert f0.path == "ports/stm32/machine_spi.c"
    assert f0.additions == 2
    assert f0.deletions == 1
    assert "static void spi_transfer_dma(mp_obj_t *self) {" in f0.functions

    f1 = files[1]
    assert f1.path == "ports/stm32/dma.c"
    assert f1.additions == 3
    assert f1.deletions == 0
    assert "void dma_configure(DMA_HandleTypeDef *hdma) {" in f1.functions


def test_parse_diff_files_multi_hunk():
    files = parse_diff_files(MULTI_HUNK_DIFF)
    assert len(files) == 1
    f = files[0]
    assert f.path == "py/obj.c"
    assert f.additions == 1
    assert f.deletions == 1
    assert len(f.functions) == 2
    assert f.functions[0] == "void mp_obj_print_helper(mp_obj_t o_in) {"
    assert f.functions[1] == "mp_obj_t mp_obj_new_int(mp_int_t value) {"


def test_parse_diff_files_empty():
    assert parse_diff_files("") == []


def test_cdata_wrap():
    assert _cdata_wrap("hello") == "<![CDATA[hello]]>"


def test_cdata_wrap_special_chars():
    text = 'has <tags> & "quotes"'
    assert _cdata_wrap(text) == f"<![CDATA[{text}]]>"


def test_cdata_wrap_cdata_end():
    text = "text with ]]> inside"
    result = _cdata_wrap(text)
    assert result == "<![CDATA[text with ]]]]><![CDATA[> inside]]>"


def test_cdata_wrap_none():
    assert _cdata_wrap(None) == "<![CDATA[]]>"


def _insert_issue(
    conn,
    number=42,
    repo="micropython/micropython",
    title="Test issue",
    body="Issue body",
    labels=None,
):
    labels = labels or ["bug", "stm32"]
    conn.execute(
        "INSERT INTO issues (number, repo, title, body, labels, state)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (number, repo, title, body, json.dumps(labels), "open"),
    )
    conn.commit()


def _insert_pr(
    conn,
    number=100,
    repo="micropython/micropython",
    title="Test PR",
    body="PR body",
    labels=None,
    diff_text=None,
):
    labels = labels or ["enhancement"]
    conn.execute(
        "INSERT INTO pull_requests"
        " (number, repo, title, body, labels, state)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (number, repo, title, body, json.dumps(labels), "open"),
    )
    if diff_text:
        conn.execute(
            "INSERT INTO pr_diffs (pr_number, repo, diff_text)"
            " VALUES (?, ?, ?)",
            (number, repo, diff_text),
        )
    conn.commit()


def _insert_summary(
    conn, item_number, item_type, repo="micropython/micropython"
):
    conn.execute(
        "INSERT INTO summaries"
        " (item_number, item_type, repo, components,"
        " item_category, synopsis,"
        " affected_code, error_signatures, concepts)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            item_number,
            item_type,
            repo,
            json.dumps(["stm32", "spi"]),
            "bug report",
            "SPI DMA fails on STM32F4",
            json.dumps(["ports/stm32/machine_spi.c"]),
            "HardFault at 0x08001234",
            json.dumps(["DMA", "SPI", "STM32"]),
        ),
    )
    conn.commit()


def test_assemble_issue(tmp_db):
    _insert_issue(tmp_db)
    xml = assemble_item(
        tmp_db, "micropython/micropython", 42, "issue"
    )
    assert '<issue number="42" repo="micropython/micropython">' in xml
    assert "<title><![CDATA[Test issue]]></title>" in xml
    assert "<description><![CDATA[Issue body]]></description>" in xml
    assert "<labels>bug, stm32</labels>" in xml
    assert "</issue>" in xml
    assert "<summary>" not in xml


def test_assemble_pr_with_diff(tmp_db):
    _insert_pr(tmp_db, diff_text=SAMPLE_DIFF)
    xml = assemble_item(
        tmp_db, "micropython/micropython", 100, "pull_request"
    )
    assert (
        '<pull_request number="100" repo="micropython/micropython">'
        in xml
    )
    assert "<diff_files>" in xml
    assert 'path="ports/stm32/machine_spi.c"' in xml
    assert 'additions="2"' in xml
    assert 'deletions="1"' in xml
    assert 'path="ports/stm32/dma.c"' in xml
    assert "</diff_files>" in xml
    assert "</pull_request>" in xml


def test_assemble_with_summary(tmp_db):
    _insert_issue(tmp_db)
    _insert_summary(tmp_db, 42, "issue")
    xml = assemble_item(
        tmp_db, "micropython/micropython", 42, "issue"
    )
    assert "<summary>" in xml
    assert "<components>stm32, spi</components>" in xml
    assert "<type>bug report</type>" in xml
    assert "<synopsis>SPI DMA fails on STM32F4</synopsis>" in xml
    assert (
        "<affected_code>ports/stm32/machine_spi.c</affected_code>" in xml
    )
    assert (
        "<error_signatures>HardFault at 0x08001234</error_signatures>"
        in xml
    )
    assert "<concepts>DMA, SPI, STM32</concepts>" in xml
    assert "</summary>" in xml


def test_assemble_without_summary(tmp_db):
    _insert_issue(tmp_db)
    xml = assemble_item(
        tmp_db, "micropython/micropython", 42, "issue"
    )
    assert "<summary>" not in xml
    assert "</summary>" not in xml
    assert "<title>" in xml
    assert "<description>" in xml
    assert "<labels>" in xml


def test_assemble_all_skip_unchanged(tmp_db):
    repo = "micropython/micropython"
    _insert_issue(tmp_db, number=1)
    _insert_issue(tmp_db, number=2)
    _insert_pr(tmp_db, number=10, diff_text=SAMPLE_DIFF)

    count1 = assemble_all(tmp_db, repo)
    assert count1 == 3

    count2 = assemble_all(tmp_db, repo)
    assert count2 == 0


def test_assemble_all_updates_on_change(tmp_db):
    repo = "micropython/micropython"
    _insert_issue(tmp_db, number=1, title="Original title")

    count1 = assemble_all(tmp_db, repo)
    assert count1 == 1

    tmp_db.execute(
        "UPDATE issues SET title = ? WHERE number = 1",
        ("Updated title",),
    )
    tmp_db.commit()

    count2 = assemble_all(tmp_db, repo)
    assert count2 == 1

    row = tmp_db.execute(
        "SELECT xml_text FROM assembled_xml"
        " WHERE item_number = 1 AND item_type = 'issue'"
    ).fetchone()
    assert "Updated title" in row["xml_text"]
