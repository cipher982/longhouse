"""The factory producer that carries the served-state proof.

`provider_console_lifecycle` drives a real turn and judges the machine.
`product_console_lifecycle` judges the served contract but supplies its own
terminal, with no provider. This producer is the one that does both, which is
the gap the ten-hour wedge fell through.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from zerg.qa import console_served_state as producer
from zerg.qa import console_served_state_core as core


def test_a_clean_report_passes_both_assertions():
    report = _qualified_report()
    assert producer.assertions_from_report(report) == {
        producer.ASSERTION_LIVE: True,
        producer.ASSERTION_SETTLED: True,
        producer.ASSERTION_CAPABILITY: True,
    }


def test_the_wedge_fails_the_settlement_cell_only():
    # The incident: the reply reached the viewer and the state never settled.
    report = {**_qualified_report(), "settle_latency_s": None}
    assertions = producer.assertions_from_report(report)
    assert assertions[producer.ASSERTION_LIVE] is True
    assert assertions[producer.ASSERTION_SETTLED] is False


def test_console_capability_assertion_rejects_helm_semantics():
    report = _qualified_report()
    report["workspace_capability"]["observed"]["input_mode"] = "live"
    assert producer.assertions_from_report(report)[producer.ASSERTION_CAPABILITY] is False


def test_no_live_frames_fails_the_delivery_cell():
    # The iOS symptom: the turn ran, nothing streamed.
    report = {**_qualified_report(), "first_live_frame_s": None, "frame_count": 0}
    assert producer.assertions_from_report(report)[producer.ASSERTION_LIVE] is False


def test_settlement_needs_a_served_reply_not_just_a_latency():
    # A turn that produced nothing has nothing to settle from, so a stray
    # latency must not be read as proof.
    report = {**_qualified_report(), "marker_served": False}
    assert producer.assertions_from_report(report)[producer.ASSERTION_SETTLED] is False


def test_the_factory_product_argv_parses_without_a_provider():
    # The auxiliary vehicle is explicit, but it is not a provider subject
    # argument: the factory supplies its exact binary/version/model separately.
    args = producer._parser().parse_args(["--evidence-root", "/tmp/evidence"])
    assert not hasattr(args, "provider")


def test_vehicle_dispatch_binds_exact_binary_model_and_run(tmp_path):
    provider_bin = tmp_path / "codex"
    provider_bin.write_text("binary", encoding="utf-8")
    claim = {
        "provider": "codex",
        "adapter": "codex_exec",
        "thread_id": "thread-1",
        "provider_identity_confirmed": True,
        "session_id": "session-1",
        "run_id": "run-1",
        "state": "terminal",
        "result": {
            "terminal_state": "run_completed",
            "argv": [str(provider_bin), "exec", "--config", 'model="gpt-5.6-sol"'],
        },
    }

    receipt = producer._vehicle_dispatch_receipt(
        claim,
        provider_bin=provider_bin,
        model="gpt-5.6-sol",
        session_id="session-1",
        thread_id="thread-1",
        run_id="run-1",
    )

    assert receipt["status"] == "pass"

    claim["provider_identity_confirmed"] = False
    assert (
        producer._vehicle_dispatch_receipt(
            claim,
            provider_bin=provider_bin,
            model="gpt-5.6-sol",
            session_id="session-1",
            thread_id="thread-1",
            run_id="run-1",
        )["status"]
        == "fail"
    )


def test_shared_oracle_fails_closed_when_served_run_is_not_retired(monkeypatch):
    from zerg.qa import console_served_state_core as core
    from zerg.qa import live_session_toolkit

    monkeypatch.setattr(
        core,
        "_served_run_retirement",
        lambda *_args, **_kwargs: {
            "retired": False,
            "active_run_count": None,
            "session_id": "session-1",
        },
    )
    monkeypatch.setattr(
        live_session_toolkit,
        "retire_qualification_session",
        lambda *_args, **_kwargs: {
            "status": "pass",
            "hidden": True,
            "archived": True,
            "present_in_served_inventory": False,
        },
    )

    receipt = core._retire_session(
        "http://127.0.0.1:9",
        "token",
        object(),
        "session-1",
        provider="codex",
        report={"run_id": "run-1"},
    )

    assert receipt["status"] == "fail"
    assert receipt["served_run_retired"] is False


def test_shared_oracle_waits_for_machine_adapter_registration(monkeypatch):
    from zerg.qa import console_served_state_core as core

    attempts = 0

    class _Client:
        def request(self, method, path, payload=None):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise core.ApiError(409, "adapter_unavailable")
            return {"session_id": "session-1"}

    monkeypatch.setattr(core.time, "sleep", lambda _seconds: None)
    created = core._create_session(_Client(), {"provider": "codex"}, timeout=1)

    assert created == {"session_id": "session-1"}
    assert attempts == 2


def test_the_result_identity_carries_no_provider_but_still_names_the_vehicle(monkeypatch, tmp_path):
    # A longhouse_product result that names a provider is compared against a
    # contract pinning `provider: null` and rejected as an inadmissible result
    # -- indistinguishable, from the outside, from a malformed producer. The
    # vehicle still has to be recoverable, so it lives beside the identity and
    # in the observation rather than inside what the result claims to be.
    report = _qualified_report()
    from zerg.qa import console_served_state_core as core

    # core.run stamps the vehicle onto the report it returns, so the stub does
    # too -- otherwise this asserts against the stub instead of the producer.
    monkeypatch.setattr(
        core,
        "run",
        lambda args, **_kwargs: {**report, "provider": args.provider},
    )
    result = producer.run_console_served_state(tmp_path, provider="codex", device_id="d", cwd="/tmp/x")
    assert result["provider"] is None
    assert result["vehicle_provider"] == "codex"
    assert result["observation"]["provider"] == "codex"
    assert result["status"] == "pass"


def test_result_status_fails_when_cleanup_receipt_fails(monkeypatch, tmp_path):
    from zerg.qa import console_served_state_core as core

    report = _qualified_report()
    report["cleanup_receipt"] = {
        "status": "fail",
        "archived": False,
        "present_in_served_inventory": True,
        "served_run_retired": False,
    }
    monkeypatch.setattr(
        core,
        "run",
        lambda args, **_kwargs: {**report, "provider": args.provider},
    )

    result = producer.run_console_served_state(tmp_path, provider="codex", device_id="d", cwd="/tmp/x")

    assert result["status"] == "fail"


def test_infrastructure_failure_is_a_typed_harness_failure_with_vehicle_identity():
    result = producer._failure_result(
        model="gpt-5.6-sol",
        provider="codex",
        device_id="factory-machine",
        session_id="session-1",
        failure=RuntimeError("runtime unavailable"),
    )

    assert result["provider"] is None
    assert result["vehicle_provider"] == "codex"
    assert result["vehicle_qualification_model"] == "gpt-5.6-sol"
    assert result["failure_context"] == {"vehicle_device_id": "factory-machine", "session_id": "session-1"}
    # No observation was reached, so no verdict: the factory reports the cause
    # instead of filing three false assertions as product findings.
    assert "observation" not in result and "assertions" not in result


def test_late_failure_keeps_a_failing_verdict_the_run_reached():
    reached = {
        "observation": {"marker_served": False},
        "assertions": {producer.ASSERTION_LIVE: False, producer.ASSERTION_SETTLED: True, producer.ASSERTION_CAPABILITY: True},
    }
    result = producer._failure_result(
        model="m", provider="codex", device_id="d", session_id="s", failure=RuntimeError("cleanup"), reached=reached
    )

    assert result["observation"] == {"marker_served": False}
    assert result["assertions"][producer.ASSERTION_LIVE] is False


def test_cleanup_failure_after_every_assertion_held_is_a_harness_failure():
    reached = {
        "observation": {"marker_served": True},
        "assertions": {producer.ASSERTION_LIVE: True, producer.ASSERTION_SETTLED: True, producer.ASSERTION_CAPABILITY: True},
    }
    result = producer._failure_result(
        model="m", provider="codex", device_id="d", session_id="s", failure=RuntimeError("cleanup"), reached=reached
    )

    assert "observation" not in result and "assertions" not in result


def _qualified_report():
    return {
        "first_live_frame_s": 0.9,
        "frame_count": 24,
        "marker_served": True,
        "settle_latency_s": 0.3,
        "assistant_reply_complete": True,
        "duplicate_assistant_marker_seen": False,
        "assistant_marker_after_settlement": {"exactly_once": True, "event_count": 1, "marker_count": 1},
        "workspace_capability": {
            "status": "pass",
            "mode": "console",
            "observed": {
                "input_mode": "console",
                "can_start_turn": True,
                "composer_enabled": True,
                "composer_disabled_reason": None,
            },
            "missing_fields": [],
            "ready": True,
        },
        "cleanup_receipt": {
            "status": "pass",
            "hidden": True,
            "archived": True,
            "present_in_served_inventory": False,
            "served_run_retired": True,
        },
    }


def test_legacy_prompt_echo_report_cannot_qualify_reply_content():
    report = {"first_live_frame_s": 0.9, "frame_count": 24, "marker_served": True, "settle_latency_s": 0.3}
    assert producer.assertions_from_report(report)[producer.ASSERTION_SETTLED] is False


def test_stream_failure_cannot_qualify_early_live_frames():
    report = {**_qualified_report(), "stream_error": "stream ended"}
    assert producer.assertions_from_report(report)[producer.ASSERTION_LIVE] is False


MARKER = "LH_SERVED_FINAL"


def _assistant(**changes):
    return {"id": "reply-1", "role": "assistant", "event_origin": "durable", "content_text": MARKER, **changes}


def _workspace(events, *, lifecycle="ended", convergence="current", run_id="run-1", **projection):
    working = lifecycle == "running"
    return {
        "projection": {
            "items": [{"kind": "event", "session_id": "session-1", "event": event} for event in events],
            "has_more": False,
            "page_offset": 0,
            **projection,
        },
        "session": {
            "capabilities": {
                "input_mode": "console",
                "can_start_turn": True,
                "composer_enabled": True,
                "composer_disabled_reason": None,
            },
            "session_state": {
                "run": {"id": run_id, "lifecycle": lifecycle},
                "activity": {"state": "thinking" if working else "idle"},
                "presentation": {"primary": {"key": "thinking" if working else "done"}},
                "working_set": "active" if working else "history",
                "transcript": {"convergence": convergence},
            },
        },
    }


@pytest.fixture
def observation(monkeypatch):
    clock = SimpleNamespace(now=100.0)
    sent = []
    state = SimpleNamespace(responses=[], reads=0, error=None, closed=False)

    def advance(seconds):
        clock.now += seconds

    class Client:
        def request(self, _method, _path, payload):
            sent.append(payload["message"])
            advance(2.0)
            if state.error == "dispatch":
                raise RuntimeError("dispatch failed")
            return {"state": "active", "run_id": "run-1", "turn_id": "turn-1"}

        def served_workspace(self, _session_id):
            advance(0.25)
            value = state.responses[min(state.reads, len(state.responses) - 1)]
            state.reads += 1
            if isinstance(value, Exception):
                raise value
            return value

    class Watcher:
        error = None
        drains = 0

        def start(self):
            pass

        def drain(self):
            self.drains += 1
            if self.drains == 1:
                return [(clock.now, "connected", {}), (clock.now, "workspace_changed", {"pubsub_seq": 1})]
            return [(clock.now, "workspace_changed", {"pubsub_seq": self.drains})]

        def close(self):
            state.closed = True

    watcher = Watcher()
    monkeypatch.setattr(core, "StreamWatcher", lambda *_args: watcher)
    monkeypatch.setattr(core.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(core.time, "sleep", advance)
    args = SimpleNamespace(watch_session=None, turn_timeout=4, settle_budget=4)

    def run(responses, *, error=None):
        state.responses = responses
        state.error = error
        return core._observe_turn(Client(), args, {}, "session-1", MARKER)

    return SimpleNamespace(run=run, state=state, sent=sent)


@pytest.mark.parametrize(
    "events",
    [
        [_assistant(role="user")],
        [_assistant(role="tool")],
        [_assistant(tool_name="shell")],
        [_assistant(content_text="unrelated", metadata={"marker": MARKER})],
        [_assistant(content_text={"metadata": MARKER})],
        [_assistant(event_origin="live_provisional", provisional_complete=False)],
        [_assistant(event_origin=None)],
        [_assistant(), _assistant(id="reply-2")],
        [_assistant(content_text=f"{MARKER} {MARKER}")],
    ],
    ids=["prompt", "tool-output", "tool-call", "metadata", "structured-content", "partial", "unknown-origin", "two-events", "two-markers"],
)
def test_non_reply_or_duplicate_evidence_cannot_pass_shared_oracle(observation, events):
    report = observation.run([_workspace(events)])
    assert report["verdict"] == "red"
    assert producer.assertions_from_report(report)[producer.ASSERTION_SETTLED] is False
    assert observation.state.closed is True


@pytest.mark.parametrize(
    "final",
    [
        _workspace([]),
        _workspace([_assistant(), _assistant(id="reply-2")]),
        _workspace([_assistant()], convergence="lagging"),
        _workspace([_assistant()], run_id="previous-run"),
        _workspace([_assistant()], has_more=True),
    ],
    ids=["reply-disappeared", "duplicate-arrived", "transcript-incomplete", "wrong-run", "partial-page"],
)
def test_early_assistant_evidence_must_survive_final_settlement(observation, final):
    report = observation.run([_workspace([_assistant()], lifecycle="running"), final])
    assert report["marker_served"] is True
    assert report["assistant_reply_complete"] is False
    assert report["settle_latency_s"] is None
    assert report["verdict"] == "red"
    assert producer.assertions_from_report(report)[producer.ASSERTION_SETTLED] is False


def test_genuine_final_reply_records_event_identity_and_honest_detection_boundaries(observation):
    report = observation.run(
        [
            _workspace([_assistant(role="user")], lifecycle="running"),
            _workspace([_assistant()], lifecycle="running"),
            _workspace([_assistant()]),
        ]
    )
    assert report["verdict"] == "green"
    assert all(producer.assertions_from_report(report).values())
    assert MARKER not in observation.sent[0]
    assert report["assistant_marker_after_settlement"]["event_ids"] == ["reply-1"]
    assert report["assistant_marker_after_settlement"]["marker_counts"] == [1]
    timing = report["timing"]
    samples = report["workspace_samples"]
    assert timing["dispatch_receipt_at_s"] - timing["dispatch_started_at_s"] == 2.0
    assert timing["first_assistant_served_at_s"] == samples[1]["response_received_at_s"]
    assert samples[0]["response_received_at_s"] < samples[1]["request_started_at_s"] < timing["first_assistant_served_at_s"]
    assert report["first_live_frame_s"] < report["marker_latency_s"]
    assert timing["first_assistant_served_at_s"] < timing["settled_at_s"]
    assert report["settle_latency_s"] == timing["settled_at_s"] - timing["first_assistant_served_at_s"]
    assert observation.state.closed is True


def test_native_turn_failure_is_not_reported_as_minutes_of_transcript_lag(observation):
    failed = _workspace([])
    failed["session"]["session_state"]["run"]["end_reason"] = "failed"
    report = observation.run([failed, RuntimeError("polled after a known terminal failure")])
    assert report["verdict"] == "red"
    assert report["terminal_failure"]["end_reason"] == "failed"
    assert report["marker_served"] is False
    assert observation.state.closed is True


def test_old_failed_run_does_not_abort_the_current_turn(observation):
    old = _workspace([], run_id="old-run")
    old["session"]["session_state"]["run"]["end_reason"] = "failed"
    report = observation.run([old, _workspace([_assistant()]), _workspace([_assistant()])])
    assert report["verdict"] == "green"
    assert report.get("terminal_failure") is None


@pytest.mark.parametrize("stage", ["dispatch", "workspace"])
def test_exception_closes_the_watched_stream(observation, stage):
    with pytest.raises(RuntimeError, match=f"{stage} failed"):
        observation.run([RuntimeError("workspace failed")], error=stage)
    assert observation.state.closed is True


def test_dispatch_failure_without_vehicle_claim_cannot_certify_cleanup(monkeypatch, tmp_path):
    monkeypatch.setenv(producer.RUNTIME_API_URL_ENV, "http://127.0.0.1:9")
    monkeypatch.setenv(producer.RUNTIME_AGENTS_TOKEN_ENV, "not-a-credential")
    monkeypatch.setattr(producer, "isolated_provider_home", lambda: tmp_path / "home")
    monkeypatch.setattr(producer.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="codex-cli fixture"))
    monkeypatch.setattr(producer, "login_with_api_key", lambda *_args, **_kwargs: {"status": "pass"})
    monkeypatch.setattr(
        producer,
        "start_transcript_shipper",
        lambda *_args, **_kwargs: SimpleNamespace(
            receipt={"machine_name": "isolated-machine"},
            stop=lambda: {"stopped": True, "process_dead": True, "process_group_dead": True},
        ),
    )
    monkeypatch.setattr(core.Client, "request", lambda *_args, **_kwargs: {"hidden": True})
    monkeypatch.setattr(producer, "retire_qualification_session", lambda *_args, **_kwargs: {"status": "pass"}, raising=False)
    monkeypatch.setattr(producer, "_terminate_live_qualification_session", lambda *_args: {"status": "fail"}, raising=False)
    monkeypatch.setattr(producer, "_wait_served_run_retirement", lambda *_args: {"retired": False}, raising=False)

    def dispatch_failure(_root, **kwargs):
        kwargs["on_session_created"]("owned-session")
        raise RuntimeError("dispatch response lost before vehicle claim")

    monkeypatch.setattr(producer, "run_console_served_state", dispatch_failure)
    evidence = tmp_path / "evidence"
    vehicle = tmp_path / "vehicle"
    vehicle.write_bytes(b"fixture")
    exit_code = producer.main(
        [
            "--evidence-root",
            str(evidence),
            "--engine",
            str(vehicle),
            "--provider-bin",
            str(vehicle),
            "--provider-version",
            "fixture",
            "--repo-root",
            str(tmp_path),
            "--model",
            "fixture",
        ]
    )
    cleanup = json.loads((evidence / "cleanup-receipt.json").read_text())
    assert exit_code == 1
    assert "dispatch response lost" in json.loads((evidence / "result.json").read_text())["error"]
    assert cleanup["status"] == "fail"
    assert cleanup["requirements"]["no_orphan_provider_processes"] is False


def test_cancellation_retires_the_created_session_and_started_run(monkeypatch, tmp_path):
    active = {"session-1": "run-1"}
    monkeypatch.setattr(core, "_defaults", lambda: ("http://127.0.0.1:9", "test-only"))
    monkeypatch.setattr(core, "_create_session", lambda *_args: {"session_id": "session-1"})

    def interrupted(_client, _args, report, _session_id, _marker):
        report["run_id"] = "run-1"
        raise KeyboardInterrupt

    def retire(_url, _token, _client, session_id, *, provider, report):
        assert report["run_id"] == active.pop(session_id)
        return {"status": "pass"}

    monkeypatch.setattr(core, "_observe_turn", interrupted)
    monkeypatch.setattr(core, "_retire_session", retire)
    args = SimpleNamespace(api_url=None, provider="codex", device_id="owned", cwd=tmp_path, drop_terminal=False)
    with pytest.raises(KeyboardInterrupt):
        core.run(args)
    assert active == {}


_MARKER = "LH_SERVED_alpha_bravo_charlie"
# Narration ahead of a tool call names the marker inside a sentence; the final
# reply puts a line about the work first and the marker alone on its last line.
_NARRATION = f"The sleep has not returned yet, so I may not reply with `{_MARKER}`."
_FINAL_REPLY = f"The command finished.\n\n{_MARKER}"


def _served_event(event_id: str, text: object, **fields: object) -> dict:
    return {"id": event_id, "role": "assistant", "event_origin": "durable", "content_text": text, **fields}


def test_served_marker_events_are_the_answers_and_mentions_are_kept_apart():
    events = [
        {"id": "prompt", "role": "user", "event_origin": "durable", "content_text": f"Reply with only {_MARKER}."},
        _served_event("narration", _NARRATION),
        # A tool call is served as its own assistant event; its text is not a reply.
        _served_event("narration-tool-call-1", _MARKER, tool_name="shell"),
        _served_event("streaming", _MARKER, event_origin="live_provisional"),
        _served_event("structured", {"marker": _MARKER}),
        _served_event("reply", _FINAL_REPLY),
    ]

    assert [event["id"] for event in core.assistant_marker_events(events, _MARKER)] == ["reply"]
    assert [event["id"] for event in core.assistant_marker_mentions(events, _MARKER)] == ["narration"]
    assert core.served_answer_fault(events, _MARKER, {"reply": 1}) is None
    assert core.served_answer_correspondence(events, _MARKER, {"reply": 1})["converged"] is True

    workspace = {
        "projection": {
            "has_more": False,
            "page_offset": 0,
            "items": [{"kind": "event", "session_id": "session-1", "event": event} for event in events],
        }
    }
    evidence = core.assistant_marker_evidence(workspace, "session-1", _MARKER)
    assert evidence["exactly_once"] is True
    assert evidence["event_ids"] == ["reply"]
    assert evidence["marker_counts"] == [1]


def test_a_page_converges_when_it_serves_the_providers_own_answers_however_many():
    """The 2026-10-11 11:23Z OMP background run: the model answered, OMP woke it when the second
    background job ended, and it answered again. Two rows of OMP's transcript, served as two events."""
    first = _served_event(
        "f1b18140", f"Both native terminal events are in: the command job exited 0 and the worker completed.\n\n{_MARKER}"
    )
    second = _served_event("adf024ae", _MARKER)
    native = {"f1b18140": 1, "adf024ae": 1}

    found = core.served_answer_correspondence([first, second], _MARKER, native)
    assert found["converged"] is True
    assert found["served_answer_event_ids"] == ["f1b18140", "adf024ae"]
    assert found["served_answer_rows"] == native == found["native_answer_rows"]
    assert core.served_answer_fault([first, second], _MARKER, native) is None

    # The page has not reached the second answer yet: lag, not a fault and not convergence.
    behind = core.served_answer_correspondence([first], _MARKER, native)
    assert behind["converged"] is False and behind["unserved_native_row_ids"] == ["adf024ae"]
    assert core.served_answer_fault([first], _MARKER, native) is None


