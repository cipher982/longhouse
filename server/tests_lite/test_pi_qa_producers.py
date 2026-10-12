from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from zerg.qa import pi_console_tool_producer as pi_console
from zerg.qa import pi_family_helm as helm
from zerg.qa import provider_console_lifecycle as lifecycle
from zerg.qa.console_served_state_core import MarkerAnswerCountError
from zerg.qa.live_kit import RuntimeHostError
from zerg.qa.pi_console_tool_producer import REGISTRATION as PI_CONSOLE_REGISTRATION
from zerg.qa.pi_console_tool_producer import pi_console_tool_assertions
from zerg.qa.pi_family_helm import RuntimeConvergenceTimeout
from zerg.qa.pi_family_helm import _cleanup_receipt
from zerg.qa.pi_helm_lifecycle import FAMILY as PI_FAMILY
from zerg.qa.pi_native import pi_native_model_evidence
from zerg.qa.pi_native import pi_native_shadow_taxonomy
from zerg.qa.pi_native import pi_transcript_rows
from zerg.qa.provider_adapters.pi import _run_pi_with_pty
from zerg.qa.provider_qualification import _PROFILES


def test_pi_qualification_profiles_are_registered_in_both_managed_lanes() -> None:
    assert {("pi", profile) for profile in ("pi_print_v1", "pi_console_tool_v1", "pi_helm_v1")} <= set(_PROFILES)


def test_pi_print_runner_allocates_tty_for_stock_cli(tmp_path) -> None:
    script = tmp_path / "tty_probe.py"
    script.write_text(
        "import os\nprint(f'PI_STDOUT_IS_TTY={os.isatty(1)}')\nprint('PI_TTY_PROOF')\n",
        encoding="utf-8",
    )
    result, timed_out = _run_pi_with_pty(
        [sys.executable, str(script)],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        timeout=10,
    )

    assert timed_out is False
    assert result.returncode == 0
    assert "PI_STDOUT_IS_TTY=True" in result.stdout
    assert "PI_TTY_PROOF" in result.stdout


def test_helm_retained_evidence_excludes_live_channel_authority() -> None:
    secret, token = "ephemeral-control-authority", "qualification-agents-token"
    state = {"session_id": "session", "run_id": "run", "channel_token": secret}
    retained = helm._redacted(
        {"owner": state, "frame": {"auth_token": secret, "session_id": "session"}, "error": f"refused {token}"}, [token]
    )
    assert secret not in json.dumps(retained) and token not in json.dumps(retained)
    assert retained["owner"]["session_id"] == "session"


def test_helm_auth_failure_is_not_hidden_as_a_convergence_timeout(monkeypatch) -> None:
    def rejected(*args):
        raise RuntimeHostError(403, "access denied")

    monkeypatch.setattr(helm, "_runtime_snapshot", rejected)
    with pytest.raises(RuntimeHostError) as error:
        _pi_converge(monkeypatch, [], session_file=Path("/native/session.jsonl"), timeout=1, patch_snapshot=False)
    assert error.value.status == 403


_PI_MARKER = "PI_HELM_SEND_0123456789abcdef"
# A turn that works through tools narrates before it answers: the narration names
# the marker inside a sentence, the final reply puts it alone on its last line.
_PI_NARRATION = f"The read has not returned yet, so I may not reply with `{_PI_MARKER}`.\n\n- read: running"
_PI_FINAL_REPLY = f"The read returned.\n\n{_PI_MARKER}"


def _pi_snapshot(events: list[dict[str, object]]) -> dict[str, object]:
    actions = {"send_input": {"state": "available"}, "terminate": {"state": "available"}}
    return {
        "detail": {"id": "session-1", "provider": "pi", "provider_session_id": "native-1"},
        "thread": {"root_session_id": "session-1", "head_session_id": "session-1", "sessions": [{"id": "session-1"}]},
        # A page read to its end, as the Runtime Host serves one: under a generation, on the head branch, with its total.
        "events": {
            "events": events,
            "has_more": False,
            "next_cursor": None,
            "generation_id": "generation-1",
            "branch_mode": "head",
            "total": len(events),
        },
        "diagnostic": {"served_path": "canonical_session_detail", "shadow": {"control": {"actions": actions}}},
    }


