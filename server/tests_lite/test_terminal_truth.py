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


# ---- stages, verdicts and scoping -------------------------------------------------------------------------------------


def _good_observation(**changes) -> tt.Observation:
    markers = [f"TTabc123{i:04d}" for i in (0, 20, 39)]
    message = " ".join(markers)
    base = dict(
        mode="helm",
        markers=markers,
        messages=[message],
        ready_evidence="'Ask Codex' on screen",
        model_requests_served=1,
        reference={m: 10.0 + i for i, m in enumerate(markers)},
        model_sent={m: 10.0 + i - 0.002 for i, m in enumerate(markers)},
        engine_posted={m: 10.0 + i for i, m in enumerate(markers)},
        host_publishes=5,
        host_publish_max_len=len(message),
        stream_first_seen={m: 10.05 + i for i, m in enumerate(markers)},
        previews={"s": [markers[0], f"{markers[0]} {markers[1]}", message]},
    )
    base.update(changes)
    return tt.Observation(**base)


def test_a_good_observation_passes_every_stage() -> None:
    stages = tt.judge(_good_observation())
    assert [s.ok for s in stages.values()] == [True] * 5, {k: v.reason for k, v in stages.items()}
    assert tt.first_failed_stage(stages) is None


def test_the_verdict_names_the_first_stage_where_text_stopped() -> None:
    def failed(**changes) -> str | None:
        return tt.first_failed_stage(tt.judge(_good_observation(**changes)))

    assert failed(ready_evidence="") == "preflight"
    assert failed(model_requests_served=0) == "preflight"
    assert failed(preflight_error="provider exited") == "preflight"
    assert failed(reference={}) == "provider_emitted"  # a cell with zero markers observed cannot pass
    assert failed(engine_posted={}) == "engine_posted"
    assert failed(host_publishes=0) == "host_published"
    assert failed(host_publish_max_len=3) == "host_published"
    assert failed(stream_first_seen={}) == "stream_delivered"
    # Everything arrives at the end of the stream: delivered, but not live.
    late = {m: 30.0 for m in _good_observation().markers}
    assert failed(stream_first_seen=late) == "stream_delivered"


def test_every_blocking_stream_check_fails_on_its_own_defect() -> None:
    obs = _good_observation()
    message = obs.messages[0]

    def failing(**changes) -> list[str]:
        return [name for name, ok, _ in tt.stream_checks(_good_observation(**changes)) if not ok]

    assert failing(previews={"s": [message[:10]]}) == ["final_preview_equals_message"]
    assert "each_preview_continues_the_last" in failing(previews={"s": ["zzz", message]})
    assert failing(previews={"s": [message, f"THINKING000 {message}"]}).count("no_reasoning_in_preview") == 1
    assert "markers_in_order" in failing(stream_first_seen={obs.markers[0]: 12.5, obs.markers[1]: 11.0, obs.markers[2]: 12.0})
    assert "median_delay_within_budget" in failing(stream_first_seen={m: t + 0.9 for m, t in obs.reference.items() if t})
    assert failing(stream_first_seen={m: t + 0.74 for m, t in obs.reference.items() if t}) == []  # inside the budget
    # Less than half before the provider's last token.
    last = obs.reference[obs.markers[-1]]
    assert "half_delivered_before_last_token" in failing(stream_first_seen={m: last + 0.1 for m in obs.markers})


def test_the_worst_single_marker_never_blocks() -> None:
    obs = _good_observation()
    seen = dict(obs.stream_first_seen)
    seen[obs.markers[0]] = obs.reference[obs.markers[0]] + 0.7  # one slow marker, median still fine
    assert tt.first_failed_stage(tt.judge(_good_observation(stream_first_seen=seen))) is None


def test_outcomes_known_bad_cells_are_never_reported_as_passes() -> None:
    expected, bad = tt.Expectation("expected"), tt.Expectation("known_bad", "none", "no landed source")
    assert tt.outcome_for(None, expected, "helm") == "pass"
    assert tt.outcome_for("stream_delivered", expected, "helm") == "fail"
    assert tt.outcome_for("engine_posted", bad, "helm") == "known_bad"
    assert tt.outcome_for(None, bad, "helm") == "known_bad_fixed"  # a stale ledger fails the run
    assert tt.outcome_for("preflight", bad, "helm") == "fail"  # a known-bad cell that cannot start is not hiding behind its label
    assert tt.outcome_for("provider_emitted", bad, "helm") == "fail"
    assert tt.outcome_for(None, tt.Expectation("not_measurable"), "console") == "not_measurable"


