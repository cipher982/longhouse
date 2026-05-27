"""Hypothesis property tests for ``build_session_runtime_display``.

Strategies generate runtime/capabilities/binding inputs that respect domain
preconditions: confidence and presence_state are coupled, capabilities reflect
managed/unmanaged consistently, terminal_state values are real members of
``EXPLICIT_CLOSED_TERMINAL_STATES``/``UNVERIFIED_TERMINAL_STATES`` rather than
arbitrary strings. Without these preconditions the strategies emit nonsense
the reducer was never meant to handle.

The asserted invariants mirror the curated list in
``test_runtime_display_invariants.py`` — when we discover a new joint-state
rule it should be added to the curated suite first (with an explanation),
then the property test inherits it for free.
"""

from __future__ import annotations

import os
from datetime import datetime
from datetime import timezone

from hypothesis import HealthCheck
from hypothesis import given
from hypothesis import settings
from hypothesis import strategies as st

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.services.agents.kernel_capabilities import KernelSessionCapabilities
from zerg.services.session_runtime import EXPLICIT_CLOSED_TERMINAL_STATES
from zerg.services.session_runtime import UNVERIFIED_TERMINAL_STATES
from zerg.services.session_runtime import SessionRuntimeView
from zerg.services.session_runtime_display import build_session_runtime_display

NOW = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)

VALID_TRUTH_TIERS = {"managed-local", "fresh", "stale", "none"}
VALID_SIGNAL_TIERS_INPUT = {"phase_signal", "transcript_progress", "none"}
VALID_PRESENCE_STATES_INPUT = {"thinking", "running", "idle", "needs_user", "blocked", "stalled"}
VALID_CONFIDENCES = {"live", "stale", None}
VALID_RUNTIME_SOURCES = {"managed_local_transport", "codex_bridge", "progress", "fallback", "semantic"}
VALID_HOST_STATES_INPUT = {"online", "stale", "offline", None}
VALID_BINDING_TERMINAL_REASONS = {"process_gone", "host_expired", None}
VALID_TERMINAL_STATES = (
    list(EXPLICIT_CLOSED_TERMINAL_STATES) + list(UNVERIFIED_TERMINAL_STATES) + [None]
)


def _capabilities_for(*, managed: bool, control_plane: str | None) -> KernelSessionCapabilities:
    return KernelSessionCapabilities(
        session_id="00000000-0000-0000-0000-000000000000",
        thread_id=None,
        run_id=None,
        connection_id=None,
        control_plane=control_plane if managed else None,
        connection_state="attached" if managed else None,
        control_label="live" if managed else "imported",
        live_control_available=managed,
        host_reattach_available=managed,
        observe_only=False,
        search_only=not managed,
        can_send_input=managed,
        can_interrupt=managed,
        can_terminate=managed,
        can_tail_output=managed,
        can_resume=managed,
        staleness_reason=None if managed else "imported_only",
    )


