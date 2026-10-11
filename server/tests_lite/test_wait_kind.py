"""A wait that expires says what it was waiting on.

For each producer family the same pair of fakes: one in which the provider's
turn stalls, one in which the provider answers and Longhouse never serves the
answer. The two must declare different kinds, read from what the provider
itself recorded, never from the wording of the timeout.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from zerg.qa import claude_helm_lifecycle
from zerg.qa import claude_live_session_support
from zerg.qa import codex_helm_launch_visibility
from zerg.qa import codex_native_resume
from zerg.qa import codex_provider_release_canary
from zerg.qa import cursor_coordination_producer
from zerg.qa import cursor_helm_product_e2e
from zerg.qa import opencode_helm_lifecycle
from zerg.qa import provider_console_lifecycle
from zerg.qa import provider_native_resume
from zerg.qa import wait_kind
from zerg.qa.wait_kind import LONGHOUSE_ENGINE
from zerg.qa.wait_kind import LONGHOUSE_INGEST
from zerg.qa.wait_kind import PROVIDER_PROCESS
from zerg.qa.wait_kind import PROVIDER_TURN
from zerg.qa.wait_kind import RUNTIME_HOST
from zerg.qa.wait_kind import expired
from zerg.qa.wait_kind import failure_fields
from zerg.qa.wait_kind import waited_on

MARKER = "LH_FIXTURE_REPLY_abc"


# --- the declaration itself ---------------------------------------------------


def test_an_expired_wait_keeps_its_type_and_message_and_carries_its_kind() -> None:
    error = expired(TimeoutError("timed out waiting for the reply"), PROVIDER_TURN)

    assert type(error) is TimeoutError
    assert str(error) == "timed out waiting for the reply"
    assert waited_on(error) == PROVIDER_TURN
    assert failure_fields(error) == {"waited_on": "provider_turn"}
    assert wait_kind.FIELD == "waited_on"
    assert set(wait_kind.WAIT_KINDS) == {PROVIDER_TURN, PROVIDER_PROCESS, LONGHOUSE_ENGINE, LONGHOUSE_INGEST, RUNTIME_HOST}


def test_a_kind_outside_the_vocabulary_cannot_be_declared() -> None:
    with pytest.raises(ValueError, match="unknown wait kind"):
        expired(RuntimeError("x"), "somewhere_else")
    # A stray attribute is not a declaration either.
    stray = RuntimeError("x")
    stray.waited_on = "somewhere_else"  # type: ignore[attr-defined]
    assert waited_on(stray) is None
    assert failure_fields(RuntimeError("not a wait")) == {}
    assert failure_fields(None) == {}


def test_one_writer_puts_a_declaration_in_an_artifact_and_refuses_anything_else() -> None:
    assert wait_kind.declared(None) == {}
    assert wait_kind.declared(PROVIDER_TURN) == {"waited_on": "provider_turn"}
    for stray in ("", "somewhere_else", "Provider_Turn", 1, True, ["provider_turn"], {"waited_on": "provider_turn"}):
        with pytest.raises(ValueError, match="unknown wait kind"):
            wait_kind.declared(stray)


def test_no_producer_writes_the_field_except_through_that_writer() -> None:
    """A kind read from another artifact, or built by hand, is validated where it is handed on."""

    qa = Path(wait_kind.__file__).parent
    hand_written = re.compile(
        r"""["']waited_on["']\s*:|\{\s*(?:WAITED_ON_)?FIELD\s*:|\[(?:WAITED_ON_)?FIELD\]\s*=|\[["']waited_on["']\]\s*="""
    )
    offenders = {
        path.name: [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if hand_written.search(line)]
        for path in sorted(qa.rglob("*.py"))
        if path.name != "wait_kind.py"
    }
    assert {name: lines for name, lines in offenders.items() if lines} == {}


def test_the_kind_follows_an_explicit_cause_and_never_an_incidental_context() -> None:
    stall = expired(RuntimeError("timed out waiting for the reply"), PROVIDER_TURN)

    # A producer that adds diagnostics re-raises from the wait: still that wait.
    try:
        try:
            raise stall
        except RuntimeError as exc:
            raise RuntimeError(f"{exc}; diagnostic={{}}") from exc
    except RuntimeError as wrapped:
        assert waited_on(wrapped) == PROVIDER_TURN

    # An error raised while the wait was being handled is a different failure.
    try:
        try:
            raise stall
        except RuntimeError:
            raise RuntimeError("cleanup could not remove the workspace")  # noqa: B904 - the point of the test
    except RuntimeError as unrelated:
        assert unrelated.__context__ is stall
        assert waited_on(unrelated) is None