def test_the_exit_code_fails_on_a_non_pass_and_on_a_declared_cell_that_did_not_run() -> None:
    declared = [tt.CellSpec("codex", "console"), tt.CellSpec("codex", "helm")]

    def cell(mode: str, outcome: str) -> tt.CellResult:
        return tt.CellResult("codex", mode, "measured", outcome=outcome)

    assert tt.exit_code([cell("console", "pass"), cell("helm", "pass")], declared) == 0
    assert tt.exit_code([cell("console", "pass"), cell("helm", "known_bad")], declared) == 0
    assert tt.exit_code([cell("console", "pass"), cell("helm", "fail")], declared) == 1
    assert tt.exit_code([cell("console", "pass"), cell("helm", "known_bad_fixed")], declared) == 1
    assert tt.exit_code([cell("console", "pass")], declared) == 1  # coverage floor: a declared cell that did not run


def test_no_driver_may_have_an_empty_ready_string() -> None:
    import pytest

    assert all(driver.ready_text.strip() for driver in tt.PROVIDERS.values())
    with pytest.raises(ValueError, match="ready prompt"):
        tt.ProviderDriver("x", "x", "  ", lambda b, w: [str(b)])


def test_a_reworded_ready_string_fails_before_any_later_stage(tmp_path) -> None:
    import sys

    import pytest

    from zerg.qa.pty_session import ProviderPtySession

    recording, timing = tmp_path / "t.raw", tmp_path / "t.timing"
    session = ProviderPtySession.start(
        argv=[sys.executable, "-c", "import time; print('Ask Codex 2.0 to do anything', flush=True); time.sleep(20)"],
        cwd=tmp_path,
        env=dict(__import__("os").environ),
        terminal_path=recording,
        timing_path=timing,
        rows=40,
        columns=132,
    )
    try:
        assert "Ask Codex" in tt._wait_ready(session, recording, timing, "Ask Codex", 10)
        with pytest.raises(RuntimeError, match="never showed"):
            tt._wait_ready(session, recording, timing, "Ask Codex to do anything", 2)  # reworded upstream
    finally:
        session.close()
    # ...and the verdict puts that at preflight, not at a later stage.
    stages = tt.judge(_good_observation(preflight_error="RuntimeError: provider TUI never showed 'x'"))
    assert tt.first_failed_stage(stages) == "preflight" and stages["stream_delivered"].ok is None


def test_the_two_message_check_wants_a_reset_a_complete_first_message_and_the_tool_step() -> None:
    first, second = "TTaaaaaa0000 one two", "TTbbbbbb0000 three four"
    good = {"s": ["TTaaaaaa0000", first, "TTbbbbbb0000", second]}
    check = tt.check_two_message_previews(good, [first, second], "THINKING")
    assert (
        check["segments"] == 2 and check["first_message_complete"] and check["second_starts_fresh"] and check["final_equals_second_message"]
    )
    # No reset: the second message is appended to the first.
    appended = tt.check_two_message_previews({"s": [first, f"{first} {second}"]}, [first, second], "THINKING")
    assert not appended["second_starts_fresh"] and not appended["final_equals_second_message"]
    assert tt.preview_segments(["a", "a b", "c", "c d"]) == [["a", "a b"], ["c", "c d"]]


def test_expectations_come_from_the_ledger() -> None:
    expectations = tt.load_expectations()
    assert expectations[("claude", "helm")].live_reply_text == "expected"
    assert expectations[("antigravity", "helm")].live_reply_text == "expected"
    assert expectations[("cursor", "console")].live_reply_text == "not_measurable"
    assert expectations[("codex", "helm")].live_reply_text == "expected"
    cells = {s.key for s in tt.declared_cells(expectations)}
    assert {"codex/console", "pi/helm", "omp/helm", "opencode/console", "cursor/helm"} <= cells


# ---- scoping: a lane reads only its own cell's sessions ---------------------------------------------------------------


