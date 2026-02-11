"""Content cleaning — noise stripping, secret redaction, tool-result detection.

Extracted from daily_digest.py (strip_noise) and shared/redaction.py (redact_secrets).
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Noise stripping — XML tags injected by Claude Code / system prompts
# ---------------------------------------------------------------------------

_NOISE_PATTERNS = [
    re.compile(r"<system-reminder>[\s\S]*?</system-reminder>", re.IGNORECASE),
    re.compile(r"<function_results>[\s\S]*?</function_results>", re.IGNORECASE),
    re.compile(r"<env>[\s\S]*?</env>", re.IGNORECASE),
    re.compile(r"<claude_background_info>[\s\S]*?</claude_background_info>", re.IGNORECASE),
    re.compile(r"<fast_mode_info>[\s\S]*?</fast_mode_info>", re.IGNORECASE),
]


def strip_noise(text: str) -> str:
    """Remove XML noise tags from content.

    Strips system-reminder, function_results, env, claude_background_info,
    fast_mode_info, and antml:* tags. Collapses excess blank lines.
    """
    if not text:
        return text
    result = text
    for pattern in _NOISE_PATTERNS:
        result = pattern.sub("", result)
    # Collapse 3+ consecutive newlines to 2
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


# ---------------------------------------------------------------------------
# Secret redaction — API keys, JWTs, AWS keys, etc.
# ---------------------------------------------------------------------------

_REDACTION_PATTERNS = [
    # Anthropic API keys (sk-ant-...) — must come before generic sk- pattern
    (re.compile(r"\bsk-ant-[\w-]{20,}\b"), "[ANTHROPIC_KEY]"),
    # OpenAI API keys (sk-..., sk-proj-..., etc.)
    (re.compile(r"\bsk-[\w-]{20,}\b"), "[OPENAI_KEY]"),
    # Generic API keys in JSON-style ("apiKey": "..." or "api_key": "...")
    (re.compile(r"""(?i)["']api[_-]?key["']\s*:\s*["'][a-zA-Z0-9_-]{20,}["']"""), '"apiKey": "[REDACTED]"'),
    # Generic API keys (api_key=... or apikey=...)
    (re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)['\"]?[a-zA-Z0-9_-]{20,}['\"]?"), r"\1[REDACTED]"),
    # Bearer tokens
    (re.compile(r"(?i)(bearer\s+)[a-zA-Z0-9_.-]{20,}"), r"\1[BEARER_TOKEN]"),
    # AWS access keys (AKIA...)
    (re.compile(r"\bAKIA[A-Z0-9]{16}\b"), "[AWS_ACCESS_KEY]"),
    # AWS temporary credentials (ASIA...)
    (re.compile(r"\bASIA[A-Z0-9]{16}\b"), "[AWS_TEMP_KEY]"),
    # AWS secret keys (40 char base64-ish)
    (
        re.compile(r"(?i)(aws[_-]?secret[_-]?access[_-]?key\s*[=:]\s*)['\"]?[a-zA-Z0-9/+=]{40}['\"]?"),
        r"\1[AWS_SECRET]",
    ),
    # GitHub fine-grained PATs (github_pat_...)
    (re.compile(r"\bgithub_pat_[a-zA-Z0-9_]{20,}\b"), "[GITHUB_PAT]"),
    # GitHub tokens (ghp_, gho_, ghu_, ghs_, ghr_)
    (re.compile(r"\bgh[pousr]_[a-zA-Z0-9]{36,}\b"), "[GITHUB_TOKEN]"),
    # Slack tokens (xoxb-, xoxp-, xoxa-, xoxr-)
    (re.compile(r"\bxox[bpar]-[a-zA-Z0-9-]+\b"), "[SLACK_TOKEN]"),
    # Private keys
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
        "[PRIVATE_KEY]",
    ),
    # JWT tokens (three base64 segments separated by dots)
    (
        re.compile(r"\beyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\b"),
        "[JWT_TOKEN]",
    ),
    # Generic secrets in env vars (SECRET=..., PASSWORD=..., TOKEN=...)
    # Negative lookahead (?!\[) prevents clobbering earlier redaction placeholders.
    (
        re.compile(r"(?i)(secret|password|token|credential)[_-]?\s*[=:]\s*['\"]?(?!\[)[^\s'\"]{8,}['\"]?"),
        r"\1=[REDACTED]",
    ),
]


def redact_secrets(text: str) -> str:
    """Redact sensitive patterns from text.

    Strips API keys, JWTs, AWS keys, bearer tokens, private keys, and
    generic secret/password/token env-var assignments.
    """
    if not text:
        return text
    result = text
    for pattern, replacement in _REDACTION_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


# ---------------------------------------------------------------------------
# Tool-result detection
# ---------------------------------------------------------------------------


def is_tool_result(event: dict) -> bool:
    """Check if an event dict represents a tool result.

    An event is a tool result only if its role is "tool". Assistant events
    that happen to carry ``tool_output_text`` (e.g. narration alongside a
    tool call) are NOT tool results — their ``content_text`` must still be
    included in transcripts.
    """
    return event.get("role") == "tool"