# --- OpenCode: the provider's own message store -------------------------------


def _opencode_rows(*rows: dict[str, object]) -> list[dict[str, object]]:
    return [{"tool_commands": [], "finish": None, "error": None, "parent_id": None, "text": "", **row} for row in rows]


def test_opencode_waits_on_the_providers_turn_once_the_provider_holds_the_prompt(monkeypatch) -> None:
    prompt = f"Reply with exactly {MARKER} and nothing else."
    held = _opencode_rows({"id": "u1", "role": "user", "text": prompt}, {"id": "a1", "role": "assistant", "parent_id": "u1"})
    monkeypatch.setattr(opencode_helm_lifecycle, "_messages", lambda _state: [])
    monkeypatch.setattr(opencode_helm_lifecycle, "_session_busy", lambda _state: True)

    # OpenCode took the prompt and its turn never finished.
    monkeypatch.setattr(opencode_helm_lifecycle, "_summarize", lambda _messages: held)
    with pytest.raises(TimeoutError, match="timed out waiting for assistant answer") as stalled:
        opencode_helm_lifecycle._wait_marker_answer({}, prompt, MARKER, timeout=0.6)
    assert waited_on(stalled.value) == PROVIDER_TURN
    with pytest.raises(TimeoutError, match="first tool call while the turn is busy") as no_tool:
        opencode_helm_lifecycle._wait_first_tool_completed({}, prompt, timeout=0.6)
    assert waited_on(no_tool.value) == PROVIDER_TURN

    # The prompt never reached OpenCode: Longhouse or OpenCode dropped it, and
    # the message store cannot say which.
    monkeypatch.setattr(opencode_helm_lifecycle, "_summarize", lambda _messages: _opencode_rows())
    with pytest.raises(TimeoutError) as undelivered:
        opencode_helm_lifecycle._wait_marker_answer({}, prompt, MARKER, timeout=0.6)
    assert waited_on(undelivered.value) is None

    # The turn ended without what was asked, or in the provider's own error: a result, not a stall.
    monkeypatch.setattr(opencode_helm_lifecycle, "_session_busy", lambda _state: False)
    for ending in ({"finish": "stop", "text": "I would rather not."}, {"error": "ProviderAuthError"}):
        ended = _opencode_rows({"id": "u1", "role": "user", "text": prompt}, {"id": "a1", "role": "assistant", "parent_id": "u1", **ending})
        monkeypatch.setattr(opencode_helm_lifecycle, "_summarize", lambda _messages, ended=ended: ended)
        with pytest.raises(TimeoutError) as over:
            opencode_helm_lifecycle._wait_marker_answer({}, prompt, MARKER, timeout=0.6)
        assert waited_on(over.value) is None
        with pytest.raises(TimeoutError) as no_tool_turn:
            opencode_helm_lifecycle._wait_first_tool_completed({}, prompt, timeout=0.6)
        assert waited_on(no_tool_turn.value) is None


# --- Claude: its own transcript ------------------------------------------------


def _claude_user(text: str) -> dict[str, object]:
    return {"type": "user", "message": {"role": "user", "content": text}}


def _claude_assistant(text: str) -> dict[str, object]:
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


_CLAUDE_TURN_END = {"type": "system", "subtype": "turn_duration"}


def test_a_claude_send_never_archived_says_which_side_stopped() -> None:
    prompt = f"Reply with exactly {MARKER}"

    # Claude took the prompt and its turn is still open at the deadline.
    stalled = [_claude_user(prompt)]
    assert claude_helm_lifecycle.unanswered_send_waited_on(stalled, marker=MARKER, prompt=prompt, outcome="pending") == PROVIDER_TURN

    # Claude answered in its own transcript; the hosted archive never showed it.
    answered = [_claude_user(prompt), _claude_assistant(MARKER), _CLAUDE_TURN_END]
    assert claude_helm_lifecycle.unanswered_send_waited_on(answered, marker=MARKER, prompt=prompt, outcome="declined") == LONGHOUSE_INGEST

    # A turn that ended without the marker is a decline, not a wait.
    declined = [_claude_user(prompt), _claude_assistant("I will not repeat that token."), _CLAUDE_TURN_END]
    assert claude_helm_lifecycle.unanswered_send_waited_on(declined, marker=MARKER, prompt=prompt, outcome="declined") is None

    # The prompt never reached Claude's transcript: not attributable from here.
    assert claude_helm_lifecycle.unanswered_send_waited_on([], marker=MARKER, prompt=prompt, outcome="pending") is None


