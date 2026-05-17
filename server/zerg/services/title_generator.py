"""Conversation title generation.

This module generates short, descriptive titles for conversations
using the configured summarization model.
"""

from __future__ import annotations

from typing import Any

from zerg.config import get_settings
from zerg.models_config import get_llm_client_for_use_case
from zerg.services.session_processing import safe_parse_json

# System prompt for title generation
TITLE_SYSTEM_PROMPT = (
    "Generate a short, helpful conversation title based on the transcript. "
    "Use Title Case, 3-8 words. "
    "No quotes, no trailing punctuation, no dates/times. "
    "Avoid generic titles like 'Conversation' or 'Chat'."
)


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
                {"role": "system", "content": TITLE_SYSTEM_PROMPT + " Return JSON only: {\"title\":\"...\"}"},
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
