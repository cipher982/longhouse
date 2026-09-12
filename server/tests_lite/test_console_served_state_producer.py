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


def test_failure_result_retains_vehicle_identity_and_false_verdicts():
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
    assert result["assertions"] == {
        producer.ASSERTION_LIVE: False,
        producer.ASSERTION_SETTLED: False,
        producer.ASSERTION_CAPABILITY: False,
    }


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
    monkeypatch.setenv(producer.RUNTIME_API_URL_ENV, "http://runtime.invalid")
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
