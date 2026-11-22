"""The Antigravity scripted lane answers each request of the Console lifecycle the way a model would.

The fixture is matched against the Gemini-shaped requests agy sends: one conversation whose history keeps every earlier
prompt, a system message appended after the user's own, and tool results returned as user-role function responses
(shapes from the 2026-11-15 bench receipts). The lifecycle's markers are `LH_ANTIGRAVITY_<STEP>_<32 hex>`.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from zerg.qa import antigravity_scripted_lane as lane
from zerg.qa import scripted_model_server as server

SHELL = {"type": "object", "properties": {"CommandLine": {"type": "string"}}, "required": ["CommandLine"]}
TOOLS = [{"functionDeclarations": [{"name": "run_command", "parametersJsonSchema": SHELL}]}]
SYSTEM = {
    "role": "user",
    "parts": [{"text": "The following is a <SYSTEM_MESSAGE> not actually sent by the user. Ephemeral context."}],
}


def _user(text: str) -> dict:
    return {
        "role": "user",
        "parts": [{"text": f"<USER_REQUEST>\n{text}\n</USER_REQUEST>\n<ADDITIONAL_METADATA>time</ADDITIONAL_METADATA>"}],
    }


def _model(text: str) -> dict:
    return {"role": "model", "parts": [{"text": text}]}


def _call(command: str) -> dict:
    return {"role": "model", "parts": [{"functionCall": {"name": "run_command", "args": {"CommandLine": command}}}]}


def _result(output: str) -> dict:
    return {"role": "user", "parts": [{"functionResponse": {"name": "run_command", "response": {"output": output}}}]}


def _plan(turns: list[dict], contents: list[dict]):
    plan = server._scripted_plan({"contents": contents, "tools": TOOLS}, turns)
    assert plan is not None, "no fixture turn matched the request"
    return plan


@pytest.fixture
def turns():
    tag = uuid4().hex
    return tag, script_turns(tag)


def script_turns(tag: str) -> list[dict]:
    return lane.script(tag)["turns"]


def _hex() -> str:
    return uuid4().hex


def test_the_whole_lifecycle_is_answered_turn_by_turn(turns):
    tag, fixture = turns
    console, resume = f"LH_ANTIGRAVITY_CONSOLE_{_hex()}", f"LH_ANTIGRAVITY_RESUME_{_hex()}"
    first, steered = f"LH_ANTIGRAVITY_STEER_FIRST_{tag}", f"LH_ANTIGRAVITY_STEERED_{tag}"
    interrupt, post = f"LH_ANTIGRAVITY_INTERRUPT_{_hex()}", f"LH_ANTIGRAVITY_POST_INTERRUPT_{_hex()}"
    steer_prompt = (
        f"Use the shell tool to run `sleep 15 && echo LH_ANTIGRAVITY_STEER_TOOL_$((40+2))_{tag}` in the foreground, "
        f"then reply with exactly {first} and nothing else."
    )
    steer_text = f"Change of plan: when the command finishes, reply with exactly {steered} and nothing else."
    tool_command = f"sleep 15 && echo LH_ANTIGRAVITY_STEER_TOOL_$((40+2))_{tag}"

    history: list[dict] = [_user(f"Reply with exactly {console} and nothing else.")]
    plan = _plan(fixture, [*history, SYSTEM])
    assert (plan.turn_id, plan.text) == ("console", console)
    history.append(_model(console))

    history.append(_user(f"Reply with exactly {resume} and nothing else."))
    plan = _plan(fixture, [*history, SYSTEM])
    assert (plan.turn_id, plan.text) == ("resume", resume)
    history.append(_model(resume))

    # The steer turn: a tool call first, with the tag the fixture was written for.
    history.append(_user(steer_prompt))
    plan = _plan(fixture, history)
    assert plan.turn_id == "steer-first" and plan.tool_call is not None
    name, args = plan.tool_call
    assert name == "run_command" and args["CommandLine"] == tool_command
    history.append(_call(tool_command))

    # Not steered: the tool result is answered with the first marker.
    unsteered = [*history, _result(f"LH_ANTIGRAVITY_STEER_TOOL_42_{tag}\n"), SYSTEM]
    plan = _plan(fixture, unsteered)
    assert (plan.turn_id, plan.text) == ("steer-tool-result", first)

    # Steered mid-tool, result not back yet: wait for the command, never answer ahead of it.
    steered_history = [*history, _user(steer_text)]
    plan = _plan(fixture, steered_history)
    assert plan.turn_id == "steered" and plan.text is None and plan.tool_call is not None
    assert plan.tool_call[1]["CommandLine"] == "sleep 5 && echo AGY_STEER_WAIT"
    # The result arrived: the turn ends on the steer's marker.
    plan = _plan(fixture, [*steered_history, _result(f"LH_ANTIGRAVITY_STEER_TOOL_42_{tag}\n"), SYSTEM])
    assert (plan.turn_id, plan.text) == ("steer-tool-result", steered)
    history = [*steered_history, _model(steered)]

    history.append(_user(f"Use the shell tool to run `sleep 8` in the foreground, then reply with exactly {interrupt} and nothing else."))
    plan = _plan(fixture, history)
    assert plan.turn_id == "interrupt" and plan.tool_call is not None
    assert plan.tool_call[1]["CommandLine"] == "sleep 8 && echo AGY_INTERRUPT_TOOL_DONE"
    history.append(_call("sleep 8 && echo AGY_INTERRUPT_TOOL_DONE"))

    # If the interrupted tool did return, the turn would end with the interrupt marker.
    plan = _plan(fixture, [*history, _result("AGY_INTERRUPT_TOOL_DONE\n")])
    assert (plan.turn_id, plan.text) == ("interrupt-tool-result", interrupt)

    history.append(_user(f"The previous request was cancelled: do not run it. Reply with exactly {post} and nothing else."))
    plan = _plan(fixture, [*history, SYSTEM])
    assert (plan.turn_id, plan.text) == ("post-interrupt", post)


def test_every_expected_turn_exists_and_the_fixture_loads(tmp_path, turns):
    tag, fixture = turns
    assert {turn["id"] for turn in fixture} == set(lane.EXPECTED_TURNS) | {"interrupt-tool-result"}  # no search markers
    path = tmp_path / "script.json"
    path.write_text(json.dumps({"turns": fixture}), encoding="utf-8")
    loaded, digest = server.load_script(path)
    assert len(loaded) == len(fixture) and len(digest) == 64


def test_the_steer_step_uses_the_fixtures_tag(monkeypatch):
    monkeypatch.setattr(lane, "_steer_tag", "a" * 32)
    assert lane.steer_tag() == "a" * 32
    monkeypatch.setattr(lane, "_steer_tag", None)
    assert lane.steer_tag() is None


def test_lifecycle_markers_are_recognised_as_scripted_input_markers():
    marker = f"LH_ANTIGRAVITY_POST_INTERRUPT_{_hex()}"
    assert server.SCRIPTED_INPUT_MARKER.search(f"Reply with exactly {marker} and nothing else.").group() == marker


def test_start_puts_the_provider_binary_back_after_the_profile_scrubs_it(tmp_path):
    # The Machine Agent advertises antigravity.turn_start only when it finds LONGHOUSE_ANTIGRAVITY_BIN (or agy on
    # PATH), and the profile scrubs that name as an ambient wrapper path. Without the put-back every Console cell
    # failed with `adapter_unavailable` (2026-11-15).
    home = tmp_path / "home"
    home.mkdir()
    binary = tmp_path / "provider"
    env = {"LONGHOUSE_ANTIGRAVITY_BIN": "/Users/someone/.local/bin/agy-wrapper"}
    scripted = lane.start(tmp_path, home, env, provider_bin=binary)
    try:
        assert env["LONGHOUSE_ANTIGRAVITY_BIN"] == str(binary)
    finally:
        scripted.close()
    bare: dict[str, str] = {"LONGHOUSE_ANTIGRAVITY_BIN": "/ambient/agy"}
    scripted = lane.start(tmp_path, home, bare)
    try:
        assert "LONGHOUSE_ANTIGRAVITY_BIN" not in bare
    finally:
        scripted.close()
