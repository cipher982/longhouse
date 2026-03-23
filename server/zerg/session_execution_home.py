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
    """Execution transport for Longhouse-managed sessions."""

    TMUX = "tmux"
