"""Canonical cleaning and episode chunking for local retrieval projection."""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from zerg.services.clean_events import event_sort_key as _event_sort_key
from zerg.services.clean_events import iter_clean_transcript_events
from zerg.services.transcript_content import redact_secrets
from zerg.services.transcript_content import strip_noise

from .tokens import truncate

EMBEDDING_BATCH_SIZE = int(os.getenv("EMBEDDING_BATCH_SIZE", "32"))
EMBEDDING_MAX_CHUNKS_PER_PASS = int(os.getenv("EMBEDDING_MAX_CHUNKS_PER_PASS", "128"))
# Only used when no model-budget truncator is supplied (tests and the pure
# chunking path). Production passes the model's own tokenizer, because budgeting
# an episode with a tokenizer the model does not use is what silently discarded
# the tail of a third of all episodes.
MAX_EMBEDDING_TOKENS = 1800

logger = logging.getLogger(__name__)


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


def _chunk_batches(chunks: Sequence[EmbeddingChunk]) -> list[list[EmbeddingChunk]]:
    batch_size = max(1, EMBEDDING_BATCH_SIZE)
    return [list(chunks[i : i + batch_size]) for i in range(0, len(chunks), batch_size)]


def prepare_turn_chunks(events: list[dict], *, provider: str | None = None) -> list[EmbeddingChunk]:
    """Prepare episode-level embedding chunks for recall.

    Detects episode boundaries (one user event through everything up to the
    next) and creates one chunk per episode.
    """
    return list(iter_turn_chunks(events, provider=provider))


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


def _iter_clean_turns(events: list[Mapping[str, object]], *, provider: str | None = None) -> Iterator[_TranscriptTurn]:
    ordered = sorted(events, key=_event_sort_key)
    current_role: str | None = None
    current_texts: list[str] = []
    current_start = 0
    current_source_id: int | None = None

    for clean_event in iter_clean_transcript_events(ordered, provider=provider):
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


def iter_turn_chunks(
    events: list[dict],
    *,
    provider: str | None = None,
    truncate_to_budget: "Callable[[str], tuple[str, bool]] | None" = None,
) -> Iterator[EmbeddingChunk]:
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
    truncated_chunks = 0
    pending_texts: list[str] = []
    pending_start: int | None = None
    pending_end: int | None = None
    pending_source_id: int | None = None

    def _flush() -> EmbeddingChunk | None:
        nonlocal chunk_idx, truncated_chunks
        if not pending_texts or pending_start is None:
            return None
        combined = "\n".join(pending_texts)
        if truncate_to_budget is None:
            combined, _, was_truncated = truncate(combined, MAX_EMBEDDING_TOKENS, strategy="sandwich")
        else:
            combined, was_truncated = truncate_to_budget(combined)
        if was_truncated:
            truncated_chunks += 1
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

    for turn in _iter_clean_turns(events, provider=provider):
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
    if truncated_chunks:
        # Truncation used to be computed and thrown away, so nothing downstream
        # could tell a whole episode from a cut one.
        logger.info("Embedding chunker truncated %d of %d episodes", truncated_chunks, chunk_idx)
