"""Stage 5: Hybrid retrieval with RRF fusion and cross-encoder reranking."""

import logging
import re
import sqlite3

import numpy as np

from .config import RetrievalConfig, get_config
from .embed import Embedder

logger = logging.getLogger(__name__)

_ALLOWED_FILTER_KEYS = frozenset({"repo", "item_type"})

# FTS5 special characters that need stripping before quoting.
_FTS5_SPECIAL_RE = re.compile(r'["\(\)\*:\^{}~\[\]!&|]')


def _sanitize_fts_query(query: str) -> str:
    """Sanitize a query string for FTS5 MATCH.

    Strips FTS5 special characters, wraps each remaining token in
    double quotes, and joins with OR.
    """
    cleaned = _FTS5_SPECIAL_RE.sub(" ", query)
    terms = cleaned.split()
    quoted = []
    for term in terms:
        term = term.strip()
        if term:
            quoted.append(f'"{term}"')
    if not quoted:
        return '""'
    return " OR ".join(quoted)


def dense_search(
    conn: sqlite3.Connection,
    query_embedding: np.ndarray,
    top_k: int = 100,
    filters: dict | None = None,
) -> list[dict]:
    """KNN search against vec_items."""
    query_bytes = query_embedding.astype(np.float32).tobytes()

    where_clauses = ["embedding MATCH ?", "k = ?"]
    params: list = [query_bytes, top_k]

    if filters:
        for key, value in filters.items():
            if key not in _ALLOWED_FILTER_KEYS:
                logger.warning("Ignoring unknown filter key: %s", key)
                continue
            where_clauses.append(f"{key} = ?")
            params.append(str(value))

    where = " AND ".join(where_clauses)
    sql = (
        "SELECT item_number, item_type, repo, distance"
        f" FROM vec_items WHERE {where} ORDER BY distance"
    )

    cursor = conn.execute(sql, params)
    results = []
    for row in cursor:
        results.append(
            {
                "item_number": row[0],
                "item_type": row[1],
                "repo": row[2],
                "score": 1.0 - row[3],
            }
        )
    return results


def keyword_search(
    conn: sqlite3.Connection,
    query_text: str,
    top_k: int = 100,
) -> list[dict]:
    """FTS5 BM25 search against item_fts."""
    sanitized = _sanitize_fts_query(query_text)

    sql = (
        "SELECT item_number, item_type, repo, rank"
        " FROM item_fts WHERE content MATCH ? ORDER BY rank LIMIT ?"
    )
    cursor = conn.execute(sql, [sanitized, top_k])

    results = []
    for row in cursor:
        results.append(
            {
                "item_number": int(row[0]),
                "item_type": row[1],
                "repo": row[2],
                "score": -row[3],
            }
        )
    return results


def reciprocal_rank_fusion(
    dense_results: list[dict],
    sparse_results: list[dict],
    k: int = 60,
) -> list[dict]:
    """Combine dense and sparse results using RRF."""
    scores: dict[tuple, float] = {}
    records: dict[tuple, dict] = {}

    for result_list in (dense_results, sparse_results):
        for rank_idx, result in enumerate(result_list):
            key = (result["item_number"], result["item_type"], result["repo"])
            rank = rank_idx + 1

            if key not in records:
                records[key] = result
                scores[key] = 0.0

            scores[key] += 1.0 / (k + rank)

    sorted_keys = sorted(scores, key=lambda x: -scores[x])

    fused = []
    for key in sorted_keys:
        record = records[key].copy()
        record["rrf_score"] = scores[key]
        fused.append(record)

    return fused


class Reranker:
    """Cross-encoder reranker with lazy model loading."""

    def __init__(self, model_name: str | None = None):
        self._model_name = model_name or "BAAI/bge-reranker-large"
        self._model = None

    def _load_model(self):
        from sentence_transformers import CrossEncoder

        self._model = CrossEncoder(self._model_name)

    def rerank(
        self, query_text: str, candidates: list[dict], top_k: int = 20
    ) -> list[dict]:
        """Score and rerank candidates using cross-encoder."""
        if not candidates:
            return []

        if self._model is None:
            self._load_model()

        pairs = [(query_text, c["content"]) for c in candidates]
        scores = self._model.predict(pairs, batch_size=32)

        scored = []
        for i, candidate in enumerate(candidates):
            entry = candidate.copy()
            entry["rerank_score"] = float(scores[i])
            scored.append(entry)

        scored.sort(key=lambda x: -x["rerank_score"])
        return scored[:top_k]


def _fetch_content(conn: sqlite3.Connection, candidate: dict) -> str:
    """Fetch text content for a candidate from assembled_xml or item_fts."""
    item_number = candidate["item_number"]
    item_type = candidate["item_type"]
    repo = candidate["repo"]

    row = conn.execute(
        "SELECT xml_text FROM assembled_xml"
        " WHERE item_number = ? AND item_type = ? AND repo = ?",
        [item_number, item_type, repo],
    ).fetchone()
    if row and row[0]:
        return row[0]

    row = conn.execute(
        "SELECT content FROM item_fts"
        " WHERE item_number = ? AND item_type = ? AND repo = ?",
        [item_number, item_type, repo],
    ).fetchone()
    if row and row[0]:
        return row[0]

    return ""


def search(
    conn: sqlite3.Connection,
    query_text: str,
    embedder: Embedder,
    *,
    config: RetrievalConfig | None = None,
    filters: dict | None = None,
    exclude: tuple[int, str] | None = None,
    reranker: Reranker | None = None,
) -> list[dict]:
    """Full search pipeline: embed, dense + sparse, RRF, rerank.

    Args:
        exclude: Optional (item_number, repo) tuple to exclude from results
            (typically the query item itself).
        reranker: Optional pre-loaded Reranker instance for caching across calls.
    """
    if config is None:
        config = get_config().retrieval

    query_embedding = embedder.encode_query(query_text)

    dense_results = dense_search(
        conn, query_embedding, top_k=config.top_k_initial, filters=filters
    )
    sparse_results = keyword_search(conn, query_text, top_k=config.top_k_initial)

    merged = reciprocal_rank_fusion(dense_results, sparse_results, k=config.rrf_k)

    # Exclude the query item from results (self-match removal).
    if exclude is not None:
        exc_number, exc_repo = exclude
        merged = [
            r for r in merged
            if not (r["item_number"] == exc_number and r["repo"] == exc_repo)
        ]

    for candidate in merged:
        candidate["content"] = _fetch_content(conn, candidate)

    if merged:
        if reranker is None:
            reranker = Reranker(config.reranker_model)
        merged = reranker.rerank(query_text, merged, top_k=config.top_k_rerank)

    return merged