def test_narration_that_quotes_the_marker_is_no_answer_on_either_side():
    """The 2026-10-11 02:10Z case: a turn narrated the marker ahead of a tool call, then answered once."""
    narration = _served_event("narration", _NARRATION)
    reply = _served_event("reply", _FINAL_REPLY)

    found = core.served_answer_correspondence([narration, reply], _MARKER, {"reply": 1})
    assert found["converged"] is True and found["served_answer_event_ids"] == ["reply"]
    assert core.served_answer_fault([narration, reply], _MARKER, {"reply": 1}) is None


def test_a_served_answer_the_providers_transcript_does_not_account_for_is_a_typed_fault():
    narration = _served_event("narration", _NARRATION)
    reply = _served_event("reply", _FINAL_REPLY)

    # One native answer, and the page serves that row twice.
    twice = core.served_answer_fault([narration, reply, dict(reply)], _MARKER, {"reply": 1})
    assert twice is not None and twice.reason == core.NATIVE_ANSWER_SERVED_MORE_THAN_ONCE
    assert twice.answer_event_ids == ["reply", "reply"] and twice.mention_event_ids == ["narration"]
    assert str(twice).startswith("native_answer_served_more_than_once: ")
    # The same row's answer under a second event of that row.
    split = core.served_answer_fault([reply, _served_event("reply-text-1", _MARKER)], _MARKER, {"reply": 1})
    assert split is not None and split.reason == core.NATIVE_ANSWER_SERVED_MORE_THAN_ONCE
    # An answering event that belongs to no answering row of the transcript.
    stray = core.served_answer_fault([reply, _served_event("again", _MARKER)], _MARKER, {"reply": 1})
    assert stray is not None and stray.reason == core.SERVED_ANSWER_WITHOUT_NATIVE_ROW
    assert stray.answer_event_ids == ["reply", "again"]
    assert "belong to none" in str(stray) and "The command finished" not in str(stray)
    # Each fault says which ids it was decided from.
    assert (twice.about, split.about, stray.about) == (("reply",), ("reply",), ("again",))

    # A row that answers in its second text block is served under the row's id plus a suffix.
    later_block = _served_event("reply-text-1", _MARKER)
    assert core.served_answer_correspondence([later_block], _MARKER, {"reply": 1})["converged"] is True
    # A row that answers twice is served as it stands.
    both = [_served_event("reply", _MARKER), _served_event("reply-text-1", _MARKER)]
    assert core.served_answer_correspondence(both, _MARKER, {"reply": 2})["converged"] is True

    # Marker only in narration. Until the page carries the row that answered,
    # the reply may simply not be ingested yet: no fault, the caller keeps waiting.
    assert core.served_answer_fault([narration], _MARKER, {"reply": 1}) is None
    assert core.served_answer_fault([narration], _MARKER, {}) is None
    assert core.served_answer_correspondence([narration], _MARKER, {})["converged"] is False
    # Marker never answered, and the page already carries the reply row.
    unanswered = core.served_answer_fault([narration, _served_event("reply", "Done.")], _MARKER, {"reply": 1})
    assert unanswered is not None and unanswered.reason == core.MARKER_REPLY_SERVED_WITHOUT_ANSWER
    assert unanswered.answer_event_ids == [] and unanswered.mention_event_ids == ["narration"]
    assert unanswered.about == ("reply",)


