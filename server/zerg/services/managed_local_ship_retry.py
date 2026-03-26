"""Shared retry timing for managed-local Claude transcript shipping.

The pre-enqueue tail is now mostly the gap between Claude finishing locally and
the transcript being ready to ship. Keep the first second dense and share the
exact shell schedule across the hook path and the direct managed-local ship
command so the two paths do not drift.
"""

from __future__ import annotations

MANAGED_LOCAL_CLAUDE_SHIP_RETRY_ATTEMPT_AT_SECS: tuple[float, ...] = (
    0.0,
    0.1,
    0.25,
    0.5,
    1.0,
    1.5,
    2.0,
    3.0,
    4.0,
    6.0,
    8.0,
)


def _format_shell_delay(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def get_managed_local_claude_ship_retry_sleep_delays() -> tuple[float, ...]:
    delays: list[float] = []
    previous = 0.0
    for attempt_at in MANAGED_LOCAL_CLAUDE_SHIP_RETRY_ATTEMPT_AT_SECS:
        delays.append(max(0.0, attempt_at - previous))
        previous = attempt_at
    return tuple(delays)


MANAGED_LOCAL_CLAUDE_SHIP_RETRY_SLEEP_DELAYS_SHELL = " ".join(
    _format_shell_delay(delay) for delay in get_managed_local_claude_ship_retry_sleep_delays()
)
