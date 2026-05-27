"""Backend-owned send/composer affordance projection.

This module owns the product answer to: can the user type into this
session, what intent should the primary send action use, and why is it
disabled?
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Literal

if TYPE_CHECKING:
    from zerg.services.agents.kernel_capabilities import KernelSessionCapabilities

InputMode = Literal["live", "offline", "read_only"]
InputIntent = Literal["auto", "steer", "queue", "none"]
SendDisabledReason = Literal["session_closed", "control_offline", "input_not_supported", "read_only"]

OFFLINE_HOST_STATES = frozenset({"stale", "offline", "lost", "unknown"})
DEFAULT_PLACEHOLDER = "Type a message..."


@dataclass(frozen=True)
class SendAffordance:
    input_mode: InputMode
    default_input_intent: InputIntent
    composer_enabled: bool
    composer_placeholder: str
    composer_disabled_reason: str | None
    send_disabled_reason: SendDisabledReason | None


def _provider_session_label(provider_label: str | None) -> str:
    provider = (provider_label or "").strip() or "session"
    if provider.lower().endswith("session"):
        return provider
    return f"{provider} session"


def _disabled(
    *,
    input_mode: InputMode,
    reason_code: SendDisabledReason,
    reason_copy: str,
) -> SendAffordance:
    return SendAffordance(
        input_mode=input_mode,
        default_input_intent="none",
        composer_enabled=False,
        composer_placeholder=DEFAULT_PLACEHOLDER,
        composer_disabled_reason=reason_copy,
        send_disabled_reason=reason_code,
    )


def _control_offline_reason(provider_session: str) -> str:
    return f"Longhouse can see this {provider_session}, but cannot send prompts until the engine reconnects."


def project_send_affordance(
    capability_flags: KernelSessionCapabilities,
    *,
    read_only_reason: str,
    provider_label: str | None = None,
    lifecycle: str | None = None,
    is_executing: bool = False,
    host_state: str | None = None,
) -> SendAffordance:
    """Project the canonical typed-input affordance for one session.

    Keep this pure and deterministic. Runtime facts may demote a live kernel
    control bucket to offline, but clients should not repeat this reasoning.
    """

    provider_session = _provider_session_label(provider_label)

    if lifecycle == "closed":
        return _disabled(
            input_mode="read_only",
            reason_code="session_closed",
            reason_copy=read_only_reason,
        )

    host_state_norm = (host_state or "").strip().lower()
    runtime_offline = host_state_norm in OFFLINE_HOST_STATES
    live = bool(capability_flags.live_control_available) and not runtime_offline
    has_managed_control_path = bool(capability_flags.live_control_available or capability_flags.host_reattach_available)

    if live and bool(capability_flags.can_send_input):
        can_steer = bool(capability_flags.can_steer_active_turn)
        default_intent: InputIntent
        if can_steer and is_executing:
            default_intent = "steer"
        elif is_executing:
            default_intent = "queue"
        else:
            default_intent = "auto"
        return SendAffordance(
            input_mode="live",
            default_input_intent=default_intent,
            composer_enabled=True,
            composer_placeholder=f"Send a message to the live {provider_session}...",
            composer_disabled_reason=None,
            send_disabled_reason=None,
        )

    if (runtime_offline and has_managed_control_path) or (not live and bool(capability_flags.host_reattach_available)):
        return _disabled(
            input_mode="offline",
            reason_code="control_offline",
            reason_copy=_control_offline_reason(provider_session),
        )

    if live:
        return _disabled(
            input_mode="read_only",
            reason_code="input_not_supported",
            reason_copy=f"This live {provider_session} is connected, but this control path cannot accept typed input.",
        )

    return _disabled(
        input_mode="read_only",
        reason_code="read_only",
        reason_copy=read_only_reason,
    )
