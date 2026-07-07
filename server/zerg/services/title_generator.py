"""Conversation title generation.

This module generates short, descriptive titles for conversations
using the configured summarization model.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from openai import AsyncOpenAI

from zerg.config import get_settings
from zerg.models_config import get_llm_client_for_use_case
from zerg.services.session_processing import safe_parse_json
from zerg.services.session_processing.content import redact_secrets
from zerg.services.session_processing.content import strip_noise

# System prompt for title generation
TITLE_SYSTEM_PROMPT = (
    "Generate a short, helpful conversation title based on the transcript. "
    "Use Title Case, 3-8 words. "
    "No quotes, no trailing punctuation, no dates/times. "
    "Avoid generic titles like 'Conversation' or 'Chat'."
)

INITIAL_SESSION_TITLE_SYSTEM_PROMPT = (
    "You name AI coding-assistant sessions from the user's first message. "
    'Return JSON with one key: "title".\n'
    "Rules:\n"
    "- 3-5 words, maximum 42 characters.\n"
    "- Name the user's goal or work area, not the fact that they asked for help.\n"
    "- Prefer the product feature, bug, file, or system being discussed.\n"
    "- If the message debates how title naming should work, title the session-title feature itself.\n"
    '- Ignore boilerplate like pasted status recaps, "done and shipped", commit SHAs, logs, and salutations.\n'
    "- If the message is a pasted recap, name the underlying feature, bug, or decision.\n"
    "- No quotes, emojis, markdown, or trailing punctuation inside the title.\n"
    "JSON only, no markdown fences."
)

_FENCE_MARKER_RE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*$", re.MULTILINE)
_IMAGE_MARKER_RE = re.compile(r"\[Image\s+#?\d+[^\]]*\]", re.IGNORECASE)


def _normalize_title_messages(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Normalize and clean messages for title generation.

    - Filters to user/assistant roles only
    - Truncates content to 800 chars per message
    - Limits to 12 messages max
    """
    if not isinstance(messages, list):
        return []

    cleaned = []
    for m in messages:
        role = m.get("role") if isinstance(m, dict) else None
        content = m.get("content", "") if isinstance(m, dict) else ""

        if role not in ("user", "assistant"):
            continue

        if not isinstance(content, str):
            content = str(content) if content else ""

        content = content.strip()
        if not content:
            continue

        # Hard cap per message to keep requests small
        cleaned.append({"role": role, "content": content[:800]})

        if len(cleaned) >= 12:
            break

    return cleaned


async def generate_conversation_title(messages: list[dict[str, Any]]) -> str | None:
    """Generate a short conversation title from messages.

    Args:
        messages: List of message dicts with 'role' and 'content' keys

    Returns:
        Generated title string, or None if generation fails

    Raises:
        Provider/API exceptions if the configured summarization client fails.
    """
    settings = get_settings()
    if settings.testing or settings.llm_disabled:
        return None

    # Normalize messages
    normalized = _normalize_title_messages(messages)
    if len(normalized) < 2:
        return None

    has_user = any(m["role"] == "user" for m in normalized)
    has_assistant = any(m["role"] == "assistant" for m in normalized)
    if not has_user or not has_assistant:
        return None

    # Build transcript
    transcript = "\n".join(f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content']}" for m in normalized)

    client, model, _provider = get_llm_client_for_use_case("summary_update")

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": TITLE_SYSTEM_PROMPT + ' Return JSON only: {"title":"..."}'},
                {"role": "user", "content": transcript},
            ],
        )
    finally:
        await client.close()

    output_text = response.choices[0].message.content if response.choices else None
    parsed = safe_parse_json(output_text)
    if parsed and isinstance(parsed.get("title"), str):
        return parsed["title"].strip() or None

    return None


def _build_initial_session_title_prompt(
    first_user_message: str,
    *,
    metadata: dict | None = None,
) -> str | None:
    message = redact_secrets(strip_noise(first_user_message or "")).strip()
    message = _FENCE_MARKER_RE.sub("", message)
    message = _IMAGE_MARKER_RE.sub("", message)
    message = re.sub(r"\n{3,}", "\n\n", message).strip()
    if not message:
        return None

    parts: list[str] = []
    if metadata:
        ctx = []
        if metadata.get("project"):
            ctx.append(f"Project: {metadata['project']}")
        if metadata.get("provider"):
            ctx.append(f"Provider: {metadata['provider']}")
        if metadata.get("git_branch"):
            ctx.append(f"Branch: {metadata['git_branch']}")
        if ctx:
            parts.append("Context: " + ", ".join(ctx))

    parts.append("First user message:\n" + message[:1200])
    return "\n\n".join(parts)


async def generate_initial_session_title(
    *,
    first_user_message: str,
    client: AsyncOpenAI,
    model: str,
    metadata: dict | None = None,
    timeout_seconds: float = 8,
) -> str | None:
    """Generate a stable, glanceable title from the first user message."""
    user_prompt = _build_initial_session_title_prompt(first_user_message, metadata=metadata)
    if not user_prompt:
        return None

    response = await asyncio.wait_for(
        client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": INITIAL_SESSION_TITLE_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        ),
        timeout=timeout_seconds,
    )
    if not response.choices:
        return None

    raw = response.choices[0].message.content or ""
    parsed = safe_parse_json(raw)
    if isinstance(parsed, dict):
        title = parsed.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()

    stripped = raw.strip().strip('"')
    return stripped or None