class _StubHost:
    """A Runtime Host with two sessions in two workspaces; only session A holds the marker."""

    def __init__(self, workspace_a: str, workspace_b: str) -> None:
        import http.server
        import json
        import threading

        listing = {
            "sessions": [{"id": "sess-a", "cwd": workspace_a}, {"id": "sess-b", "cwd": workspace_b}],
        }

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path.startswith("/api/agents/sessions?"):
                    body = json.dumps(listing)
                elif "sess-a" in self.path:
                    body = json.dumps({"events": [{"text": "TTabc1230000 from cell A"}]})
                else:
                    body = json.dumps({"events": [{"text": "nothing yet"}]})
                payload = body.encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()


def test_a_cells_lanes_never_read_another_cells_session(tmp_path) -> None:
    workspace_a, workspace_b = tmp_path / "a", tmp_path / "b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    host = _StubHost(str(workspace_a), str(workspace_b))
    try:
        cell_b = tt.RuntimeApi(host.url, "token", workspace=workspace_b)
        events_lane = cell_b.poll_lanes()[0]
        assert cell_b.session_ids() == ["sess-b"]
        assert "TTabc1230000" not in (events_lane.read() or "")  # cell A's marker is invisible to cell B
        cell_a = tt.RuntimeApi(host.url, "token", workspace=workspace_a)
        assert "TTabc1230000" in (cell_a.poll_lanes()[0].read() or "")
        pinned = tt.RuntimeApi(host.url, "token")  # no workspace and nothing pinned: reads nothing rather than everything
        assert pinned.session_ids() == []
    finally:
        host.close()


def test_the_tee_proxy_attributes_markers_to_the_request_that_carried_them_whatever_the_encoding() -> None:
    import gzip
    import socket
    import threading
    import time

    import zstandard

    received = bytearray()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)

    def upstream() -> None:
        conn, _ = listener.accept()
        while chunk := conn.recv(65536):
            received.extend(chunk)
        conn.close()

    threading.Thread(target=upstream, daemon=True).start()
    proxy = tt.TeeProxy(f"http://127.0.0.1:{listener.getsockname()[1]}")
    proxy.start()

    def request(path: str, body: bytes, encoding: str | None = None, chunked: bool = False) -> bytes:
        head = f"POST {path} HTTP/1.1\r\nHost: x\r\n" + (f"Content-Encoding: {encoding}\r\n" if encoding else "")
        if chunked:
            half = len(body) // 2
            parts = [body[:half], body[half:]]
            framed = b"".join(f"{len(p):x}\r\n".encode() + p + b"\r\n" for p in parts) + b"0\r\n\r\n"
            return (head + "Transfer-Encoding: chunked\r\n\r\n").encode() + framed
        return (head + f"Content-Length: {len(body)}\r\n\r\n").encode() + body

    try:
        client = socket.create_connection(("127.0.0.1", int(proxy.url.rsplit(":", 1)[1])))
        durable = request("/api/agents/ingest", b'{"t":"TTabc1230001"}')
        live = request("/api/agents/runtime/events/batch", b'{"live_text":"TTabc1230002 more"}')
        zstd = request("/api/agents/runtime/events/batch", zstandard.ZstdCompressor().compress(b'{"x":"TTabc1230003"}'), "zstd")
        gz = request("/api/agents/runtime/events/batch", gzip.compress(b'{"x":"TTabc1230004"}'), "gzip")
        chunked = request("/api/agents/runtime/events/batch", b'{"x":"TTabc1230005"}', chunked=True)
        client.sendall(durable)
        client.sendall(live[:30])  # a request split across TCP segments, and a marker split by the second send
        client.sendall(live[30:] + zstd + gz + chunked)
        client.close()
        wanted = {f"TTabc12300{n:02d}" for n in (2, 3, 4, 5)}
        deadline = time.monotonic() + 3
        while not wanted <= set(proxy.runtime_posts) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert wanted <= set(proxy.runtime_posts), proxy.runtime_posts  # plain, zstd, gzip and chunked bodies are all read
        assert "TTabc1230001" in proxy.other_posts and "TTabc1230001" not in proxy.runtime_posts  # the durable upload is not live text
        assert proxy.runtime_post_count == 4
        total = len(durable) + len(live) + len(zstd) + len(gz) + len(chunked)
        while len(received) < total and time.monotonic() < deadline + 3:
            time.sleep(0.02)
        assert bytes(received) == durable + live + zstd + gz + chunked  # bytes pass through unchanged
    finally:
        proxy.close()
        listener.close()


