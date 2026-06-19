"""Dispatch contract for runtime tool calls.

Pure functions that classify and normalize tool dispatch before execution.
Extracted from the runtime loop so the logic outlives any particular execution
harness.

- _classify_dispatch_lane: categorize a turn as direct / quick-tool / cli delegation
- _infer_requested_backend: detect explicit backend hints in user text
- _apply_dispatch_contract: inject inferred backend into spawn calls that omit one
"""

from __future__ import annotations

import logging
import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zerg.types.messages import BaseMessage

logger = logging.getLogger(__name__)

# Natural-language backend hints for spawn_commis dispatch normalization.
_BACKEND_HINT_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("codex", (r"\bcodex\b", r"\bopenai\b", r"\bgpt[-\s]?5\b", r"\bgpt[-\s]?4o\b")),
    ("antigravity", (r"\bantigravity\b", r"\bagy\b", r"\bgemini\b")),
    ("zai", (r"\bz\.?ai\b", r"\bglm(?:[-\s]?\d+(?:\.\d+)?)?\b")),
    ("bedrock", (r"\bbedrock\b",)),
    ("anthropic", (r"\banthropic\b",)),
)


def _latest_user_text(messages: list[BaseMessage]) -> str | None:
    """Return the latest user prompt text from the message list."""
    for msg in reversed(messages):
        if getattr(msg, "type", None) in ("human", "user"):
            content = getattr(msg, "content", None)
            if isinstance(content, str) and content.strip():
                return content
    return None


def _infer_requested_backend(messages: list[BaseMessage]) -> str | None:
    """Infer explicit commis backend preference from latest user text."""
    text = _latest_user_text(messages)
    if not text:
        return None
    lowered = text.lower()

    for backend, patterns in _BACKEND_HINT_PATTERNS:
        if any(re.search(pattern, lowered) for pattern in patterns):
            return backend
    return None


def _classify_dispatch_lane(tool_calls: list[dict] | None) -> str:
    """Classify current turn as direct, quick-tool, or cli delegation."""
    if not tool_calls:
        return "direct"
    if any(tc.get("name") == "spawn_commis" for tc in tool_calls):
        return "cli_delegation"
    return "quick_tool"


def _apply_dispatch_contract(tool_calls: list[dict] | None, messages: list[BaseMessage]) -> list[dict] | None:
    """Apply dispatch normalization rules before tool execution.

    Current rules:
    - If the user explicitly requested a backend and a spawn_commis call
      omits backend, inject the inferred backend to keep behavior deterministic.
    - Never override an explicit backend already provided by the model.
    - Skip dispatch contract when JOB_QUEUE_ENABLED is off (commis disabled).
    """
    if not tool_calls:
        return tool_calls

    if os.getenv("JOB_QUEUE_ENABLED", "").strip().lower() not in ("1", "true", "yes", "on"):
        return tool_calls

    requested_backend = _infer_requested_backend(messages)
    if not requested_backend:
        return tool_calls

    normalized_calls: list[dict] = []
    injected = 0

    for tool_call in tool_calls:
        if tool_call.get("name") != "spawn_commis":
            normalized_calls.append(tool_call)
            continue

        args = tool_call.get("args")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            normalized_calls.append(tool_call)
            continue
        if args.get("backend"):
            normalized_calls.append(tool_call)
            continue

        patched_args = dict(args)
        patched_args["backend"] = requested_backend
        patched_call = dict(tool_call)
        patched_call["args"] = patched_args
        normalized_calls.append(patched_call)
        injected += 1

    if injected > 0:
        logger.info(
            "[DispatchContract] injected backend=%s into %s spawn_commis call(s)",
            requested_backend,
            injected,
        )
    return normalized_calls
