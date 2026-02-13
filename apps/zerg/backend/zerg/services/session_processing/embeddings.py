"""Embedding generation and session chunking pipeline.

Generates embeddings for session search (session-level) and recall (turn-level).
Supports Gemini (default) and OpenAI providers.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

from .content import redact_secrets
from .content import strip_noise
from .tokens import truncate
from .transcript import build_transcript

if TYPE_CHECKING:
    from sqlalchemy.orm import Session as DBSession

    from zerg.models.agents import AgentEvent
    from zerg.models.agents import AgentSession
    from zerg.models_config import EmbeddingConfig

logger = logging.getLogger(__name__)

# Max tokens for embedding input (Gemini limit is 2048, keep buffer)
MAX_EMBEDDING_TOKENS = 1800


@dataclass
class EmbeddingChunk:
    """A chunk of text ready for embedding."""

    kind: str  # "session" or "turn"
    chunk_index: int  # -1 for session, >=0 for turn
    text: str
    content_hash: str
    event_index_start: int | None = None
    event_index_end: int | None = None


def sanitize_for_embedding(text: str) -> str:
    """Clean text for embedding: strip noise and redact secrets."""
    if not text:
        return ""
    return redact_secrets(strip_noise(text))


def content_hash(text: str) -> str:
    """SHA-256 hash of text content for dedup."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def estimate_tokens_gemini(text: str) -> int:
    """Conservative token estimate for Gemini (char-based, ~3 chars/token)."""
    return len(text) // 3


def embedding_to_bytes(arr: np.ndarray) -> bytes:
    """Serialize numpy float32 array to bytes."""
    return arr.astype(np.float32).tobytes()


def bytes_to_embedding(data: bytes, dims: int) -> np.ndarray:
    """Deserialize bytes back to numpy float32 array."""
    return np.frombuffer(data, dtype=np.float32).copy().reshape(dims)


async def generate_embedding(text: str, config: "EmbeddingConfig") -> np.ndarray:
    """Generate an embedding vector for the given text.

    Dispatches to Gemini or OpenAI based on config.provider.
    """
    if config.provider == "gemini":
        return await _generate_gemini(text, config)
    elif config.provider == "openai":
        return await _generate_openai(text, config)
    else:
        raise ValueError(f"Unknown embedding provider: {config.provider}")


async def _generate_gemini(text: str, config: "EmbeddingConfig") -> np.ndarray:
    """Generate embedding via Gemini API.

    The Gemini client is synchronous, so we run it in a thread to avoid
    blocking the event loop.
    """
    from google import genai

    def _call() -> np.ndarray:
        client = genai.Client(api_key=config.api_key)
        result = client.models.embed_content(
            model=config.model,
            contents=text,
            config={"output_dimensionality": config.dims},
        )
        return np.array(result.embeddings[0].values, dtype=np.float32)

    return await asyncio.to_thread(_call)


async def _generate_openai(text: str, config: "EmbeddingConfig") -> np.ndarray:
    """Generate embedding via OpenAI API."""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=config.api_key)
    try:
        response = await client.embeddings.create(
            model=config.model,
            input=text,
            dimensions=config.dims,
        )
        return np.array(response.data[0].embedding, dtype=np.float32)
    finally:
        await client.close()


def prepare_session_chunk(
    session: "AgentSession",
    events: list[dict],
) -> EmbeddingChunk | None:
    """Prepare a session-level embedding chunk.

    Uses summary if available, else builds transcript truncated to MAX_EMBEDDING_TOKENS.
    """
    # Prefer pre-computed summary
    if session.summary:
        text = session.summary
        if session.summary_title:
            text = f"{session.summary_title}. {text}"
        text = sanitize_for_embedding(text)
        if text.strip():
            return EmbeddingChunk(
                kind="session",
                chunk_index=-1,
                text=text,
                content_hash=content_hash(text),
            )

    # Fallback: build transcript
    transcript = build_transcript(
        events,
        include_tool_calls=False,
        strip_noise=True,
        redact_secrets=True,
        token_budget=MAX_EMBEDDING_TOKENS,
    )
    if not transcript.messages:
        return None

    text = "\n".join(m.content for m in transcript.messages)
    text = text.strip()
    if not text:
        return None

    return EmbeddingChunk(
        kind="session",
        chunk_index=-1,
        text=text,
        content_hash=content_hash(text),
    )


