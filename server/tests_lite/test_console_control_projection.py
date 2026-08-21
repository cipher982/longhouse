from zerg.services.console_control_projection import ConsoleControlProjection
from zerg.services.console_control_projection import project_console_control


def test_console_projection_keeps_connection_independent_from_start_blocker():
    projection = project_console_control(
        closed=False,
        execution_target_available=False,
        turn_state="idle",
        machine_online=True,
        adapter_available=True,
        interrupt_adapter_available=False,
    )

    assert projection.connection == "connected"
    assert projection.can_start_turn is False
    assert projection.start_turn_blocked_by == "execution_target_missing"
    assert projection.interrupt_unavailable_reason == "unsupported"


def test_console_projection_exposes_supported_interrupt_only_during_execution():
    active = project_console_control(
        closed=False,
        execution_target_available=True,
        turn_state="active",
        machine_online=True,
        adapter_available=True,
        interrupt_adapter_available=True,
    )
    idle = project_console_control(
        closed=False,
        execution_target_available=True,
        turn_state="idle",
        machine_online=True,
        adapter_available=True,
        interrupt_adapter_available=True,
    )

    assert active.can_interrupt_active_turn is True
    assert idle.can_interrupt_active_turn is False
    assert idle.interrupt_unavailable_reason == "no_active_turn"


def test_console_projection_catalog_round_trip_preserves_typed_answer():
    original = project_console_control(
        closed=False,
        execution_target_available=True,
        turn_state="starting",
        machine_online=False,
        adapter_available=True,
        interrupt_adapter_available=True,
    )

    restored = ConsoleControlProjection.from_catalog_facts(original.as_catalog_facts())

    assert restored == original
    assert restored.connection == "disconnected"
    assert restored.start_turn_blocked_by == "machine_offline"
