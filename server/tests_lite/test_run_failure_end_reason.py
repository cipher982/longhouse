"""A failed run says it failed, whatever the process exit code was."""

import pytest

from zerg.services.session_runtime import FAILED_RUN_END_REASONS
from zerg.services.session_runtime import _exit_status_for_terminal


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        # The adapter killed a provider that had already given up: exit_143.
        ({"exit_code": 143, "terminal_reason": "run_failed"}, "run_failed"),
        ({"exit_code": 1}, "run_failed"),
        # An older engine put the sentence where the code goes.
        ({"exit_code": 0, "terminal_reason": "OMP native session source did not drain completely"}, "run_failed"),
        ({"exit_code": 1, "terminal_reason": "provider_auth_required"}, "provider_auth_required"),
        ({"exit_status": "adapter_unavailable", "exit_code": 1}, "adapter_unavailable"),
    ],
)
def test_failed_runs_keep_a_failure_end_reason(payload, expected):
    reason = _exit_status_for_terminal("run_failed", payload)
    assert reason == expected
    assert reason in FAILED_RUN_END_REASONS


def test_other_terminals_still_report_their_exit_code():
    assert _exit_status_for_terminal("run_completed", {"exit_code": 0}) == "exit_0"
    assert _exit_status_for_terminal("run_cancelled", {"exit_code": 143}) == "exit_143"