def test_only_a_transcript_read_to_the_end_of_a_complete_line_is_a_transcript_that_was_read():
    assert core.native_transcript_state(None) == core.NATIVE_TRANSCRIPT_UNREADABLE
    assert core.native_transcript_state(b"") == core.NATIVE_TRANSCRIPT_EMPTY
    assert core.native_transcript_state(b"\n \n") == core.NATIVE_TRANSCRIPT_EMPTY
    assert core.native_transcript_state(b'{"type":"message"}\n{"type":"mess') == core.NATIVE_TRANSCRIPT_MID_WRITE
    assert core.native_transcript_state(b'{"type":"message"}\n') == core.NATIVE_TRANSCRIPT_READ


def test_a_served_answer_fault_is_raised_only_once_it_has_stood(monkeypatch):
    """The provider may have appended the row between the engine shipping it and the read that missed it."""

    clock = [100.0]
    monkeypatch.setattr(core.time, "monotonic", lambda: clock[0])
    reply = _served_event("reply", _FINAL_REPLY)

    def stray() -> core.MarkerAnswerCountError:
        fault = core.served_answer_fault([reply, _served_event("again", _MARKER)], _MARKER, {"reply": 1})
        assert fault is not None
        return fault

    refusal = core.StandingServedAnswerFault()
    assert refusal.standing(stray()) is None
    clock[0] += core.SERVED_ANSWER_SETTLE_SECONDS - 0.1
    assert refusal.standing(stray()) is None
    clock[0] += 0.2
    assert refusal.standing(stray()).reason == core.SERVED_ANSWER_WITHOUT_NATIVE_ROW

    # A read that could not judge, or that found nothing wrong, starts the clock again.
    refusal = core.StandingServedAnswerFault()
    assert refusal.standing(stray()) is None
    clock[0] += 10
    assert refusal.standing(None) is None
    assert refusal.standing(stray()) is None
    # So does a different fault: it is not the one that was standing.
    clock[0] += 10
    twice = core.served_answer_fault([reply, dict(reply)], _MARKER, {"reply": 1})
    assert refusal.standing(twice) is None
    clock[0] += core.SERVED_ANSWER_SETTLE_SECONDS
    assert refusal.standing(twice) is twice


