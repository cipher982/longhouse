"""Conversation title generation using OpenAI.

This module generates short, descriptive titles for conversations
using OpenAI's responses API with structured output.

Replaces the jarvis-server proxy layer.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from zerg.config import get_settings

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


def _extract_output_text(response_json: dict[str, Any] | None) -> str | None:
    """Extract text output from OpenAI responses API format."""
    if not response_json or not isinstance(response_json, dict):
        return None

    # Direct output_text field
    if isinstance(response_json.get("output_text"), str):
        text = response_json["output_text"].strip()
        if text:
            return text

    # Extract from output array
    output = response_json.get("output")
    if not isinstance(output, list):
        return None

    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()

    return None


def _safe_parse_json_object(text: str | None) -> dict[str, Any] | None:
    """Safely parse JSON, attempting recovery if malformed."""
    if not isinstance(text, str):
        return None

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to recover a JSON object substring
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
        return None


async def generate_conversation_title(messages: list[dict[str, Any]]) -> str | None:
    """Generate a short conversation title from messages.

    Args:
        messages: List of message dicts with 'role' and 'content' keys

    Returns:
        Generated title string, or None if generation fails

    Raises:
        httpx.TimeoutException: If OpenAI API times out
        httpx.HTTPStatusError: If OpenAI API returns an error status
    """
    settings = get_settings()

    # Normalize messages
    normalized = _normalize_title_messages(messages)
    if len(normalized) < 2:
        return None

    has_user = any(m["role"] == "user" for m in normalized)
    has_assistant = any(m["role"] == "assistant" for m in normalized)
    if not has_user or not has_assistant:
        return None

    model = os.getenv("JARVIS_TITLE_MODEL", "gpt-5-mini")
    reasoning_effort = os.getenv("JARVIS_TITLE_REASONING_EFFORT", "minimal")

    # Build transcript
    transcript = "\n".join(f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content']}" for m in normalized)

    payload = {
        "model": model,
        "reasoning": {"effort": reasoning_effort},
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": TITLE_SYSTEM_PROMPT}]},
            {"role": "user", "content": [{"type": "input_text", "text": transcript}]},
        ],
        "max_output_tokens": 200,
        "text": {
            "verbosity": "low",
            "format": {
                "type": "json_schema",
                "name": "conversation_title",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"title": {"type": "string"}},
                    "required": ["title"],
                },
            },
        },
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {settings.openai_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()
        result = response.json()

        output_text = _extract_output_text(result)

        # Retry with higher token cap if output was truncated
        if not output_text and result.get("status") == "incomplete":
            incomplete_details = result.get("incomplete_details", {})
            if incomplete_details.get("reason") == "max_output_tokens":
                payload["max_output_tokens"] = 400
                response = await client.post(
                    "https://api.openai.com/v1/responses",
                    headers={
                        "Authorization": f"Bearer {settings.openai_api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                if response.is_success:
                    output_text = _extract_output_text(response.json())

        # Parse the JSON output
        parsed = _safe_parse_json_object(output_text)
        if parsed and isinstance(parsed.get("title"), str):
            return parsed["title"].strip() or None

        return None
