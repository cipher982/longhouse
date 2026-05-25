from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from zerg.services.send_affordance import OFFLINE_HOST_STATES

if TYPE_CHECKING:
    from zerg.services.agents.kernel_capabilities import KernelSessionCapabilities


@dataclass(frozen=True)
class SessionCapabilityDisplay:
    label: str
    detail: str
    tone: str


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
    runtime_offline = host_state_norm in OFFLINE_HOST_STATES

    has_host_control = capability_flags.live_control_available or capability_flags.host_reattach_available
    home_label = "On this Mac" if has_host_control else None
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