def test_a_providers_environment_never_inherits_the_callers_longhouse_hooks_or_secrets(monkeypatch, tmp_path) -> None:
    """A hook URL and token in the caller's shell would send a test provider's lifecycle events to a real Runtime Host."""

    leaks = {
        "LONGHOUSE_HOOK_URL": "https://real-host.example",
        "LONGHOUSE_HOOK_TOKEN": "real-token",
        "LONGHOUSE_MANAGED_SESSION_ID": "real-session",
        "LONGHOUSE_OWNER_INPUT_ENGINE": "/real/engine",
        "INFISICAL_TOKEN": "real-secret",
        "SSH_AUTH_SOCK": "/real/agent.sock",
        "CLAUDE_PID": "1234",
    }
    for name, value in leaks.items():
        monkeypatch.setenv(name, value)
    script = tmp_path / "script.json"
    script.write_text('{"turns":[{"id":"x","match":"x","reply":"y"}]}')
    for provider in tt.PROVIDERS:
        environment = tt.provider_environment(provider, "http://127.0.0.1:1", script, tmp_path / f"home-{provider}")
        assert not (set(leaks) & set(environment)), (provider, sorted(set(leaks) & set(environment)))
        assert environment["LONGHOUSE_SCRIPTED_MODEL_BASE_URL"] == "http://127.0.0.1:1"


# ---- safety properties of the gate: scoping on a shared host, ambient environment, informational cells ---------------------


class _TwoCellHost:
    """One Runtime Host holding two cells' sessions whose scripted replies differ. Each session serves only its own text."""

    def __init__(self, workspaces: dict[str, str], texts: dict[str, str]) -> None:
        import http.server
        import json
        import threading

        listing = {"sessions": [{"id": sid, "cwd": cwd} for sid, cwd in workspaces.items()]}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path.startswith("/api/agents/sessions?"):
                    return self._send(json.dumps(listing), "application/json")
                session = next((sid for sid in texts if sid in self.path), None)
                if session is None:
                    return self._send("{}", "application/json")
                if self.path.endswith("/workspace/stream"):
                    frame = {"session_id": session, "transcript_preview": {"text": texts[session]}}
                    return self._send(f"event: workspace\ndata: {json.dumps(frame)}\n\n", "text/event-stream")
                return self._send(json.dumps({"events": [{"text": texts[session]}]}), "application/json")

            def _send(self, body: str, kind: str) -> None:
                payload = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()


def test_two_cells_on_one_host_with_different_replies_are_never_credited_with_each_others_markers(tmp_path) -> None:
    import threading
    import time

    workspace_a, workspace_b = tmp_path / "a", tmp_path / "b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    markers_a = ["TTaaaaaa0000", "TTaaaaaa0001"]
    markers_b = ["TTbbbbbb0000", "TTbbbbbb0001"]
    host = _TwoCellHost(
        {"sess-a": str(workspace_a), "sess-b": str(workspace_b)},
        {"sess-a": " ".join(markers_a), "sess-b": " ".join(markers_b)},
    )
    try:
        for workspace, own, foreign in ((workspace_a, markers_a, markers_b), (workspace_b, markers_b, markers_a)):
            api = tt.RuntimeApi(host.url, "token", workspace=workspace)
            stop = threading.Event()
            watchers = [tt.LaneWatcher(lane, own + foreign, stop) for lane in api.poll_lanes()]
            stream = tt.StreamWatcher("sse", lambda api=api, stop=stop: api.stream_lines(stop), own + foreign, stop)
            watchers.append(stream)
            for watcher in watchers:
                watcher.start()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not all(set(own) <= set(w.first_seen) for w in watchers):
                time.sleep(0.05)
            stop.set()
            for watcher in watchers:
                watcher.join(timeout=5)
            for watcher in watchers:
                assert set(watcher.first_seen) == set(own), (watcher.lane.name, sorted(watcher.first_seen))  # all of its own, none foreign
            assert set(stream.previews) <= {"sess-a" if workspace == workspace_a else "sess-b"}
            assert api.known_sessions == {"sess-a" if workspace == workspace_a else "sess-b"}
    finally:
        host.close()


