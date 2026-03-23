"""Secret redaction utilities for safe logging and digest generation.

Redacts API keys, tokens, and other sensitive patterns from text.
"""

from __future__ import annotations

import re

# Patterns for sensitive data that should be redacted
_REDACTION_PATTERNS = [
    # OpenAI API keys (sk-...)
    (re.compile(r"\bsk-[a-zA-Z0-9]{20,}\b"), "[OPENAI_KEY]"),
    # Anthropic API keys (sk-ant-...)
    (re.compile(r"\bsk-ant-[a-zA-Z0-9-]{20,}\b"), "[ANTHROPIC_KEY]"),
    # Generic API keys (often formatted as api_key=... or apikey=...)
    (re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)['\"]?[a-zA-Z0-9_-]{20,}['\"]?"), r"\1[REDACTED]"),
    # Bearer tokens
    (re.compile(r"(?i)(bearer\s+)[a-zA-Z0-9_.-]{20,}"), r"\1[BEARER_TOKEN]"),
    # AWS access keys (AKIA...)
    (re.compile(r"\bAKIA[A-Z0-9]{16}\b"), "[AWS_ACCESS_KEY]"),
    # AWS secret keys (40 char base64-ish)
    (re.compile(r"(?i)(aws[_-]?secret[_-]?access[_-]?key\s*[=:]\s*)['\"]?[a-zA-Z0-9/+=]{40}['\"]?"), r"\1[AWS_SECRET]"),
    # GitHub tokens (ghp_, gho_, ghu_, ghs_, ghr_)
    (re.compile(r"\bgh[pousr]_[a-zA-Z0-9]{36,}\b"), "[GITHUB_TOKEN]"),
    # Slack tokens (xoxb-, xoxp-, xoxa-, xoxr-)
    (re.compile(r"\bxox[bpar]-[a-zA-Z0-9-]+\b"), "[SLACK_TOKEN]"),
    # Private keys (-----BEGIN ... PRIVATE KEY-----)
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), "[PRIVATE_KEY]"),
    # JWT tokens (three base64 segments separated by dots)
    (re.compile(r"\beyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\b"), "[JWT_TOKEN]"),
    # Generic secrets in env vars (SECRET=..., PASSWORD=..., TOKEN=...)
    (re.compile(r"(?i)(secret|password|token|credential)[_-]?\s*[=:]\s*['\"]?[^\s'\"]{8,}['\"]?"), r"\1=[REDACTED]"),
]


def redact_text(text: str) -> str:
    """Redact sensitive patterns from text.

    Args:
        text: Input text potentially containing secrets

    Returns:
        Text with sensitive values replaced by placeholders
    """
    if not text:
        return text

    result = text
    for pattern, replacement in _REDACTION_PATTERNS:
        result = pattern.sub(replacement, result)

    return result
