"""Stage 4: Embedding and sqlite-vec/FTS5 index management."""

import gc
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import numpy as np
from tqdm import tqdm

from .config import EmbeddingConfig, get_config

logger = logging.getLogger(__name__)

# Safety truncation for texts that somehow exceed the assembler's budget.
# The assembler targets 8K chars; this is a backstop.
MAX_TEXT_CHARS = 10000


class Embedder:
    """Model-agnostic embedding wrapper with lazy loading."""

    def __init__(self, config: Optional[EmbeddingConfig] = None):
        self._config = config or get_config().embedding
        self._model = None

    @property
    def config(self) -> EmbeddingConfig:
        """Public access to embedding configuration."""
        return self._config

    def _load_model(self) -> None:
        """Lazy load the SentenceTransformer model."""
        if self._model is not None:
            return

        from sentence_transformers import SentenceTransformer

        logger.info("Loading embedding model: %s", self._config.model_id)
        logger.info("Using device: %s", self._config.device)

        self._model = SentenceTransformer(self._config.model_id, device=self._config.device)
        self._model.max_seq_length = self._config.max_seq_length
        logger.info("Model loaded successfully")

    def encode_documents(self, texts: list[str], batch_size: int = 32) -> np.ndarray:
        """Encode document texts into embeddings.

        Args:
            texts: List of document texts to embed.
            batch_size: Number of texts to encode at once.

        Returns:
            Array of embeddings with shape (len(texts), embedding_dim).
        """
        self._load_model()
        prefix = self._config.document_prefix
        if prefix:
            texts = [prefix + t for t in texts]
        texts = [t[:MAX_TEXT_CHARS] for t in texts]
        return self._model.encode(
            texts,
            batch_size=batch_size,
            show_progress_bar=len(texts) > 100,
            normalize_embeddings=True,
        )

    def encode_query(self, text: str) -> np.ndarray:
        """Encode a query text into an embedding.

        Args:
            text: Query text to embed.

        Returns:
            1D embedding vector.
        """
        self._load_model()
        prefixed = self._config.query_prefix + text
        result = self._model.encode(
            [prefixed],
            batch_size=1,
            show_progress_bar=False,
            normalize_embeddings=True,
        )
        return result[0]

def build_index(conn: sqlite3.Connection, config: EmbeddingConfig) -> None:
    """Create vec_items, item_fts, and embedding_meta tables.

    Args:
        conn: SQLite connection (must have sqlite-vec loaded).
        config: Embedding configuration for dimension info.
    """
    dim = config.embedding_dim

    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_items USING vec0("
        f"item_number integer, "
        f"item_type text, "
        f"repo text, "
        f"embedding float[{dim}] distance_metric=cosine"
        f")"
    )

    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS item_fts USING fts5("
        "item_number, item_type, repo, content"
        ")"
    )

    conn.execute(
        "CREATE TABLE IF NOT EXISTS embedding_meta ("
        "key TEXT PRIMARY KEY, "
        "value TEXT"
        ")"
    )

    conn.execute(
        "INSERT OR REPLACE INTO embedding_meta (key, value) VALUES (?, ?)",
        ("model_id", config.model_id),
    )
    conn.execute(
        "INSERT OR REPLACE INTO embedding_meta (key, value) VALUES (?, ?)",
        ("embedding_dim", str(dim)),
    )
    conn.execute(
        "INSERT OR REPLACE INTO embedding_meta (key, value) VALUES (?, ?)",
        ("created_at", datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def _get_indexed_keys(conn: sqlite3.Connection) -> set[tuple[int, str, str]]:
    """Return the set of (item_number, item_type, repo) already in vec_items."""
    try:
        rows = conn.execute(
            "SELECT item_number, item_type, repo FROM vec_items"
        ).fetchall()
        return {(r[0], r[1], r[2]) for r in rows}
    except sqlite3.OperationalError:
        return set()


def index_all(
    conn: sqlite3.Connection,
    embedder: Embedder,
    *,
    batch_size: int = 4,
    gc_interval: int = 50,
) -> int:
    """Embed and index all assembled XML. Resume-capable.

    Args:
        conn: SQLite connection (must have sqlite-vec loaded and tables created).
        embedder: Embedder instance for generating vectors.
        batch_size: Number of items to embed per batch.
        gc_interval: Run gc.collect() every this many batches.

    Returns:
        Number of items newly indexed.
    """
    already_indexed = _get_indexed_keys(conn)

    rows = conn.execute(
        "SELECT item_number, item_type, repo, xml_text FROM assembled_xml"
    ).fetchall()

    to_index = [
        r for r in rows if (r[0], r[1], r[2]) not in already_indexed
    ]

    if not to_index:
        logger.info("All items already indexed, nothing to do")
        return 0

    logger.info("Indexing %d items (%d already indexed)", len(to_index), len(already_indexed))

    count = 0
    batch_count = 0

    for i in tqdm(range(0, len(to_index), batch_size), desc="Indexing", disable=None):
        batch = to_index[i : i + batch_size]
        texts = [r[3] or "" for r in batch]

        embeddings = embedder.encode_documents(texts, batch_size=batch_size)

        for j, row in enumerate(batch):
            item_number, item_type, repo, xml_text = row[0], row[1], row[2], row[3]
            vec_bytes = embeddings[j].astype(np.float32).tobytes()

            conn.execute(
                "INSERT INTO vec_items (item_number, item_type, repo, embedding) "
                "VALUES (?, ?, ?, ?)",
                (item_number, item_type or "", repo or "", vec_bytes),
            )
            conn.execute(
                "INSERT INTO item_fts (item_number, item_type, repo, content) "
                "VALUES (?, ?, ?, ?)",
                (str(item_number), item_type or "", repo or "", xml_text or ""),
            )

        conn.commit()
        count += len(batch)
        batch_count += 1

        if batch_count % gc_interval == 0:
            gc.collect()

    logger.info("Indexed %d items", count)
    return count


def rebuild_index(conn: sqlite3.Connection, embedder: Embedder) -> int:
    """Drop and recreate the full index.

    Args:
        conn: SQLite connection (must have sqlite-vec loaded).
        embedder: Embedder instance for generating vectors.

    Returns:
        Number of items indexed.
    """
    conn.execute("DROP TABLE IF EXISTS vec_items")
    conn.execute("DROP TABLE IF EXISTS item_fts")
    conn.commit()

    build_index(conn, embedder.config)
    return index_all(conn, embedder)
