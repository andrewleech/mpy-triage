"""Tests for embedding and indexing."""

import sqlite3
from pathlib import Path

import numpy as np
import pytest
import sqlite_vec

from mpy_triage.config import EmbeddingConfig
from mpy_triage.embed import Embedder, build_index, index_all, rebuild_index

EMBED_DIM = 64


class MockEmbedder(Embedder):
    """Embedder that returns fixed-dimension random vectors without loading a real model."""

    def __init__(self, dim: int = EMBED_DIM):
        config = EmbeddingConfig(
            model_id="mock-model",
            embedding_dim=dim,
            query_prefix="query: ",
            document_prefix="",
            device="cpu",
        )
        super().__init__(config)
        self._dim = dim
        self._rng = np.random.RandomState(42)

    def _load_model(self) -> None:
        pass  # No-op, skip real model loading

    def encode_documents(self, texts: list[str], batch_size: int = 32) -> np.ndarray:
        vecs = self._rng.randn(len(texts), self._dim).astype(np.float32)
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        return vecs / norms

    def encode_query(self, text: str) -> np.ndarray:
        vec = self._rng.randn(self._dim).astype(np.float32)
        return vec / np.linalg.norm(vec)


@pytest.fixture
def schema_path():
    return Path(__file__).parent.parent / "schema.sql"


@pytest.fixture
def vec_db(tmp_path, schema_path):
    """File-based SQLite database with sqlite-vec loaded and schema applied."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.executescript(schema_path.read_text())
    yield conn
    conn.close()


@pytest.fixture
def mock_embedder():
    return MockEmbedder(dim=EMBED_DIM)


def _insert_assembled_rows(conn, count=5, repo="micropython/micropython"):
    """Insert sample assembled_xml rows."""
    for i in range(count):
        conn.execute(
            "INSERT INTO assembled_xml (item_number, item_type, repo, xml_text, xml_hash, "
            "has_summary, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                i + 1,
                "issue",
                repo,
                f"<item><title>Test issue {i + 1}</title>"
                f"<body>Description {i + 1}</body></item>",
                f"hash{i + 1}",
                1,
                "2025-01-01T00:00:00Z",
            ),
        )
    conn.commit()


class TestBuildIndex:
    def test_creates_tables(self, vec_db, mock_embedder):
        config = mock_embedder._config
        build_index(vec_db, config)

        tables = {
            row[0]
            for row in vec_db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "vec_items" in tables
        assert "item_fts" in tables
        assert "embedding_meta" in tables

    def test_writes_metadata(self, vec_db, mock_embedder):
        config = mock_embedder._config
        build_index(vec_db, config)

        row = vec_db.execute(
            "SELECT value FROM embedding_meta WHERE key = 'model_id'"
        ).fetchone()
        assert row[0] == "mock-model"

        row = vec_db.execute(
            "SELECT value FROM embedding_meta WHERE key = 'embedding_dim'"
        ).fetchone()
        assert row[0] == str(EMBED_DIM)

    def test_idempotent(self, vec_db, mock_embedder):
        config = mock_embedder._config
        build_index(vec_db, config)
        # Second call should not raise
        build_index(vec_db, config)


class TestIndexAll:
    def test_indexes_all_items(self, vec_db, mock_embedder):
        _insert_assembled_rows(vec_db, count=5)
        build_index(vec_db, mock_embedder._config)

        count = index_all(vec_db, mock_embedder, batch_size=2)
        assert count == 5

        vec_count = vec_db.execute("SELECT count(*) FROM vec_items").fetchone()[0]
        assert vec_count == 5

        fts_count = vec_db.execute("SELECT count(*) FROM item_fts").fetchone()[0]
        assert fts_count == 5

    def test_resume_no_duplicates(self, vec_db, mock_embedder):
        _insert_assembled_rows(vec_db, count=5)
        build_index(vec_db, mock_embedder._config)

        count1 = index_all(vec_db, mock_embedder, batch_size=2)
        assert count1 == 5

        count2 = index_all(vec_db, mock_embedder, batch_size=2)
        assert count2 == 0

        vec_count = vec_db.execute("SELECT count(*) FROM vec_items").fetchone()[0]
        assert vec_count == 5

    def test_resume_partial(self, vec_db, mock_embedder):
        """Index some items, add more, index again - only new items indexed."""
        _insert_assembled_rows(vec_db, count=3)
        build_index(vec_db, mock_embedder._config)

        count1 = index_all(vec_db, mock_embedder, batch_size=2)
        assert count1 == 3

        for i in range(3, 6):
            vec_db.execute(
                "INSERT INTO assembled_xml (item_number, item_type, repo, xml_text, xml_hash, "
                "has_summary, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    i + 1,
                    "issue",
                    "micropython/micropython",
                    f"<item><title>New issue {i + 1}</title></item>",
                    f"hash{i + 1}",
                    1,
                    "2025-01-01T00:00:00Z",
                ),
            )
        vec_db.commit()

        count2 = index_all(vec_db, mock_embedder, batch_size=2)
        assert count2 == 3

        vec_count = vec_db.execute("SELECT count(*) FROM vec_items").fetchone()[0]
        assert vec_count == 6

    def test_empty_assembled(self, vec_db, mock_embedder):
        build_index(vec_db, mock_embedder._config)
        count = index_all(vec_db, mock_embedder, batch_size=2)
        assert count == 0

    def test_fts_searchable(self, vec_db, mock_embedder):
        _insert_assembled_rows(vec_db, count=3)
        build_index(vec_db, mock_embedder._config)
        index_all(vec_db, mock_embedder, batch_size=2)

        results = vec_db.execute(
            "SELECT item_number FROM item_fts WHERE item_fts MATCH 'issue'",
        ).fetchall()
        assert len(results) == 3


class TestRebuildIndex:
    def test_drops_and_recreates(self, vec_db, mock_embedder):
        _insert_assembled_rows(vec_db, count=5)
        build_index(vec_db, mock_embedder._config)
        index_all(vec_db, mock_embedder, batch_size=2)

        assert vec_db.execute("SELECT count(*) FROM vec_items").fetchone()[0] == 5

        count = rebuild_index(vec_db, mock_embedder)
        assert count == 5

        assert vec_db.execute("SELECT count(*) FROM vec_items").fetchone()[0] == 5

    def test_rebuild_with_changed_data(self, vec_db, mock_embedder):
        _insert_assembled_rows(vec_db, count=3)
        build_index(vec_db, mock_embedder._config)
        index_all(vec_db, mock_embedder, batch_size=2)

        vec_db.execute("DELETE FROM assembled_xml WHERE item_number > 2")
        vec_db.commit()

        count = rebuild_index(vec_db, mock_embedder)
        assert count == 2
        assert vec_db.execute("SELECT count(*) FROM vec_items").fetchone()[0] == 2
