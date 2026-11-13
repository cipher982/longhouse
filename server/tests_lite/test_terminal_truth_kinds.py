"""The live-kinds scenarios cannot pass vacuously: a scenario that did not happen fails its preflight checks."""

from __future__ import annotations

import http.client
import http.server
import json
import queue
import re
import threading
import time

import pytest

from zerg.qa import scripted_model_server as server_module
from zerg.qa import terminal_truth_kinds as kinds
from zerg.qa.terminal_truth_lanes import RuntimeApi
from zerg.qa.terminal_truth_lanes import TeeProxy
from zerg.qa.terminal_truth_verdicts import CellSpec

MARKER = TeeProxy.MARKER.pattern.decode()


def _plan(scenario: str, provider: str = "claude") -> kinds.KindPlan:
    return kinds.plan_kind_cell(CellSpec(provider, "console", scenario))


def _good(plan: kinds.KindPlan) -> kinds.KindObservation:
    obs = kinds.KindObservation(plan, ready_evidence="ready")
    obs.turns_served = dict.fromkeys(plan.expected_turn_ids, 1)
    obs.model_sent = {s.marker: 1.0 for s in plan.signals if s.group in plan.model_groups}
    obs.side = {s.marker: 1.0 for s in plan.signals if s.group in plan.side_file_groups}
    return obs


@pytest.mark.parametrize("scenario", kinds.SCENARIOS)
def test_every_signal_marker_is_unique_and_visible_to_the_wire_scanner(scenario: str) -> None:
    plan = _plan(scenario)
    markers = plan.markers
    assert markers and len(markers) == len(set(markers))
    assert all(re.fullmatch(MARKER, m) for m in markers), markers


@pytest.mark.parametrize("scenario", kinds.SCENARIOS)
def test_a_cell_where_everything_happened_passes_its_checks(scenario: str) -> None:
    plan = _plan(scenario)
    assert all(ok for _, ok, _ in kinds.judge_kinds(_good(plan)))


def test_a_model_that_never_emits_the_thinking_block_fails_preflight() -> None:
    plan = _plan("thinking")
    obs = _good(plan)
    for s in plan.group("thinking"):
        obs.model_sent[s.marker] = None  # the scripted model sent the reply but no reasoning
    failed = [n for n, ok, _ in kinds.judge_kinds(obs) if not ok]
    assert failed == ["model_emitted_every_marker"]


def test_a_tool_scenario_whose_result_never_came_back_fails() -> None:
    plan = _plan("tool")
    obs = _good(plan)
    obs.turns_served["claude/console:b"] = 0  # the model never saw the tool result carrying the last output line
    obs.side = {}
    failed = {n for n, ok, _ in kinds.judge_kinds(obs) if not ok}
    assert {"every_scripted_turn_served", "command_really_ran"} <= failed


def test_a_subagent_scenario_whose_subagent_never_asked_the_model_fails() -> None:
    plan = _plan("subagent")
    obs = _good(plan)
    obs.turns_served["claude/console:sub"] = 0
    assert "every_scripted_turn_served" in {n for n, ok, _ in kinds.judge_kinds(obs) if not ok}


def test_a_provider_without_a_subagent_tool_is_not_measured_with_a_reason() -> None:
    plan = _plan("subagent", "pi")
    assert plan.not_measured and not plan.turns


def test_a_cell_that_failed_to_start_fails_even_when_the_model_was_quiet() -> None:
    plan = _plan("thinking")
    obs = kinds.KindObservation(plan, preflight_error="RuntimeError: provider exited")
    assert not all(ok for _, ok, _ in kinds.judge_kinds(obs))


# ---- the scripted model: Anthropic thinking framing (the Claude Code retry defect) ----------------------------------------


def _serve(plan: kinds.KindPlan):
    turns, sha = (
        server_module.load_script_from_turns(plan.turns) if hasattr(server_module, "load_script_from_turns") else (plan.turns, None)
    )
    server = server_module.ThreadedServer(("127.0.0.1", 0), server_module.MockHandler, script_turns=turns, script_sha256=sha)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _anthropic_frames(server, prompt: str, *, thinking: bool) -> list[tuple[str, dict]]:
    body = {"model": "mock", "stream": True, "max_tokens": 100, "messages": [{"role": "user", "content": prompt}]}
    if thinking:
        body["thinking"] = {"type": "enabled", "budget_tokens": 1024}
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=60)
    connection.request("POST", "/v1/messages", json.dumps(body), {"Content-Type": "application/json"})
    raw = connection.getresponse().read().decode()
    frames = []
    for block in raw.split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "data" in lines:
            frames.append((lines.get("event", ""), json.loads(lines["data"])))
    return frames


