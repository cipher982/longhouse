"""Lightweight shared session loop-mode enum.

This lives outside ``zerg.models`` so tooling and deterministic eval harnesses
can import the contract without booting the database model package.
"""

from __future__ import annotations

from enum import Enum


class SessionLoopMode(str, Enum):
    """How much autonomy Oikos may exercise for a coding session."""

    ASSIST = "assist"
    AUTOPILOT = "autopilot"


def coerce_session_loop_mode(value: str | SessionLoopMode | None) -> SessionLoopMode:
    """Normalize stored/legacy loop-mode values to the public policy contract."""

    try:
        return SessionLoopMode(str(value or SessionLoopMode.ASSIST.value))
    except ValueError:
        return SessionLoopMode.ASSIST
