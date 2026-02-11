"""LLM summarization — quick, structured, and batch modes.

Provider-agnostic: takes an ``openai.AsyncOpenAI`` client (caller configures
base_url / api_key). Three entry points:

- :func:`quick_summary` — 2-4 sentence summary for briefings and digests
- :func:`structured_summary` — structured JSON for memory files
- :func:`batch_summarize` — batch with concurrency control
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

from openai import AsyncOpenAI

from .transcript import SessionTranscript

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


def _safe_parse_json(text: str | None) -> dict | None:
    """Parse JSON from LLM output, tolerating markdown fences."""
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
        # Try to extract JSON object
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                return None
        return None


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

    raw = response.choices[0].message.content or ""
    parsed = _safe_parse_json(raw)

    if parsed:
        return SessionSummary(
            session_id=transcript.session_id,
            title=parsed.get("title", "Untitled Session"),
            summary=parsed.get("summary", raw),
        )

    # Fallback: use raw text as summary
    return SessionSummary(
        session_id=transcript.session_id,
        title="Untitled Session",
        summary=raw.strip()[:500] if raw.strip() else "No summary generated.",
    )


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

    raw = response.choices[0].message.content or ""
    parsed = _safe_parse_json(raw)

    if parsed:
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
        max_concurrent: Max concurrent LLM calls (semaphore limit).

    Returns:
        List of :class:`SessionSummary` (one per transcript, failed ones skipped).
    """
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