def test_the_thinking_block_is_framed_so_every_block_opens_and_closes_once_at_its_own_index() -> None:
    plan = kinds.plan_kind_cell(CellSpec("claude", "console", "thinking"))
    fast = dict(plan.turns[0]["pace"], initial_delay=0.4, reasoning_tokens=3)  # the framing, not the 8 s pacing
    plan.turns[0]["pace"] = fast
    server, thread = _serve(plan)
    try:
        frames = _anthropic_frames(server, f"TTRUTH{plan.nonce} go", thinking=True)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    starts = [f["index"] for e, f in frames if e == "content_block_start"]
    stops = [f["index"] for e, f in frames if e == "content_block_stop"]
    assert starts == [0, 1] and stops == [0, 1], (starts, stops)  # the text block used to be stopped as index 0 (twice), never as 1
    kinds_by_index = {f["index"]: f["content_block"]["type"] for e, f in frames if e == "content_block_start"}
    assert kinds_by_index == {0: "thinking", 1: "text"}
    thinking = "".join(f["delta"].get("thinking", "") for e, f in frames if e == "content_block_delta")
    assert all(f"TT{fast['reasoning_prefix'][2:]}000{i}" in thinking for i in range(3))


def test_a_request_that_did_not_ask_for_thinking_gets_none_so_the_preflight_catches_it() -> None:
    plan = kinds.plan_kind_cell(CellSpec("claude", "console", "thinking"))
    plan.turns[0]["pace"] = dict(plan.turns[0]["pace"], initial_delay=0.2, reasoning_tokens=3)
    server, thread = _serve(plan)
    try:
        frames = _anthropic_frames(server, f"TTRUTH{plan.nonce} go", thinking=False)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert not any(f.get("content_block", {}).get("type") == "thinking" for _e, f in frames)


@pytest.mark.parametrize(("scenario", "mutation"), [("thinking", "no_thinking"), ("tool", "no_tool"), ("subagent", "no_subagent")])
def test_each_mutation_breaks_its_scenario_and_the_checks_notice(scenario: str, mutation: str) -> None:
    plan = kinds.plan_kind_cell(CellSpec("claude", "console", scenario), mutation)
    # What a run of the mutated script produces: the model wrote only what it was scripted to write, and the turns that
    # needed the skipped step never arrived.
    obs = _good(plan)
    if mutation == "no_thinking":
        assert plan.turns[0]["pace"]["reasoning_tokens"] == 0
        for s in plan.group("thinking"):
            obs.model_sent[s.marker] = None
    else:
        assert not any("tool_call" in t for t in plan.turns if t["id"].endswith(":a"))
        obs.turns_served[plan.expected_turn_ids[-1]] = 0  # the tool result or subagent text the next turn matches on never came back
    assert not all(ok for _, ok, _ in kinds.judge_kinds(obs))


# ---- scripted model server: what the new scenarios rely on ---------------------------------------------------------------


def test_any_input_scope_finds_a_gemini_tool_result_that_arrives_in_a_model_role_content() -> None:
    payload = {
        "contents": [
            {"role": "user", "parts": [{"text": "TTRUTHabc123 go"}]},
            {"role": "model", "parts": [{"functionCall": {"name": "run_command", "args": {"CommandLine": "echo hi"}}}]},
            {"role": "model", "parts": [{"functionResponse": {"name": "run_command", "response": {"output": "TTfeed000006"}}}]},
            {"role": "user", "parts": [{"text": "<ADDITIONAL_METADATA>appended context</ADDITIONAL_METADATA>"}]},
        ]
    }
    text = server_module._all_input_text(payload)
    assert "TTfeed000006" in text and "TTRUTHabc123" in text
    assert "TTfeed000006" not in server_module._latest_input_text(
        payload
    )  # why the scope exists: the latest message is the appended context
    turns = [{"id": "b", "match": "TTfeed000006", "match_scope": "any_input", "reply": "done"}]
    assert server_module._scripted_plan(payload, turns).turn_id == "b"