@st.composite
def runtime_inputs(draw):
    managed = draw(st.booleans())
    control_plane = draw(st.sampled_from(["codex_bridge", "claude_channel_bridge", "opencode_process"])) if managed else None

    # Coupled choices: presence requires live confidence, terminal_state precludes presence.
    terminal_state = draw(st.sampled_from(VALID_TERMINAL_STATES))
    has_presence = draw(st.booleans())
    if terminal_state in EXPLICIT_CLOSED_TERMINAL_STATES:
        has_presence = False
        confidence = None
        presence_state = None
    elif has_presence:
        confidence = "live"
        presence_state = draw(st.sampled_from(sorted(VALID_PRESENCE_STATES_INPUT)))
    else:
        confidence = draw(st.sampled_from(["stale", None]))
        presence_state = None

    signal_tier = draw(st.sampled_from(sorted(VALID_SIGNAL_TIERS_INPUT)))
    if signal_tier == "phase_signal":
        runtime_source = draw(st.sampled_from(["managed_local_transport", "codex_bridge", "semantic"]))
    elif signal_tier == "transcript_progress":
        runtime_source = "progress"
    else:
        runtime_source = "fallback"

    host_state = draw(st.sampled_from(sorted(VALID_HOST_STATES_INPUT, key=lambda v: v or "")))
    binding_terminal_reason = draw(st.sampled_from(sorted(VALID_BINDING_TERMINAL_REASONS, key=lambda v: v or "")))

    runtime_view = SessionRuntimeView(
        signal_tier=signal_tier,
        runtime_phase=presence_state if presence_state in {"running", "thinking", "blocked"} else None,
        phase_started_at=NOW,
        last_progress_at=NOW,
        runtime_source=runtime_source,
        terminal_state=terminal_state,
        terminal_reason=None,
        terminal_source=None,
        runtime_version=1,
        status="active" if has_presence else "idle",
        presence_state=presence_state,
        presence_tool=None,
        presence_updated_at=NOW if has_presence else None,
        last_live_at=NOW if has_presence else None,
        display_phase="Recent",
        active_tool=None,
        confidence=confidence,
        timeline_anchor_at=NOW,
    )
    capabilities = _capabilities_for(managed=managed, control_plane=control_plane)
    return runtime_view, capabilities, host_state, binding_terminal_reason


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(runtime_inputs())
def test_projection_satisfies_invariants(payload):
    runtime_view, capabilities, binding_host_state, binding_terminal_reason = payload
    display = build_session_runtime_display(
        runtime_view=runtime_view,
        capabilities=capabilities,
        ended_at=None,
        binding_host_state=binding_host_state,
        binding_terminal_reason=binding_terminal_reason,
    )

    # I_enum: every axis in its valid set.
    assert display.truth_tier in {"managed-local", "fresh", "stale", "none"}
    assert display.control_path in {"managed", "unmanaged"}
    assert display.lifecycle in {"open", "closed", "unknown"}
    assert display.host_state in {"online", "stale", "offline", "unknown"}
    assert display.activity_recency in {"live", "recent", "stale", "none"}
    assert display.tone in {
        "stalled",
        "blocked",
        "running",
        "thinking",
        "idle",
        "active",
        "inactive",
        "closed",
    }

    # I_live = is_executing.
    assert display.is_live == display.is_executing

    # Running/thinking implies executing.
    if display.state in {"running", "thinking"}:
        assert display.is_executing

    # Managed-local truth implies managed control_path and is_managed_local_truth.
    if display.truth_tier == "managed-local":
        assert display.control_path == "managed"
        assert display.is_managed_local_truth

    # needs_attention implies blocked and not closed.
    if display.needs_attention:
        assert display.state == "blocked"
        assert display.lifecycle != "closed"

    # is_stalled locks state and tone.
    if display.is_stalled:
        assert display.state == "stalled"
        assert display.tone == "stalled"

    # Closed lifecycle locks outputs.
    if display.lifecycle == "closed":
        assert display.terminal_reason is not None
        assert display.is_executing is False
        assert display.is_live is False
        assert display.needs_attention is False
        assert display.is_idle is True
        assert display.state is None
        assert display.headline == "Closed"
        assert display.phase_label == "Closed"
        assert display.tone == "closed"

    # No-signal constraints.
    if display.has_signal is False:
        assert display.state is None
        assert display.truth_tier in {"stale", "none"}
        assert display.activity_recency in {"stale", "none"}

    # Syncing transcript excludes idle/executing.
    if display.state == "syncing_transcript":
        assert display.is_idle is False
        assert display.is_executing is False

    # Explicit closed terminal_state must produce closed lifecycle.
    if runtime_view.terminal_state in EXPLICIT_CLOSED_TERMINAL_STATES:
        assert display.lifecycle == "closed"

    # Unmanaged sessions cannot be managed-local.
    if not (capabilities.live_control_available or capabilities.host_reattach_available):
        assert display.control_path == "unmanaged"
        assert display.truth_tier != "managed-local"
        assert display.is_managed_local_truth is False
