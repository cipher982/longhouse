"""Provider-independent postcondition oracles for control-boundary scenarios."""

from __future__ import annotations

from collections.abc import Mapping


def unsupported_steer_assertions(observation: Mapping[str, object]) -> dict[str, bool]:
    return {
        "rejected_before_control_write": (observation.get("rejected") is True and observation.get("control_write_count") == 0),
    }
