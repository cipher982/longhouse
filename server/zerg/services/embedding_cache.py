"""In-memory embedding cache for fast vector similarity search.

Loads session and turn embeddings into numpy matrices for cosine similarity search.
Uses L2-normalization on load so dot product equals cosine similarity.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

import numpy as np

from zerg.services.session_processing.embeddings import bytes_to_embedding

if TYPE_CHECKING:
    from sqlalchemy.orm import Session as DBSession

logger = logging.getLogger(__name__)


class EmbeddingCache:
    """Thread-safe in-memory cache for embedding vectors.

    Session embeddings (~10MB for 10K sessions @ 256 dims) are loaded eagerly.
    Turn embeddings (~340MB) are loaded lazily on first recall query.
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._session_ids: list[str] = []
        self._session_matrix: np.ndarray | None = None  # (N, dims) normalized
        self._turn_session_ids: list[str] = []
        self._turn_chunk_indices: list[int] = []
        self._turn_event_starts: list[int | None] = []
        self._turn_event_ends: list[int | None] = []
        self._turn_matrix: np.ndarray | None = None  # (M, dims) normalized
        self._session_loaded = False
        self._turn_loaded = False
        self._dims = 0
        self._model = ""
        self._initialized = True

    def load_session_embeddings(self, db: "DBSession", model: str, dims: int) -> int:
        """Eagerly load all session-level embeddings into a numpy matrix."""
        from zerg.models.agents import SessionEmbedding

        embeddings = db.query(SessionEmbedding).filter(SessionEmbedding.kind == "session", SessionEmbedding.model == model).all()

        if not embeddings:
            self._session_loaded = True
            self._dims = dims
            self._model = model
            return 0

        ids = []
        vecs = []
        for emb in embeddings:
            try:
                vec = bytes_to_embedding(emb.embedding, dims)
                ids.append(str(emb.session_id))
                vecs.append(vec)
            except Exception:
                logger.warning("Skipping malformed embedding for session %s", emb.session_id)

        if vecs:
            matrix = np.stack(vecs)
            # L2-normalize so dot product = cosine similarity
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            norms = np.where(norms == 0, 1, norms)
            self._session_matrix = matrix / norms
            self._session_ids = ids

        self._session_loaded = True
        self._dims = dims
        self._model = model
        return len(ids)

    def load_turn_embeddings(self, db: "DBSession", model: str, dims: int) -> int:
        """Lazy-load turn-level embeddings for recall queries."""
        from zerg.models.agents import SessionEmbedding

        embeddings = db.query(SessionEmbedding).filter(SessionEmbedding.kind == "turn", SessionEmbedding.model == model).all()

        if not embeddings:
            self._turn_loaded = True
            return 0

        ids = []
        chunk_indices = []
        event_starts = []
        event_ends = []
        vecs = []
        for emb in embeddings:
            try:
                vec = bytes_to_embedding(emb.embedding, dims)
                ids.append(str(emb.session_id))
                chunk_indices.append(emb.chunk_index)
                event_starts.append(emb.event_index_start)
                event_ends.append(emb.event_index_end)
                vecs.append(vec)
            except Exception:
                logger.warning(
                    "Skipping malformed turn embedding for session %s chunk %d",
                    emb.session_id,
                    emb.chunk_index,
                )

        if vecs:
            matrix = np.stack(vecs)
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            norms = np.where(norms == 0, 1, norms)
            self._turn_matrix = matrix / norms
            self._turn_session_ids = ids
            self._turn_chunk_indices = chunk_indices
            self._turn_event_starts = event_starts
            self._turn_event_ends = event_ends

        self._turn_loaded = True
        return len(ids)

    def search_sessions(
        self,
        query_embedding: np.ndarray,
        limit: int = 10,
        session_filter: set[str] | None = None,
    ) -> list[tuple[str, float]]:
        """Search sessions by cosine similarity.

        Returns list of (session_id, score) sorted by score descending.
        """
        if self._session_matrix is None or len(self._session_ids) == 0:
            return []

        # Normalize query
        query_norm = np.linalg.norm(query_embedding)
        if query_norm == 0:
            return []
        query_normalized = query_embedding / query_norm

        # Dot product = cosine similarity (both normalized)
        scores = self._session_matrix @ query_normalized

        # Apply filter if provided
        if session_filter:
            for i, sid in enumerate(self._session_ids):
                if sid not in session_filter:
                    scores[i] = -1.0

        # Top-K using argpartition
        k = min(limit, len(scores))
        if k <= 0:
            return []
        top_indices = np.argpartition(scores, -k)[-k:]
        top_indices = top_indices[np.argsort(scores[top_indices])[::-1]]

        results = []
        for idx in top_indices:
            score = float(scores[idx])
            if score <= 0:
                continue
            results.append((self._session_ids[idx], score))

        return results

    def search_turns(
        self,
        query_embedding: np.ndarray,
        limit: int = 10,
        session_filter: set[str] | None = None,
    ) -> list[tuple[str, int, float, int | None, int | None]]:
        """Search turns by cosine similarity.

        Returns list of (session_id, chunk_index, score, event_start, event_end)
        sorted by score descending.
        """
        if self._turn_matrix is None or len(self._turn_session_ids) == 0:
            return []

        query_norm = np.linalg.norm(query_embedding)
        if query_norm == 0:
            return []
        query_normalized = query_embedding / query_norm

        scores = self._turn_matrix @ query_normalized

        if session_filter:
            for i, sid in enumerate(self._turn_session_ids):
                if sid not in session_filter:
                    scores[i] = -1.0

        k = min(limit, len(scores))
        if k <= 0:
            return []
        top_indices = np.argpartition(scores, -k)[-k:]
        top_indices = top_indices[np.argsort(scores[top_indices])[::-1]]

        results = []
        for idx in top_indices:
            score = float(scores[idx])
            if score <= 0:
                continue
            results.append(
                (
                    self._turn_session_ids[idx],
                    self._turn_chunk_indices[idx],
                    score,
                    self._turn_event_starts[idx],
                    self._turn_event_ends[idx],
                )
            )

        return results

    def invalidate(self, session_id: str | None = None) -> None:
        """Mark cache for reload. If session_id given, only mark that session."""
        # Simple approach: just clear everything
        self._session_loaded = False
        self._turn_loaded = False
        self._session_matrix = None
        self._turn_matrix = None
        self._session_ids = []
        self._turn_session_ids = []
        self._turn_chunk_indices = []
        self._turn_event_starts = []
        self._turn_event_ends = []

    @classmethod
    def reset(cls) -> None:
        """Reset singleton for testing."""
        with cls._lock:
            cls._instance = None
