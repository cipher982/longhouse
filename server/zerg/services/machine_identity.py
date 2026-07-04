"""Machine identity helpers for managed provider sessions."""

from __future__ import annotations

import os
import platform
from pathlib import Path

from zerg.services.longhouse_paths import resolve_longhouse_home_from_provider_home
from zerg.services.shipper.token import load_machine_name


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
