"""Token counting and truncation utilities using tiktoken.

Ported from sauron-jobs/shared/tokens.py for use in digest jobs.
"""

from __future__ import annotations

import os

import tiktoken

# NOTE: As of tiktoken 0.12.0, `encoding_for_model("gpt-5.2")` is not mapped.
# In practice, gpt-5 / o-series models use `o200k_base`, so we default to that.
_DEFAULT_ENCODING = os.getenv("TIKTOKEN_ENCODING", "o200k_base")

try:
    _enc = tiktoken.get_encoding(_DEFAULT_ENCODING)
except Exception:
    # Fall back to the most common legacy encoding.
    _enc = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    """Count tokens in text using tiktoken."""
    if not text:
        return 0
    return len(_enc.encode(text))


def truncate_to_tokens(
    text: str,
    max_tokens: int,
    head_ratio: float = 0.67,
) -> tuple[str, bool]:
    """Truncate text to max_tokens, keeping head + tail.

    Args:
        text: Text to truncate
        max_tokens: Maximum tokens allowed
        head_ratio: Fraction of tokens to keep from head (rest from tail)

    Returns:
        Tuple of (truncated_text, was_truncated)
    """
    if not text:
        return text, False
    if max_tokens <= 0:
        return "", True

    tokens = _enc.encode(text)
    if len(tokens) <= max_tokens:
        return text, False

    # Calculate initial head/tail split. We'll adjust to account for the marker tokens.
    head_tokens = max(0, int(max_tokens * head_ratio))
    tail_tokens = max_tokens - head_tokens

    # If the marker itself doesn't fit, fall back to a hard cut with no marker.
    marker = "\n\n[...truncated...]\n\n"
    if len(_enc.encode(marker)) >= max_tokens:
        return _enc.decode(tokens[:max_tokens]), True

    # Iteratively shrink head/tail until (head + marker + tail) fits max_tokens.
    while True:
        truncated_count = max(0, len(tokens) - head_tokens - tail_tokens)
        marker = f"\n\n[...{truncated_count:,} tokens truncated...]\n\n"
        marker_tokens = len(_enc.encode(marker))
        total = head_tokens + tail_tokens + marker_tokens
        if total <= max_tokens:
            break

        overflow = total - max_tokens
        # Prefer shrinking tail (keeps more setup/context).
        shrink_tail = min(tail_tokens, overflow)
        tail_tokens -= shrink_tail
        overflow -= shrink_tail
        if overflow > 0:
            head_tokens = max(0, head_tokens - overflow)

        # If we run out of room, stop trying to include both sides.
        if head_tokens == 0 and tail_tokens == 0:
            break

    head = _enc.decode(tokens[:head_tokens]) if head_tokens > 0 else ""
    tail = _enc.decode(tokens[-tail_tokens:]) if tail_tokens > 0 else ""
    truncated_count = max(0, len(tokens) - head_tokens - tail_tokens)
    marker = f"\n\n[...{truncated_count:,} tokens truncated...]\n\n"
    combined = f"{head}{marker}{tail}".strip()

    # Safety: guarantee we never exceed max_tokens due to unexpected encoding behavior.
    combined_tokens = _enc.encode(combined)
    if len(combined_tokens) > max_tokens:
        combined = _enc.decode(combined_tokens[:max_tokens])

    return combined, True
