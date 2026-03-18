"""Lightweight shared session loop-mode enum.

This lives outside ``zerg.models`` so tooling and deterministic eval harnesses
can import the contract without booting the database model package.
"""

from __future__ import annotations

from enum import Enum


class SessionLoopMode(str, Enum):
    """How much autonomy Oikos may exercise for a coding session."""

    MANUAL = "manual"
    ASSIST = "assist"
    AUTOPILOT = "autopilot"
