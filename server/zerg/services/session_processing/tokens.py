"""Token counting and truncation with explicit encoding control.

Callers specify the encoding — never change the default silently. The default
``cl100k_base`` matches the default embedding model. Callers using
GPT-5 era models should pass ``o200k_base`` explicitly.

The two vendored tokenizer blobs live under ``zerg/vendor/tiktoken``. They are
read-only seed data for the runtime cache, not an in-repo writable cache.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from functools import lru_cache
from pathlib import Path

import tiktoken

_ENCODING_URLS = {
    "cl100k_base": "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken",
    "o200k_base": "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken",
}
_FALLBACK_ENCODINGS = {"o200k_base": "cl100k_base"}
_VENDORED_TIKTOKEN_DIR = Path(__file__).resolve().parents[2] / "vendor" / "tiktoken"


def _default_runtime_tiktoken_cache_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "longhouse" / "tiktoken"
    if os.name == "nt":
        local_app_data = os.getenv("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "Longhouse" / "tiktoken"
        return Path.home() / "AppData" / "Local" / "Longhouse" / "tiktoken"
    xdg_cache_home = os.getenv("XDG_CACHE_HOME")
    if xdg_cache_home:
        return Path(xdg_cache_home).expanduser() / "longhouse" / "tiktoken"
    return Path.home() / ".cache" / "longhouse" / "tiktoken"


def _seed_runtime_tiktoken_cache_from_vendor(*, vendored_dir: Path, runtime_cache_dir: Path) -> Path | None:
    vendored_files = [path for path in vendored_dir.iterdir() if path.is_file()]
    if not vendored_files:
        return None

    runtime_cache_dir.mkdir(parents=True, exist_ok=True)
    for source_path in vendored_files:
        target_path = runtime_cache_dir / source_path.name
        if target_path.is_file() and target_path.stat().st_size == source_path.stat().st_size:
            continue
        shutil.copy2(source_path, target_path)
    return runtime_cache_dir


def _vendored_dir_has_known_encodings(vendored_dir: Path) -> bool:
    for url in _ENCODING_URLS.values():
        if not (vendored_dir / hashlib.sha1(url.encode()).hexdigest()).is_file():
            return False
    return True


def _use_vendored_tiktoken_data_if_available() -> Path | None:
    """Seed a writable runtime cache from vendored blobs when no cache dir is configured.

    tiktoken lazily downloads its BPE files on first use. That makes unit tests
    and fresh runtimes depend on external network reachability. When this repo
    vendors the hashed tokenizer blobs, prefer them transparently without
    writing back into the repo or installed package directory.
    """
    if os.getenv("TIKTOKEN_CACHE_DIR") or os.getenv("DATA_GYM_CACHE_DIR"):
        return None
    if not _VENDORED_TIKTOKEN_DIR.is_dir():
        return None

    try:
        runtime_cache_dir = _seed_runtime_tiktoken_cache_from_vendor(
            vendored_dir=_VENDORED_TIKTOKEN_DIR,
            runtime_cache_dir=_default_runtime_tiktoken_cache_dir(),
        )
    except OSError:
        if _vendored_dir_has_known_encodings(_VENDORED_TIKTOKEN_DIR):
            os.environ["TIKTOKEN_CACHE_DIR"] = str(_VENDORED_TIKTOKEN_DIR)
            return _VENDORED_TIKTOKEN_DIR
        return None
    if runtime_cache_dir is None:
        if _vendored_dir_has_known_encodings(_VENDORED_TIKTOKEN_DIR):
            os.environ["TIKTOKEN_CACHE_DIR"] = str(_VENDORED_TIKTOKEN_DIR)
            return _VENDORED_TIKTOKEN_DIR
        return None

    os.environ["TIKTOKEN_CACHE_DIR"] = str(runtime_cache_dir)
    return runtime_cache_dir


_use_vendored_tiktoken_data_if_available()


def _cache_blob_path(encoding: str) -> Path | None:
    url = _ENCODING_URLS.get(encoding)
    cache_dir = os.getenv("TIKTOKEN_CACHE_DIR") or os.getenv("DATA_GYM_CACHE_DIR")
    if not url or not cache_dir:
        return None
    return Path(cache_dir) / hashlib.sha1(url.encode()).hexdigest()


@lru_cache(maxsize=4)
def _get_encoding(encoding: str) -> tiktoken.Encoding:
    """Return a cached tiktoken Encoding object."""
    fallback = _FALLBACK_ENCODINGS.get(encoding)
    if os.getenv("TESTING") and fallback:
        cache_blob = _cache_blob_path(encoding)
        if cache_blob is None or not cache_blob.is_file():
            return _get_encoding(fallback)
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