def _pi_converge(
    monkeypatch,
    events: list[dict[str, object]],
    *,
    reads: list[int] | None = None,
    native_answers: dict[str, int] | None = None,
    patch_snapshot: bool = True,
    **kwargs: object,
):
    """Converge, as Pi's lifecycle does, on a fixed served page. ``native_answers`` is what Pi's own transcript
    answers with: entry id to count, by default the one entry ``reply``."""

    def snapshot(*_args: object) -> dict[str, object]:
        if reads is not None:
            reads.append(1)
        return _pi_snapshot(events)

    if patch_snapshot:
        monkeypatch.setattr(helm, "_runtime_snapshot", snapshot)
    # A refusal has to stand; these tests do not wait for the window, only for the second read.
    monkeypatch.setattr("zerg.qa.console_served_state_core.SERVED_ANSWER_SETTLE_SECONDS", kwargs.pop("settle", 0.0))
    if "session_file" not in kwargs:
        answers = {"reply": 1} if native_answers is None else native_answers
        monkeypatch.setattr("zerg.qa.provider_console_lifecycle._native_answer_rows", lambda *_a, **_k: (dict(answers), "read"))
    arguments = {"native_reply_id": "reply", "timeout": 30, **kwargs}
    return helm._runtime_convergence(
        "https://runtime.invalid",
        "fixture",
        family=PI_FAMILY,
        session_id="session-1",
        native_session_id="native-1",
        marker=str(arguments.pop("marker", _PI_MARKER)),
        flush={},
        native_source_path=str(arguments.pop("session_file", "/native/session.jsonl")),
        **arguments,
    )


def test_pi_runtime_convergence_counts_the_answer_not_the_narration_that_quotes_it(monkeypatch) -> None:
    events = [
        {"id": "prompt", "role": "user", "content_text": f"Reply with exactly {_PI_MARKER} and no other text."},
        {"id": "narration", "role": "assistant", "content_text": _PI_NARRATION},
        {"id": "narration-tool-call-1", "role": "assistant", "tool_name": "read", "content_text": _PI_MARKER},
        {"id": "reply", "role": "assistant", "content_text": _PI_FINAL_REPLY},
    ]
    reads: list[int] = []

    runtime = _pi_converge(monkeypatch, events, reads=reads)

    assert runtime["served_projection"]["marker_event_ids"] == ["reply"]
    assert reads == [1]


def test_pi_runtime_convergence_serves_the_providers_own_answers_however_many(monkeypatch) -> None:
    twice = [
        {"id": "reply", "role": "assistant", "content_text": _PI_FINAL_REPLY},
        {"id": "again", "role": "assistant", "content_text": _PI_MARKER},
    ]
    reads: list[int] = []

    # Pi's transcript answers in two entries and the page serves both: converged.
    runtime = _pi_converge(monkeypatch, twice, reads=reads, native_answers={"reply": 1, "again": 1})
    assert reads == [1]
    assert runtime["served_projection"]["marker_event_ids"] == ["reply", "again"]
    assert runtime["answer_correspondence"]["native_answer_rows"] == {"reply": 1, "again": 1}


def test_pi_runtime_convergence_fails_at_once_on_an_answer_the_transcript_does_not_account_for(monkeypatch) -> None:
    # Pi answered once, and the page carries a second answering event of no entry.
    twice = [
        {"id": "reply", "role": "assistant", "content_text": _PI_FINAL_REPLY},
        {"id": "again", "role": "assistant", "content_text": _PI_MARKER},
    ]
    reads: list[int] = []
    with pytest.raises(MarkerAnswerCountError) as raised:
        _pi_converge(monkeypatch, twice, reads=reads)
    # Raised on the second read: the first only starts the clock.
    assert reads == [1, 1]
    assert "served_answer_without_native_row: " in str(raised.value)
    assert "has 2 answering served assistant event(s)" in str(raised.value)
    assert "timed out" not in str(raised.value)
    assert raised.value.observation["served_projection"]["marker_event_ids"] == ["reply", "again"]

    # The one entry that answered, served twice.
    reply = {"id": "reply", "role": "assistant", "content_text": _PI_FINAL_REPLY}
    with pytest.raises(MarkerAnswerCountError) as raised:
        _pi_converge(monkeypatch, [reply, dict(reply)])
    assert "native_answer_served_more_than_once: " in str(raised.value)

    # The page carries the first event of the native reply row and nothing on
    # it answers. The answering block may still be on its way, so the wait
    # polls to its deadline and then says what it saw, not that it timed out.
    served_without_answer = [{"id": "reply", "role": "assistant", "content_text": _PI_NARRATION}]
    reads.clear()
    with pytest.raises(MarkerAnswerCountError) as raised:
        _pi_converge(monkeypatch, served_without_answer, reads=reads, timeout=0.5)
    assert len(reads) > 2
    assert "marker_reply_served_without_answer: " in str(raised.value)
    assert "timed out" not in str(raised.value)
    assert len(raised.value.observation["served_projection"]["marker_mention_events"]) == 1


_PI_HEADER = json.dumps({"type": "session", "id": "native-1", "version": 3, "cwd": "/workspace"})


def _pi_native_entry(entry_id: str, role: str, text: str) -> str:
    message = {
        "role": role,
        "content": [{"type": "text", "text": text}],
        **({"stopReason": "stop", "model": "fixture"} if role == "assistant" else {}),
    }
    return json.dumps({"type": "message", "id": entry_id, "parentId": None, "timestamp": "2026-01-01T00:00:01Z", "message": message})


