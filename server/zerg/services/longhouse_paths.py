"""Canonical path helpers for Longhouse-owned local state."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

_PROVIDER_HOME_NAMES = frozenset({".claude", ".codex", ".gemini"})
LEGACY_CLAUDE_MANAGED_LOCAL_PROVIDERS = {
    "codex_bridge": "codex-bridge",
    "opencode": "opencode",
    "antigravity": "antigravity",
}
LonghouseHomeMode = Literal["stable", "scratch"]


def canonical_longhouse_home() -> Path:
    """Return the default durable Longhouse home for this user."""
    return (Path.home() / ".longhouse").expanduser().resolve(strict=False)


def resolve_longhouse_home(base_dir: Path | None = None) -> Path:
    """Return the Longhouse home directory.

    Resolution order:
    1. Explicit ``base_dir`` if provided
    2. ``LONGHOUSE_HOME`` environment variable
    3. ``CLAUDE_CONFIG_DIR`` mapped from provider home to sibling ``.longhouse``
    4. ``~/.longhouse``

    Explicit paths are treated as Longhouse homes so tests and internal
    call sites can target a temporary state root directly. Provider-owned
    paths should instead go through ``resolve_longhouse_home_from_provider_home``.
    """

    if base_dir is not None:
        return _normalize_longhouse_home(Path(base_dir).expanduser())

    configured_home = os.getenv("LONGHOUSE_HOME")
    if configured_home:
        return Path(configured_home).expanduser()

    provider_home = os.getenv("CLAUDE_CONFIG_DIR")
    if provider_home:
        return resolve_longhouse_home_from_provider_home(provider_home)

    return Path.home() / ".longhouse"


def classify_longhouse_home(base_dir: Path | None = None) -> LonghouseHomeMode:
    """Classify a Longhouse home as the stable default or a scratch override."""
    resolved = resolve_longhouse_home(base_dir).expanduser().resolve(strict=False)
    return "stable" if resolved == canonical_longhouse_home() else "scratch"


def is_stable_longhouse_home(base_dir: Path | None = None) -> bool:
    return classify_longhouse_home(base_dir) == "stable"


def resolve_longhouse_home_from_provider_home(provider_home: str | Path | None) -> Path:
    """Map a provider-owned config root to the sibling Longhouse home.

    ``provider_home`` is treated as foreign state even when it is a custom path
    such as ``/tmp/claude-config``. Longhouse should never store its own agent
    state inside that tree.
    """

    if provider_home is None:
        return resolve_longhouse_home()

    path = Path(provider_home).expanduser()
    if path.name == ".longhouse":
        return path

    parent = path.parent
    if parent == path:
        return path / ".longhouse"
    return parent / ".longhouse"


def get_machine_state_dir(base_dir: Path | None = None) -> Path:
    """Return the directory for machine-owned target/auth state."""
    return resolve_longhouse_home(base_dir) / "machine"


def get_machine_token_path(base_dir: Path | None = None) -> Path:
    """Return the machine auth token path."""
    return get_machine_state_dir(base_dir) / "device-token"


def get_machine_state_path(base_dir: Path | None = None) -> Path:
    """Return the canonical machine state file path."""
    return get_machine_state_dir(base_dir) / "state.json"


def get_machine_state_journal_path(base_dir: Path | None = None) -> Path:
    """Return the append-only machine state journal path."""
    return get_machine_state_dir(base_dir) / "state-journal.jsonl"


def get_agent_state_dir(base_dir: Path | None = None) -> Path:
    """Return the directory for Longhouse-owned machine agent state."""
    return resolve_longhouse_home(base_dir) / "agent"


def get_agent_outbox_dir(base_dir: Path | None = None) -> Path:
    """Return the agent outbox directory."""
    return get_agent_state_dir(base_dir) / "outbox"


def get_agent_runtime_events_outbox_dir(base_dir: Path | None = None) -> Path:
    """Return the durable runtime-event outbox directory."""
    return get_agent_state_dir(base_dir) / "runtime-events-outbox"


def get_agent_status_path(base_dir: Path | None = None) -> Path:
    """Return the local engine status file path."""
    return get_agent_state_dir(base_dir) / "engine-status.json"


def get_agent_db_path(base_dir: Path | None = None) -> Path:
    """Return the local engine spool/state database path."""
    return get_agent_state_dir(base_dir) / "longhouse-shipper.db"


def get_agent_log_dir(base_dir: Path | None = None) -> Path:
    """Return the local engine log directory."""
    return get_agent_state_dir(base_dir) / "logs"


def get_runtime_config_path(base_dir: Path | None = None) -> Path:
    """Return the Longhouse runtime config path."""
    return resolve_longhouse_home(base_dir) / "config.toml"


def get_managed_local_dir(provider: str, *, base_dir: Path | None = None) -> Path:
    """Return the per-provider managed-local state directory.

    Longhouse-owned managed-session state for non-Claude providers (codex
    bridge state, opencode runtime plugin + bridge state, antigravity
    runtime plugin staging) lives under ``~/.longhouse/managed-local/``.

    Earlier code wrote this state into ``~/.claude/managed-local/<provider>/``
    by analogy with the Claude bridge, but that tree is provider-owned
    foreign state per ``_PROVIDER_HOME_NAMES``. Longhouse should never
    park its own state under another provider's home dir.
    """

    name = (provider or "").strip()
    if not name:
        raise ValueError("provider must not be empty")
    return resolve_longhouse_home(base_dir) / "managed-local" / name


def get_legacy_claude_managed_local_dir(provider: str, *, base_dir: Path | None = None) -> Path:
    """Return the old provider-owned managed-local directory for migration checks.

    Before Longhouse-owned managed-session state moved to the Longhouse home,
    Codex/OpenCode/Antigravity state was written under Claude's config root.
    This path is legacy evidence only; active liveness must use
    ``get_managed_local_dir``.
    """

    name = (provider or "").strip()
    if not name:
        raise ValueError("provider must not be empty")

    configured_claude_home = os.getenv("CLAUDE_CONFIG_DIR")
    if configured_claude_home:
        claude_home = Path(configured_claude_home).expanduser()
    elif base_dir is not None:
        longhouse_home = resolve_longhouse_home(base_dir)
        if longhouse_home.name == ".longhouse":
            claude_home = longhouse_home.parent / ".claude"
        else:
            claude_home = Path.home() / ".claude"
    else:
        claude_home = Path.home() / ".claude"

    return claude_home / "managed-local" / name


def _normalize_longhouse_home(path: Path) -> Path:
    if path.name in _PROVIDER_HOME_NAMES:
        parent = path.parent
        if parent != path:
            return parent / ".longhouse"
    return path
