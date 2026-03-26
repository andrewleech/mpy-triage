"""Tests for search pipeline."""

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from mpy_triage.search import (
    _sanitize_fts_query,
    keyword_search,
    reciprocal_rank_fusion,
)

# --- FTS5 sanitization ---


class TestSanitizeFtsQuery:
    def test_single_word(self):
        assert _sanitize_fts_query("hello") == '"hello"'

    def test_multiple_words(self):
        result = _sanitize_fts_query("hello world")
        assert result == '"hello" OR "world"'

    def test_special_characters_stripped(self):
        result = _sanitize_fts_query('foo*bar "baz" (qux)')
        assert '"foo' in result
        assert '"bar"' in result
        assert '"baz"' in result
        assert '"qux"' in result
        assert "*" not in result
        assert "(" not in result

    def test_empty_query(self):
        assert _sanitize_fts_query("") == '""'

    def test_only_special_characters(self):
        assert _sanitize_fts_query('*()":') == '""'

    def test_preserves_normal_tokens(self):
        result = _sanitize_fts_query("SPI DMA transfer")
        assert result == '"SPI" OR "DMA" OR "transfer"'


# --- Reciprocal rank fusion ---


class TestReciprocalRankFusion:
    def _make_result(self, item_number, item_type="issue", repo="micropython/micropython"):
        return {
            "item_number": item_number,
            "item_type": item_type,
            "repo": repo,
            "score": 0.5,
        }

    def test_basic_merge(self):
        dense = [self._make_result(1), self._make_result(2)]
        sparse = [self._make_result(2), self._make_result(3)]

        fused = reciprocal_rank_fusion(dense, sparse, k=60)

        numbers = [r["item_number"] for r in fused]
        assert numbers[0] == 2
        assert set(numbers) == {1, 2, 3}

    def test_deduplication(self):
        dense = [self._make_result(1), self._make_result(2)]
        sparse = [self._make_result(1), self._make_result(2)]

        fused = reciprocal_rank_fusion(dense, sparse, k=60)
        assert len(fused) == 2

    def test_different_item_types_not_deduped(self):
        dense = [self._make_result(1, "issue")]
        sparse = [self._make_result(1, "pull_request")]

        fused = reciprocal_rank_fusion(dense, sparse, k=60)
        assert len(fused) == 2

    def test_score_accumulation(self):
        dense = [self._make_result(1)]
        sparse = [self._make_result(1)]

        fused = reciprocal_rank_fusion(dense, sparse, k=60)

        expected = 2.0 / 61.0
        assert abs(fused[0]["rrf_score"] - expected) < 1e-9

    def test_ordering_by_score(self):
        dense = [self._make_result(10), self._make_result(20)]
        sparse = [self._make_result(10), self._make_result(20)]

        fused = reciprocal_rank_fusion(dense, sparse, k=60)

        assert fused[0]["item_number"] == 10
        assert fused[1]["item_number"] == 20
        assert fused[0]["rrf_score"] > fused[1]["rrf_score"]

    def test_empty_inputs(self):
        assert reciprocal_rank_fusion([], []) == []
        assert len(reciprocal_rank_fusion([self._make_result(1)], [])) == 1
        assert len(reciprocal_rank_fusion([], [self._make_result(1)])) == 1


# --- Keyword search (requires file-based DB for FTS5) ---


