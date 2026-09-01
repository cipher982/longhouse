from __future__ import annotations

import os
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("AUTH_DISABLED", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-value")
os.environ.setdefault("INTERNAL_API_SECRET", "test-internal-secret-value")

from tests_lite._capability_test_helper import build_session_capabilities
from zerg.services.session_views import build_session_capabilities_response
from zerg.services.session_views import project_compat_capabilities_from_state


def _session(**overrides):
    values = {
        "id": uuid4(),
        "provider": "codex",
        "execution_home": "managed_local",
        "managed_transport": "codex_app_server",
        "source_runner_id": 17,
        "source_runner_name": "Demo MacBook",
        "continuation_kind": None,
        "origin_label": None,
        "environment": "development",
        "ended_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _state(**overrides):
    values = {
        "lifecycle": "open",
        "host_state": "online",
        "activity_state": "quiescent",
        "control_connection": "connected",
    }
    values.update(overrides)
    connection = values["control_connection"]
    action_state = "available" if connection == "connected" else "unavailable"
    action_reason = None if action_state == "available" else f"control_{connection}"
    access = {
        "connected": SimpleNamespace(key="live_control", label="Live control", tone="live"),
        "disconnected": SimpleNamespace(key="reattach", label="Reattach", tone="reattach"),
        "degraded": SimpleNamespace(key="control_degraded", label="Control degraded", tone="degraded"),
        "unknown": SimpleNamespace(key="control_unknown", label="Control unknown", tone="inactive"),
    }.get(connection)
    return SimpleNamespace(
        mode="helm",
        disposition=SimpleNamespace(state=values["lifecycle"]),
        run=None,
        host=SimpleNamespace(state=values["host_state"]),
        activity=SimpleNamespace(state=values["activity_state"]),
        control=SimpleNamespace(
            ownership="owned",
            connection=connection,
            actions=SimpleNamespace(
                start_turn=SimpleNamespace(state="unavailable", reason="not_console"),
                send_input=SimpleNamespace(state=action_state, reason=action_reason),
                interrupt=SimpleNamespace(state=action_state, reason=action_reason),
                terminate=SimpleNamespace(state=action_state, reason=action_reason),
                reattach=SimpleNamespace(
                    state="available" if connection == "disconnected" else "unavailable",
                    reason=None if connection == "disconnected" else "not_needed",
                ),
                resume=SimpleNamespace(state=action_state, reason=action_reason),
            ),
        ),
        presentation=SimpleNamespace(primary=None, access=access),
        launch=None,
        transcript=SimpleNamespace(live_observation=True),
    )


def _projected_response(session, state):
    response = build_session_capabilities_response(
        session=session,
        capability_flags=build_session_capabilities(session),
        session_state=state,
    )
    return project_compat_capabilities_from_state(response, state)


def test_live_idle_session_exposes_enabled_composer_with_auto_intent():
    session = _session()

    response = build_session_capabilities_response(
        session=session,
        capability_flags=build_session_capabilities(session),
        session_state=_state(),
    )

    assert response.input_mode == "live"
    assert response.default_input_intent == "auto"
    assert response.composer_enabled is True
    assert response.composer_disabled_reason is None
    assert response.send_disabled_reason is None
    assert response.composer_placeholder == "Send a message to the live Codex session..."


def test_active_steerable_session_exposes_steer_as_primary_intent():
    session = _session()

    response = build_session_capabilities_response(
        session=session,
        capability_flags=build_session_capabilities(session),
        session_state=_state(activity_state="executing"),
    )

    assert response.input_mode == "live"
    assert response.default_input_intent == "steer"
    assert response.composer_enabled is True
    assert response.send_disabled_reason is None


def test_active_claude_channel_session_exposes_steer_as_primary_intent():
    session = _session(
        provider="claude",
        managed_transport="claude_channel_bridge",
    )

    response = build_session_capabilities_response(
        session=session,
        capability_flags=build_session_capabilities(session),
        session_state=_state(activity_state="executing"),
    )

    assert response.input_mode == "live"
    assert response.default_input_intent == "steer"
    assert response.can_steer_active_turn is True
    assert response.composer_placeholder == "Send a message to the live Claude session..."


def test_idle_claude_channel_session_exposes_auto_primary_intent():
    session = _session(
        provider="claude",
        managed_transport="claude_channel_bridge",
    )

    response = build_session_capabilities_response(
        session=session,
        capability_flags=build_session_capabilities(session),
        session_state=_state(),
    )

    assert response.input_mode == "live"
    assert response.default_input_intent == "auto"
    assert response.composer_placeholder == "Send a message to the live Claude session..."


def test_offline_managed_session_exposes_disabled_composer_reason():
    session = _session()

    response = build_session_capabilities_response(
        session=session,
        capability_flags=build_session_capabilities(session),
        session_state=_state(host_state="stale"),
    )

    assert response.input_mode == "offline"
    assert response.default_input_intent == "none"
    assert response.composer_enabled is False
    assert response.composer_disabled_reason == (
        "Longhouse can see this Codex session, but cannot send prompts until the engine reconnects."
    )
    assert response.send_disabled_reason == "control_offline"


def test_live_control_without_send_bit_exposes_typed_reason_not_offline_copy():
    session = _session()
    caps = replace(build_session_capabilities(session), can_send_input=False)

    response = build_session_capabilities_response(
        session=session,
        capability_flags=caps,
        session_state=_state(),
    )

    assert response.input_mode == "read_only"
    assert response.default_input_intent == "none"
    assert response.composer_enabled is False
    assert response.composer_disabled_reason == ("This live Codex session is connected, but this control path cannot accept typed input.")
    assert response.send_disabled_reason == "input_not_supported"


def test_closed_session_lifecycle_overrides_stale_live_capabilities():
    session = _session()

    response = build_session_capabilities_response(
        session=session,
        capability_flags=build_session_capabilities(session),
        session_state=_state(lifecycle="closed"),
    )

    assert response.live_control_available is False
    assert response.host_reattach_available is False
    assert response.reply_to_live_session_available is False
    assert response.can_queue_next_input is False
    assert response.can_steer_active_turn is False
    assert response.can_send_input is False
    assert response.can_interrupt is False
    assert response.can_terminate is False
    assert response.can_resume is False
    assert response.attach_images is False
    assert response.input_mode == "read_only"
    assert response.default_input_intent == "none"
    assert response.composer_enabled is False
    assert response.composer_disabled_reason == "This session has ended."
    assert response.send_disabled_reason == "session_closed"


def test_control_transport_offline_fact_disables_composer_even_when_host_is_online():
    session = _session()
    state = _state(control_connection="disconnected")

    response = _projected_response(session, state)

    assert response.live_control_available is False
    assert response.host_reattach_available is True
    assert response.input_mode == "offline"
    assert response.default_input_intent == "none"
    assert response.composer_enabled is False
    assert response.send_disabled_reason == "control_offline"
    assert response.display_label == "Reattach"


def test_host_state_keeps_control_offline_in_projection():
    session = _session()
    state = _state(host_state="stale", control_connection="disconnected")

    response = _projected_response(session, state)

    assert response.live_control_available is False
    assert response.input_mode == "offline"
    assert response.send_disabled_reason == "control_offline"
    assert response.display_label == "Reattach"


def test_control_transport_degraded_fact_disables_composer_without_closing_session():
    session = _session()
    state = _state(control_connection="degraded")

    response = _projected_response(session, state)

    assert response.live_control_available is False
    assert response.host_reattach_available is False
    assert response.input_mode == "offline"
    assert response.send_disabled_reason == "control_offline"
    assert response.display_label == "Control degraded"


def test_unknown_control_state_is_explicitly_offline():
    session = _session()
    state = _state(control_connection="unknown")

    response = _projected_response(session, state)

    assert response.live_control_available is False
    assert response.input_mode == "offline"
    assert response.send_disabled_reason == "control_offline"
    assert response.display_label == "Control unknown"
