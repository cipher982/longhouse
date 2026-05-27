"""Lightweight shared session execution-home contracts."""

from __future__ import annotations

from enum import Enum

_GENERIC_ENVIRONMENT_LABELS = {"production", "development", "dev", "test", "e2e"}


class SessionExecutionHome(str, Enum):
    """Where a coding session currently lives."""

    UNMANAGED_LOCAL = "unmanaged_local"
    MANAGED_LOCAL = "managed_local"
    MANAGED_HOSTED = "managed_hosted"
    CLOUD_TAKEOVER = "cloud_takeover"  # frozen — no new sessions use this


class ManagedSessionTransport(str, Enum):
    """Execution transport for Longhouse-managed sessions.

    Transport is auto-determined by launch context — not user-selectable.
    """

    CLAUDE_CHANNEL_BRIDGE = "claude_channel_bridge"
    CODEX_APP_SERVER = "codex_app_server"
    OPENCODE_PROCESS = "opencode_process"
    ANTIGRAVITY_PROCESS = "antigravity_process"

    @staticmethod
    def for_provider(
        provider: str,
        *,
        machine_name: str | None = None,
        native_claude_channels_available: bool | None = None,
    ) -> "ManagedSessionTransport":
        del machine_name, native_claude_channels_available
        from zerg.services.managed_provider_contracts import managed_transport_for_provider

        return managed_transport_for_provider(provider)


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


def _execution_home_from_labels(
    *,
    origin_label: str | None,
    environment: str | None,
) -> SessionExecutionHome | None:
    for value in (origin_label, environment):
        normalized = str(value or "").strip().lower()
        if normalized == "cloud":
            return SessionExecutionHome.CLOUD_TAKEOVER
        if normalized == "hosted":
            return SessionExecutionHome.MANAGED_HOSTED
    return None


def _origin_label_from_context(
    *,
    environment: str | None,
    device_id: str | None,
) -> str | None:
    normalized_environment = normalize_session_label(environment)
    if normalized_environment and not is_generic_environment_label(normalized_environment):
        return normalized_environment

    normalized_device_id = normalize_session_label(device_id)
    if normalized_device_id:
        return normalized_device_id.replace("shipper-", "")

    return normalized_environment


def infer_execution_home(
    *,
    execution_home: str | None,
    continuation_kind: str | None,
    origin_label: str | None,
    environment: str | None,
) -> SessionExecutionHome:
    explicit = coerce_execution_home(execution_home)
    if explicit is not None and explicit != SessionExecutionHome.UNMANAGED_LOCAL:
        return explicit

    from_kind = execution_home_for_continuation_kind(continuation_kind)
    if from_kind is not None:
        return from_kind

    from_labels = _execution_home_from_labels(origin_label=origin_label, environment=environment)
    if from_labels is not None:
        return from_labels

    return explicit or SessionExecutionHome.UNMANAGED_LOCAL


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

    from_context = _origin_label_from_context(environment=environment, device_id=device_id)
    if from_context:
        return from_context
    return "Local"