def test_the_claude_wait_helper_declares_what_its_caller_says_and_nothing_otherwise(monkeypatch) -> None:
    with pytest.raises(claude_live_session_support.ScenarioError, match="timed out waiting for the end of the turn") as stalled:
        claude_live_session_support.wait_until(lambda: None, timeout=0.2, description="the end of the turn", waited_on=PROVIDER_TURN)
    assert waited_on(stalled.value) == PROVIDER_TURN

    with pytest.raises(claude_live_session_support.ScenarioError) as decided_late:
        claude_live_session_support.wait_until(lambda: None, timeout=0.2, description="a reply", waited_on=lambda: LONGHOUSE_INGEST)
    assert waited_on(decided_late.value) == LONGHOUSE_INGEST

    with pytest.raises(claude_live_session_support.ScenarioError) as undeclared:
        claude_live_session_support.wait_until(lambda: None, timeout=0.2, description="channel readiness")
    assert waited_on(undeclared.value) is None

    # The marker in Claude's own transcript, after a turn the caller typed itself:
    # the model's turn only while that transcript holds the prompt and its turn is open.
    monkeypatch.setattr(claude_live_session_support, "assistant_transcript_contains", lambda *_a, **_k: (False, None, None, None))
    prompt = _claude_user(f"Reply with exactly {MARKER}")
    ended = {"type": "system", "subtype": "turn_duration"}
    for rows, expected in (([prompt], PROVIDER_TURN), ([], None), ([prompt, ended], None)):
        monkeypatch.setattr(claude_live_session_support, "transcript_rows", lambda *_a, rows=rows: rows)
        with pytest.raises(claude_live_session_support.ScenarioError) as no_marker:
            claude_live_session_support.await_assistant_marker(session_id="s", marker=MARKER, timeout=0.2)
        assert waited_on(no_marker.value) == expected


def test_a_claude_turn_wait_is_on_the_model_only_while_claude_holds_the_prompt_and_the_turn_is_open() -> None:
    prompt = _claude_user("Run lh_claude_step_abc_1 then the others")
    ended = {"type": "system", "subtype": "turn_duration"}

    assert claude_helm_lifecycle.open_turn_waited_on([prompt], "lh_claude_step_abc") == PROVIDER_TURN
    # The prompt never reached Claude: the same silence, and not a stalled turn.
    assert claude_helm_lifecycle.open_turn_waited_on([], "lh_claude_step_abc") is None
    # The turn ended without doing what was awaited: a result.
    assert claude_helm_lifecycle.open_turn_waited_on([prompt, ended], "lh_claude_step_abc") is None


# --- Cursor: its own hooks ------------------------------------------------------


def _write_cursor_hooks(root: Path, session_id: str, *events: tuple[str, dict[str, object]]) -> None:
    path = root / "hook-events" / f"{session_id}.ndjson"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps({"event": name, "payload": payload}) + "\n" for name, payload in events), encoding="utf-8")