def test_a_responses_tool_offered_inside_a_namespace_is_found_and_called_with_its_namespace() -> None:
    payload = {
        "tools": [
            {"type": "function", "name": "exec_command", "parameters": {}},
            {
                "type": "namespace",
                "name": "multi_agent_v1",
                "tools": [{"type": "function", "name": "spawn_agent", "parameters": {"properties": {}}}],
            },
        ]
    }
    assert "spawn_agent" in {name for name, _ in server_module._declared_tools(payload)}
    assert server_module._tool_namespace(payload, "spawn_agent") == "multi_agent_v1"
    assert server_module._tool_namespace(payload, "exec_command") is None


def test_reasoning_prefix_words_are_unique_to_the_cell_and_the_default_words_are_unchanged() -> None:
    cell = server_module.parse_pacing({"rate": 5, "initial_delay": 4, "reasoning_tokens": 2, "reasoning_prefix": "TTabc123"})
    assert [w for w, _ in server_module.reasoning_chunks(cell)] == ["TTabc1230000 ", "TTabc1230001 "]
    legacy = server_module.parse_pacing({"rate": 5, "initial_delay": 4, "reasoning_tokens": 2})
    assert [w for w, _ in server_module.reasoning_chunks(legacy)] == ["THINKING000 ", "THINKING001 "]
    with pytest.raises(ValueError):
        server_module.parse_pacing({"rate": 5, "reasoning_prefix": "bad prefix"})


# ---- the prompt waits for the stream to be subscribed ------------------------------------------------------------------------


class _SseHandler(http.server.BaseHTTPRequestHandler):
    """A host that sends ``connected`` at once and its first ``workspace_changed`` (the subscription) only when released."""

    release = threading.Event()

    def do_GET(self) -> None:  # noqa: N802 - http.server's name
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(b"event: connected\r\ndata: {}\r\n\r\n")
        self.wfile.flush()
        self.release.wait(5)
        self.wfile.write(b'event: workspace_changed\r\ndata: {"session_id": "s1"}\r\n\r\n')
        self.wfile.flush()

    def log_message(self, *_args: object) -> None:
        pass


def test_the_prompt_waits_for_the_stream_subscription_not_for_the_connection() -> None:
    """2026-11-12: a subagent start published 50 ms before the stream subscribed went to no subscriber and read as a host drop."""

    _SseHandler.release.clear()
    host = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SseHandler)
    threading.Thread(target=host.serve_forever, daemon=True).start()
    stop = threading.Event()
    try:
        api = RuntimeApi(f"http://127.0.0.1:{host.server_address[1]}", "token")
        api.pinned_sessions = ["s1"]
        lines: queue.Queue = queue.Queue()
        threading.Thread(target=api._stream_one, args=("s1", lines, stop), daemon=True).start()
        assert "connected" in lines.get(timeout=5)  # the connection is up and the host has said so
        opened, wanted, waited = kinds._wait_for_open_streams(api, timeout=0.3)
        assert (opened, wanted) == (0, 1) and waited >= 0.3, "connected alone must not count as subscribed"

        threading.Timer(0.2, _SseHandler.release.set).start()
        opened, wanted, waited = kinds._wait_for_open_streams(api, timeout=5)
        assert (opened, wanted) == (1, 1) and 0.15 <= waited < 3
    finally:
        stop.set()
        _SseHandler.release.set()
        host.shutdown()
        host.server_close()


