"""The live-kinds scenarios cannot pass vacuously: a scenario that did not happen fails its preflight checks."""

from __future__ import annotations

import http.client
import json
import re
import threading

import pytest

from zerg.qa import scripted_model_server as server_module
from zerg.qa import terminal_truth_kinds as kinds
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
