"""Token counting and truncation with explicit encoding control.

Callers specify the encoding — never change the default silently. The default
``cl100k_base`` matches embedding models (text-embedding-3-small). Callers using
GPT-5 era models should pass ``o200k_base`` explicitly.
"""

from __future__ import annotations

from functools import lru_cache

import tiktoken


@lru_cache(maxsize=4)
def _get_encoding(encoding: str) -> tiktoken.Encoding:
    """Return a cached tiktoken Encoding object."""
    return tiktoken.get_encoding(encoding)


def count_tokens(text: str, encoding: str = "cl100k_base") -> int:
    """Count tokens in *text* using the specified tiktoken encoding.

    Args:
        text: Input text.
        encoding: tiktoken encoding name (e.g. ``cl100k_base``, ``o200k_base``).

    Returns:
        Token count (0 for empty/None input).
    """
    if not text:
        return 0
    enc = _get_encoding(encoding)
    return len(enc.encode(text))


def truncate(
    text: str,
    max_tokens: int,
    strategy: str = "tail",
    encoding: str = "cl100k_base",
) -> tuple[str, int, bool]:
    """Truncate *text* to fit within *max_tokens*.

    Strategies:
        - ``"head"``: Keep the beginning, cut the end.
        - ``"tail"``: Keep the end, cut the beginning.
        - ``"sandwich"``: Keep head + tail with a truncation marker in between
          (default 67% head, 33% tail).

    Args:
        text: Input text.
        max_tokens: Maximum allowed tokens in output.
        strategy: Truncation strategy (``"head"``, ``"tail"``, ``"sandwich"``).
        encoding: tiktoken encoding name.

    Returns:
        ``(truncated_text, token_count, was_truncated)``
    """
    if not text:
        return text or "", 0, False

    enc = _get_encoding(encoding)
    tokens = enc.encode(text)
    token_count = len(tokens)

    if token_count <= max_tokens:
        return text, token_count, False

    if max_tokens <= 0:
        return "", 0, True

    if strategy == "head":
        truncated = enc.decode(tokens[:max_tokens])
        return truncated, max_tokens, True

    if strategy == "tail":
        truncated = enc.decode(tokens[-max_tokens:])
        return truncated, max_tokens, True

    if strategy == "sandwich":
        return _truncate_sandwich(tokens, max_tokens, enc)

    raise ValueError(f"Unknown truncation strategy: {strategy!r}")


def _truncate_sandwich(
    tokens: list[int],
    max_tokens: int,
    enc: tiktoken.Encoding,
    head_ratio: float = 0.67,
) -> tuple[str, int, bool]:
    """Keep head + tail with a truncation marker in between."""
    head_tokens = max(0, int(max_tokens * head_ratio))
    tail_tokens = max_tokens - head_tokens

    # Iteratively shrink to make room for the marker text.
    marker = "\n\n[...truncated...]\n\n"
    if len(enc.encode(marker)) >= max_tokens:
        truncated = enc.decode(tokens[:max_tokens])
        return truncated, max_tokens, True

    while True:
        truncated_count = max(0, len(tokens) - head_tokens - tail_tokens)
        marker = f"\n\n[...{truncated_count:,} tokens truncated...]\n\n"
        marker_tokens = len(enc.encode(marker))
        total = head_tokens + tail_tokens + marker_tokens
        if total <= max_tokens:
            break

        overflow = total - max_tokens
        shrink_tail = min(tail_tokens, overflow)
        tail_tokens -= shrink_tail
        overflow -= shrink_tail
        if overflow > 0:
            head_tokens = max(0, head_tokens - overflow)

        if head_tokens == 0 and tail_tokens == 0:
            break

    head = enc.decode(tokens[:head_tokens]) if head_tokens > 0 else ""
    tail = enc.decode(tokens[-tail_tokens:]) if tail_tokens > 0 else ""
    truncated_count = max(0, len(tokens) - head_tokens - tail_tokens)
    marker = f"\n\n[...{truncated_count:,} tokens truncated...]\n\n"
    combined = f"{head}{marker}{tail}".strip()

    # Safety: guarantee we never exceed max_tokens.
    combined_tokens = enc.encode(combined)
    if len(combined_tokens) > max_tokens:
        combined = enc.decode(combined_tokens[:max_tokens])
        combined_tokens = enc.encode(combined)

    return combined, len(combined_tokens), True


def estimate_tokens_fast(text: str) -> int:
    """Conservative token estimate (~3 chars/token).

    Use when tiktoken is unavailable or for rough budget checks.
    """
    if not text:
        return 0
    return len(text) // 3