def prepare_turn_chunks(events: list[dict]) -> list[EmbeddingChunk]:
    """Prepare turn-level embedding chunks for recall.

    Detects user/assistant turn boundaries and creates one chunk per pair.
    """
    transcript = build_transcript(
        events,
        include_tool_calls=False,
        strip_noise=True,
        redact_secrets=True,
    )
    if not transcript.turns:
        return []

    chunks: list[EmbeddingChunk] = []
    chunk_idx = 0

    # Build event index mapping: for each turn, find approximate event range
    event_idx = 0
    turn_event_map: list[tuple[int, int]] = []  # (start, end) per turn

    for turn in transcript.turns:
        start = event_idx
        event_idx += turn.message_count
        turn_event_map.append((start, event_idx - 1))

    for i in range(len(transcript.turns)):
        turn = transcript.turns[i]
        if turn.role != "user":
            continue

        # Combine user turn with next assistant turn if available
        text_parts = [turn.combined_text]
        event_start, _ = turn_event_map[i]
        event_end = turn_event_map[i][1]

        if i + 1 < len(transcript.turns) and transcript.turns[i + 1].role == "assistant":
            text_parts.append(transcript.turns[i + 1].combined_text)
            event_end = turn_event_map[i + 1][1]

        combined = "\n".join(text_parts)
        # Truncate to token limit
        combined, _, was_truncated = truncate(
            combined,
            MAX_EMBEDDING_TOKENS,
            strategy="head",
        )

        if combined.strip():
            chunks.append(
                EmbeddingChunk(
                    kind="turn",
                    chunk_index=chunk_idx,
                    text=combined,
                    content_hash=content_hash(combined),
                    event_index_start=event_start,
                    event_index_end=event_end,
                )
            )
            chunk_idx += 1

    return chunks


@dataclass
class _PendingEmbedding:
    """Embedding vector ready to be written to DB."""

    chunk: EmbeddingChunk
    vec_bytes: bytes


async def embed_session(
    session_id: str,
    session: "AgentSession",
    events: list["AgentEvent"],
    config: "EmbeddingConfig",
    db: "DBSession",
) -> int:
    """Orchestrate embedding generation for a session.

    Phase 1: Generate all embeddings via API (no DB access — slow network I/O).
    Phase 2: Write all results in one short DB transaction (fast).

    This separation prevents the DB write lock from being held during API calls,
    which caused SQLite "database is locked" errors under concurrent backfill.
    """
    from sqlalchemy import text as sa_text

    from zerg.models.agents import SessionEmbedding

    # Convert ORM events to dicts for transcript building
    event_dicts = [
        {
            "role": e.role,
            "content_text": e.content_text,
            "tool_name": e.tool_name,
            "tool_input_json": e.tool_input_json,
            "tool_output_text": e.tool_output_text,
            "timestamp": e.timestamp,
            "session_id": str(e.session_id),
        }
        for e in events
    ]

    # --- Phase 1: Generate embeddings (network I/O, no DB) ---
    session_pending: _PendingEmbedding | None = None
    turn_pending: list[_PendingEmbedding] = []

    session_chunk = prepare_session_chunk(session, event_dicts)
    if session_chunk:
        try:
            vec = await generate_embedding(session_chunk.text, config)
            session_pending = _PendingEmbedding(chunk=session_chunk, vec_bytes=embedding_to_bytes(vec))
        except Exception:
            logger.exception("Failed to generate session embedding for %s", session_id)

    turn_chunks = prepare_turn_chunks(event_dicts)
    for chunk in turn_chunks:
        try:
            vec = await generate_embedding(chunk.text, config)
            turn_pending.append(_PendingEmbedding(chunk=chunk, vec_bytes=embedding_to_bytes(vec)))
        except Exception:
            logger.exception("Failed to generate turn embedding %d for %s", chunk.chunk_index, session_id)

    # --- Phase 2: Write all results in one short DB transaction ---
    count = 0
    session_embedding_ok = False

    if session_pending:
        existing = (
            db.query(SessionEmbedding)
            .filter(
                SessionEmbedding.session_id == session_id,
                SessionEmbedding.kind == "session",
                SessionEmbedding.chunk_index == -1,
                SessionEmbedding.model == config.model,
            )
            .first()
        )
        if existing:
            existing.embedding = session_pending.vec_bytes
            existing.content_hash = session_pending.chunk.content_hash
            existing.dims = config.dims
        else:
            db.add(
                SessionEmbedding(
                    session_id=session_id,
                    kind="session",
                    chunk_index=-1,
                    model=config.model,
                    dims=config.dims,
                    embedding=session_pending.vec_bytes,
                    content_hash=session_pending.chunk.content_hash,
                )
            )
        count += 1
        session_embedding_ok = True

    for pending in turn_pending:
        existing = (
            db.query(SessionEmbedding)
            .filter(
                SessionEmbedding.session_id == session_id,
                SessionEmbedding.kind == "turn",
                SessionEmbedding.chunk_index == pending.chunk.chunk_index,
                SessionEmbedding.model == config.model,
            )
            .first()
        )
        if existing:
            existing.embedding = pending.vec_bytes
            existing.content_hash = pending.chunk.content_hash
            existing.dims = config.dims
            existing.event_index_start = pending.chunk.event_index_start
            existing.event_index_end = pending.chunk.event_index_end
        else:
            db.add(
                SessionEmbedding(
                    session_id=session_id,
                    kind="turn",
                    chunk_index=pending.chunk.chunk_index,
                    model=config.model,
                    dims=config.dims,
                    embedding=pending.vec_bytes,
                    content_hash=pending.chunk.content_hash,
                    event_index_start=pending.chunk.event_index_start,
                    event_index_end=pending.chunk.event_index_end,
                )
            )
        count += 1

    # Only clear the flag when the session-level embedding succeeded so
    # backfill can retry on transient failures.
    if session_embedding_ok:
        db.execute(
            sa_text("UPDATE sessions SET needs_embedding = 0 WHERE id = :sid"),
            {"sid": session_id},
        )
    db.commit()

    return count
