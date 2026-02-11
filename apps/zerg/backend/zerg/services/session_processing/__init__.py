"""Session processing module — clean, structured processing of AgentEvent data.

This module centralizes session event processing that's used by multiple consumers
(daily digest, memory summarizer, session briefing). It does NOT touch databases —
callers pass in event data, module processes and returns results.

Public API:
    content: strip_noise(), redact_secrets(), is_tool_result()
    tokens:  count_tokens(), truncate()
    transcript: build_transcript(), detect_turns(), SessionMessage, Turn, SessionTranscript
"""

from .content import is_tool_result
from .content import redact_secrets
from .content import strip_noise
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
]