def test_a_cursor_reply_missing_from_the_archive_says_which_side_stopped(monkeypatch, tmp_path) -> None:
    prompt = ("beforeSubmitPrompt", {"prompt": f"Reply with exactly {MARKER}", "generation_id": "g1"})
    response = ("afterAgentResponse", {"text": MARKER, "generation_id": "g1"})
    monkeypatch.setattr(cursor_coordination_producer, "_hosted_events", lambda *_a: {"events": []})

    def reply_wait(hook_root: Path | None) -> BaseException:
        with pytest.raises(RuntimeError, match="timed out waiting for Cursor session s1 reply containing marker") as raised:
            cursor_coordination_producer._wait_marker_reply(
                "https://runtime.invalid", "token", "s1", MARKER, timeout=0.4, hook_root=hook_root
            )
        return raised.value

    # Cursor took the prompt and never answered.
    _write_cursor_hooks(tmp_path, "s1", prompt)
    assert waited_on(reply_wait(tmp_path)) == PROVIDER_TURN
    # Cursor answered; Longhouse never served the answer.
    _write_cursor_hooks(tmp_path, "s1", prompt, response)
    assert waited_on(reply_wait(tmp_path)) == LONGHOUSE_INGEST
    # Cursor took the prompt and its turn stopped without a reply (its usage
    # limit, in the retained runs): the provider's own failure, not a stall.
    _write_cursor_hooks(tmp_path, "s1", prompt, ("stop", {"status": "error", "generation_id": "g1"}))
    assert waited_on(reply_wait(tmp_path)) is None
    # A stop that belongs to an earlier generation leaves this turn open.
    _write_cursor_hooks(tmp_path, "s1", ("stop", {"status": "completed", "generation_id": "g0"}), prompt)
    assert waited_on(reply_wait(tmp_path)) == PROVIDER_TURN
    _write_cursor_hooks(tmp_path, "s1", prompt, ("stop", {"status": "completed", "generation_id": "g0"}))
    assert waited_on(reply_wait(tmp_path)) == PROVIDER_TURN
    # Cursor never took the prompt, or its hooks are not at hand: nothing to declare.
    _write_cursor_hooks(tmp_path, "s1")
    assert waited_on(reply_wait(tmp_path)) is None
    assert waited_on(reply_wait(None)) is None

    rows = cursor_helm_product_e2e._hook_rows(tmp_path, "s1")
    assert cursor_helm_product_e2e.hosted_reply_waited_on(rows, MARKER) is None
    assert cursor_helm_product_e2e.hosted_reply_waited_on([{"event": "afterAgentResponse", "text": "another reply"}], MARKER) is None


def _cursor_resume_fixture(tmp_path: Path, *events: tuple[str, dict[str, object]], claim_status: str = "observed") -> tuple[dict, dict]:
    state = {"session_id": "s1", "provider_session_id": "conversation-1", "run_id": "run-1"}
    root = tmp_path / "managed-local" / "cursor-helm"
    (root / "binding-probes").mkdir(parents=True, exist_ok=True)
    (root / "binding-probes" / "s1.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "provider": "cursor",
                "status": claim_status,
                "session_id": "s1",
                "conversation_uuid": "conversation-1",
                "run_id": "run-1",
                "launch_id": "launch-1",
            }
        ),
        encoding="utf-8",
    )
    (root / "hook-events").mkdir(exist_ok=True)
    (root / "hook-events" / "s1.ndjson").write_text(
        "".join(
            json.dumps(
                {
                    "session_id": "s1",
                    "conversation_id": "conversation-1",
                    "launch_id": "launch-1",
                    "event": name,
                    "payload": {"session_id": "conversation-1", "conversation_id": "conversation-1", "generation_id": "g1", **payload},
                }
            )
            + "\n"
            for name, payload in events
        ),
        encoding="utf-8",
    )
    return state, {"LONGHOUSE_HOME": str(tmp_path)}


def test_the_cursor_hook_sequence_wait_reads_the_hooks_to_say_what_it_waited_on(tmp_path) -> None:
    prompt = f"Reply with exactly {MARKER}"

    def sequence(*events: tuple[str, dict[str, object]], typed: bool, claim_status: str = "observed") -> BaseException:
        state, environment = _cursor_resume_fixture(tmp_path, *events, claim_status=claim_status)
        with pytest.raises(RuntimeError, match="did not publish an ordered beforeSubmitPrompt/afterAgentResponse hook sequence") as raised:
            provider_native_resume._wait_cursor_hook_sequence(
                state,
                environment,
                marker=MARKER,
                expected_prompt=prompt,
                minimum_hook_event_bytes=0,
                label="fixture",
                timeout=0.6,
                typed_into_provider=typed,
            )
        return raised.value

    before = ("beforeSubmitPrompt", {"prompt": prompt})
    after = ("afterAgentResponse", {"text": MARKER})
    # Cursor took the prompt and never responded.
    assert waited_on(sequence(before, typed=False)) == PROVIDER_TURN
    # Typed into Cursor's terminal and never submitted by it.
    assert waited_on(sequence(typed=True)) == PROVIDER_PROCESS
    # Sent through Longhouse and never seen by Cursor: either side may have dropped it.
    assert waited_on(sequence(typed=False)) is None
    # Cursor did all of its part; the engine's binding claim never reached `observed`.
    assert waited_on(sequence(before, after, typed=False, claim_status="pending")) == LONGHOUSE_ENGINE
    # A response that names the marker under another generation is another
    # turn's: Cursor took this prompt and has not answered it, whatever the
    # engine's claim says. Attributing it to the engine would withhold the retry.
    other_turn = ("afterAgentResponse", {"text": MARKER, "generation_id": "g0"})
    assert waited_on(sequence(before, other_turn, typed=False, claim_status="pending")) == PROVIDER_TURN
    assert waited_on(sequence(before, other_turn, typed=False)) == PROVIDER_TURN
    # So is one that came before the prompt was taken.
    assert waited_on(sequence(after, before, typed=False, claim_status="pending")) == PROVIDER_TURN


