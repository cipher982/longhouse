"""The terminal-truth selfcheck verdict: scheduler noise passes, a broken recovery still fails."""

from __future__ import annotations

import random

from zerg.qa.terminal_truth_pty import LAG_SLACK_FACTOR
from zerg.qa.terminal_truth_pty import SINGLE_OUTLIER_MAX_S
from zerg.qa.terminal_truth_pty import judge_schedule

TOL = 0.05


def _clean(n: int = 12) -> list[float]:
    rng = random.Random(7)
    return [rng.uniform(0.0, 0.01) for _ in range(n)]


def test_idle_machine_keeps_the_plain_tolerance_for_all_but_one_marker():
    assert judge_schedule(_clean(), TOL, lag_s=0.0) == []
    spiky = _clean()
    spiky[3] = 0.7307  # the single late marker seen on the loaded CI VM, with no lag measured
    assert judge_schedule(spiky, TOL, lag_s=0.0) == [], "one stalled marker is scheduler noise"
    spiky[9] = 0.147
    assert judge_schedule(spiky, TOL, lag_s=0.0), "a second late marker with no measured lag is a defect"


def test_the_one_outlier_is_capped():
    errors = _clean()
    errors[3] = SINGLE_OUTLIER_MAX_S + 0.01
    assert judge_schedule(errors, TOL, lag_s=0.0), "a marker a whole second off is a lost or misplaced chunk"


def test_late_wakeups_are_tolerated_up_to_the_measured_lag():
    errors = _clean()
    errors[3] = 0.147
    errors[8] = 0.406
    errors[10] = 0.9  # the one allowed outlier
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
