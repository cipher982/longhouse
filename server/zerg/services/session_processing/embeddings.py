"""Episode chunking and embedding generation.

Shared by the storage-v2 embeddings-v1 projector (server/zerg/services/embeddings_v2_projector.py):
episode-boundary chunking (one user event through everything up to the next),
sanitization, and the OpenAI-compatible embedding API call. Embedding model
configured in config/models.json.
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

from .content import redact_secrets
from .content import strip_noise
from .tokens import truncate
from .transcript import _extract_content

if TYPE_CHECKING:
    from zerg.models_config import EmbeddingConfig

logger = logging.getLogger(__name__)

# Max tokens for embedding input (OpenAI limit is 8191, keep conservative)
MAX_EMBEDDING_TOKENS = 1800
EMBEDDING_REQUEST_TIMEOUT_SECONDS = float(os.getenv("EMBEDDING_REQUEST_TIMEOUT_SECONDS", "10"))
EMBEDDING_BATCH_SIZE = int(os.getenv("EMBEDDING_BATCH_SIZE", "32"))
EMBEDDING_MAX_CHUNKS_PER_PASS = int(os.getenv("EMBEDDING_MAX_CHUNKS_PER_PASS", "128"))


class PermanentEmbeddingConfigError(ValueError):
    """A misconfiguration that retrying will never fix on its own.

    Distinct from other ValueErrors this module raises (a batch returning the
    wrong item count, a missing embedding in one response) which are more
    likely transient API flakiness worth retrying soon. Callers with a
    retry/backoff loop should treat this specifically as "stop hammering the
    API and surface it," not fold it into the same fast-retry bucket as every
    other ValueError -- including callers elsewhere in this codebase (e.g. the
    catalog-side ValueErrors in embeddings_v2_projector.py) that are genuinely
    transient and should keep retrying quickly.
    """


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
    # See _TranscriptTurn.source_event_id_start: the resolvable half of the
    # locator, carried so a stored episode can be pointed back at a transcript
    # position without reproducing the clean-index projection.
    source_event_id_start: int | None = None


@dataclass(frozen=True)
class CleanTranscriptEvent:
    """A content-bearing transcript event in the same projection used for turn embeddings."""

    index: int
    event_id: int | None
    role: str
    content: str
    tool_name: str | None = None


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
        raise PermanentEmbeddingConfigError(f"Unsupported embedding provider: {config.provider}. Use 'openai' or 'openrouter'.")

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
            vector = np.array(embedding, dtype=np.float32)
            if vector.shape[0] != config.dims:
                # A provider that ignores `dimensions` returns its native size
                # instead of truncating. Stored under the configured dims it
                # would later be silently skipped by the cache loader's shape
                # check -- fail loudly here instead, at the one point that
                # knows both the expected and actual size. Permanent: retrying
                # the same model/config will produce the same mismatch forever.
                raise PermanentEmbeddingConfigError(
                    f"Embedding dims mismatch: expected {config.dims}, got {vector.shape[0]} from model {config.model}"
                )
            vectors.append(vector)
        return vectors
    finally:
        await client.close()


def _chunk_batches(chunks: Sequence[EmbeddingChunk]) -> list[list[EmbeddingChunk]]:
    batch_size = max(1, EMBEDDING_BATCH_SIZE)
    return [list(chunks[i : i + batch_size]) for i in range(0, len(chunks), batch_size)]


def prepare_turn_chunks(events: list[dict]) -> list[EmbeddingChunk]:
    """Prepare episode-level embedding chunks for recall.

    Detects episode boundaries (one user event through everything up to the
    next) and creates one chunk per episode.
    """
    return list(iter_turn_chunks(events))


@dataclass(frozen=True)
class _TranscriptTurn:
    role: str
    combined_text: str
    event_index_start: int
    event_index_end: int
    # Source event id of the turn's first clean event. The index fields are
    # clean-message ordinals, which nothing downstream can resolve back to a
    # transcript position without re-running this module's sanitization; the
    # source id survives that projection and is what makes a chunk locatable.
    source_event_id_start: int | None = None


def _event_sort_key(event: Mapping[str, object]) -> tuple[datetime, int]:
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


def iter_clean_transcript_events(
    events: list[Mapping[str, object]],
    *,
    include_tool_calls: bool = False,
) -> Iterator[CleanTranscriptEvent]:
    """Yield content-bearing events in the clean index space used by turn embeddings."""
    ordered = sorted(events, key=_event_sort_key)
    message_index = 0

    for event in ordered:
        content = _extract_content(event, include_tool_calls=include_tool_calls, tool_output_max_chars=500)
        if content is None:
            continue
        content = redact_secrets(strip_noise(content))
        if not content.strip():
            continue

        event_id_value = event.get("id")
        event_id = int(event_id_value) if event_id_value is not None else None
        tool_name_value = event.get("tool_name")
        yield CleanTranscriptEvent(
            index=message_index,
            event_id=event_id,
            role=str(event.get("role") or "unknown"),
            content=content,
            tool_name=str(tool_name_value) if tool_name_value else None,
        )
        message_index += 1


def _iter_clean_turns(events: list[Mapping[str, object]]) -> Iterator[_TranscriptTurn]:
    ordered = sorted(events, key=_event_sort_key)
    current_role: str | None = None
    current_texts: list[str] = []
    current_start = 0
    current_source_id: int | None = None

    for clean_event in iter_clean_transcript_events(ordered):
        content = clean_event.content
        role = clean_event.role
        if current_role is None:
            current_role = role
            current_texts = [content]
            current_start = clean_event.index
            current_source_id = clean_event.event_id
        elif role == current_role:
            current_texts.append(content)
        else:
            if current_role is not None and current_texts:
                yield _TranscriptTurn(
                    role=current_role,
                    combined_text="\n".join(current_texts),
                    event_index_start=current_start,
                    event_index_end=clean_event.index - 1,
                    source_event_id_start=current_source_id,
                )
            current_role = role
            current_texts = [content]
            current_start = clean_event.index
            current_source_id = clean_event.event_id

    if current_role is not None and current_texts:
        yield _TranscriptTurn(
            role=current_role,
            combined_text="\n".join(current_texts),
            event_index_start=current_start,
            event_index_end=current_start + len(current_texts) - 1,
            source_event_id_start=current_source_id,
        )


def iter_turn_chunks(events: list[dict]) -> Iterator[EmbeddingChunk]:
    """Yield episode-level embedding chunks without provider or DB work.

    An episode spans one user event through everything up to (but not
    including) the next user event -- the full work an agent did in response
    to one request, not just the first assistant reply. LongMemEval (ICLR
    2025) found this "round" boundary beats both fixed windows and whole
    sessions for conversational recall; stopping at the first assistant turn
    silently dropped everything after a multi-round tool-call episode, which
    is most of a coding-agent transcript.
    """
    chunk_idx = 0
    pending_texts: list[str] = []
    pending_start: int | None = None
    pending_end: int | None = None
    pending_source_id: int | None = None

    def _flush() -> EmbeddingChunk | None:
        nonlocal chunk_idx
        if not pending_texts or pending_start is None:
            return None
        combined = "\n".join(pending_texts)
        combined, _, _was_truncated = truncate(
            combined,
            MAX_EMBEDDING_TOKENS,
            strategy="sandwich",
        )
        if not combined.strip():
            return None
        chunk = EmbeddingChunk(
            kind="turn",
            chunk_index=chunk_idx,
            text=combined,
            content_hash=content_hash(combined),
            event_index_start=pending_start,
            event_index_end=pending_end,
            source_event_id_start=pending_source_id,
        )
        chunk_idx += 1
        return chunk

    for turn in _iter_clean_turns(events):
        if turn.role == "user" and pending_start is not None:
            chunk = _flush()
            if chunk is not None:
                yield chunk
            pending_texts = []
            pending_start = None
            pending_source_id = None

        if pending_start is None:
            pending_start = turn.event_index_start
            pending_source_id = turn.source_event_id_start
        pending_texts.append(turn.combined_text)
        pending_end = turn.event_index_end

    chunk = _flush()
    if chunk is not None:
        yield chunk
