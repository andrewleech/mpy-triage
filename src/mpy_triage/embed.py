"""Stage 4: Embedding and sqlite-vec/FTS5 index management."""

import sqlite3
from typing import Optional

import numpy as np

from .config import EmbeddingConfig


class Embedder:
    """Model-agnostic embedding wrapper with lazy loading."""

    def __init__(self, config: Optional[EmbeddingConfig] = None):
        self._config = config
        self._model = None

    def encode_documents(self, texts: list[str], batch_size: int = 32) -> np.ndarray:
        """Encode document texts into embeddings."""
        raise NotImplementedError

    def encode_query(self, text: str) -> np.ndarray:
        """Encode a query text into an embedding."""
        raise NotImplementedError


def build_index(conn: sqlite3.Connection, config: EmbeddingConfig) -> None:
    """Create vec_items, item_fts, and embedding_meta tables."""
    raise NotImplementedError


def index_all(
    conn: sqlite3.Connection,
    embedder: Embedder,
    *,
    batch_size: int = 4,
    gc_interval: int = 50,
) -> int:
    """Embed and index all assembled XML. Resume-capable. Returns count indexed."""
    raise NotImplementedError


def rebuild_index(conn: sqlite3.Connection, embedder: Embedder) -> int:
    """Drop and recreate the full index."""
    raise NotImplementedError
