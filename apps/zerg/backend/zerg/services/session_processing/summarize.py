"""LLM summarization — quick summary via OpenAI-compatible chat completions.

Primary entry point for downstream consumers:

- :func:`summarize_events` — events in, SessionSummary out. Handles
  transcript building, context-window-aware truncation, and LLM call.
  Used by: ingest background summarizer, backfill endpoint, daily digest.

Lower-level (used by summarize_events internally):

- :func:`quick_summary` — 2-4 sentence summary for briefings and digests
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass

from openai import AsyncOpenAI

from .transcript import SessionTranscript
from .transcript import build_transcript

logger = logging.getLogger(__name__)


@dataclass
class SessionSummary:
    """Result of summarizing a session transcript."""

    session_id: str
    title: str  # 3-8 words
    summary: str  # 2-4 sentences
    topic: str | None = None
    outcome: str | None = None
    bullets: list[str] | None = None  # for structured mode
    tags: list[str] | None = None


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_QUICK_SYSTEM = (
    "You summarize AI coding sessions. Return JSON with two keys:\n"
    '- "title": 3-8 word title (Title Case)\n'
    '- "summary": 2-4 sentence summary of what was worked on and accomplished\n'
    "Be specific about files, features, or bugs. JSON only, no markdown fences."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_user_prompt(transcript: SessionTranscript) -> str:
    """Build the user prompt from a transcript."""
    parts: list[str] = []

    # Context from metadata
    meta = transcript.metadata or {}
    context_items = []
    if meta.get("project"):
        context_items.append(f"Project: {meta['project']}")
    if meta.get("provider"):
        context_items.append(f"Provider: {meta['provider']}")
    if meta.get("git_branch"):
        context_items.append(f"Branch: {meta['git_branch']}")
    if context_items:
        parts.append("Context: " + ", ".join(context_items))

    # Goal / outcome signals
    if transcript.first_user_message:
        parts.append(f"User's initial request: {transcript.first_user_message[:500]}")
    if transcript.last_assistant_message:
        parts.append(f"Final assistant message: {transcript.last_assistant_message[:500]}")

    # Turn-level summary (compact, avoids blowing up token count)
    turn_lines: list[str] = []
    for turn in transcript.turns:
        preview = turn.combined_text[:300]
        if len(turn.combined_text) > 300:
            preview += "..."
        turn_lines.append(f"[{turn.role}] {preview}")
    if turn_lines:
        parts.append("Transcript turns:\n" + "\n".join(turn_lines))

    return "\n\n".join(parts)


def safe_parse_json(text: str | None) -> dict | None:
    """Parse JSON from LLM output, tolerating markdown fences and unquoted values."""
    if not text:
        return None
    # Strip markdown code fences if present
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # Remove opening fence (with optional language tag)
        first_nl = cleaned.index("\n") if "\n" in cleaned else len(cleaned)
        cleaned = cleaned[first_nl + 1 :]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Try to extract JSON object substring
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        fragment = cleaned[start : end + 1]
        try:
            return json.loads(fragment)
        except json.JSONDecodeError:
            pass

        # Handle unquoted string values (e.g. "title": Some Text Here)
        # Only match values that don't start with a quote (after optional whitespace)
        fixed = re.sub(
            r'("(?:title|summary|topic|outcome)")\s*:\s*' r'(?!\s*"|\s*\d|\s*true|\s*false|\s*null|\s*\[|\s*\{)(.+?)(?=,\s*"|\s*\})',
            lambda m: f'{m.group(1)}: "{m.group(2).strip()}"',
            fragment,
        )
        try:
            return json.loads(fixed)
        except json.JSONDecodeError:
            pass

    return None


def _parse_quick_summary_raw(raw: str, session_id: str) -> SessionSummary:
    """Parse quick-summary JSON output with robust fallback behavior."""
    parsed = safe_parse_json(raw)
    if isinstance(parsed, dict):
        title = parsed.get("title")
        summary = parsed.get("summary")
        title_str = title if isinstance(title, str) and title.strip() else "Untitled Session"
        # If summary key missing/empty, use title rather than storing raw JSON
        if isinstance(summary, str) and summary.strip():
            summary_str = summary.strip()
        else:
            summary_str = title_str
        return SessionSummary(
            session_id=session_id,
            title=title_str,
            summary=summary_str,
        )

    # Could not parse JSON at all — use raw text (not JSON) as summary
    stripped = raw.strip()
    # Guard: if it looks like unparsed JSON, don't store it verbatim
    if stripped.startswith("{"):
        return SessionSummary(
            session_id=session_id,
            title="Untitled Session",
            summary="No summary generated.",
        )

    return SessionSummary(
        session_id=session_id,
        title="Untitled Session",
        summary=stripped[:500] if stripped else "No summary generated.",
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def quick_summary(
    transcript: SessionTranscript,
    client: AsyncOpenAI,
    model: str = "glm-4.7",
) -> SessionSummary:
    """Generate a 2-4 sentence summary. For briefings and digests.

    Args:
        transcript: Cleaned session transcript.
        client: Async OpenAI-compatible client (caller configures base_url/api_key).
        model: Model identifier.

    Returns:
        A :class:`SessionSummary` with title and summary populated.
    """
    user_prompt = _build_user_prompt(transcript)

    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _QUICK_SYSTEM},
            {"role": "user", "content": user_prompt},
        ],
    )

    if not response.choices:
        return SessionSummary(
            session_id=transcript.session_id,
            title="Untitled Session",
            summary="No summary generated.",
        )

    raw = response.choices[0].message.content or ""
    return _parse_quick_summary_raw(raw, transcript.session_id)


# ---------------------------------------------------------------------------
# High-level entry point — events in, SessionSummary out
# ---------------------------------------------------------------------------

# Default context window budget (tokens). GLM-4.7-Flash = 200k, gpt-5-mini = 128k.
# Leave headroom for system prompt + output. Sessions under this pass through
# untouched; only the ~1.5% that exceed it get head+tail sandwich truncation.
DEFAULT_CONTEXT_BUDGET = 120_000


async def summarize_events(
    events: list[dict],
    *,
    client: AsyncOpenAI,
    model: str,
    metadata: dict | None = None,
    context_budget: int = DEFAULT_CONTEXT_BUDGET,
    timeout_seconds: float = 120,
) -> SessionSummary | None:
    """Summarize a session from raw event dicts — single entry point.

    Handles the full pipeline: events → transcript (with context-window-aware
    truncation for long sessions) → LLM call → SessionSummary.

    Args:
        events: List of dicts matching AgentEvent shape (role, content_text,
            timestamp, tool_name, tool_input_json, tool_output_text, session_id).
        client: Async OpenAI-compatible client (caller configures base_url/api_key).
        model: Model identifier string.
        metadata: Optional dict with project, provider, git_branch keys
            for prompt context.
        context_budget: Max tokens for the transcript. Sessions exceeding this
            get head+tail sandwich truncation. Default 120k (safe for most models).
        timeout_seconds: Max time for the LLM call.

    Returns:
        SessionSummary on success, None if transcript is empty.
    """
    transcript = build_transcript(
        events,
        include_tool_calls=False,
        token_budget=context_budget,
    )

    if metadata:
        transcript.metadata = metadata

    if not transcript.messages:
        return None

    return await asyncio.wait_for(
        quick_summary(transcript, client, model),
        timeout=timeout_seconds,
    )