@pytest.fixture
def fts_db(tmp_path):
    """File-based SQLite DB with FTS5 table for keyword search testing."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    conn.execute(
        "CREATE VIRTUAL TABLE item_fts USING fts5("
        "item_number, item_type, repo, content"
        ")"
    )

    rows = [
        (1, "issue", "micropython/micropython", "SPI DMA transfer fails on STM32"),
        (2, "issue", "micropython/micropython", "I2C timeout on ESP32 with large payloads"),
        (
            3,
            "pull_request",
            "micropython/micropython",
            "Fix SPI DMA buffer alignment",
        ),
        (4, "issue", "micropython/micropython-lib", "urllib request hangs on redirect"),
    ]
    conn.executemany(
        "INSERT INTO item_fts(item_number, item_type, repo, content) VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    yield conn
    conn.close()


class TestKeywordSearch:
    def test_basic_search(self, fts_db):
        results = keyword_search(fts_db, "SPI DMA")

        assert len(results) >= 2
        numbers = [r["item_number"] for r in results]
        assert 1 in numbers
        assert 3 in numbers

    def test_returns_positive_scores(self, fts_db):
        results = keyword_search(fts_db, "SPI")
        for r in results:
            assert r["score"] > 0

    def test_respects_top_k(self, fts_db):
        results = keyword_search(fts_db, "SPI DMA transfer", top_k=1)
        assert len(results) <= 1

    def test_no_results(self, fts_db):
        results = keyword_search(fts_db, "nonexistent_xyzzy_term")
        assert results == []

    def test_special_chars_in_query(self, fts_db):
        results = keyword_search(fts_db, 'SPI* AND "DMA"')
        assert isinstance(results, list)

    def test_result_fields(self, fts_db):
        results = keyword_search(fts_db, "ESP32")
        assert len(results) >= 1
        r = results[0]
        assert "item_number" in r
        assert "item_type" in r
        assert "repo" in r
        assert "score" in r


# --- Dense search (requires sqlite-vec) ---


class TestDenseSearch:
    """Dense search tests. Skipped if sqlite-vec is unavailable."""

    @pytest.fixture
    def vec_db(self, tmp_path):
        try:
            import sqlite_vec
        except ImportError:
            pytest.skip("sqlite-vec not installed")

        db_path = tmp_path / "vec_test.db"
        conn = sqlite3.connect(str(db_path))
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)

        conn.execute(
            "CREATE VIRTUAL TABLE vec_items USING vec0("
            "item_number integer, item_type text, repo text, "
            "embedding float[4]"
            ")"
        )

        import numpy as np

        rows = [
            (
                1,
                "issue",
                "micropython/micropython",
                np.array([1, 0, 0, 0], dtype=np.float32).tobytes(),
            ),
            (
                2,
                "issue",
                "micropython/micropython",
                np.array([0, 1, 0, 0], dtype=np.float32).tobytes(),
            ),
            (
                3,
                "pull_request",
                "micropython/micropython",
                np.array([0.9, 0.1, 0, 0], dtype=np.float32).tobytes(),
            ),
        ]
        conn.executemany(
            "INSERT INTO vec_items(item_number, item_type, repo, embedding)"
            " VALUES (?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        yield conn
        conn.close()

    def test_basic_dense_search(self, vec_db):
        import numpy as np

        from mpy_triage.search import dense_search

        query = np.array([1, 0, 0, 0], dtype=np.float32)
        results = dense_search(vec_db, query, top_k=3)

        assert len(results) == 3
        assert results[0]["item_number"] == 1
        assert results[0]["score"] >= results[1]["score"]

    def test_filter_by_item_type(self, vec_db):
        import numpy as np

        from mpy_triage.search import dense_search

        query = np.array([1, 0, 0, 0], dtype=np.float32)
        results = dense_search(
            vec_db, query, top_k=10, filters={"item_type": "pull_request"}
        )

        for r in results:
            assert r["item_type"] == "pull_request"

    def test_invalid_filter_key_ignored(self, vec_db):
        import numpy as np

        from mpy_triage.search import dense_search

        query = np.array([1, 0, 0, 0], dtype=np.float32)
        results = dense_search(vec_db, query, top_k=3, filters={"bogus_key": "value"})
        assert len(results) == 3


# --- Reranker (mocked) ---


class TestReranker:
    def test_rerank_returns_sorted(self):
        from mpy_triage.search import Reranker

        reranker = Reranker()
        mock_model = MagicMock()
        mock_model.predict.return_value = [0.1, 0.9, 0.5]
        reranker._model = mock_model

        candidates = [
            {"content": "aaa", "item_number": 1},
            {"content": "bbb", "item_number": 2},
            {"content": "ccc", "item_number": 3},
        ]

        results = reranker.rerank("query", candidates, top_k=2)

        assert len(results) == 2
        assert results[0]["item_number"] == 2
        assert results[0]["rerank_score"] == 0.9
        assert results[1]["item_number"] == 3

    def test_rerank_empty_candidates(self):
        from mpy_triage.search import Reranker

        reranker = Reranker()
        assert reranker.rerank("query", []) == []


# --- Full search pipeline (mocked) ---


class TestSearchPipeline:
    @patch("mpy_triage.search.Reranker")
    def test_search_pipeline(self, MockReranker, fts_db):
        """Integration test with mocked reranker and real FTS5."""
        from mpy_triage.config import RetrievalConfig
        from mpy_triage.search import search

        fts_db.execute(
            "CREATE TABLE IF NOT EXISTS assembled_xml("
            "item_number INTEGER, item_type TEXT, repo TEXT, xml_text TEXT,"
            "UNIQUE(repo, item_number, item_type))"
        )
        fts_db.execute(
            "INSERT INTO assembled_xml VALUES"
            " (1, 'issue', 'micropython/micropython', '<item>SPI DMA</item>')"
        )
        fts_db.commit()

        mock_embedder = MagicMock()
        import numpy as np

        mock_embedder.encode_query.return_value = np.zeros(4, dtype=np.float32)

        mock_reranker_instance = MagicMock()
        mock_reranker_instance.rerank.side_effect = lambda q, cands, top_k: cands[:top_k]
        MockReranker.return_value = mock_reranker_instance

        config = RetrievalConfig(top_k_initial=10, top_k_rerank=5, rrf_k=60)

        with patch("mpy_triage.search.dense_search", return_value=[]):
            results = search(fts_db, "SPI DMA", mock_embedder, config=config)

        assert isinstance(results, list)
        for r in results:
            assert "content" in r