def test_a_cursor_turn_wait_is_on_the_model_only_once_cursor_took_the_prompt() -> None:
    step = "LONGHOUSE_CURSOR_STEP_abc"
    taken = {"event": "beforeSubmitPrompt", "prompt": f"Run `sleep 6; echo {step}_1` then two more", "generation_id": "g1"}
    stopped = {"event": "stop", "status": "error", "generation_id": "g1"}
    other = {"event": "beforeSubmitPrompt", "prompt": "an earlier prompt", "generation_id": "g0"}

    assert cursor_helm_product_e2e.open_turn_waited_on([other, taken], step) == PROVIDER_TURN
    # Longhouse sent the prompt and Cursor's hooks never show it taken.
    assert cursor_helm_product_e2e.open_turn_waited_on([other], step) is None
    # Cursor ended the turn itself before the step ran.
    assert cursor_helm_product_e2e.open_turn_waited_on([taken, stopped], step) is None


# --- Codex: the bridge's turn state beside Codex's own rollout -------------------


def test_a_codex_turn_wait_reads_the_bridge_and_the_rollout() -> None:
    kind = codex_provider_release_canary.expired_turn_wait
    open_turn = {"last_turn_status": "inProgress", "active_turn_id": "turn-1"}

    # The turn is still open and Codex's rollout has no reply: Codex's turn.
    assert kind(open_turn, provider_answered=False) == {"waited_on": PROVIDER_TURN}
    # Codex wrote the reply and the bridge never recorded the turn's end.
    assert kind(open_turn, provider_answered=True) == {"waited_on": LONGHOUSE_ENGINE}
    # A turn that ended, in any status, is not an expired wait.
    assert kind({"last_turn_status": "failed", "active_turn_id": None}, provider_answered=False) == {}
    assert kind({"last_turn_status": "completed", "active_turn_id": None}, provider_answered=False) == {}
    # A turn the bridge never opened declares nothing.
    assert kind({}, provider_answered=False) == {}


def test_the_codex_marker_wait_declares_an_open_turn_and_not_a_failed_one(monkeypatch, tmp_path) -> None:
    state_file = tmp_path / "state.json"
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("", encoding="utf-8")
    monkeypatch.setattr(codex_provider_release_canary, "_assistant_transcript_contains", lambda _path, _marker: False)

    state_file.write_text(json.dumps({"last_turn_status": "inProgress", "active_turn_id": "t1", "thread_path": str(rollout)}))
    with pytest.raises(RuntimeError, match="Codex turn did not retain the expected assistant marker") as stalled:
        codex_native_resume._wait_for_marker(state_file, MARKER, timeout=1)
    assert waited_on(stalled.value) == PROVIDER_TURN

    state_file.write_text(json.dumps({"last_turn_status": "failed", "active_turn_id": None, "thread_path": str(rollout)}))
    with pytest.raises(RuntimeError, match="Codex turn did not retain the expected assistant marker") as failed:
        codex_native_resume._wait_for_marker(state_file, MARKER, timeout=1)
    assert waited_on(failed.value) is None


