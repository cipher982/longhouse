"""Pure Console control projection shared by every served session path."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from typing import Literal
from typing import Mapping

ConsoleTurnState = Literal["idle", "queued", "starting", "active", "draining"]
ConsoleStartBlocker = Literal[
    "session_closed",
    "execution_target_missing",
    "machine_offline",
    "adapter_unavailable",
]
ConsoleConnection = Literal["connected", "degraded", "disconnected"]

_EXECUTION_STATES = frozenset({"starting", "active", "draining"})


def _turn_state(value: object) -> ConsoleTurnState:
    normalized = str(value or "idle").strip().lower()
    if normalized in {"idle", "queued", "starting", "active", "draining"}:
        return normalized  # type: ignore[return-value]
    return "idle"


@dataclass(frozen=True)
class ConsoleControlProjection:
    """One typed answer for Console reachability and turn-scoped actions.

    Connection is deliberately independent from whether a new turn can start.
    A closed thread or a missing execution target can still have a connected
    machine channel; those conditions gate ``start_turn`` without rewriting
    the reachability observation.
    """

    turn_state: ConsoleTurnState
    machine_online: bool
    adapter_available: bool
    interrupt_adapter_available: bool
    can_start_turn: bool
    start_turn_blocked_by: ConsoleStartBlocker | None
    can_interrupt_active_turn: bool

    @property
    def connection(self) -> ConsoleConnection:
        if not self.machine_online:
            return "disconnected"
        if not self.adapter_available:
            return "degraded"
        return "connected"

    @property
    def turn_is_executing(self) -> bool:
        return self.turn_state in _EXECUTION_STATES

    @property
    def interrupt_unavailable_reason(self) -> str:
        if not self.machine_online:
            return "machine_offline"
        if not self.interrupt_adapter_available:
            return "unsupported"
        if not self.turn_is_executing:
            return "no_active_turn"
        return "interrupt_unavailable"

    def as_catalog_facts(self) -> dict[str, Any]:
        return {
            "turn_state": self.turn_state,
            "machine_online": self.machine_online,
            "adapter_available": self.adapter_available,
            "interrupt_adapter_available": self.interrupt_adapter_available,
            "can_start_turn": self.can_start_turn,
            "start_turn_blocked_by": self.start_turn_blocked_by,
            "can_interrupt_active_turn": self.can_interrupt_active_turn,
        }

    @classmethod
    def from_catalog_facts(cls, value: Mapping[str, Any]) -> ConsoleControlProjection:
        blocked = str(value.get("start_turn_blocked_by") or "").strip() or None
        if blocked not in {
            None,
            "session_closed",
            "execution_target_missing",
            "machine_offline",
            "adapter_unavailable",
        }:
            blocked = "adapter_unavailable"
        return cls(
            turn_state=_turn_state(value.get("turn_state")),
            machine_online=bool(value.get("machine_online")),
            adapter_available=bool(value.get("adapter_available")),
            interrupt_adapter_available=bool(value.get("interrupt_adapter_available")),
            can_start_turn=bool(value.get("can_start_turn")),
            start_turn_blocked_by=blocked,  # type: ignore[arg-type]
            can_interrupt_active_turn=bool(value.get("can_interrupt_active_turn")),
        )


def project_console_control(
    *,
    closed: bool,
    execution_target_available: bool,
    turn_state: object,
    machine_online: bool,
    adapter_available: bool,
    interrupt_adapter_available: bool,
) -> ConsoleControlProjection:
    """Project Console reachability and independent actions without I/O."""

    normalized_state = _turn_state(turn_state)
    blocked_by: ConsoleStartBlocker | None = "session_closed" if closed else None
    if blocked_by is None and not execution_target_available:
        blocked_by = "execution_target_missing"
    if blocked_by is None and not machine_online:
        blocked_by = "machine_offline"
    if blocked_by is None and not adapter_available:
        blocked_by = "adapter_unavailable"
    return ConsoleControlProjection(
        turn_state=normalized_state,
        machine_online=machine_online,
        adapter_available=adapter_available,
        interrupt_adapter_available=interrupt_adapter_available,
        can_start_turn=blocked_by is None,
        start_turn_blocked_by=blocked_by,
        can_interrupt_active_turn=(normalized_state in _EXECUTION_STATES and machine_online and interrupt_adapter_available),
    )


__all__ = [
    "ConsoleControlProjection",
    "ConsoleStartBlocker",
    "ConsoleTurnState",
    "project_console_control",
]