def test_every_cell_gets_its_own_nonce_in_every_marker_and_turn_match() -> None:
    from zerg.qa.scripted_model_server import parse_pacing

    pace = parse_pacing({"rate": 20, "tokens": 100, "marker_every": 20, "initial_delay": 1.0, "marker_prefix": "TT", "reasoning_tokens": 5})
    plans = [tt.plan_cell(tt.CellSpec(p, m), pace) for p in ("codex", "pi", "omp") for m in ("console", "helm")]
    assert len({p.nonce for p in plans}) == len(plans)
    for plan in plans:
        markers = [word for _, word, _ in tt.plan_schedule(plan)]
        assert markers and all(m.startswith(f"TT{plan.nonce}") for m in markers)
        assert plan.prompt.startswith(f"TTRUTH{plan.nonce}") and plan.turns[0]["match"] == f"TTRUTH{plan.nonce}"
        for other in plans:
            if other is not plan:
                assert not any(m.startswith(f"TT{other.nonce}") for m in markers)


def test_no_process_the_harness_starts_inherits_the_callers_longhouse_hooks_or_secrets(monkeypatch, tmp_path) -> None:
    import subprocess

    leaks = {
        "LONGHOUSE_HOOK_URL": "http://127.0.0.1:9/real",
        "LONGHOUSE_HOOK_TOKEN": "real-token",
        "LONGHOUSE_MANAGED_SESSION_ID": "real-session",
        "INFISICAL_TOKEN": "real-secret",
        "SSH_AUTH_SOCK": "/real/agent.sock",
    }
    for name, value in leaks.items():
        monkeypatch.setenv(name, value)
    assert not (set(leaks) & set(tt.clean_environ()))
    assert tt.clean_environ(PYTHONPATH="x")["PYTHONPATH"] == "x"

    # `--version` probes of a provider and of the engine
    probe = tmp_path / "probe"
    probe.write_text(
        '#!/bin/sh\necho "version hook=[$LONGHOUSE_HOOK_URL] token=[$LONGHOUSE_HOOK_TOKEN] sess=[$LONGHOUSE_MANAGED_SESSION_ID]"\n'
    )
    probe.chmod(0o755)
    assert tt._version(str(probe)) == "version hook=[] token=[] sess=[]"

    # the scripted model server and the scratch Runtime Host are started with a scrubbed environment too
    captured: list[dict[str, str]] = []

    class _Stop(Exception):
        pass

    def fake_popen(*args, **kwargs):
        captured.append(kwargs["env"])
        raise _Stop

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    model = tt.ScriptedModel(tmp_path, [{"id": "x", "match": "x", "reply": "y"}])
    host = tt.ScratchRuntimeHost(tmp_path)
    for starter in (model.start, host.start):
        try:
            starter()
        except _Stop:
            pass
    assert len(captured) == 2
    for environment in captured:
        assert not (set(leaks) & set(environment)), sorted(set(leaks) & set(environment))


def test_an_informational_cell_that_never_got_ready_is_a_failure_not_a_measurement() -> None:
    shadow = tt.Expectation("informational")
    assert tt.outcome_for("preflight", shadow, "shadow") == "fail"
    assert tt.outcome_for("stream_delivered", shadow, "shadow") == "measured"
    assert tt.outcome_for(None, shadow, "shadow") == "measured"


def test_a_cells_median_budget_comes_from_the_budget_file_and_falls_back_to_the_default(monkeypatch, tmp_path) -> None:
    from zerg.qa import terminal_truth_verdicts as verdicts

    path = tmp_path / "budget.json"
    path.write_text('{"budget_s": {"omp/helm": 1.5}}')
    monkeypatch.setattr(verdicts, "BUDGET_PATH", path)
    assert verdicts.median_budget_s("omp", "helm") == 1.5
    assert verdicts.median_budget_s("codex", "helm") == verdicts.MEDIAN_DELAY_BUDGET_S
    monkeypatch.setattr(verdicts, "BUDGET_PATH", tmp_path / "missing.json")
    assert verdicts.median_budget_s("omp", "helm") == verdicts.MEDIAN_DELAY_BUDGET_S
    # the stream check applies the cell's own budget: a 1.0 s median passes omp/helm at 1.5 s and fails the default
    monkeypatch.setattr(verdicts, "BUDGET_PATH", path)
    base = _good_observation()
    slow = {m: 11.0 + i for i, m in enumerate(base.markers)}
    for provider, expected in (("omp", True), ("codex", False)):
        obs = _good_observation(provider=provider, stream_first_seen=slow)
        medians = [c for c in verdicts.stream_checks(obs) if c[0] == "median_delay_within_budget"]
        assert medians[0][1] is expected, (provider, medians)
