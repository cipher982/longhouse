"""LLM summarization — quick, structured, and batch modes.

Provider-aware: supports OpenAI-compatible chat completions and
Anthropic-compatible messages APIs.

Primary entry point for downstream consumers:

- :func:`summarize_events` — events in, SessionSummary out. Handles
  transcript building, context-window-aware truncation, and provider dispatch.
  Used by: ingest background summarizer, backfill endpoint, daily digest.

Lower-level (used by summarize_events internally):

- :func:`quick_summary` — 2-4 sentence summary for briefings and digests
- :func:`structured_summary` — structured JSON for memory files
- :func:`batch_summarize` — batch with concurrency control
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

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

_STRUCTURED_SYSTEM = (
    "You summarize AI coding sessions into structured JSON. Return JSON with keys:\n"
    '- "title": 3-8 word title (Title Case)\n'
    '- "topic": short topic phrase\n'
    '- "outcome": one-sentence outcome\n'
    '- "summary": 2-4 sentence summary\n'
    '- "bullets": 3-6 short bullet points of key actions\n'
    '- "tags": 3-6 lowercase tags (no spaces, use hyphens)\n'
    "Be specific and factual. JSON only, no markdown fences."
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


async def quick_summary_anthropic(
    transcript: SessionTranscript,
    client: Any,
    model: str = "glm-4.7",
) -> SessionSummary:
    """Generate a quick summary using an Anthropic-compatible client."""
    user_prompt = _build_user_prompt(transcript)

    response = await client.messages.create(
        model=model,
        max_tokens=512,
        system=_QUICK_SYSTEM,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw = response.content[0].text if response.content else ""
    return _parse_quick_summary_raw(raw, transcript.session_id)


async def quick_summary_for_provider(
    transcript: SessionTranscript,
    client: Any,
    model: str,
    provider: Any,
) -> SessionSummary:
    """Dispatch quick-summary call based on provider value."""
    provider_value = str(getattr(provider, "value", provider or "")).lower()
    if provider_value == "anthropic":
        return await quick_summary_anthropic(transcript, client, model)
    return await quick_summary(transcript, client, model)


async def structured_summary(
    transcript: SessionTranscript,
    client: AsyncOpenAI,
    model: str = "gpt-5-mini",
) -> SessionSummary:
    """Generate a structured JSON summary. For memory files.

    Args:
        transcript: Cleaned session transcript.
        client: Async OpenAI-compatible client (caller configures base_url/api_key).
        model: Model identifier.

    Returns:
        A :class:`SessionSummary` with all fields populated.
    """
    user_prompt = _build_user_prompt(transcript)

    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _STRUCTURED_SYSTEM},
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
    parsed = safe_parse_json(raw)

    if isinstance(parsed, dict):
        bullets = parsed.get("bullets")
        if isinstance(bullets, list):
            bullets = [str(b) for b in bullets]
        else:
            bullets = None

        tags = parsed.get("tags")
        if isinstance(tags, list):
            tags = [str(t).lower().replace(" ", "-") for t in tags]
        else:
            tags = None

        return SessionSummary(
            session_id=transcript.session_id,
            title=parsed.get("title", "Untitled Session"),
            summary=parsed.get("summary", raw),
            topic=parsed.get("topic"),
            outcome=parsed.get("outcome"),
            bullets=bullets,
            tags=tags,
        )

    # Fallback
    return SessionSummary(
        session_id=transcript.session_id,
        title="Untitled Session",
        summary=raw.strip()[:500] if raw.strip() else "No summary generated.",
    )


async def batch_summarize(
    transcripts: list[SessionTranscript],
    client: AsyncOpenAI,
    model: str = "glm-4.7",
    max_concurrent: int = 3,
) -> list[SessionSummary]:
    """Batch-summarize multiple transcripts with concurrency control.

    Args:
        transcripts: List of session transcripts.
        client: Async OpenAI-compatible client.
        model: Model identifier.
        max_concurrent: Max concurrent LLM calls (semaphore limit). Must be >= 1.

    Returns:
        List of :class:`SessionSummary` (one per transcript, failed ones skipped).

    Raises:
        ValueError: If max_concurrent < 1 (would deadlock the semaphore).
    """
    if max_concurrent < 1:
        raise ValueError("max_concurrent must be >= 1")
    semaphore = asyncio.Semaphore(max_concurrent)
    results: list[SessionSummary] = []

    async def _summarize_one(t: SessionTranscript) -> SessionSummary | None:
        async with semaphore:
            try:
                return await quick_summary(t, client, model)
            except Exception:
                logger.exception("Failed to summarize session %s", t.session_id)
                return None

    tasks = [_summarize_one(t) for t in transcripts]
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)

    for outcome in outcomes:
        if isinstance(outcome, Exception):
            logger.error("Batch summarize error: %s", outcome)
            continue
        if outcome is not None:
            results.append(outcome)

    return results


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
    client: Any,
    model: str,
    provider: Any,
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
        client: Async LLM client (OpenAI or Anthropic SDK).
        model: Model identifier string.
        provider: Provider enum or string ("openai", "anthropic", etc.).
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
        quick_summary_for_provider(transcript, client, model, provider),
        timeout=timeout_seconds,
    )
