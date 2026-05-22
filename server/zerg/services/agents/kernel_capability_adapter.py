"""Translation from the kernel projection to the legacy capability dataclass.

Phase 4 (B) of the session identity kernel. The legacy
``SessionCapabilityFlags`` dataclass — and its handful of callers in
session views, chat, current-control, and APNS — predate the kernel. While
we migrate readers, we keep the dataclass alive but redirect its source of
truth: the adapter delegates to ``project_session_capabilities`` and never
reads ``session.execution_home`` or ``session.managed_transport``.

This is a translation, not a fallback. There is one source of truth — the
kernel rows. ``execution_home`` and ``managed_transport`` are no longer
authoritative capability inputs. The adapter exists only so call sites can
keep building their existing payload shape during the migration; in
Phase 5 the dataclass and its remaining call sites disappear.

See docs/specs/session-identity-kernel.md (Phase 4 sub-commit B, step 2).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.services.agents.kernel_capabilities import KernelSessionCapabilities
from zerg.services.agents.kernel_capabilities import project_session_capabilities
from zerg.services.session_capabilities import SessionCapabilityFlags
from zerg.session_execution_home import ManagedSessionTransport
from zerg.session_execution_home import SessionExecutionHome


_CONTROL_PLANE_TO_TRANSPORT: dict[str, ManagedSessionTransport] = {
    "codex_bridge": ManagedSessionTransport.CODEX_APP_SERVER,
    "claude_channel_bridge": ManagedSessionTransport.CLAUDE_CHANNEL_BRIDGE,
    "opencode_process": ManagedSessionTransport.OPENCODE_PROCESS,
    "antigravity_process": ManagedSessionTransport.ANTIGRAVITY_PROCESS,
}


def _execution_home_from_kernel(kernel: KernelSessionCapabilities) -> SessionExecutionHome:
    """Derive an execution-home label purely from the kernel projection.

    Pre-kernel callers used this enum to decide UI affordances. Post-kernel,
    the bucket (``control_label``) is what drives affordances; the enum is
    only kept for legacy DTO fields. Map purely from kernel state — never
    from the now-gone ``session.execution_home`` column.
    """

    if kernel.live_control_available or kernel.host_reattach_available:
        return SessionExecutionHome.MANAGED_LOCAL
    return SessionExecutionHome.UNMANAGED_LOCAL


def _managed_transport_from_kernel(
    kernel: KernelSessionCapabilities,
) -> ManagedSessionTransport | None:
    """Translate the connection's control plane into the legacy transport enum.

    ``codex_bridge`` is the wire name the writers stamp; the legacy enum
    string is ``codex_app_server``. Don't conflate the two — the legacy
    enum was never written into ``connections.control_plane``.

    A connection that doesn't grant managed control returns None: log_tail
    observe-only rows, attached connections on closed runs, etc.
    """

    if not (kernel.live_control_available or kernel.host_reattach_available):
        return None
    plane = (kernel.control_plane or "").strip()
    return _CONTROL_PLANE_TO_TRANSPORT.get(plane)


def _home_label_from_kernel(kernel: KernelSessionCapabilities) -> str | None:
    if _execution_home_from_kernel(kernel) == SessionExecutionHome.MANAGED_LOCAL:
        return "On this Mac"
    return None


def build_session_capabilities_from_kernel(
    db: Session,
    session: AgentSession | None,
    *,
    kernel: KernelSessionCapabilities | None = None,
) -> SessionCapabilityFlags:
    """Build the legacy capability dataclass from the kernel projection.

    The mapping mirrors the spec's "send / queue / steer" table:

    - ``live_control_available`` = ``control_label == "live"`` (carried
      through from the projection).
    - ``host_reattach_available`` = ``control_label == "reattach"``. The
      runtime-aware projection in
      ``project_current_session_capabilities_from_facts`` is what flips a
      stale-control "live" session into the reattach affordance — until
      that down-gate fires, ``live`` is sufficient on its own.
    - ``reply_to_live_session_available`` and ``can_queue_next_input`` =
      ``live_control_available AND can_send_input``. A live attached
      connection without the send capability does not show a reply
      affordance.
    - ``can_steer_active_turn`` = ``live_control_available AND
      provider == "codex" AND control_plane == "codex_bridge"``. Runtime
      phase gating happens in
      ``project_current_session_capabilities*`` — adapter exposes the
      durable steer eligibility, not the in-the-moment availability.

    The adapter never reads ``session.execution_home`` or
    ``session.managed_transport``. They are no longer authoritative.
    """

    if session is None:
        return SessionCapabilityFlags(
            execution_home=SessionExecutionHome.UNMANAGED_LOCAL,
            managed_transport=None,
            live_control_available=False,
            host_reattach_available=False,
            reply_to_live_session_available=False,
            can_queue_next_input=False,
            can_steer_active_turn=False,
            home_label=None,
        )

    if kernel is None:
        kernel = project_session_capabilities(db, session_id=session.id)

    live = bool(kernel.live_control_available)
    reattach = bool(kernel.host_reattach_available)
    can_send = bool(kernel.can_send_input)
    reply_to_live = live and can_send
    can_queue = reply_to_live

    provider = (getattr(session, "provider", "") or "").strip().lower()
    control_plane = (kernel.control_plane or "").strip()
    can_steer = live and provider == "codex" and control_plane == "codex_bridge"

    return SessionCapabilityFlags(
        execution_home=_execution_home_from_kernel(kernel),
        managed_transport=_managed_transport_from_kernel(kernel),
        live_control_available=live,
        host_reattach_available=reattach,
        reply_to_live_session_available=reply_to_live,
        can_queue_next_input=can_queue,
        can_steer_active_turn=can_steer,
        home_label=_home_label_from_kernel(kernel),
    )


__all__ = ["build_session_capabilities_from_kernel"]
