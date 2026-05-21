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

from tests_lite._capability_test_helper import build_session_capabilities
from zerg.services.session_views import build_session_capabilities_response


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
