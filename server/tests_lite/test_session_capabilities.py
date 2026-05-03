from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import uuid4

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("AUTH_DISABLED", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-value")
os.environ.setdefault("INTERNAL_API_SECRET", "test-internal-secret-value")

from zerg.services.session_capabilities import build_session_capabilities
from zerg.services.session_capabilities import project_current_session_capabilities
from zerg.services.session_views import build_session_capabilities_response


def _make_session(**overrides):
    values = {
        "id": uuid4(),
        "provider": "claude",
        "execution_home": "legacy",
        "continuation_kind": None,
        "origin_label": None,
        "environment": "development",
        "managed_transport": None,
        "source_runner_id": None,
        "ended_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _runtime_display(**overrides):
    values = {
        "lifecycle": "open",
        "host_state": "online",
        "activity_recency": "live",
        "state": "idle",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_build_session_capabilities_marks_native_managed_local_session():
    session = _make_session(
        execution_home="managed_local",
        managed_transport="claude_channel_bridge",
        source_runner_id=17,
    )

    capabilities = build_session_capabilities(session)

    assert capabilities.execution_home.value == "managed_local"
    assert capabilities.managed_transport is not None
    assert capabilities.managed_transport.value == "claude_channel_bridge"
    assert capabilities.live_control_available is True
    assert capabilities.host_reattach_available is True
    assert capabilities.reply_to_live_session_available is True
    assert capabilities.home_label == "On this Mac"


def test_build_session_capabilities_drops_legacy_tmux_sessions_out_of_live_control():
    session = _make_session(
        execution_home="managed_local",
        managed_transport="tmux",
        source_runner_id=17,
    )

    capabilities = build_session_capabilities(session)

    assert capabilities.managed_transport is None
    assert capabilities.live_control_available is False
    assert capabilities.host_reattach_available is False
    assert capabilities.reply_to_live_session_available is False


def test_capability_response_prefers_source_runner_name_for_display_label():
    session = _make_session(
        execution_home="managed_local",
        managed_transport="claude_channel_bridge",
        source_runner_id=17,
        source_runner_name="David MacBook",
    )
    capabilities = build_session_capabilities(session)

    response = build_session_capabilities_response(
        session=session,
        capability_flags=capabilities,
        runtime_display=_runtime_display(),
    )

    assert response.display_label == "Live on David MacBook"
    assert response.display_tone == "success"


def test_capability_response_marks_unmanaged_sessions_read_only():
    session = _make_session()
    capabilities = build_session_capabilities(session)

    response = build_session_capabilities_response(
        session=session,
        capability_flags=capabilities,
        runtime_display=_runtime_display(activity_recency="recent"),
    )

    assert response.live_control_available is False
    assert response.host_reattach_available is False
    assert response.display_label == "Read only"
    assert response.display_tone == "neutral"


def test_capability_response_does_not_claim_live_without_runtime_truth():
    session = _make_session(
        execution_home="managed_local",
        managed_transport="claude_channel_bridge",
        source_runner_id=17,
        source_runner_name="David MacBook",
    )
    capabilities = build_session_capabilities(session)

    response = build_session_capabilities_response(session=session, capability_flags=capabilities)

    assert response.live_control_available is False
    assert response.host_reattach_available is True
    assert response.display_label == "Control offline"
    assert response.display_tone == "warning"


def test_capability_response_marks_closed_managed_session_not_live_or_reattachable():
    session = _make_session(
        execution_home="managed_local",
        managed_transport="claude_channel_bridge",
        source_runner_id=17,
        source_runner_name="David MacBook",
    )
    capabilities = build_session_capabilities(session)

    response = build_session_capabilities_response(
        session=session,
        capability_flags=capabilities,
        runtime_display=_runtime_display(lifecycle="closed", host_state="offline", activity_recency="stale"),
    )

    assert response.live_control_available is False
    assert response.host_reattach_available is False
    assert response.reply_to_live_session_available is False
    assert response.can_queue_next_input is False
    assert response.can_steer_active_turn is False
    assert response.display_label == "Closed"
    assert response.display_tone == "neutral"


def test_capability_response_marks_disconnected_managed_session_control_offline():
    session = _make_session(
        execution_home="managed_local",
        managed_transport="claude_channel_bridge",
        source_runner_id=17,
        source_runner_name="David MacBook",
    )
    capabilities = build_session_capabilities(session)

    response = build_session_capabilities_response(
        session=session,
        capability_flags=capabilities,
        runtime_display=_runtime_display(host_state="stale"),
    )

    assert response.live_control_available is False
    assert response.host_reattach_available is True
    assert response.reply_to_live_session_available is False
    assert response.display_label == "Control offline"
    assert response.display_tone == "warning"


def test_current_capability_projection_only_allows_steer_during_active_runtime():
    session = _make_session(
        execution_home="managed_local",
        managed_transport="codex_app_server",
        source_runner_id=17,
    )
    capabilities = build_session_capabilities(session)

    idle = project_current_session_capabilities(capabilities, runtime_display=_runtime_display(state="idle"))
    running = project_current_session_capabilities(capabilities, runtime_display=_runtime_display(state="running"))

    assert idle.live_control_available is True
    assert idle.can_steer_active_turn is False
    assert running.live_control_available is True
    assert running.can_steer_active_turn is True
