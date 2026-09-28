"""Session title generation from the first user message."""

from __future__ import annotations

import asyncio
import re

from openai import AsyncOpenAI

from zerg.services.session_processing import safe_parse_json
from zerg.services.session_processing.summarize import ai_titles_and_summaries_enabled
from zerg.services.transcript_content import redact_secrets
from zerg.services.transcript_content import strip_noise

# The reply is constrained to this shape by the provider, not parsed out of
# prose. Hosted session e425ca05 froze "Great to hear you simplified your..."
# as its AI title when the model answered the user instead of naming them.
INITIAL_SESSION_TITLE_SCHEMA = {
    "name": "session_title",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {"title": {"type": "string"}},
        "required": ["title"],
        "additionalProperties": False,
    },
}

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
    """Generate a stable, glanceable title from the first user message.

    Returns ``None`` when the operator has not opted in to sending transcript
    text to a model provider. The gate is here rather than at the call sites
    because this is the chokepoint every title path funnels through -- the
    legacy ingest trigger and the storage-v2 worker both arrive at this
    function, and gating callers one at a time is how the hosted path stayed
    live after the first attempt.
    """
    if not ai_titles_and_summaries_enabled():
        return None

    user_prompt = _build_initial_session_title_prompt(first_user_message, metadata=metadata)
    if not user_prompt:
        return None

    from zerg.models_config import llm_request_policy_kwargs

    response = await asyncio.wait_for(
        client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": INITIAL_SESSION_TITLE_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            **llm_request_policy_kwargs(client, json_schema=INITIAL_SESSION_TITLE_SCHEMA, reasoning=False),
        ),
        timeout=timeout_seconds,
    )
    if not response.choices:
        return None

    message = response.choices[0].message
    # A structured tool call is never a title, regardless of what (if
    # anything) also landed in ``content``.
    if getattr(message, "tool_calls", None):
        return None

    # Only a parsed {"title": ...} is a title; anything else returns None so
    # the caller records empty_model_response and retries instead of
    # freezing the write-once anchor on it.
    parsed = safe_parse_json(message.content)
    if isinstance(parsed, dict):
        title = parsed.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()
    return None
