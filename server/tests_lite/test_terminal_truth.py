"""The terminal-truth harness checks itself: known schedule recovered, a late path reported late."""

from __future__ import annotations

from zerg.qa import terminal_truth as tt


def test_lane_stats_arithmetic_and_never_served() -> None:
    terminal = {"A": 1.0, "B": 2.0, "C": 3.0, "D": None}
    stats = tt.lane_stats("x", terminal, {"A": 1.5, "B": 4.0}, 0.1, ["A", "B", "C", "D"])
    assert stats.delays == {"A": 0.5, "B": 2.0, "C": None}  # D never on screen: not scored
    assert (stats.first, stats.median, stats.worst) == (0.5, 1.25, 2.0)
    assert stats.never_served == ["C"]


def test_pacing_error_is_zero_for_a_terminal_that_streams_on_schedule() -> None:
    schedule = [(0, "A", 1.0), (5, "B", 2.0), (10, "C", 3.0)]
    assert tt.pacing_error(schedule, {"A": 10.0, "B": 11.0, "C": 12.0}) == 0
    # A terminal that printed everything at the end is far off the schedule.
    assert tt.pacing_error(schedule, {"A": 12.0, "B": 12.0, "C": 12.0}) > 0.9


def test_selfcheck_recovers_the_fake_tui_schedule_and_reports_the_end_only_lane_late() -> None:
    outcome = tt.run_selfcheck(markers=8, spacing_s=0.4)
    assert outcome.passed, outcome.detail
    # Redraws (spinner, bottom input box, a marker that exists only in the box) did not hide or move any marker.
    assert outcome.schedule_error_max_s < 0.05
    # Negative control: the lane that serves only at the end of the stream is late by about the stream length.
    assert outcome.end_only_lane.first is not None
    assert outcome.end_only_lane.first > 0.9 * outcome.stream_seconds
    assert outcome.end_only_lane.worst is not None and outcome.end_only_lane.worst >= outcome.end_only_lane.first
    # The prompt lane is not mistaken for a late one.
    assert outcome.prompt_lane.worst is not None and outcome.prompt_lane.worst < 0.6


def test_a_late_lane_is_never_scored_as_near_zero() -> None:
    # Direct guard for the harness's central failure: zero delay for a path that only had text at the end.
    terminal = {f"M{i}": float(i) for i in range(10)}  # streamed over 9 s
    served = {f"M{i}": 9.5 for i in range(10)}  # served once, at the end
    stats = tt.lane_stats("end-only", terminal, served, 0.1, list(terminal))
    assert stats.first is not None and stats.first >= 9.0
    assert stats.median is not None and stats.median > 4.0


def test_render_table_has_a_could_not_run_row() -> None:
    row = tt.RunResult(provider="x", mode="shadow", status="could_not_run", reason="not installed")
    table = tt.render_table([row])
    assert "could not run" in table and "not installed" in table
    header, rule, line = table.splitlines()
    assert header.count("|") == rule.count("|") == line.count("|")