def test_a_helm_reply_cell_holds_its_prompt_until_its_session_is_listed_and_subscribed() -> None:
    """Runner, 2026-11-13: omp/helm alone on its own host was listed 9.9 s after launch; the prompt went first and the cell read late."""

    _SseHandler.release.clear()
    host = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SseHandler)
    threading.Thread(target=host.serve_forever, daemon=True).start()
    stop = threading.Event()
    try:
        api = RuntimeApi(f"http://127.0.0.1:{host.server_address[1]}", "token")
        assert api.wait_for_session_stream(timeout=0.2) == (None, None)  # no session listed yet: it waits the whole time

        def appear() -> None:
            api.pinned_sessions = ["s1"]  # the host lists the session now
            threading.Thread(target=api._stream_one, args=("s1", queue.Queue(), stop), daemon=True).start()
            threading.Timer(0.2, _SseHandler.release.set).start()

        threading.Timer(0.2, appear).start()
        listed, subscribed = api.wait_for_session_stream(timeout=5)
        assert listed is not None and subscribed is not None
        assert 0.15 <= listed < subscribed < 3, "listed first, subscribed when the host said so"
    finally:
        stop.set()
        _SseHandler.release.set()
        host.shutdown()
        host.server_close()


def test_a_cell_with_no_session_yet_does_not_wait() -> None:
    api = RuntimeApi("http://127.0.0.1:9", "token")  # no pinned session and no workspace: nothing is listed, no request is made
    assert kinds._wait_for_open_streams(api, timeout=5)[:2] == (0, 0)


# ---- the proxy's added engine-to-host delay -----------------------------------------------------------------------------------


class _EchoHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - http.server's name
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


def test_the_proxy_delays_each_way_and_keeps_every_byte_in_order() -> None:
    host = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _EchoHandler)
    threading.Thread(target=host.serve_forever, daemon=True).start()
    proxy = TeeProxy(f"http://127.0.0.1:{host.server_address[1]}")
    proxy.start()
    try:
        port = int(proxy.url.rsplit(":", 1)[1])

        def round_trip(body: bytes) -> tuple[float, bytes]:
            started = time.monotonic()
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                connection.request("POST", "/api/agents/runtime/events", body)
                echoed = connection.getresponse().read()
                return time.monotonic() - started, echoed
            finally:
                connection.close()

        body = b"TTabcdef0001 " * 20000  # larger than one recv, so several chunks ride the delay
        direct_s, echoed = round_trip(body)
        assert echoed == body and direct_s < 0.15

        proxy.delay_s = 0.1
        delayed_s, echoed = round_trip(body)
        assert echoed == body, "a delayed chunk must not be lost or reordered"
        assert 0.2 <= delayed_s < 1.5, f"one delay each way, not one per chunk: {delayed_s:.2f} s"
        assert "TTabcdef0001" in proxy.runtime_posts, "the engine's post is still noted, at its own send time"
    finally:
        proxy.close()
        host.shutdown()
        host.server_close()


def _state(sid: str, headline: str, detail: str | None = None) -> dict:
    return {"session_id": sid, "headline": headline, "detail": detail}


def test_a_state_served_with_no_wake_is_unshown_for_as_long_as_it_is_served():
    """2026-11-12, Antigravity Helm: the host served "Using run_command" and the page said "Thinking" until the tool ended."""
    from zerg.qa.terminal_truth_lanes import woken_view_lags

    polled = [
        (0.0, _state("s", "Idle")),
        (10.4, _state("s", "Thinking")),
        (11.4, _state("s", "Using run_command")),
        (16.6, _state("s", "Thinking")),
        (17.6, _state("s", "Idle")),
    ]
    # Woken at the runtime event (state still "Thinking"), then not again until the tool's result.
    woken = [
        (0.1, _state("s", "Idle")),
        (10.5, _state("s", "Thinking")),
        (10.7, _state("s", "Thinking")),
        (16.7, _state("s", "Thinking")),
        (17.0, _state("s", "Thinking")),
    ]
    lags = {lag["headline"] + str(i): lag for i, lag in enumerate(woken_view_lags(polled, woken, since=10.0, end_t=22.0))}

    assert lags["Thinking0"]["shown"] and lags["Thinking0"]["lag_s"] == 0.1
    assert not lags["Using run_command1"]["shown"] and lags["Using run_command1"]["lag_s"] == 5.2
    assert lags["Thinking2"]["shown"] and lags["Thinking2"]["lag_s"] == 0.1
    # The turn is over and nothing wakes the page: "Thinking" stays up until the run stops looking.
    assert not lags["Idle3"]["shown"] and lags["Idle3"]["lag_s"] == 4.4


