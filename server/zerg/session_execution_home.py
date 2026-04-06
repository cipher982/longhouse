"""Lightweight shared session execution-home contracts."""

from __future__ import annotations

from enum import Enum

_GENERIC_ENVIRONMENT_LABELS = {"production", "development", "dev", "test", "e2e"}


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
    def for_provider(
        provider: str,
        *,
        machine_name: str | None = None,
        native_claude_channels_available: bool | None = None,
    ) -> "ManagedSessionTransport":
        if provider == "codex":
            return ManagedSessionTransport.CODEX_APP_SERVER
        if provider == "claude" and str(machine_name or "").strip():
            if native_claude_channels_available is False:
                return ManagedSessionTransport.TMUX
            return ManagedSessionTransport.CLAUDE_CHANNEL_BRIDGE
        return ManagedSessionTransport.TMUX


def coerce_execution_home(value: str | None) -> SessionExecutionHome | None:
    if value is None or not str(value).strip():
        return None
    try:
        return SessionExecutionHome(str(value).strip())
    except ValueError:
        return None


def normalize_session_label(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def is_generic_environment_label(value: str | None) -> bool:
    """Return True when the label is a broad environment class, not a machine name."""
    if not value:
        return True

    normalized = value.strip().lower()
    return normalized in _GENERIC_ENVIRONMENT_LABELS or normalized.startswith("test:")


def execution_home_for_continuation_kind(kind: str | None) -> SessionExecutionHome | None:
    normalized = str(kind or "").strip().lower()
    if normalized == "cloud":
        return SessionExecutionHome.CLOUD_TAKEOVER
    if normalized == "runner":
        return SessionExecutionHome.MANAGED_HOSTED
    return None


def continuation_kind_for_execution_home(execution_home: SessionExecutionHome | None) -> str | None:
    if execution_home == SessionExecutionHome.CLOUD_TAKEOVER:
        return "cloud"
    if execution_home == SessionExecutionHome.MANAGED_HOSTED:
        return "runner"
    return None


def origin_label_for_execution_home(execution_home: SessionExecutionHome | None) -> str | None:
    if execution_home == SessionExecutionHome.CLOUD_TAKEOVER:
        return "Cloud"
    if execution_home == SessionExecutionHome.MANAGED_HOSTED:
        return "Hosted"
    return None


def infer_execution_home(
    *,
    execution_home: str | None,
    continuation_kind: str | None,
    origin_label: str | None,
    environment: str | None,
) -> SessionExecutionHome:
    explicit = coerce_execution_home(execution_home)
    if explicit is not None and explicit != SessionExecutionHome.LEGACY:
        return explicit

    from_kind = execution_home_for_continuation_kind(continuation_kind)
    if from_kind is not None:
        return from_kind

    normalized_origin = str(origin_label or "").strip().lower()
    normalized_environment = str(environment or "").strip().lower()
    if normalized_origin == "cloud" or normalized_environment == "cloud":
        return SessionExecutionHome.CLOUD_TAKEOVER
    if normalized_origin == "hosted" or normalized_environment == "hosted":
        return SessionExecutionHome.MANAGED_HOSTED

    return explicit or SessionExecutionHome.LEGACY


def infer_continuation_kind(
    *,
    continuation_kind: str | None,
    execution_home: str | None,
    origin_label: str | None,
    environment: str | None,
) -> str:
    explicit = normalize_session_label(continuation_kind)
    if explicit:
        return explicit

    from_home = continuation_kind_for_execution_home(
        infer_execution_home(
            execution_home=execution_home,
            continuation_kind=continuation_kind,
            origin_label=origin_label,
            environment=environment,
        )
    )
    if from_home:
        return from_home
    return "local"


def infer_origin_label(
    *,
    origin_label: str | None,
    environment: str | None,
    device_id: str | None,
    execution_home: str | None,
    continuation_kind: str | None,
) -> str:
    explicit = normalize_session_label(origin_label)
    if explicit:
        return explicit

    from_home = origin_label_for_execution_home(
        infer_execution_home(
            execution_home=execution_home,
            continuation_kind=continuation_kind,
            origin_label=origin_label,
            environment=environment,
        )
    )
    if from_home:
        return from_home

    normalized_environment = normalize_session_label(environment)
    if normalized_environment and not is_generic_environment_label(normalized_environment):
        return normalized_environment

    normalized_device_id = normalize_session_label(device_id)
    if normalized_device_id:
        return normalized_device_id.replace("shipper-", "")

    if normalized_environment:
        return normalized_environment
    return "Local"
