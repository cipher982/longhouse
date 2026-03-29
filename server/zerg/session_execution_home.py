"""Lightweight shared session execution-home contracts."""

from __future__ import annotations

from enum import Enum


class SessionExecutionHome(str, Enum):
    """Where a coding session currently lives."""

    LEGACY = "legacy"
    MANAGED_LOCAL = "managed_local"
    MANAGED_HOSTED = "managed_hosted"
    CLOUD_TAKEOVER = "cloud_takeover"


class ManagedSessionTransport(str, Enum):
    """Execution transport for Longhouse-managed sessions.

    Transport is auto-determined by launch context — not user-selectable.
    """

    TMUX = "tmux"
    CLAUDE_CHANNEL_BRIDGE = "claude_channel_bridge"
    CODEX_APP_SERVER = "codex_app_server"

    @staticmethod
    def for_provider(provider: str, *, machine_name: str | None = None) -> "ManagedSessionTransport":
        if provider == "codex":
            return ManagedSessionTransport.CODEX_APP_SERVER
        if provider == "claude" and str(machine_name or "").strip():
            return ManagedSessionTransport.CLAUDE_CHANNEL_BRIDGE
        return ManagedSessionTransport.TMUX
