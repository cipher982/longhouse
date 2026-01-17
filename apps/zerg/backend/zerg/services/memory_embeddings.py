"""Embedding utilities for Memory Files."""

from __future__ import annotations

import logging
import os
from typing import Iterable

import numpy as np
from openai import OpenAI
from sqlalchemy.orm import Session

from zerg.models.models import MemoryEmbedding

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "text-embedding-3-small"

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        client_kwargs = {}
        base_url = os.getenv("OPENAI_BASE_URL")
        if base_url:
            client_kwargs["base_url"] = base_url
        _client = OpenAI(**client_kwargs)
    return _client


def serialize_embedding(vec: np.ndarray) -> bytes:
    """Serialize a float32 numpy array to bytes."""
    if vec.dtype != np.float32:
        vec = vec.astype(np.float32)
    return vec.tobytes()


def deserialize_embedding(data: bytes) -> np.ndarray:
    """Deserialize bytes into a float32 numpy array."""
    return np.frombuffer(data, dtype=np.float32)


def _normalize(vec: np.ndarray) -> np.ndarray:
    """Return unit-normalized vector (or original if norm is zero)."""
    norm = np.linalg.norm(vec)
    if norm == 0:
        return vec
    return vec / norm


def embed_query(query: str) -> np.ndarray:
    """Generate embedding for a query string."""
    client = _get_client()
    response = client.embeddings.create(
        model=EMBEDDING_MODEL,
        input=[query],
    )
    vec = np.array(response.data[0].embedding, dtype=np.float32)
    return _normalize(vec)


def embed_texts(texts: Iterable[str]) -> np.ndarray:
    """Generate embeddings for a list of strings."""
    client = _get_client()
    response = client.embeddings.create(
        model=EMBEDDING_MODEL,
        input=list(texts),
    )
    vectors = np.array([item.embedding for item in response.data], dtype=np.float32)
    return np.vstack([_normalize(v) for v in vectors])


def embeddings_enabled(settings) -> bool:
    """Return True if embeddings can be generated in current environment."""
    if getattr(settings, "testing", False):
        return False
    if getattr(settings, "llm_disabled", False):
        return False
    return bool(getattr(settings, "openai_api_key", None))


def upsert_memory_embedding(
    db: Session,
    *,
    owner_id: int,
    memory_file_id: int,
    model: str,
    embedding: np.ndarray,
) -> MemoryEmbedding:
    """Create or update an embedding row."""
    existing = (
        db.query(MemoryEmbedding)
        .filter(
            MemoryEmbedding.owner_id == owner_id,
            MemoryEmbedding.memory_file_id == memory_file_id,
            MemoryEmbedding.model == model,
        )
        .first()
    )

    payload = serialize_embedding(_normalize(embedding))

    if existing:
        existing.embedding = payload
        db.commit()
        db.refresh(existing)
        return existing

    row = MemoryEmbedding(
        owner_id=owner_id,
        memory_file_id=memory_file_id,
        model=model,
        embedding=payload,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def maybe_upsert_embedding(
    db: Session,
    *,
    owner_id: int,
    memory_file_id: int,
    content: str,
    model: str = EMBEDDING_MODEL,
) -> bool:
    """Generate and upsert an embedding for a memory file if enabled.

    Returns True if an embedding was written, False if skipped.
    """
    from zerg.config import get_settings

    settings = get_settings()
    if not embeddings_enabled(settings):
        return False

    try:
        vectors = embed_texts([content])
        upsert_memory_embedding(
            db,
            owner_id=owner_id,
            memory_file_id=memory_file_id,
            model=model,
            embedding=vectors[0],
        )
        return True
    except Exception as e:
        logger.warning("Failed to generate memory embedding: %s", e)
        return False


def search_memory_embeddings(
    db: Session,
    *,
    owner_id: int,
    query_embedding: np.ndarray,
    limit: int = 5,
    model: str | None = None,
) -> list[tuple[int, float]]:
    """Return (memory_file_id, score) pairs ordered by similarity."""
    query_vec = _normalize(query_embedding)

    q = db.query(MemoryEmbedding).filter(MemoryEmbedding.owner_id == owner_id)
    if model:
        q = q.filter(MemoryEmbedding.model == model)

    rows = q.all()
    if not rows:
        return []

    scored: list[tuple[int, float]] = []
    for row in rows:
        emb = deserialize_embedding(row.embedding)
        score = float(np.dot(emb, query_vec))
        scored.append((row.memory_file_id, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:limit]