def _clocked(monkeypatch) -> list[float]:
    clock = [100.0]
    monkeypatch.setattr(core.time, "monotonic", lambda: clock[0])
    return clock


def test_a_standing_fault_is_the_same_reason_about_the_same_ids(monkeypatch):
    clock = _clocked(monkeypatch)
    reply = _served_event("reply", _FINAL_REPLY)
    stray = _served_event("again", _MARKER)

    def fault(*events: dict) -> core.MarkerAnswerCountError:
        found = core.served_answer_fault(list(events), _MARKER, {"reply": 1})
        assert found is not None and found.reason == core.SERVED_ANSWER_WITHOUT_NATIVE_ROW
        return found

    # The page keeps growing beside the fault: other events, and even another
    # event that only mentions the marker, are not the fault and do not restart the clock.
    refusal = core.StandingServedAnswerFault()
    assert refusal.standing(fault(reply, stray)) is None
    clock[0] += core.SERVED_ANSWER_SETTLE_SECONDS / 2
    assert refusal.standing(fault(reply, stray, _served_event("later", "Still working."))) is None
    clock[0] += core.SERVED_ANSWER_SETTLE_SECONDS / 2
    grown = fault(reply, stray, _served_event("later", "Still working."), _served_event("narration", _NARRATION))
    assert refusal.standing(grown) is grown

    # The same reason about another event is another fault: the first one stood
    # for the settle time, this one has only just been seen.
    refusal = core.StandingServedAnswerFault()
    assert refusal.standing(fault(reply, stray)) is None
    clock[0] += core.SERVED_ANSWER_SETTLE_SECONDS
    other = fault(reply, _served_event("another", _MARKER))
    assert other.about == ("another",)
    assert refusal.standing(other) is None
    clock[0] += core.SERVED_ANSWER_SETTLE_SECONDS
    assert refusal.standing(fault(reply, _served_event("another", _MARKER))).about == ("another",)