@pytest.mark.parametrize(
    ("state", "write"),
    [
        # Present and not readable: a directory where the transcript should be.
        ("unreadable", lambda path: path.mkdir()),
        ("empty", lambda path: path.write_text("", encoding="utf-8")),
        # Pi is in the middle of appending the entry: the last line has no newline yet.
        (
            "mid_write",
            lambda path: path.write_text(
                _PI_HEADER + "\n" + _pi_native_entry("reply", "assistant", _PI_FINAL_REPLY)[:-9], encoding="utf-8"
            ),
        ),
        ("unreadable", lambda path: None),
    ],
)
def test_pi_runtime_convergence_gives_no_verdict_on_a_transcript_it_did_not_read(monkeypatch, tmp_path, state, write) -> None:
    """A transcript that was not read to the end is unknown, not empty: the served answer is neither refused nor accepted."""

    session_file = tmp_path / "session.jsonl"
    write(session_file)
    reply = {"id": "reply", "role": "assistant", "content_text": _PI_FINAL_REPLY}
    reads: list[int] = []
    with pytest.raises(RuntimeConvergenceTimeout, match="timed out waiting") as raised:
        _pi_converge(monkeypatch, [reply], reads=reads, session_file=session_file, timeout=0.6)

    # It kept polling: an answering served page with no transcript to hold it to is not a product finding.
    assert len(reads) > 2
    assert raised.value.observation["answer_correspondence"]["native_transcript_state"] == state


def test_pi_runtime_convergence_waits_for_an_entry_the_page_served_before_the_read_saw_it(monkeypatch, tmp_path) -> None:
    """Pi appends the answering entry, the engine ships it, and this process's read of the file is the last to see it."""

    session_file = tmp_path / "session.jsonl"
    session_file.write_text(
        _PI_HEADER + "\n" + _pi_native_entry("prompt", "user", f"Reply with exactly {_PI_MARKER}.") + "\n", encoding="utf-8"
    )
    reply = {"id": "reply", "role": "assistant", "content_text": _PI_FINAL_REPLY}
    reads: list[int] = []

    def snapshot(*_args: object) -> dict[str, object]:
        reads.append(1)
        if len(reads) == 2:
            with session_file.open("a", encoding="utf-8") as handle:
                handle.write(_pi_native_entry("reply", "assistant", _PI_FINAL_REPLY) + "\n")
        return _pi_snapshot([reply])

    monkeypatch.setattr(helm, "_runtime_snapshot", snapshot)
    runtime = _pi_converge(monkeypatch, [reply], session_file=session_file, patch_snapshot=False)

    # The first read lacked the entry and refused nothing; the second read has it.
    assert len(reads) == 2
    assert runtime["answer_correspondence"]["native_answer_rows"] == {"reply": 1}


def test_pi_runtime_convergence_refuses_a_served_answer_only_after_later_reads_still_lack_its_entry(monkeypatch, tmp_path) -> None:
    session_file = tmp_path / "session.jsonl"
    session_file.write_text(
        _PI_HEADER + "\n" + _pi_native_entry("prompt", "user", f"Reply with exactly {_PI_MARKER}.") + "\n", encoding="utf-8"
    )
    reply = {"id": "reply", "role": "assistant", "content_text": _PI_FINAL_REPLY}

    # Inside the window nothing is refused: the wait runs to its own deadline.
    with pytest.raises(RuntimeConvergenceTimeout):
        _pi_converge(monkeypatch, [reply], session_file=session_file, settle=30.0, timeout=0.6)
    # Once the same fault has stood for the window, on a transcript that was read each time, it is final.
    reads: list[int] = []
    with pytest.raises(MarkerAnswerCountError, match="served_answer_without_native_row: ") as raised:
        _pi_converge(monkeypatch, [reply], reads=reads, session_file=session_file, settle=0.4, timeout=30)
    assert len(reads) >= 3
    assert raised.value.observation["answer_correspondence"]["native_transcript_state"] == "read"


def test_pi_runtime_convergence_keeps_waiting_until_the_reply_row_is_served(monkeypatch) -> None:
    only_narration = [{"id": "narration", "role": "assistant", "content_text": _PI_NARRATION}]
    reads: list[int] = []
    with pytest.raises(RuntimeConvergenceTimeout) as raised:
        _pi_converge(monkeypatch, only_narration, reads=reads, timeout=0.5)

    assert len(reads) > 1
    assert "timed out waiting for Runtime Host convergence for Pi marker" in str(raised.value)
    # The deadline reports the last read, not None.
    assert "'answer_event_count': 0" in str(raised.value)
    assert "'mention_event_count': 1" in str(raised.value)