def test_a_state_announced_when_it_is_served_is_shown_at_once_and_a_blip_is_not_a_finding():
    from zerg.qa.terminal_truth_lanes import woken_view_lags

    polled = [
        (0.0, _state("s", "Idle")),
        (10.4, _state("s", "Thinking")),
        (10.5, _state("s", "Using Bash")),
        (16.6, _state("s", "Idle")),
        (9.0, _state("other", "Idle")),
    ]
    # The reader read "Using Bash" a tick before the poller did, and never read the 0.1 s "Thinking" at all.
    woken = [
        (0.1, _state("s", "Idle")),
        (10.45, _state("s", "Using Bash")),
        (16.65, _state("s", "Idle")),
        (16.7, _state("other", "Thinking")),
    ]
    lags = woken_view_lags(polled, woken, since=10.0, end_t=22.0)

    assert [(lag["headline"], lag["shown"], lag["lag_s"]) for lag in lags] == [
        ("Thinking", False, 0.1),
        ("Using Bash", True, 0.0),
        ("Idle", True, 0.05),
    ]
    # Before the prompt nothing is judged, and one session's reads never stand in for another's.
    assert all(lag["session_id"] == "s" for lag in lags)


def test_the_woken_reader_reads_once_per_wake_and_once_more_for_a_wake_that_lands_mid_read():
    import threading as _threading

    from zerg.qa.terminal_truth_lanes import WokenStateReader

    release = _threading.Event()
    started = _threading.Event()
    bodies = iter(["Thinking", "Using Bash", "Idle"])

    class _Api:
        calls = 0

        def get(self, path, *, browser=False, timeout=10):
            _Api.calls += 1
            started.set()
            if _Api.calls == 1:
                release.wait(5)
            label = next(bodies)
            return 200, json.dumps(
                {"session": {"session_state": {"activity": {"state": "x"}, "presentation": {"primary": {"label": label}}}}}
            )

    stop = _threading.Event()
    reader = WokenStateReader(_Api(), stop)
    reader.start()
    try:
        reader.wake("s")
        assert started.wait(5)
        reader.wake("s")  # lands during the first read: one more read, however many wakes
        reader.wake("s")
        release.set()
        deadline = time.monotonic() + 5
        while reader.reads < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.3)
    finally:
        stop.set()
        reader.join(timeout=5)

    assert (reader.wakes, reader.reads) == (3, 2)
    assert [snap["headline"] for _, snap in reader.timeline] == ["Thinking", "Using Bash"]


def _thinking_result(frames: list[dict]) -> dict:
    """An OpenCode Console thinking cell whose two reasoning markers reached the engine post and the stream (no terminal reference)."""

    signals = [
        {"name": f"thinking {i}", "group": "thinking", "marker": marker, "reference": None, "engine_runtime_post": 1.0 + i, "sse": 1.1 + i,
         "terminal_any": None, "terminal_default_view": None, "provider_file": None, "events": None, "workspace": None}
        for i, marker in enumerate(("TTKa0001", "TTKa0002"))
    ]  # fmt: skip
    return {"cell": "opencode/console", "scenario": "thinking", "outcome": "measured", "signals": signals, "sse_frames": frames}


def test_reasoning_that_also_arrives_as_reply_text_fails_the_published_kind_check_at_kind() -> None:
    """Linux runner, 2026-11-13: deltas previewed as reply text, the part snapshot as thinking, and the check read delivered."""

    from zerg.qa.terminal_truth_kinds import kinds_expectation

    thinking = {"kind": "thinking", "text_tail": "weighing it TTKa0001 TTKa0002"}
    reply = {"kind": "reply", "text_tail": "the answer is TTKr0001"}
    clean = kinds_expectation(_thinking_result([thinking, reply]))
    assert clean is not None and clean["ok"] and clean["thinking_markers_in_reply_frames"] == 0

    mislabelled = {"kind": "reply", "text_tail": "weighing it TTKa0001"}
    both = kinds_expectation(_thinking_result([mislabelled, thinking, reply]))
    assert both is not None and (both["ok"], both["stage"], both["thinking_markers_in_reply_frames"]) == (False, "kind", 1)

    only_as_reply = kinds_expectation(_thinking_result([mislabelled, reply]))
    assert only_as_reply is not None and (only_as_reply["ok"], only_as_reply["stage"]) == (False, "kind")
