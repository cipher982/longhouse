from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zerg.services.agents.kernel_capabilities import KernelSessionCapabilities

STEERABLE_RUNTIME_STATES = frozenset({"thinking", "running"})


@dataclass(frozen=True)
class SessionCapabilityDisplay:
    label: str
    detail: str
    tone: str


@dataclass(frozen=True)
class SessionInputPresentation:
    input_mode: str
    default_input_intent: str
    composer_enabled: bool
    composer_placeholder: str
    composer_disabled_reason: str | None


def _normalize_host_label(host_label: str | None) -> str | None:
    value = (host_label or "").strip()
    if not value:
        return None
    if value.lower().startswith("on "):
        value = value[3:].strip()
    return value or None


def build_session_capability_display(
    capability_flags: KernelSessionCapabilities,
    *,
    host_label: str | None = None,
    lifecycle: str | None = None,
    host_state: str | None = None,
) -> SessionCapabilityDisplay:
    if lifecycle == "closed":
        return SessionCapabilityDisplay(
            label="Closed",
            detail="This session has ended.",
            tone="neutral",
        )

    host_state_norm = (host_state or "").strip().lower()
    runtime_offline = host_state_norm in {"stale", "offline", "lost", "unknown"}

    home_label = "On this Mac" if (capability_flags.live_control_available or capability_flags.host_reattach_available) else None
    normalized_host = _normalize_host_label(host_label or home_label)
    if capability_flags.live_control_available and not runtime_offline:
        return SessionCapabilityDisplay(
            label=f"Live on {normalized_host}" if normalized_host else "Send",
            detail="Longhouse can send prompts into this live session.",
            tone="success",
        )
    if capability_flags.host_reattach_available or (capability_flags.live_control_available and runtime_offline):
        return SessionCapabilityDisplay(
            label="Control offline",
            detail="Reattach on the host to resume control.",
            tone="warning",
        )
    return SessionCapabilityDisplay(
        label="Read only",
        detail="This imported session is searchable, but Longhouse cannot steer it.",
        tone="neutral",
    )


def build_session_input_presentation(
    capability_flags: KernelSessionCapabilities,
    *,
    capability_display: SessionCapabilityDisplay,
    provider_label: str | None = None,
    lifecycle: str | None = None,
    is_executing: bool = False,
    host_state: str | None = None,
) -> SessionInputPresentation:
    provider = (provider_label or "").strip() or "session"
    provider_session = provider if provider.lower().endswith("session") else f"{provider} session"

    if lifecycle == "closed":
        return SessionInputPresentation(
            input_mode="read_only",
            default_input_intent="none",
            composer_enabled=False,
            composer_placeholder="Type a message...",
            composer_disabled_reason=capability_display.detail,
        )

    # Even if the kernel projection says live, runtime overlay can demote
    # the session to offline when heartbeats go stale or the host is gone.
    host_state_norm = (host_state or "").strip().lower()
    runtime_offline = host_state_norm in {"stale", "offline", "lost", "unknown"}

    live = bool(capability_flags.live_control_available) and not runtime_offline
    control_plane = (capability_flags.control_plane or "").strip()
    can_steer = live and provider.lower() == "codex" and control_plane == "codex_bridge"
    can_queue = live and bool(capability_flags.can_send_input)

    if live and capability_flags.can_send_input:
        if can_steer and is_executing:
            default_intent = "steer"
        elif can_queue and is_executing:
            default_intent = "queue"
        else:
            default_intent = "auto"
        return SessionInputPresentation(
            input_mode="live",
            default_input_intent=default_intent,
            composer_enabled=True,
            composer_placeholder=f"Send a message to the live {provider_session}...",
            composer_disabled_reason=None,
        )

    if capability_flags.host_reattach_available:
        return SessionInputPresentation(
            input_mode="offline",
            default_input_intent="none",
            composer_enabled=False,
            composer_placeholder="Type a message...",
            composer_disabled_reason=(f"Longhouse can see this {provider_session}, but cannot send prompts until the engine reconnects."),
        )

    return SessionInputPresentation(
        input_mode="read_only",
        default_input_intent="none",
        composer_enabled=False,
        composer_placeholder="Type a message...",
        composer_disabled_reason=capability_display.detail,
    )