def test_pi_runtime_convergence_reads_a_recalled_phrase_by_presence(monkeypatch) -> None:
    resume, phrase = "PI_HELM_RESUME_0123456789abcdef", "PI_HELM_CONTEXT_fedcba9876543210"
    events = [{"id": "reply", "role": "assistant", "content_text": f"The context phrase I was asked to remember is: {phrase}\n\n{resume}"}]

    # The marker is the answer; it may share a line with the phrase.
    assert _pi_converge(monkeypatch, events, marker=resume, alongside=(phrase,))["status"] == "pass"
    # The phrase is a recall, which a model may put in a sentence.
    assert _pi_converge(monkeypatch, events, marker=phrase, recalled=True)["served_projection"]["marker_event_ids"] == ["reply"]


def _pi_native_row(entry_id: str, text: str, *, stop: str, parent: str | None, tool_call: bool = False) -> str:
    content: list[dict[str, object]] = [{"type": "text", "text": text}]
    if tool_call:
        content.append({"type": "toolCall", "id": "call-1", "name": "read", "arguments": {"path": "a.txt"}})
    message = {"role": "assistant", "stopReason": stop, "content": content, "model": "fixture"}
    return json.dumps({"type": "message", "id": entry_id, "parentId": parent, "timestamp": "2026-01-01T00:00:01Z", "message": message})


def test_pi_wait_native_marker_returns_on_the_answer_and_counts_the_narration_apart(monkeypatch, tmp_path) -> None:
    session_file = tmp_path / "session.jsonl"
    header = json.dumps({"type": "session", "id": "native-1", "version": 3, "cwd": "/workspace"})
    narration = _pi_native_row("narration", _PI_NARRATION, stop="toolUse", parent=None, tool_call=True)
    session_file.write_text(f"{header}\n{narration}\n", encoding="utf-8")
    monkeypatch.setattr(helm, "_wait", lambda predicate, **_kwargs: predicate())

    # Marker only in narration: the turn has not answered yet.
    assert helm._wait_native_marker(session_file, _PI_MARKER) is None

    reply = _pi_native_row("reply", _PI_FINAL_REPLY, stop="stop", parent="narration")
    session_file.write_text(f"{header}\n{narration}\n{reply}\n", encoding="utf-8")
    row = helm._wait_native_marker(session_file, _PI_MARKER)
    native = helm._native_marker_evidence(row, session_file, marker=_PI_MARKER, minimum_offset=0, native_session_id="native-1")

    assert (native["event_id"], native["native_source_bound"], native["marker_count"]) == ("reply", True, 1)
    assert native["assistant_answer_rows"] == 1
    # Rows that name the marker, which the factory recounts from the retained bytes.
    assert native["assistant_marker_rows"] == 2
    assert (native["native_header_session_id"], native["native_model"], native["user_marker_rows"]) == ("native-1", "fixture", 0)
    assert native["native_file"]["present"] is True and native["source_end_offset"] == session_file.stat().st_size


def test_pi_console_main_failure_retains_partial_artifact_manifest(monkeypatch, tmp_path, capsys) -> None:
    def fail(_provider, _variant, _args, root, **_kwargs):
        (root / "partial-receipt.json").write_text("{}\n", encoding="utf-8")
        raise RuntimeError("synthetic qualification failure")

    monkeypatch.setattr(pi_console.lifecycle, "_run_live", fail)
    result = pi_console.main(
        [
            "--variant",
            pi_console._VARIANT,
            "--evidence-root",
            str(tmp_path / "evidence"),
            "--repo-root",
            str(tmp_path),
            "--engine",
            "/bin/true",
            "--longhouse-cli",
            "/bin/true",
            "--provider-bin",
            "/bin/true",
            "--provider-version",
            "0.0.0",
            "--model",
            "fixture-model",
        ]
    )

    assert result == 1
    payload = json.loads((tmp_path / "evidence" / "result.json").read_text(encoding="utf-8"))
    assert payload["failure_code"] == "pi_console_tool_lifecycle_failed"
    assert payload["error"] == "RuntimeError: synthetic qualification failure"
    assert [entry["path"] for entry in payload["artifact_manifest"]] == ["partial-receipt.json"]
    assert "synthetic qualification failure" in capsys.readouterr().out


def test_pi_native_taxonomy_pairs_native_tool_call_and_result() -> None:
    rows = [
        {"type": "assistant", "role": "assistant", "tool_call_id": "call-1"},
        {"type": "tool_result", "role": "toolresult", "tool_call_id": "call-1"},
        {"type": "assistant", "role": "assistant", "tool_call_id": "call-without-result"},
    ]
    taxonomy = pi_native_shadow_taxonomy(
        rows,
        {
            "has_header": True,
            "provider_session_id": "session-1",
            "native_shapes": {
                "session": 1,
                "message/assistant/text+thinking+toolCall": 1,
                "message/toolResult": 1,
            },
        },
    )

    assert taxonomy["source"] == "pi_native_session_jsonl"
    assert taxonomy["tool_pairs"] == ["call-1"]
    assert taxonomy["tool_calls_without_results"] == ["call-without-result"]
    assert taxonomy["tool_results_without_calls"] == []


