"""Postcondition oracle for stock OpenCode launch-scoped coordination config."""

from __future__ import annotations

from collections.abc import Mapping


def opencode_launch_config_assertions(observation: Mapping[str, object]) -> dict[str, bool]:
    return {
        "launch_scoped_coordination_config_loaded": observation.get("instruction_loaded") is True,
    }
