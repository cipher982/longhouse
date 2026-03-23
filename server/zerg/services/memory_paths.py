"""Path normalization helpers for Memory Files."""

from __future__ import annotations

from pathlib import PurePosixPath

MAX_MEMORY_PATH_LENGTH = 512


def normalize_memory_path(path: str) -> str:
    """Normalize and validate a memory file path."""
    return _normalize(path, allow_prefix=False)


def normalize_memory_prefix(prefix: str) -> str:
    """Normalize and validate a memory file prefix."""
    return _normalize(prefix, allow_prefix=True)


def _normalize(raw_value: str, *, allow_prefix: bool) -> str:
    raw = (raw_value or "").strip()
    if not raw:
        raise ValueError("Path cannot be empty.")

    normalized = raw.replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")

    if "\x00" in normalized:
        raise ValueError("Path cannot contain null bytes.")
    if normalized.startswith("/"):
        raise ValueError("Memory paths must be relative.")

    if allow_prefix:
        normalized = normalized.rstrip("/")
        if not normalized:
            raise ValueError("Prefix cannot be empty.")
    elif normalized.endswith("/"):
        raise ValueError("Memory file paths cannot end with '/'.")

    raw_parts = normalized.split("/")
    clean_parts: list[str] = []
    for part in raw_parts:
        if part in {"", ".", ".."}:
            raise ValueError("Memory paths cannot contain empty, '.', or '..' segments.")
        clean_parts.append(part)

    pure_path = PurePosixPath(*clean_parts)
    parts = pure_path.parts
    if not parts:
        raise ValueError("Path cannot be empty.")

    result = "/".join(clean_parts)
    if len(result) > MAX_MEMORY_PATH_LENGTH:
        raise ValueError(f"Path is too long (max {MAX_MEMORY_PATH_LENGTH} characters).")

    return result
