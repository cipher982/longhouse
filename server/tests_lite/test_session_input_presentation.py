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

from zerg.services.session_views import build_session_capabilities_response

from tests_lite._capability_test_helper import build_session_capabilities


def _session(**overrides):
    values = {
        "id": uuid4(),
        "provider": "codex",
        "execution_home": "managed_local",
        "managed_transport": "codex_app_server",
        "source_runner_id": 17,
        "source_runner_name": "David MacBook",
        "continuation_kind": None,
        "origin_label": None,
        "environment": "development",
        "ended_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _runtime(**overrides):
    values = {
        "lifecycle": "open",
        "host_state": "online",
        "activity_recency": "live",
        "state": "idle",
        "is_executing": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _facts(**overrides):
    values = {
        "control_path": "managed",
        "control_state": "online",
        "control_reason": None,
        "host_state": "online",
        "lifecycle": "open",
        "phase_kind": "idle",
    }
    values.update(overrides)
    return SimpleNamespace(
        control_path=values["control_path"],
        control=SimpleNamespace(
            state="none" if values["control_path"] == "unmanaged" else values["control_state"],
            reason=values["control_reason"],
            source="machine_heartbeat" if values["control_path"] == "managed" else None,
        ),
        host=SimpleNamespace(
            state=values["host_state"],
            source="machine_heartbeat" if values["host_state"] != "unknown" else None,
        ),
        lifecycle=SimpleNamespace(state=values["lifecycle"], reason=None),
        phase=SimpleNamespace(kind=values["phase_kind"]),
    )


def test_live_idle_session_exposes_enabled_composer_with_auto_intent():
    session = _session()

    response = build_session_capabilities_response(
        session=session,
        capability_flags=build_session_capabilities(session),
        runtime_display=_runtime(),
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
        runtime_display=_runtime(state="running", is_executing=True),
    )

    assert response.input_mode == "live"
    assert response.default_input_intent == "steer"
    assert response.composer_enabled is True
    assert response.send_disabled_reason is None


def test_offline_managed_session_exposes_disabled_composer_reason():
    session = _session()

    response = build_session_capabilities_response(
        session=session,
        capability_flags=build_session_capabilities(session),
        runtime_display=_runtime(host_state="stale", activity_recency="stale", is_executing=False),
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
        runtime_display=_runtime(),
    )

    assert response.input_mode == "read_only"
    assert response.default_input_intent == "none"
    assert response.composer_enabled is False
    assert response.composer_disabled_reason == (
        "This live Codex session is connected, but this control path cannot accept typed input."
    )
    assert response.send_disabled_reason == "input_not_supported"


def test_closed_session_lifecycle_overrides_stale_live_capabilities():
    session = _session()

    response = build_session_capabilities_response(
        session=session,
        capability_flags=build_session_capabilities(session),
        runtime_display=_runtime(lifecycle="closed", host_state="online"),
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

    response = build_session_capabilities_response(
        session=session,
        capability_flags=build_session_capabilities(session),
        runtime_display=_runtime(host_state="online"),
        runtime_facts=_facts(
            host_state="online",
            control_state="offline",
            control_reason="lease_stale",
        ),
    )

    assert response.live_control_available is False
    assert response.host_reattach_available is True
    assert response.input_mode == "offline"
    assert response.default_input_intent == "none"
    assert response.composer_enabled is False
    assert response.send_disabled_reason == "control_offline"
    assert response.display_label == "Control offline"


def test_host_offline_fact_overrides_stale_runtime_display_host_copy():
    session = _session()

    response = build_session_capabilities_response(
        session=session,
        capability_flags=build_session_capabilities(session),
        runtime_display=_runtime(host_state="online"),
        runtime_facts=_facts(host_state="stale", control_state="online"),
    )

    assert response.live_control_available is False
    assert response.input_mode == "offline"
    assert response.send_disabled_reason == "control_offline"
    assert response.display_label == "Control offline"


def test_control_transport_degraded_fact_disables_composer_without_closing_session():
    session = _session()

    response = build_session_capabilities_response(
        session=session,
        capability_flags=build_session_capabilities(session),
        runtime_display=_runtime(host_state="online"),
        runtime_facts=_facts(
            host_state="online",
            control_state="degraded",
            control_reason="bridge_unavailable",
            lifecycle="open",
        ),
    )

    assert response.live_control_available is False
    assert response.host_reattach_available is True
    assert response.input_mode == "offline"
    assert response.send_disabled_reason == "control_offline"
    assert response.display_label == "Control offline"


def test_unknown_control_cold_start_does_not_override_positive_host_display():
    session = _session()

    response = build_session_capabilities_response(
        session=session,
        capability_flags=build_session_capabilities(session),
        runtime_display=_runtime(host_state="online"),
        runtime_facts=_facts(
            host_state="online",
            control_state="unknown",
            control_reason="cold_start",
            lifecycle="open",
        ),
    )

    assert response.live_control_available is True
    assert response.input_mode == "live"
    assert response.send_disabled_reason is None
