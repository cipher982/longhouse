"""Machine identity helpers for managed provider sessions."""

from __future__ import annotations

import os
import platform
import unicodedata
from pathlib import Path

from zerg.services.longhouse_paths import resolve_longhouse_home_from_provider_home
from zerg.services.shipper.token import load_machine_name


def canonical_machine_id(value: object, *, field: str = "machine_id", maximum_bytes: int = 255) -> str:
    """Validate the explicit machine identity shared by auth-disabled routes."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{field} must already be NFC-normalized")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"{field} exceeds {maximum_bytes} UTF-8 bytes")
    return value


def resolve_machine_id(
    principal: object | None,
    *,
    explicit_machine_id: object | None,
    fallback_machine_id: object,
) -> str:
    """Resolve token identity first, then an explicit dev identity, then fallback."""
    if principal is not None:
        # Device-token identity is already authenticated and retains the exact
        # established token semantics; canonical validation applies to the
        # explicit auth-disabled identity only.
        return getattr(principal, "device_id", None) or f"device:{getattr(principal, 'id', '')}"
    value = explicit_machine_id if explicit_machine_id is not None else fallback_machine_id
    return canonical_machine_id(value)


def get_claude_config_dir() -> Path:
    """Get the Claude config directory, respecting CLAUDE_CONFIG_DIR."""
    config_dir = os.getenv("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir)
    return Path.home() / ".claude"


def get_machine_name_label() -> str:
    """Return the configured Longhouse machine label, falling back to hostname."""
    machine_name = load_machine_name(resolve_longhouse_home_from_provider_home(get_claude_config_dir()))
    if machine_name:
        return machine_name
    return platform.node() or "unknown"