@pytest.mark.parametrize(
    "rows",
    [
        [
            {"type": "tool_result", "role": "toolresult", "tool_call_id": "call-1", "tool_name": "read"},
            {"type": "assistant", "role": "assistant", "tool_call_id": "call-1", "tool_name": "read"},
        ],
        [
            {"type": "assistant", "role": "assistant", "tool_call_id": "call-1", "tool_name": "read"},
            {"type": "tool_result", "role": "toolresult", "tool_call_id": "call-1", "tool_name": "write"},
        ],
    ],
)
def test_pi_native_taxonomy_rejects_reordered_or_mismatched_tool_pairs(rows) -> None:
    taxonomy = pi_native_shadow_taxonomy(
        rows,
        {"native_shapes": {}, "has_header": True, "provider_session_id": "session-1"},
    )

    assert taxonomy["tool_pairs"] == []
    assert taxonomy["tool_calls_without_results"] == ["call-1"]
    assert taxonomy["tool_results_without_calls"] == ["call-1"]


def test_pi_console_tool_oracle_requires_complete_generic_lifecycle() -> None:
    assert pi_console_tool_assertions({"pi_tool_enabled": True})["pi_console_tool_enabled"] is False


def test_pi_console_tool_oracle_requires_context_recall() -> None:
    observation = {
        "adapter_dispatch_started": True,
        "qualification_model_bound": True,
        "stock_provider_response_bound": True,
        "exact_session_thread_run_binding": True,
        "transcript_converged_exactly_once": True,
        "interrupt_contract_preserved": True,
        "post_interrupt_sendable": True,
        "no_orphan_provider_processes": True,
        "pi_tool_enabled": True,
        "continuation_context_recalled": False,
    }

    assert pi_console_tool_assertions(observation)["pi_console_tool_enabled"] is False
    observation["continuation_context_recalled"] = True
    assert pi_console_tool_assertions(observation)["pi_console_tool_enabled"] is True


def test_pi_console_tool_oracle_rejects_unpaired_native_tool_evidence() -> None:
    observation = {
        "adapter_dispatch_started": True,
        "qualification_model_bound": True,
        "stock_provider_response_bound": True,
        "exact_session_thread_run_binding": True,
        "transcript_converged_exactly_once": True,
        "interrupt_contract_preserved": True,
        "post_interrupt_sendable": True,
        "no_orphan_provider_processes": True,
        "pi_tool_enabled": False,
    }
    assert pi_console_tool_assertions(observation)["pi_console_tool_enabled"] is False