def test_the_codex_seed_turn_says_whether_codex_took_the_typed_prompt(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(codex_helm_launch_visibility, "_answer_startup_dialogs", lambda _tui: [])

    class _Tui:
        def submit_line(self, _text: str) -> None:
            return None

        def alive(self) -> bool:
            return True

    def seed(codex_home: Path) -> BaseException:
        with pytest.raises(RuntimeError, match="Codex TUI did not persist the seed turn") as raised:
            codex_helm_launch_visibility._seed_codex_rollout(_Tui(), codex_home=codex_home, marker=MARKER, timeout=0.3)
        return raised.value

    # The rollout holds the prompt and no reply: the model's turn.
    prompted = tmp_path / "prompted"
    (prompted / "sessions" / "2026").mkdir(parents=True)
    (prompted / "sessions" / "2026" / "rollout.jsonl").write_text(
        json.dumps({"type": "response_item", "payload": {"role": "user", "content": f"Reply with exactly {MARKER}"}}) + "\n"
    )
    assert waited_on(seed(prompted)) == PROVIDER_TURN
    # No rollout names the prompt: Codex never took what was typed into it.
    assert waited_on(seed(tmp_path / "never-prompted")) == PROVIDER_PROCESS


# --- Console: the adapter's claim, then the served reply --------------------------


def test_a_console_claim_wait_reads_the_claim_to_say_what_it_waited_on(tmp_path) -> None:
    path = tmp_path / "claim.json"
    terminal = frozenset({"terminal", "failed"})

    def claim_wait() -> BaseException:
        with pytest.raises(RuntimeError, match=r"Console claim did not reach \['failed', 'terminal'\]") as raised:
            provider_console_lifecycle._wait_claim(path, states=terminal, timeout=0.3)
        return raised.value

    # No claim: the run was never dispatched or never taken. Not attributable.
    assert waited_on(claim_wait()) is None
    # The engine holds the run and has not started the provider.
    path.write_text(json.dumps({"state": "claimed"}))
    assert waited_on(claim_wait()) == LONGHOUSE_ENGINE
    # A state this producer has not been taught says nothing about who was awaited.
    path.write_text(json.dumps({"state": "parked", "pid": os.getpid()}))
    assert waited_on(claim_wait()) is None
    # A spawned claim that records no process says nothing: there is no pid to find alive or gone.
    for unrecorded in ({"state": "spawned"}, {"state": "spawned", "pid": 0}, {"state": "spawned", "pid": None}):
        path.write_text(json.dumps(unrecorded))
        assert waited_on(claim_wait()) is None
    # The provider process is running and has not finished its turn.
    path.write_text(json.dumps({"state": "spawned", "pid": os.getpid()}))
    assert waited_on(claim_wait()) == PROVIDER_TURN
    # The provider process is gone and the engine never recorded the exit.
    child = os.fork()
    if child == 0:
        os._exit(0)
    os.waitpid(child, 0)
    path.write_text(json.dumps({"state": "spawned", "pid": child}))
    assert waited_on(claim_wait()) == LONGHOUSE_ENGINE


def test_the_console_turn_terminal_wait_is_on_the_runtime_host(monkeypatch) -> None:
    monkeypatch.setattr(provider_console_lifecycle, "_request", lambda *_args: {"turn_id": "turn-1", "run_id": "run-1", "state": "active"})
    with pytest.raises(RuntimeError, match="Runtime Host never observed Console turn terminal") as raised:
        provider_console_lifecycle._wait_turn_terminal(
            api_url="https://runtime.invalid",
            token="token",
            session_id="session-1",
            message="m",
            request_id="r",
            turn_id="turn-1",
            run_id="run-1",
            timeout=0.4,
        )
    assert waited_on(raised.value) == RUNTIME_HOST


# --- direct native Resume: the hosted transcript against the provider's own record -


def test_an_uncorrelated_hosted_transcript_says_which_side_stopped_for_each_provider(monkeypatch, tmp_path) -> None:
    prompt = f"Reply exactly {MARKER} and nothing else."

    def kind(provider: str, state: dict[str, object], environment: dict[str, str] | None = None) -> str | None:
        return provider_native_resume._hosted_correlation_waited_on(
            provider_native_resume.SPECS[provider], state, marker=MARKER, home=tmp_path, environment=environment or {}
        )

    # Claude: its transcript holds the prompt; the turn is open, then finished.
    claude_state = {"session_id": "s1", "provider_session_id": "native-1"}
    monkeypatch.setattr(claude_helm_lifecycle, "_transcript_rows", lambda *_a: [_claude_user(prompt)])
    assert kind("claude", claude_state) == PROVIDER_TURN
    monkeypatch.setattr(
        claude_helm_lifecycle, "_transcript_rows", lambda *_a: [_claude_user(prompt), _claude_assistant("ok"), _CLAUDE_TURN_END]
    )
    assert kind("claude", claude_state) == LONGHOUSE_INGEST
    monkeypatch.setattr(claude_helm_lifecycle, "_transcript_rows", lambda *_a: [])
    assert kind("claude", claude_state) is None

    # OpenCode: its message store.
    monkeypatch.setattr(opencode_helm_lifecycle, "_messages", lambda _state: [])
    open_turn = _opencode_rows({"id": "u1", "role": "user", "text": prompt}, {"id": "a1", "role": "assistant", "parent_id": "u1"})
    monkeypatch.setattr(opencode_helm_lifecycle, "_summarize", lambda _messages: open_turn)
    assert kind("opencode", {"provider_session_id": "p1"}) == PROVIDER_TURN
    finished = _opencode_rows(
        {"id": "u1", "role": "user", "text": prompt}, {"id": "a1", "role": "assistant", "parent_id": "u1", "finish": "stop"}
    )
    monkeypatch.setattr(opencode_helm_lifecycle, "_summarize", lambda _messages: finished)
    assert kind("opencode", {"provider_session_id": "p1"}) == LONGHOUSE_INGEST
    errored = _opencode_rows(
        {"id": "u1", "role": "user", "text": prompt}, {"id": "a1", "role": "assistant", "parent_id": "u1", "error": "ProviderAuthError"}
    )
    monkeypatch.setattr(opencode_helm_lifecycle, "_summarize", lambda _messages: errored)
    assert kind("opencode", {"provider_session_id": "p1"}) is None

    # Cursor: its hooks.
    hook_root = tmp_path / "managed-local" / "cursor-helm"
    environment = {"LONGHOUSE_HOME": str(tmp_path)}
    _write_cursor_hooks(hook_root, "s1", ("beforeSubmitPrompt", {"prompt": prompt}))
    assert kind("cursor", {"session_id": "s1"}, environment) == PROVIDER_TURN
    _write_cursor_hooks(hook_root, "s1", ("beforeSubmitPrompt", {"prompt": prompt}), ("afterAgentResponse", {"text": MARKER}))
    assert kind("cursor", {"session_id": "s1"}, environment) == LONGHOUSE_INGEST

    # A record that cannot be read declares nothing and never raises.
    def unreadable(*_a):
        raise OSError("transcript unavailable")

    monkeypatch.setattr(claude_helm_lifecycle, "_transcript_rows", unreadable)
    assert kind("claude", claude_state) is None
    assert kind("cursor", {"session_id": "s1"}) is None


class _DeliveryShipper:
    """A shipper whose at-timeout snapshot says what the engine had delivered."""

    def __init__(self, *, known: bool = True, delivered: bool = True, reply_written: bool = True) -> None:
        self.captured: list[tuple[str, str]] = []
        self.delivery = {
            "known": known,
            "source_holds_prompt_and_reply": known and reply_written,
            "store_root_captured": known and delivered,
            "store_captured_records": 34 if known else None,
            "store_shipped_records": 34 if known else None,
            "projection_bytes": 899 if known else None,
            "projection_shipped_bytes": 899 if known else None,
            "pending_envelopes": 0,
            "delivered": known and delivered and reply_written,
        }

    def cursor_turn_delivery(self, state: dict, *, marker: str, label: str) -> dict:
        self.captured.append((marker, label))
        return dict(self.delivery)


def test_a_cursor_correlation_failure_retains_where_the_reply_was_when_the_wait_gave_up(monkeypatch) -> None:
    """The snapshots around the flush predate the hosted wait; this one is taken when it expires."""

    shipper = _DeliveryShipper(known=False)
    captured = shipper.captured

    monkeypatch.setattr(provider_native_resume, "_hosted_correlation_waited_on", lambda *_a, **_k: LONGHOUSE_INGEST)

    def failure(provider: str, shipper: object | None) -> BaseException:
        return provider_native_resume._uncorrelated(
            f"provider transcript did not correlate post-resume {provider} marker {MARKER}",
            provider_native_resume.SPECS[provider],
            {"session_id": "s1"},
            marker=MARKER,
            home=None,
            environment={},
            shipper=shipper,
            label="post-resume",
        )

    error = failure("cursor", shipper)
    assert captured == [(MARKER, "post-resume-at-timeout")]
    assert waited_on(error) == LONGHOUSE_INGEST and str(error).startswith("provider transcript did not correlate post-resume cursor")
    # Only Cursor has a store to snapshot, and a run without a shipper has nothing to ask.
    failure("claude", shipper)
    failure("cursor", None)
    assert len(captured) == 1


def test_a_reply_cursor_gave_and_the_engine_shipped_but_the_hosted_transcript_never_showed_is_its_own_result(monkeypatch, tmp_path) -> None:
    message = f"provider transcript did not correlate resumed Cursor bootstrap marker {MARKER}"
    receipt = tmp_path / "resume-bootstrap-response-correlation.json"

    def failure(shipper: _DeliveryShipper, hooks: str | None) -> tuple[BaseException, dict]:
        monkeypatch.setattr(provider_native_resume, "_hosted_correlation_waited_on", lambda *_a, **_k: hooks)
        correlation: dict = {"marker_observed_in_assistant": False, "new_assistant_events": 0, "hosted_transcript_gap": None}
        error = provider_native_resume._uncorrelated(
            message,
            provider_native_resume.SPECS["cursor"],
            {"session_id": "s1"},
            marker=MARKER,
            home=None,
            environment={},
            shipper=shipper,
            label="resume-bootstrap",
            correlation=correlation,
            receipt=receipt,
        )
        assert json.loads(receipt.read_text(encoding="utf-8")) == correlation
        return error, correlation

    # Cursor's hooks hold the reply and the engine shipped the turn: Longhouse's hosted side is what failed.
    error, correlation = failure(_DeliveryShipper(), LONGHOUSE_INGEST)
    assert str(error).startswith(f"hosted_transcript_missing_shipped_reply: {message}: Cursor answered and the engine shipped")
    assert "(the store's latest root captured, 34 of 34 store records and 899 of 899 transcript bytes shipped, nothing pending)" in str(
        error
    )
    assert waited_on(error) == LONGHOUSE_INGEST
    assert correlation["hosted_transcript_gap"] == {
        "reason": provider_native_resume.HOSTED_TRANSCRIPT_MISSING_SHIPPED_REPLY,
        "provider_answered": True,
        "engine_delivery": correlation["engine_delivery"],
    }
    assert correlation["engine_delivery"]["delivered"] is True

    # Cursor's records hold the reply and the engine has not captured the store as it
    # stands: the engine is what was awaited, and there is no gap to name.
    error, correlation = failure(_DeliveryShipper(delivered=False), LONGHOUSE_INGEST)
    assert str(error) == message and waited_on(error) == LONGHOUSE_ENGINE
    assert correlation["hosted_transcript_gap"] is None and correlation["engine_delivery"]["store_root_captured"] is False

    # Cursor's own records do not show the reply: neither the engine nor the hosted side is named for it.
    error, correlation = failure(_DeliveryShipper(reply_written=False), LONGHOUSE_INGEST)
    assert str(error) == message and waited_on(error) == LONGHOUSE_INGEST and correlation["hosted_transcript_gap"] is None

    # Without the engine's state nothing more is known than what the hooks say.
    error, correlation = failure(_DeliveryShipper(known=False), LONGHOUSE_INGEST)
    assert str(error) == message and waited_on(error) == LONGHOUSE_INGEST and correlation["hosted_transcript_gap"] is None

    # Cursor never answered: whatever the engine shipped, the reply was never there to serve.
    for hooks in (PROVIDER_TURN, None):
        error, correlation = failure(_DeliveryShipper(), hooks)
        assert str(error) == message and waited_on(error) == hooks and correlation["hosted_transcript_gap"] is None


def test_the_post_resume_receipt_says_what_the_providers_hooks_showed() -> None:
    correlation = {"marker_observed_in_assistant": False, "new_assistant_events": 0, "timed_out": True}
    hooks = {"hook_response_correlated": True, "generation_id": "g1"}

    assert provider_native_resume._with_hook_sequence(correlation, hooks) == {**correlation, "hook_sequence": hooks}
    # The hosted counts the verdict reads are untouched, and a provider without hooks adds nothing.
    assert (
        provider_native_resume._post_resume_response_correlated("cursor", provider_native_resume._with_hook_sequence(correlation, hooks))
        is False
    )
    assert provider_native_resume._with_hook_sequence(correlation, None) == correlation