def test_a_fault_still_on_the_page_at_the_deadline_is_what_the_wait_reports(monkeypatch):
    clock = _clocked(monkeypatch)
    reply = _served_event("reply", _FINAL_REPLY)

    # A fault whose ids change on every read never stands, and is still a fault of that reason.
    refusal = core.StandingServedAnswerFault()
    assert refusal.at_deadline() is None
    for number in range(4):
        stray = core.served_answer_fault([reply, _served_event(f"again-{number}", _MARKER)], _MARKER, {"reply": 1})
        assert refusal.standing(stray) is None
        clock[0] += core.SERVED_ANSWER_SETTLE_SECONDS / 2
    assert refusal.at_deadline() is stray
    assert refusal.at_deadline().reason == core.SERVED_ANSWER_WITHOUT_NATIVE_ROW

    # One seen only on the last read has not settled: the wait timed out.
    refusal = core.StandingServedAnswerFault()
    assert refusal.standing(stray) is None
    clock[0] += core.SERVED_ANSWER_SETTLE_SECONDS / 2
    assert refusal.at_deadline() is None
    # Nor is one that a later read no longer found.
    clock[0] += core.SERVED_ANSWER_SETTLE_SECONDS
    assert refusal.standing(None) is None
    assert refusal.at_deadline() is None


def test_a_recalled_phrase_is_a_reply_wherever_the_model_puts_it():
    phrase = "LH_FIXTURE_CONTEXT_0123456789abcdef"
    events = [_served_event("reply", f"The phrase I was asked to remember is {phrase}.\n\n{_MARKER}")]

    assert core.assistant_marker_events(events, phrase) == []
    assert [event["id"] for event in core.assistant_marker_events(events, phrase, recalled=True)] == ["reply"]
    assert core.assistant_marker_mentions(events, phrase, recalled=True) == []
    assert [event["id"] for event in core.assistant_marker_events(events, _MARKER, alongside=(phrase,))] == ["reply"]