def _write_native_tool_session(
    path,
    *,
    result_call_id: str | None = "call-1",
    include_result: bool = True,
    assistant_final_id: str = "assistant-final-1",
    narration: str | None = None,
    later_assistant_rows: tuple[tuple[str, str], ...] = (),
) -> None:
    """``narration`` is what the row that calls the tool says first; ``later_assistant_rows`` follow the reply."""
    events = [
        {"type": "session", "id": "native-session-1"},
        {
            "type": "message",
            "id": "assistant-tool-1",
            "message": {
                "role": "assistant",
                "model": "fixture-model",
                "content": [
                    *([{"type": "text", "text": narration}] if narration else []),
                    {"type": "toolCall", "id": "call-1", "name": "read", "arguments": {"path": "proof.txt"}},
                ],
                "stopReason": "toolUse",
            },
        },
    ]
    if include_result:
        events.append(
            {
                "type": "message",
                "id": "tool-result-1",
                "message": {
                    "role": "toolResult",
                    "toolCallId": result_call_id,
                    "toolName": "read",
                    "content": [{"type": "text", "text": "PI_CONSOLE_PROOF"}],
                    "isError": False,
                },
            }
        )
    events.append(
        {
            "type": "message",
            "id": assistant_final_id,
            "message": {"role": "assistant", "content": [{"type": "text", "text": "LH_MARKER"}], "stopReason": "stop"},
        }
    )
    events.extend(
        {"type": "message", "id": row_id, "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}
        for row_id, text in later_assistant_rows
    )
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")


def _native_tool_receipt_inputs(native_source):
    identity = {
        "provider": "pi",
        "session_id": "session-1",
        "thread_id": "thread-1",
        "run_id": "run-1",
        "prompt_digest": "sha256:" + "a" * 64,
        "provider_thread_id": "native-session-1",
    }
    return {
        "native_source": native_source,
        "provider_response_source": native_source,
        "inspection": {
            "linked_tool_call_ids": ["call-1"],
            "output_marker_observed": True,
            "native_shapes": {"tool_execution_start": 1, "tool_execution_end": 1},
        },
        "dispatch": {"status": "pass", **identity},
        "binding": {
            "status": "pass",
            **identity,
            "marker": "LH_MARKER",
            "provider_response_source_kind": "stdout_path",
            "provider_response_source_sha256": "sha256:" + "b" * 64,
            "provider_response_marker_count": 1,
            "bound_assistant_event_id": "event-1",
            "bound_assistant_event_origin": "durable",
        },
        "binary_receipt": {"provider": "pi", "path": "/opt/pi", "sha256": "sha256:" + "c" * 64, "version": "0.85.1"},
        "cleanup": {
            "status": "pass",
            "provider_process_dead": True,
            "process_group_dead": True,
            "orphan_count": 0,
            "process_stop_verified": True,
            "source_retention_verified": True,
        },
        "marker": "LH_MARKER",
    }


def test_pi_native_tool_receipt_binds_actual_call_result_session_and_response(tmp_path) -> None:
    native_source = tmp_path / "native.jsonl"
    _write_native_tool_session(native_source)

    receipt = lifecycle._pi_native_tool_receipt(
        **_native_tool_receipt_inputs(native_source),
        native_retained_path="native.jsonl",
        provider_response_retained_path="native.jsonl",
    )

    assert receipt["status"] == "pass"
    assert receipt["provider_session_id"] == "native-session-1"
    assert receipt["native_model"] == "fixture-model"
    assert receipt["tool_call"]["id"] == "call-1"
    assert receipt["tool_call"]["name"] == "read"
    assert receipt["tool_call"]["arguments"] == {"path": "proof.txt"}
    assert receipt["tool_call"]["native_message_id"] == "assistant-tool-1"
    assert receipt["tool_result"]["id"] == "tool-result-1"
    assert receipt["tool_result"]["tool_call_id"] == "call-1"
    assert receipt["tool_result"]["result"] == [{"type": "text", "text": "PI_CONSOLE_PROOF"}]
    assert receipt["provider_response"]["native_message_id"] == "assistant-final-1"
    assert receipt["provider_response"]["retained_source_path"] == str(native_source)
    assert receipt["provider_response"]["retained_path"] == "native.jsonl"
    assert receipt["native_source"]["retained_path"] == "native.jsonl"
    assert receipt["linkage"] == {
        "live_inspection_confirmed": True,
        "native_session_matches_provider_thread": True,
        "native_response_follows_tool_result": True,
        "provider_response_bound": True,
        "native_projected_assistant_linkage": True,
        "native_message_id_preserved": True,
        "projected_assistant_event_id_preserved": True,
        "runtime_identity_matches": True,
        "cleanup_pass": True,
    }


@pytest.mark.parametrize(
    ("result_call_id", "include_result"),
    [("call-other", True), (None, False)],
)
def test_pi_native_tool_receipt_rejects_missing_or_mismatched_native_pair(tmp_path, result_call_id, include_result) -> None:
    native_source = tmp_path / "native.jsonl"
    _write_native_tool_session(native_source, result_call_id=result_call_id, include_result=include_result)

    receipt = lifecycle._pi_native_tool_receipt(**_native_tool_receipt_inputs(native_source))

    assert receipt["status"] == "fail"
    assert "native_tool_call_result_pair_missing" in receipt["failure_reasons"]


def test_pi_native_tool_receipt_binds_the_row_that_answers_when_the_tool_call_row_narrates(tmp_path) -> None:
    native_source = tmp_path / "native.jsonl"
    _write_native_tool_session(
        native_source,
        narration="I will read proof.txt, then reply with `LH_MARKER`.",
        # A later row that names the marker in a sentence is a mention too.
        later_assistant_rows=(("assistant-recap-1", "Earlier I replied with LH_MARKER as asked."),),
    )
    inputs = _native_tool_receipt_inputs(native_source)
    # Every occurrence in the provider's output, the narration's included.
    inputs["binding"]["provider_response_marker_count"] = 3

    receipt = lifecycle._pi_native_tool_receipt(**inputs)

    assert receipt["status"] == "pass"
    assert receipt["failure_reasons"] == []
    assert receipt["provider_response"]["native_message_id"] == "assistant-final-1"
    assert receipt["provider_response"]["native_marker_count"] == 1
    observation = lifecycle._pi_native_tool_observation(native_source, "LH_MARKER")
    assert [row["native_message_id"] for row in observation["marker_responses"]] == ["assistant-final-1"]
    assert [row["native_message_id"] for row in observation["marker_mentions"]] == ["assistant-tool-1", "assistant-recap-1"]


def test_pi_native_tool_receipt_refuses_a_marker_answered_in_two_rows_with_the_typed_reason(tmp_path) -> None:
    native_source = tmp_path / "native.jsonl"
    _write_native_tool_session(native_source, later_assistant_rows=(("assistant-final-2", "LH_MARKER"),))

    # Not a failed receipt: that would be a verdict about the subject.
    with pytest.raises(MarkerAnswerCountError) as raised:
        lifecycle._pi_native_tool_receipt(**_native_tool_receipt_inputs(native_source))

    assert raised.value.reason == "marker_answered_more_than_once"
    assert raised.value.answer_event_ids == ["assistant-final-1", "assistant-final-2"]
    assert "has 2 answering native assistant row(s), expected exactly 1" in str(raised.value)


def test_pi_native_tool_receipt_accepts_native_projected_id_equality(tmp_path) -> None:
    native_source = tmp_path / "native.jsonl"
    _write_native_tool_session(native_source, assistant_final_id="event-1")

    receipt = lifecycle._pi_native_tool_receipt(**_native_tool_receipt_inputs(native_source))

    assert receipt["status"] == "pass"
    assert "native_projected_assistant_id_collision" not in receipt["failure_reasons"]
    assert receipt["linkage"]["native_projected_assistant_linkage"] is True


def test_pi_console_contract_revision_advances_with_native_receipt() -> None:
    registration = PI_CONSOLE_REGISTRATION.to_dict()

    assert registration["producer_revision"] == 5
    assert registration["scenario_revision"] == 23
    assert "native_tool_receipt" in registration["required_artifacts"]


def test_pi_helm_cleanup_oracle_does_not_accept_missing_process_identity() -> None:
    receipt = _cleanup_receipt(
        [
            {"label": "launcher", "pid": -1, "pgid": -1, "birth_matches": False},
            {"label": "provider", "pid": -1, "pgid": -1, "birth_matches": False},
        ]
    )
    assert receipt["status"] == "fail"
    assert receipt["provider_process_dead"] is False
    assert receipt["no_orphan_provider_processes"] is False


def test_pi_native_snapshot_contract_keeps_metadata_and_taxonomy_distinct(tmp_path) -> None:
    native = tmp_path / "session.jsonl"
    native.write_text(
        '{"type":"session","id":"native-session"}\n'
        '{"type":"message","id":"reply","message":{"role":"assistant","model":"fixture-model","content":"reply"}}\n',
        encoding="utf-8",
    )

    rows, _provider_session_id, metadata = pi_transcript_rows(native)
    taxonomy = pi_native_shadow_taxonomy(rows, metadata)

    assert rows
    assert metadata["provider_session_id"] == "native-session"
    assert metadata["source_end_offset"] == native.stat().st_size
    assert taxonomy["provider_session_id"] == "native-session"
    assert taxonomy["source"] == "pi_native_session_jsonl"


def test_pi_native_parser_and_taxonomy_account_for_every_tool_call_in_one_message(tmp_path) -> None:
    native = tmp_path / "multi-tool.jsonl"
    events = [
        {"type": "session", "id": "native-session"},
        {
            "type": "message",
            "id": "assistant-tools",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "toolCall", "id": "call-read", "name": "read", "arguments": {"path": "a"}},
                    {"type": "toolCall", "id": "call-write", "name": "write", "arguments": {"path": "b"}},
                ],
            },
        },
        {
            "type": "message",
            "id": "result-read",
            "message": {
                "role": "toolResult",
                "toolCallId": "call-read",
                "toolName": "read",
                "content": [{"type": "text", "text": "read result"}],
            },
        },
        {
            "type": "message",
            "id": "result-write",
            "message": {
                "role": "toolResult",
                "toolCallId": "call-write",
                "toolName": "write",
                "content": [{"type": "text", "text": "write result"}],
            },
        },
    ]
    native.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    rows, _session_id, metadata = pi_transcript_rows(native)
    taxonomy = pi_native_shadow_taxonomy(rows, metadata)

    assistant = next(row for row in rows if row.get("type") == "assistant")
    assert assistant["tool_call_ids"] == ["call-read", "call-write"]
    assert assistant["tool_call_count"] == 2
    assert taxonomy["tool_call_ids"] == ["call-read", "call-write"]
    assert taxonomy["tool_result_ids"] == ["call-read", "call-write"]
    assert taxonomy["tool_pairs"] == ["call-read", "call-write"]
    assert taxonomy["tool_calls_without_results"] == []
    assert taxonomy["tool_results_without_calls"] == []
    assert taxonomy["shadow_classes"]["transcript:assistant_tool"] == 1
    assert taxonomy["shadow_classes"]["provider_tool:result"] == 2
    assert [fact["call_arguments"] for fact in taxonomy["tool_pair_facts"]] == [{"path": "a"}, {"path": "b"}]


