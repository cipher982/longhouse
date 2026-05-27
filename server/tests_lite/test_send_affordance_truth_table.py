from __future__ import annotations

from zerg.services.agents.kernel_capabilities import KernelSessionCapabilities
from zerg.services.send_affordance import project_send_affordance


def _caps(**overrides) -> KernelSessionCapabilities:
    values = {
        "session_id": "session-1",
        "thread_id": "thread-1",
        "run_id": "run-1",
        "connection_id": 1,
        "control_plane": "codex_bridge",
        "connection_state": "attached",
        "control_label": "live",
        "live_control_available": True,
        "host_reattach_available": True,
        "observe_only": False,
        "search_only": False,
        "can_send_input": True,
        "can_interrupt": True,
        "can_terminate": True,
        "can_tail_output": True,
        "can_resume": True,
        "staleness_reason": None,
    }
    values.update(overrides)
    return KernelSessionCapabilities(**values)


def _project(capability_flags: KernelSessionCapabilities, **overrides):
    values = {
        "read_only_reason": "This imported session is searchable, but Longhouse cannot steer it.",
        "provider_label": "Codex",
        "lifecycle": "open",
        "is_executing": False,
        "host_state": "online",
    }
    values.update(overrides)
    return project_send_affordance(capability_flags, **values)


def test_live_idle_send_uses_auto_intent():
    affordance = _project(_caps())

    assert affordance.input_mode == "live"
    assert affordance.default_input_intent == "auto"
    assert affordance.composer_enabled is True
    assert affordance.send_disabled_reason is None


def test_live_idle_claude_channel_bridge_uses_auto_intent():
    affordance = _project(_caps(control_plane="claude_channel_bridge"), provider_label="Claude")

    assert affordance.input_mode == "live"
    assert affordance.default_input_intent == "auto"
    assert affordance.composer_placeholder == "Send a message to the live Claude session..."
    assert affordance.send_disabled_reason is None


def test_live_executing_codex_bridge_uses_steer_intent():
    affordance = _project(_caps(control_plane="codex_bridge"), is_executing=True)

    assert affordance.input_mode == "live"
    assert affordance.default_input_intent == "steer"
    assert affordance.send_disabled_reason is None


def test_live_executing_claude_channel_bridge_uses_steer_intent():
    affordance = _project(_caps(control_plane="claude_channel_bridge"), provider_label="Claude", is_executing=True)

    assert affordance.input_mode == "live"
    assert affordance.default_input_intent == "steer"
    assert affordance.composer_placeholder == "Send a message to the live Claude session..."
    assert affordance.send_disabled_reason is None


def test_live_executing_non_steerable_transport_uses_queue_intent():
    affordance = _project(_caps(control_plane="opencode_server_bridge"), provider_label="OpenCode", is_executing=True)

    assert affordance.input_mode == "live"
    assert affordance.default_input_intent == "queue"
    assert affordance.send_disabled_reason is None


def test_closed_lifecycle_overrides_live_send_capability():
    affordance = _project(
        _caps(),
        lifecycle="closed",
        read_only_reason="This session has ended.",
        host_state="online",
    )

    assert affordance.input_mode == "read_only"
    assert affordance.default_input_intent == "none"
    assert affordance.composer_enabled is False
    assert affordance.send_disabled_reason == "session_closed"
    assert affordance.composer_disabled_reason == "This session has ended."


def test_offline_runtime_overrides_live_send_capability():
    affordance = _project(_caps(), host_state="stale")

    assert affordance.input_mode == "offline"
    assert affordance.default_input_intent == "none"
    assert affordance.composer_enabled is False
    assert affordance.send_disabled_reason == "control_offline"
    assert "engine reconnects" in (affordance.composer_disabled_reason or "")


def test_closed_stale_runtime_reports_closed_not_offline():
    affordance = _project(
        _caps(),
        lifecycle="closed",
        read_only_reason="This session has ended.",
        host_state="stale",
    )

    assert affordance.input_mode == "read_only"
    assert affordance.send_disabled_reason == "session_closed"
    assert affordance.composer_disabled_reason == "This session has ended."


def test_reattach_without_live_control_is_control_offline():
    affordance = _project(
        _caps(
            control_label="reattach",
            live_control_available=False,
            can_send_input=False,
            can_interrupt=False,
            can_terminate=False,
            host_reattach_available=True,
        )
    )

    assert affordance.input_mode == "offline"
    assert affordance.default_input_intent == "none"
    assert affordance.send_disabled_reason == "control_offline"


def test_live_control_without_send_bit_is_not_reported_as_engine_offline():
    affordance = _project(_caps(can_send_input=False))

    assert affordance.input_mode == "read_only"
    assert affordance.default_input_intent == "none"
    assert affordance.composer_enabled is False
    assert affordance.send_disabled_reason == "input_not_supported"
    assert affordance.composer_disabled_reason == (
        "This live Codex session is connected, but this control path cannot accept typed input."
    )


def test_imported_session_is_read_only():
    affordance = _project(
        _caps(
            thread_id=None,
            run_id=None,
            connection_id=None,
            control_plane=None,
            connection_state=None,
            control_label="imported",
            live_control_available=False,
            host_reattach_available=False,
            search_only=True,
            can_send_input=False,
            can_interrupt=False,
            can_terminate=False,
            can_tail_output=False,
            can_resume=False,
            staleness_reason="imported_only",
        )
    )

    assert affordance.input_mode == "read_only"
    assert affordance.default_input_intent == "none"
    assert affordance.send_disabled_reason == "read_only"
    assert affordance.composer_disabled_reason == "This imported session is searchable, but Longhouse cannot steer it."


def test_imported_session_with_unknown_host_state_stays_read_only_not_offline():
    affordance = _project(
        _caps(
            thread_id=None,
            run_id=None,
            connection_id=None,
            control_plane=None,
            connection_state=None,
            control_label="imported",
            live_control_available=False,
            host_reattach_available=False,
            search_only=True,
            can_send_input=False,
            can_interrupt=False,
            can_terminate=False,
            can_tail_output=False,
            can_resume=False,
            staleness_reason="imported_only",
        ),
        host_state="unknown",
    )

    assert affordance.input_mode == "read_only"
    assert affordance.send_disabled_reason == "read_only"
