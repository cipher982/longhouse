"""The terminal-truth selfcheck verdict: scheduler noise passes, a broken recovery still fails."""

from __future__ import annotations

import random

from zerg.qa.terminal_truth_pty import LAG_SLACK_FACTOR
from zerg.qa.terminal_truth_pty import judge_schedule

TOL = 0.05


def _clean(n: int = 12) -> list[float]:
    rng = random.Random(7)
    return [rng.uniform(0.0, 0.01) for _ in range(n)]


def test_idle_machine_keeps_the_plain_tolerance():
    assert judge_schedule(_clean(), TOL, lag_s=0.0) == []
    spiky = _clean()
    spiky[3] = 0.147  # the loaded-VM sample that used to refuse pushes
    assert judge_schedule(spiky, TOL, lag_s=0.0), "with no measured lag a 147 ms error is a defect"


def test_late_wakeups_are_tolerated_up_to_the_measured_lag():
    errors = _clean()
    errors[3] = 0.147
    errors[8] = 0.406
    assert judge_schedule(errors, TOL, lag_s=0.2) == []  # slack = 0.6 s
    # ...but only as far as the lag that was measured
    errors[5] = 0.406 + 0.5
    assert judge_schedule(errors, TOL, lag_s=0.2)


def test_a_systematic_offset_fails_even_under_load():
    # A recovery that is wrong moves every marker, so the median exposes it however noisy the machine was.
    for lag in (0.0, 0.03, 0.05):
        biased = [e + 0.12 for e in _clean()]
        detail = judge_schedule(biased, TOL, lag_s=lag)
        assert detail, f"a 120 ms constant offset passed at lag {lag}"


def test_nothing_recovered_fails():
    assert judge_schedule([], TOL, lag_s=1.0)


def test_slack_is_the_documented_multiple_of_lag():
    assert LAG_SLACK_FACTOR == 3.0