def test_pi_native_thinking_plus_text_fixture_preserves_both_blocks(tmp_path) -> None:
    native = tmp_path / "thinking-text.jsonl"
    native.write_text(
        "\n".join(
            [
                json.dumps({"type": "session", "id": "native-session"}),
                json.dumps(
                    {
                        "type": "message",
                        "id": "assistant-thinking-text",
                        "message": {
                            "role": "assistant",
                            "model": "fixture-model",
                            "provider": "fixture-provider",
                            "content": [
                                {"type": "thinking", "thinking": "reason first"},
                                {"type": "text", "text": "final answer"},
                            ],
                            "stopReason": "stop",
                            "usage": {"output": 1},
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    rows, _session_id, metadata = pi_transcript_rows(native)
    taxonomy = pi_native_shadow_taxonomy(rows, metadata)
    assistant = next(row for row in rows if row.get("type") == "assistant")

    assert assistant["text"] == "final answer"
    assert assistant["thinking"] == ["reason first"]
    assert metadata["native_shapes"]["message/assistant/text+thinking"] == 1
    assert taxonomy["shadow_classes"]["transcript:assistant_reasoning"] == 1


def test_pi_helm_negative_control_requires_fired_fault_and_expected_rejection() -> None:
    from zerg.qa.pi_family_turn_oracle import negative_control_verdict

    assertions = {"pi_helm_launch_registration": True, "pi_helm_send_idle": True, "pi_helm_steer_active": False}
    observation = {"steer_turn_verdict": {"passed": False, "code": "steer_delivered_as_queued_follow_up"}}
    fired = [{"fault": "pi_steer_as_follow_up", "session_id": "s1"}]

    passed = negative_control_verdict(
        "steer", provider="pi", assertions=assertions, observation=observation, fault_receipts=fired, session_id="s1"
    )
    assert passed["status"] == "pass"
    # No receipt: the failure could be unrelated, so it is inconclusive.
    assert (
        negative_control_verdict(
            "steer", provider="pi", assertions=assertions, observation=observation, fault_receipts=[], session_id="s1"
        )["status"]
        == "inconclusive"
    )
    # Wrong failure reason: also inconclusive.
    timeout = {"steer_turn_verdict": {"passed": False, "code": "steer_prompt_not_unique"}}
    assert (
        negative_control_verdict("steer", provider="pi", assertions=assertions, observation=timeout, fault_receipts=fired, session_id="s1")[
            "status"
        ]
        == "inconclusive"
    )


def test_pi_accounting_includes_tool_rounds_but_never_hides_a_failed_tail(tmp_path) -> None:
    transcript = tmp_path / "native.jsonl"
    events = [
        {"type": "session", "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
        {
            "type": "message",
            "id": "tool-round",
            "message": {
                "role": "assistant",
                "content": [{"type": "toolCall", "id": "read-1", "name": "read", "arguments": {}}],
                "stopReason": "toolUse",
                "model": "fixture-model",
                "usage": {"input": 8, "output": 2, "cost": {"total": 0.2}},
            },
        },
        {
            "type": "message",
            "id": "final-reply",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "The tool finished."}],
                "stopReason": "stop",
                "model": "fixture-model",
                "usage": {"input": 12, "output": 3, "cost": {"total": 0.3}},
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(event) for event in events) + "\n")
    evidence = pi_native_model_evidence(transcript, source_canary="fixture", api_key_configured=True)
    assert evidence is not None
    assert evidence["result_event"]["usage"]["input"] == 20
    assert evidence["result_event"]["usage"]["output"] == 5
    assert evidence["result_event"]["total_cost_usd"] == 0.5

    with transcript.open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "message",
                    "id": "failed-next-turn",
                    "message": {"role": "assistant", "content": [], "stopReason": "error", "errorMessage": "provider failed"},
                }
            )
            + "\n"
        )
    assert pi_native_model_evidence(transcript, source_canary="fixture", api_key_configured=True) is None


@pytest.mark.parametrize(("host_fails", "declared"), ((True, "runtime_host"), (False, None)))
def test_pi_control_identity_wait_declares_the_runtime_host_only_for_a_read_it_failed(monkeypatch, host_fails, declared) -> None:
    from zerg.qa import wait_kind

    def diagnostic(*_args):
        if host_fails:
            raise RuntimeHostError(503, "unavailable")
        # Served, and without the identity: the engine may not have registered the lease yet.
        return {"shadow": {"fact_sources": {"control": {"subject_key": "connection:other:lease"}}}}

    ticks = iter((0.0, 0.0, 31.0))
    monkeypatch.setattr(helm, "_runtime_get", diagnostic)
    monkeypatch.setattr(helm.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(helm.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="timed out waiting for Runtime Host managed control identity") as timed_out:
        helm._wait_runtime_control_identity(
            "https://runtime.example",
            "token",
            family=PI_FAMILY,
            session_id="session-1",
            state={"connection_id": "c", "lease_generation": "l"},
            timeout=30,
            bound=PI_FAMILY.control_bound,
        )
    assert wait_kind.waited_on(timed_out.value) == declared
