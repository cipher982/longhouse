"""Embedding generation and session chunking pipeline.

Generates embeddings for session search (session-level) and recall (turn-level).
Embedding model configured in config/models.json (default: OpenAI, 256 dims).
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import TYPE_CHECKING

import numpy as np
from sqlalchemy import text as sa_text

from .content import redact_secrets
from .content import strip_noise
from .tokens import truncate
from .transcript import _extract_content
from .transcript import build_transcript

if TYPE_CHECKING:
    from sqlalchemy.orm import Session as DBSession

    from zerg.models.agents import AgentEvent
    from zerg.models.agents import AgentSession
    from zerg.models_config import EmbeddingConfig

logger = logging.getLogger(__name__)

# Max tokens for embedding input (OpenAI limit is 8191, keep conservative)
MAX_EMBEDDING_TOKENS = 1800
EMBEDDING_REQUEST_TIMEOUT_SECONDS = float(os.getenv("EMBEDDING_REQUEST_TIMEOUT_SECONDS", "10"))
EMBEDDING_BATCH_SIZE = int(os.getenv("EMBEDDING_BATCH_SIZE", "32"))
EMBEDDING_MAX_CHUNKS_PER_PASS = int(os.getenv("EMBEDDING_MAX_CHUNKS_PER_PASS", "128"))


@dataclass
class EmbeddingChunk:
    """A chunk of text ready for embedding."""

    kind: str  # "session" or "turn"
    # Turn indices are clean-message indices, not raw DB row ids.
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


def estimate_tokens(text: str) -> int:
    """Conservative token estimate (char-based, ~3 chars/token)."""
    return len(text) // 3


def embedding_to_bytes(arr: np.ndarray) -> bytes:
    """Serialize numpy float32 array to bytes."""
    return arr.astype(np.float32).tobytes()


def bytes_to_embedding(data: bytes, dims: int) -> np.ndarray:
    """Deserialize bytes back to numpy float32 array."""
    return np.frombuffer(data, dtype=np.float32).copy().reshape(dims)


async def generate_embedding(text: str, config: "EmbeddingConfig") -> np.ndarray:
    """Generate one embedding vector via an OpenAI-compatible API."""
    embeddings = await generate_embeddings([text], config)
    return embeddings[0]


async def generate_embeddings(texts: Sequence[str], config: "EmbeddingConfig") -> list[np.ndarray]:
    """Generate embedding vectors via an OpenAI-compatible API (OpenAI, OpenRouter)."""
    from openai import AsyncOpenAI

    from zerg.models_config import build_openai_compatible_client_kwargs

    inputs = list(texts)
    if not inputs:
        return []

    if config.provider not in ("openai", "openrouter"):
        raise ValueError(f"Unsupported embedding provider: {config.provider}. Use 'openai' or 'openrouter'.")

    kwargs = build_openai_compatible_client_kwargs(
        provider=config.provider, api_key=config.api_key, base_url=getattr(config, "base_url", None)
    )
    client = AsyncOpenAI(**kwargs, max_retries=0, timeout=EMBEDDING_REQUEST_TIMEOUT_SECONDS)
    try:
        response = await client.embeddings.create(
            model=config.model,
            input=inputs,
            dimensions=config.dims,
        )
        data = list(response.data or [])
        if len(data) != len(inputs):
            raise ValueError(f"Expected {len(inputs)} embeddings, received {len(data)}")
        def _order_key(pair) -> int:
            fallback, item = pair
            index = getattr(item, "index", None)
            return index if index is not None else fallback

        ordered = sorted(enumerate(data), key=_order_key)
        vectors: list[np.ndarray] = []
        for _pos, item in ordered:
            embedding = getattr(item, "embedding", None)
            if not embedding:
                raise ValueError("No embedding data received")
            vectors.append(np.array(embedding, dtype=np.float32))
        return vectors
    finally:
        await client.close()


def _chunk_batches(chunks: Sequence[EmbeddingChunk]) -> list[list[EmbeddingChunk]]:
    batch_size = max(1, EMBEDDING_BATCH_SIZE)
    return [list(chunks[i : i + batch_size]) for i in range(0, len(chunks), batch_size)]


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
    return list(iter_turn_chunks(events))


@dataclass
class _PendingEmbedding:
    """Embedding vector ready to be written to DB."""

    chunk: EmbeddingChunk
    vec_bytes: bytes


@dataclass(frozen=True)
class _ExistingEmbedding:
    content_hash: str | None
    dims: int


@dataclass(frozen=True)
class _TranscriptTurn:
    role: str
    combined_text: str
    event_index_start: int
    event_index_end: int


def _event_sort_key(event: dict) -> tuple[datetime, int]:
    timestamp = event.get("timestamp")
    if isinstance(timestamp, datetime):
        ts = timestamp
    elif isinstance(timestamp, str):
        try:
            ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            ts = datetime.min.replace(tzinfo=timezone.utc)
    else:
        ts = datetime.min.replace(tzinfo=timezone.utc)
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts, int(event.get("id") or 0)


def _iter_clean_turns(events: list[dict]) -> Iterator[_TranscriptTurn]:
    ordered = sorted(events, key=_event_sort_key)
    current_role: str | None = None
    current_texts: list[str] = []
    current_start = 0
    message_index = 0

    for event in ordered:
        content = _extract_content(event, include_tool_calls=False, tool_output_max_chars=500)
        if content is None:
            continue
        content = redact_secrets(strip_noise(content))
        if not content.strip():
            continue

        role = event.get("role", "unknown")
        if current_role is None:
            current_role = role
            current_texts = [content]
            current_start = message_index
        elif role == current_role:
            current_texts.append(content)
        else:
            if current_role is not None and current_texts:
                yield _TranscriptTurn(
                    role=current_role,
                    combined_text="\n".join(current_texts),
                    event_index_start=current_start,
                    event_index_end=message_index - 1,
                )
            current_role = role
            current_texts = [content]
            current_start = message_index
        message_index += 1

    if current_role is not None and current_texts:
        yield _TranscriptTurn(
            role=current_role,
            combined_text="\n".join(current_texts),
            event_index_start=current_start,
            event_index_end=message_index - 1,
        )


def iter_turn_chunks(events: list[dict]) -> Iterator[EmbeddingChunk]:
    """Yield turn-level embedding chunks without provider or DB work."""
    chunk_idx = 0
    pending_user: _TranscriptTurn | None = None

    def _make_chunk(user_turn: _TranscriptTurn, assistant_turn: _TranscriptTurn | None = None) -> EmbeddingChunk | None:
        text_parts = [user_turn.combined_text]
        event_end = user_turn.event_index_end
        if assistant_turn is not None:
            text_parts.append(assistant_turn.combined_text)
            event_end = assistant_turn.event_index_end

        combined = "\n".join(text_parts)
        combined, _, _was_truncated = truncate(
            combined,
            MAX_EMBEDDING_TOKENS,
            strategy="head",
        )
        if not combined.strip():
            return None
        return EmbeddingChunk(
            kind="turn",
            chunk_index=chunk_idx,
            text=combined,
            content_hash=content_hash(combined),
            event_index_start=user_turn.event_index_start,
            event_index_end=event_end,
        )

    for turn in _iter_clean_turns(events):
        if pending_user is not None:
            if turn.role == "assistant":
                chunk = _make_chunk(pending_user, turn)
                if chunk is not None:
                    yield chunk
                    chunk_idx += 1
                pending_user = None
                continue
            chunk = _make_chunk(pending_user)
            if chunk is not None:
                yield chunk
                chunk_idx += 1
            pending_user = None

        if turn.role == "user":
            pending_user = turn

    if pending_user is not None:
        chunk = _make_chunk(pending_user)
        if chunk is not None:
            yield chunk


def _load_existing_embeddings(
    db: "DBSession",
    *,
    session_id: str,
    model: str,
) -> dict[tuple[str, int], _ExistingEmbedding]:
    from zerg.models.agents import SessionEmbedding

    rows = (
        db.query(
            SessionEmbedding.kind,
            SessionEmbedding.chunk_index,
            SessionEmbedding.content_hash,
            SessionEmbedding.dims,
        )
        .filter(
            SessionEmbedding.session_id == session_id,
            SessionEmbedding.model == model,
        )
        .all()
    )
    return {
        (kind, chunk_index): _ExistingEmbedding(
            content_hash=row_hash,
            dims=dims,
        )
        for kind, chunk_index, row_hash, dims in rows
    }


def _chunk_is_current(
    chunk: EmbeddingChunk,
    existing: dict[tuple[str, int], _ExistingEmbedding],
    *,
    dims: int,
) -> bool:
    row = existing.get((chunk.kind, chunk.chunk_index))
    return row is not None and row.dims == dims and row.content_hash == chunk.content_hash


def _desired_embedding_slice(
    *,
    session: "AgentSession",
    event_dicts: list[dict],
    existing: dict[tuple[str, int], _ExistingEmbedding],
    dims: int,
    max_chunks: int,
) -> tuple[list[EmbeddingChunk], bool]:
    missing: list[EmbeddingChunk] = []
    limit = max(1, max_chunks)

    session_chunk = prepare_session_chunk(session, event_dicts)
    if session_chunk and not _chunk_is_current(session_chunk, existing, dims=dims):
        missing.append(session_chunk)

    for chunk in iter_turn_chunks(event_dicts):
        if _chunk_is_current(chunk, existing, dims=dims):
            continue
        missing.append(chunk)
        if len(missing) > limit:
            return missing[:limit], True

    return missing[:limit], False


async def _persist_embedding_batch(
    session_id: str,
    pending_batch: list[_PendingEmbedding],
    config: "EmbeddingConfig",
    db: "DBSession | None",
) -> int:
    from zerg.models.agents import SessionEmbedding
    from zerg.services.write_serializer import get_write_serializer

    if not pending_batch:
        return 0

    def _persist_embeddings(write_db: "DBSession") -> int:
        count = 0
        for pending in pending_batch:
            existing = (
                write_db.query(SessionEmbedding)
                .filter(
                    SessionEmbedding.session_id == session_id,
                    SessionEmbedding.kind == pending.chunk.kind,
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
                write_db.add(
                    SessionEmbedding(
                        session_id=session_id,
                        kind=pending.chunk.kind,
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
        return count

    ws = get_write_serializer()
    fallback_db = db
    owns_fallback = False
    if fallback_db is None:
        from zerg.database import get_session_factory

        fallback_db = get_session_factory()()
        owns_fallback = True
    try:
        return await ws.execute_or_direct(_persist_embeddings, fallback_db, label="embeddings")
    finally:
        if owns_fallback:
            fallback_db.close()


async def mark_session_embedding_complete(
    session_id: str,
    *,
    transcript_revision: int | None,
    db: "DBSession | None" = None,
) -> None:
    from zerg.services.write_serializer import get_write_serializer

    target_revision = int(transcript_revision or 0)

    def _mark_complete(write_db: "DBSession") -> None:
        if target_revision > 0:
            write_db.execute(
                sa_text(
                    """
                    UPDATE sessions
                    SET needs_embedding = 0,
                        embedding_revision = CASE
                            WHEN COALESCE(embedding_revision, 0) < :rev THEN :rev
                            ELSE COALESCE(embedding_revision, 0)
                        END
                    WHERE id = :sid
                    """
                ),
                {"sid": session_id, "rev": target_revision},
            )
        else:
            write_db.execute(sa_text("UPDATE sessions SET needs_embedding = 0 WHERE id = :sid"), {"sid": session_id})

    ws = get_write_serializer()
    fallback_db = db
    owns_fallback = False
    if fallback_db is None:
        from zerg.database import get_session_factory

        fallback_db = get_session_factory()()
        owns_fallback = True
    try:
        await ws.execute_or_direct(_mark_complete, fallback_db, label="embeddings-complete")
    finally:
        if owns_fallback:
            fallback_db.close()


async def embed_session(
    session_id: str,
    session: "AgentSession",
    events: list["AgentEvent"],
    config: "EmbeddingConfig",
    db: "DBSession",
    *,
    transcript_revision: int | None = None,
) -> tuple[int, int]:
    """Reconcile a bounded slice of embeddings for a session.

    Returns ``(written, remaining)``. ``remaining`` is exact when zero and a
    positive sentinel when more missing/stale chunks still exist. Callers mark
    the session current only when ``remaining == 0``.
    """

    # Convert ORM events to dicts for transcript building
    event_dicts = []
    for e in events:
        if isinstance(e, Mapping):
            event_dicts.append(dict(e))
            continue
        event_dicts.append(
            {
                "role": e.role,
                "content_text": e.content_text,
                "tool_name": e.tool_name,
                "tool_input_json": e.tool_input_json,
                "tool_output_text": e.tool_output_text,
                "timestamp": e.timestamp,
                "session_id": str(e.session_id),
                "id": e.id,
            }
        )

    read_db = db
    owns_read_db = False
    if read_db is None:
        from zerg.database import get_session_factory

        read_db = get_session_factory()()
        owns_read_db = True
    try:
        existing = _load_existing_embeddings(read_db, session_id=session_id, model=config.model)
    finally:
        if owns_read_db:
            read_db.close()

    chunks, has_more = _desired_embedding_slice(
        session=session,
        event_dicts=event_dicts,
        existing=existing,
        dims=config.dims,
        max_chunks=EMBEDDING_MAX_CHUNKS_PER_PASS,
    )
    if not chunks:
        return 0, 0

    written = 0
    for batch in _chunk_batches(chunks):
        vectors = await generate_embeddings([chunk.text for chunk in batch], config)
        pending = [
            _PendingEmbedding(chunk=chunk, vec_bytes=embedding_to_bytes(vec))
            for chunk, vec in zip(batch, vectors, strict=True)
        ]
        written += await _persist_embedding_batch(session_id, pending, config, db)

    return written, 1 if has_more else 0
