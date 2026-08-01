"""Session processing module — clean, structured processing of AgentEvent data.

This module centralizes session event processing that's used by multiple consumers
(daily digest, memory summarizer, startup continuity). It does NOT touch databases —
callers pass in event data, module processes and returns results.

Public API:
    transcript_content: strip_noise(), redact_secrets(), is_tool_result() —
        these live in zerg.services.transcript_content, outside this package, so
        searchd can use them without importing tiktoken through .tokens
    tokens:  count_tokens(), truncate()
    transcript: build_transcript(), detect_turns(), SessionMessage, Turn, SessionTranscript
    summarize: summarize_events(), quick_summary(), SessionSummary
"""

from zerg.services.transcript_content import is_tool_result
from zerg.services.transcript_content import redact_secrets
from zerg.services.transcript_content import strip_noise

from .embeddings import bytes_to_embedding
from .embeddings import embedding_to_bytes
from .embeddings import generate_embedding
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
    "prepare_turn_chunks",
]
