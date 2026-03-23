"""Session processing module — clean, structured processing of AgentEvent data.

This module centralizes session event processing that's used by multiple consumers
(daily digest, memory summarizer, session briefing). It does NOT touch databases —
callers pass in event data, module processes and returns results.

Public API:
    content: strip_noise(), redact_secrets(), is_tool_result()
    tokens:  count_tokens(), truncate()
    transcript: build_transcript(), detect_turns(), SessionMessage, Turn, SessionTranscript
    summarize: summarize_events(), quick_summary(), SessionSummary
"""

from .content import is_tool_result
from .content import redact_secrets
from .content import strip_noise
from .embeddings import bytes_to_embedding
from .embeddings import embed_session
from .embeddings import embedding_to_bytes
from .embeddings import generate_embedding
from .embeddings import prepare_session_chunk
from .embeddings import prepare_turn_chunks
from .embeddings import sanitize_for_embedding
from .summarize import DEFAULT_CONTEXT_BUDGET
from .summarize import SessionSummary
from .summarize import incremental_summary
from .summarize import quick_summary
from .summarize import safe_parse_json
from .summarize import summarize_events
from .tokens import count_tokens
from .tokens import truncate
from .transcript import SessionMessage
from .transcript import SessionTranscript
from .transcript import Turn
from .transcript import build_transcript
from .transcript import detect_turns

__all__ = [
    # content
    "strip_noise",
    "redact_secrets",
    "is_tool_result",
    # tokens
    "count_tokens",
    "truncate",
    # transcript
    "SessionMessage",
    "Turn",
    "SessionTranscript",
    "build_transcript",
    "detect_turns",
    # summarize
    "incremental_summary",
    "summarize_events",
    "DEFAULT_CONTEXT_BUDGET",
    "SessionSummary",
    "quick_summary",
    "safe_parse_json",
    # embeddings
    "sanitize_for_embedding",
    "generate_embedding",
    "embedding_to_bytes",
    "bytes_to_embedding",
    "prepare_session_chunk",
    "prepare_turn_chunks",
    "embed_session",
]
