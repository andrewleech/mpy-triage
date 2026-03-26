"""Stage 5: Hybrid retrieval with RRF fusion and cross-encoder reranking."""

import sqlite3

import numpy as np

from .config import RetrievalConfig
from .embed import Embedder


def dense_search(
    conn: sqlite3.Connection,
    query_embedding: np.ndarray,
    top_k: int = 100,
    filters: dict | None = None,
) -> list[dict]:
    """KNN search against vec_items."""
    raise NotImplementedError


def keyword_search(
    conn: sqlite3.Connection,
    query_text: str,
    top_k: int = 100,
) -> list[dict]:
    """FTS5 BM25 search against item_fts."""
    raise NotImplementedError


def reciprocal_rank_fusion(
    dense_results: list[dict],
    sparse_results: list[dict],
    k: int = 60,
) -> list[dict]:
    """Combine dense and sparse results using RRF."""
    raise NotImplementedError


class Reranker:
    """Cross-encoder reranker with lazy model loading."""

    def __init__(self, model_name: str | None = None):
        self._model_name = model_name
        self._model = None

    def rerank(
        self, query_text: str, candidates: list[dict], top_k: int = 20
    ) -> list[dict]:
        """Score and rerank candidates using cross-encoder."""
        raise NotImplementedError


def search(
    conn: sqlite3.Connection,
    query_text: str,
    embedder: Embedder,
    *,
    config: RetrievalConfig | None = None,
    filters: dict | None = None,
) -> list[dict]:
    """Full search pipeline: embed, dense + sparse, RRF, rerank."""
    raise NotImplementedError
