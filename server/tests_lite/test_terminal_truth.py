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


def test_file_watcher_dates_a_provider_write_and_ship_lag_is_lane_minus_write(tmp_path) -> None:
    import threading
    import time

    stop = threading.Event()
    watcher = tt.FileWatcher(tmp_path, ["WROTE0001"], stop, interval=0.02)
    (tmp_path / ".longhouse").mkdir()
    (tmp_path / ".longhouse" / "state").write_text("WROTE0001 in machine agent state is not a provider write")
    watcher.scan_once()
    watcher.start()
    time.sleep(0.2)
    wrote_at = time.monotonic()
    (tmp_path / "session.jsonl").write_text('{"text":"WROTE0001"}')
    time.sleep(0.3)
    stop.set()
    watcher.join(timeout=2)
    assert watcher.first_path == {"WROTE0001": "session.jsonl"}
    assert 0 <= watcher.first_seen["WROTE0001"] - wrote_at < 0.1
    stats = tt.lane_stats("x", {"WROTE0001": 1.0}, {"WROTE0001": 5.0}, 0.1, ["WROTE0001"], {"WROTE0001": 4.0})
    assert stats.delays == {"WROTE0001": 4.0} and stats.ship_lag == {"WROTE0001": 1.0}


def test_stream_watcher_keeps_the_last_preview_per_session_after_the_last_marker() -> None:
    import json
    import threading

    def frame(session: str, text: str) -> str:
        return "data: " + json.dumps({"session_id": session, "change_kind": "runtime", "transcript_preview": {"text": text}})

    lines = [
        frame("s1", "alpha"),
        frame("s1", "alpha beta"),  # carries the last marker
        "data: " + json.dumps({"session_id": "s1", "transcript_preview": None}),
        frame("s1", "alpha beta gamma"),  # arrives after every marker was seen
        frame("s2", "other"),
    ]
    stop = threading.Event()
    watcher = tt.StreamWatcher("s", lambda: iter(lines), ["beta"], stop)
    watcher.run()
    assert watcher.first_seen.keys() == {"beta"}
    assert watcher.final_preview == {"s1": "alpha beta gamma", "s2": "other"}


def test_live_preview_check_accepts_a_cumulative_stream_and_flags_every_failure() -> None:
    from zerg.qa.terminal_truth import check_live_previews

    reply = "alpha beta gamma delta"
    good = check_live_previews({"s": ["alpha", "alpha", "alpha beta gamma", reply]}, reply, "THINKING")
    assert good["each_continues_the_last"] and good["final_equals_reply"] and not good["reasoning_leaked"]
    # A repeated or reordered chunk breaks the chain; a short ending is not the whole reply.
    assert check_live_previews({"s": ["alpha beta", "alpha beta alpha beta"]}, reply, "THINKING")["each_continues_the_last"]
    reordered = check_live_previews({"s": ["alpha beta", "beta alpha"]}, reply, "THINKING")
    assert not reordered["each_continues_the_last"] and reordered["first_break_at_preview"] == 1
    assert not check_live_previews({"s": ["alpha"]}, reply, "THINKING")["final_equals_reply"]
    assert check_live_previews({"s": ["THINKING000 alpha"]}, reply, "THINKING")["reasoning_leaked"]
    assert check_live_previews({}, reply, "THINKING") == {"previews": 0}
