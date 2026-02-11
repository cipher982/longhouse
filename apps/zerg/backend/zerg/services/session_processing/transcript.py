"""Transcript building — convert raw AgentEvent dicts into structured transcripts.

The main entry point is :func:`build_transcript`, which takes a list of event
dicts (matching the AgentEvent DB model shape) and returns a
:class:`SessionTranscript` with cleaned messages and grouped turns.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from datetime import datetime

from .content import is_tool_result
from .content import redact_secrets as _redact_secrets
from .content import strip_noise as _strip_noise
from .tokens import count_tokens
from .tokens import truncate


@dataclass
class SessionMessage:
    """A single cleaned message from a session."""

    role: str  # user, assistant, tool
    content: str  # cleaned text
    timestamp: datetime
    tool_name: str | None = None


@dataclass
class Turn:
    """A group of consecutive same-role messages."""

    turn_index: int
    role: str
    combined_text: str
    timestamp: datetime  # timestamp of first message in turn
    message_count: int
    token_count: int


@dataclass
class SessionTranscript:
    """Structured, cleaned transcript of an agent session."""

    session_id: str
    messages: list[SessionMessage]
    turns: list[Turn]
    first_user_message: str | None
    last_assistant_message: str | None
    total_tokens: int
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Turn detection
# ---------------------------------------------------------------------------


def detect_turns(messages: list[SessionMessage]) -> list[Turn]:
    """Group consecutive same-role messages into turns.

    Each turn aggregates contiguous messages with the same ``role`` into a
    single combined text block, preserving order.
    """
    if not messages:
        return []

    turns: list[Turn] = []
    current_role = messages[0].role
    current_texts: list[str] = [messages[0].content]
    current_ts = messages[0].timestamp
    current_count = 1

    for msg in messages[1:]:
        if msg.role == current_role:
            current_texts.append(msg.content)
            current_count += 1
        else:
            combined = "\n".join(current_texts)
            turns.append(
                Turn(
                    turn_index=len(turns),
                    role=current_role,
                    combined_text=combined,
                    timestamp=current_ts,
                    message_count=current_count,
                    token_count=count_tokens(combined),
                )
            )
            current_role = msg.role
            current_texts = [msg.content]
            current_ts = msg.timestamp
            current_count = 1

    # Flush final turn
    combined = "\n".join(current_texts)
    turns.append(
        Turn(
            turn_index=len(turns),
            role=current_role,
            combined_text=combined,
            timestamp=current_ts,
            message_count=current_count,
            token_count=count_tokens(combined),
        )
    )

    return turns


# ---------------------------------------------------------------------------
# Transcript building
# ---------------------------------------------------------------------------


def _extract_content(
    event: dict,
    include_tool_calls: bool,
    tool_output_max_chars: int,
) -> str | None:
    """Extract displayable text from an event dict.

    Returns ``None`` if the event should be skipped.
    """
    # Tool-result events: skip unless caller wants them
    if is_tool_result(event) and not include_tool_calls:
        return None

    parts: list[str] = []

    content_text = event.get("content_text") or ""
    if content_text.strip():
        parts.append(content_text)

    # Append truncated tool output when present
    tool_output = event.get("tool_output_text") or ""
    if tool_output.strip() and include_tool_calls:
        truncated = tool_output[:tool_output_max_chars]
        if len(tool_output) > tool_output_max_chars:
            truncated += "..."
        parts.append(f"Tool output: {truncated}")

    combined = "\n".join(parts).strip()
    if not combined:
        return None

    return combined


def build_transcript(
    events: list[dict],
    *,
    include_tool_calls: bool = False,
    tool_output_max_chars: int = 500,
    strip_noise: bool = True,
    redact_secrets: bool = True,
    token_budget: int | None = None,
    token_encoding: str = "cl100k_base",
) -> SessionTranscript:
    """Build a clean, structured transcript from AgentEvent rows.

    Args:
        events: List of dicts matching the AgentEvent DB model shape.
            Required keys: ``role``, ``content_text``, ``timestamp``.
            Optional: ``tool_name``, ``tool_input_json``, ``tool_output_text``,
            ``session_id``.
        include_tool_calls: If False (default), skip tool-result events.
        tool_output_max_chars: Max chars to keep from ``tool_output_text``.
        strip_noise: Remove XML noise tags (system-reminder, etc.).
        redact_secrets: Replace API keys, JWTs, etc. with placeholders.
        token_budget: If set, truncate the whole transcript to this many tokens
            using "sandwich" strategy.
        token_encoding: tiktoken encoding for token counting/truncation.

    Returns:
        A :class:`SessionTranscript` with messages, turns, and metadata.
    """
    # Sort events by timestamp to guarantee chronological order
    events = sorted(events, key=lambda e: e.get("timestamp") or datetime.min)

    # Derive session_id from the first event (all events should share it)
    session_id = ""
    if events:
        session_id = str(events[0].get("session_id", ""))

    messages: list[SessionMessage] = []

    for event in events:
        content = _extract_content(event, include_tool_calls, tool_output_max_chars)
        if content is None:
            continue

        if strip_noise:
            content = _strip_noise(content)
        if redact_secrets:
            content = _redact_secrets(content)

        # Skip if cleaning left nothing
        if not content.strip():
            continue

        ts = event.get("timestamp")
        if ts is None:
            ts = datetime.min

        messages.append(
            SessionMessage(
                role=event.get("role", "unknown"),
                content=content,
                timestamp=ts,
                tool_name=event.get("tool_name"),
            )
        )

    # Extract goal/outcome signals from full session BEFORE budget truncation
    first_user = None
    last_assistant = None
    for msg in messages:
        if msg.role == "user" and first_user is None:
            first_user = msg.content
        if msg.role == "assistant":
            last_assistant = msg.content

    # Apply token budget via truncation of the full concatenated text
    if token_budget is not None and messages:
        messages = _apply_token_budget(messages, token_budget, token_encoding)

    # Build turns from the final message list
    turns = detect_turns(messages)

    total_tokens = sum(count_tokens(m.content, token_encoding) for m in messages)

    return SessionTranscript(
        session_id=session_id,
        messages=messages,
        turns=turns,
        first_user_message=first_user,
        last_assistant_message=last_assistant,
        total_tokens=total_tokens,
        metadata={},
    )


def _apply_token_budget(
    messages: list[SessionMessage],
    budget: int,
    encoding: str,
) -> list[SessionMessage]:
    """Trim messages to stay within a token budget.

    Strategy: walk from the end (most recent = most relevant) and accumulate
    messages until the budget is exhausted. If the first included message
    exceeds remaining budget, truncate it with "tail" strategy.
    """
    result: list[SessionMessage] = []
    remaining = budget

    for msg in reversed(messages):
        msg_tokens = count_tokens(msg.content, encoding)
        if msg_tokens <= remaining:
            result.append(msg)
            remaining -= msg_tokens
        elif remaining > 0:
            # Truncate this message to fit
            truncated_text, used, _ = truncate(msg.content, remaining, strategy="tail", encoding=encoding)
            result.append(
                SessionMessage(
                    role=msg.role,
                    content=truncated_text,
                    timestamp=msg.timestamp,
                    tool_name=msg.tool_name,
                )
            )
            remaining -= used
            break
        else:
            break

    # Reverse back to chronological order
    result.reverse()
    return result
