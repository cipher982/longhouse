from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest

from zerg.qa import console_served_state
from zerg.qa import omp_console_producer
from zerg.qa import pi_console_tool_producer
from zerg.qa import product_console_lifecycle
from zerg.qa import provider_console_lifecycle
from zerg.qa import provider_generic_resume
from zerg.qa import title_dependency_live_producer
from zerg.qa import title_dependency_recovery_producer
from zerg.qa import transcript_search_producer
from zerg.qa import workspace_suggestions_live_producer
from zerg.qa.producer_shell import Run


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _assert_result(root: Path, result: dict[str, object]) -> None:
    assert _json(root / "result.json") == result
    assert result["status"] in {"fail", "inconclusive"}


def test_console_served_state_entrypoint_retains_late_stream_failure(monkeypatch, tmp_path):
    from zerg.qa import console_served_state_core

    root = tmp_path / "served"
    root.mkdir()
    report = {
        "first_live_frame_s": 0.12,
        "frame_count": 3,
        "stream_error": None,
        "marker_served": False,
        "assistant_reply_complete": False,
        "assistant_marker_after_settlement": {"exactly_once": False, "event_count": 0, "marker_count": 0},
        "cleanup_receipt": {"status": "fail", "archived": True, "present_in_served_inventory": True, "served_run_retired": False},
    }
    monkeypatch.setattr(console_served_state_core, "run", lambda _args, on_session_created=None: report)
    run = Run(root=root)
    console_served_state.observe_console_served_state(
        run, provider="codex", device_id="failure-machine", cwd="/workspace/served", model="model"
    )
    result = console_served_state.PRODUCER.verdict(run)
    assert result["status"] == "fail"
    assert result["observation"]["first_live_frame_s"] == 0.12
    assert result["assertions"][console_served_state.ASSERTION_LIVE] is True
    assert result["assertions"][console_served_state.ASSERTION_SETTLED] is False
    assert _json(root / "console-served-state-observation.json")["frame_count"] == 3


def _late_provider_boundary(monkeypatch, lifecycle, tmp_path, provider: str):
    provider_bin = tmp_path / f"{provider}-provider"
    provider_bin.write_text("provider fixture\n", encoding="utf-8")
    provider_bin.chmod(0o755)
    engine = tmp_path / "engine"
    engine.write_text("engine fixture\n", encoding="utf-8")
    engine.chmod(0o755)
    cli = tmp_path / "longhouse"
    cli.write_text("cli fixture\n", encoding="utf-8")
    cli.chmod(0o755)
    monkeypatch.setenv(lifecycle.RUNTIME_API_URL_ENV, "http://127.0.0.1:9")
    monkeypatch.setenv(lifecycle.RUNTIME_AGENTS_TOKEN_ENV, "test-runtime-token")
    monkeypatch.setenv("LONGHOUSE_QUALIFICATION_SANDBOX", "provider-qualification-bwrap-v3")
    qualification_home = tmp_path / "qualification-home"
    qualification_home.mkdir()
    monkeypatch.setenv("LONGHOUSE_QUALIFICATION_HOME", str(qualification_home))
    monkeypatch.setenv("HOME", str(qualification_home))

    class Shipper:
        receipt = {"machine_name": "late-failure-machine"}

        def stop(self):
            return {"stopped": True, "process_dead": True, "process_group_dead": True}

    monkeypatch.setattr(
        lifecycle, "_provider_environment", lambda _provider, _args, home: {"LONGHOUSE_HOME": str(home), "CODEX_API_KEY": "fixture-key"}
    )
    monkeypatch.setattr(lifecycle, "login_with_api_key", lambda *_args, **_kwargs: {"status": "pass", "authenticated": True})
    monkeypatch.setattr(lifecycle, "require_disposable_runtime", lambda _url: None)
    monkeypatch.setattr(lifecycle, "_probe_version", lambda _provider, _binary: ("fixture-1", "fixture-1"))
    monkeypatch.setattr(lifecycle, "start_transcript_shipper", lambda *_args, **_kwargs: Shipper())
    monkeypatch.setattr(
        lifecycle, "_create_session", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("late create-session boundary failed"))
    )
    monkeypatch.setattr(
        lifecycle,
        "retire_qualification_session",
        lambda *_args, **_kwargs: {"status": "pass", "archived": True, "hidden": True, "present_in_served_inventory": False},
    )
    return provider_bin, engine, cli


def _provider_args(root, provider_bin, engine, cli, provider):
    variant = provider_console_lifecycle.SUPPORTED_VARIANT
    return [
        "--provider",
        provider,
        "--variant",
        variant,
        "--evidence-root",
        str(root),
        "--repo-root",
        str(root.parent),
        "--engine",
        str(engine),
        "--longhouse-cli",
        str(cli),
        "--provider-bin",
        str(provider_bin),
        "--provider-version",
        "fixture-1",
        "--model",
        "fixture-model",
    ]


def test_provider_console_lifecycle_main_retains_boot_receipts_on_late_failure(monkeypatch, tmp_path):
    root = tmp_path / "provider"
    provider_bin, engine, cli = _late_provider_boundary(monkeypatch, provider_console_lifecycle, tmp_path, "codex")
    assert provider_console_lifecycle.main(_provider_args(root, provider_bin, engine, cli, "codex")) == 1
    result = _json(root / "result.json")
    assert result["status"] == "fail"
    assert result["failure_code"] == "provider_console_lifecycle_failed"
    assert "late create-session boundary failed" in result["error"]
    assert _json(root / "provider-binary-receipt.json")["version"] == "fixture-1"
    assert _json(root / "cleanup-receipt.json")["shipper_stop_verified"] is True


def test_pi_console_tool_main_retains_boot_receipts_on_late_failure(monkeypatch, tmp_path):
    root = tmp_path / "pi"
    provider_bin, engine, cli = _late_provider_boundary(monkeypatch, provider_console_lifecycle, tmp_path, "pi")
    args = [
        "--variant",
        pi_console_tool_producer._VARIANT,
        "--evidence-root",
        str(root),
        "--repo-root",
        str(root.parent),
        "--engine",
        str(engine),
        "--longhouse-cli",
        str(cli),
        "--provider-bin",
        str(provider_bin),
        "--provider-version",
        "fixture-1",
        "--model",
        "fixture-model",
    ]
    assert pi_console_tool_producer.main(args) == 1
    result = _json(root / "result.json")
    assert result["status"] == "fail"
    assert result["failure_code"] == "pi_console_tool_lifecycle_failed"
    assert _json(root / "provider-binary-receipt.json")["provider"] == "pi"
    assert _json(root / "cleanup-receipt.json")["shipper_stop_verified"] is True


def test_omp_console_main_retains_boot_receipts_on_late_failure(monkeypatch, tmp_path):
    root = tmp_path / "omp"
    provider_bin, engine, cli = _late_provider_boundary(monkeypatch, provider_console_lifecycle, tmp_path, "omp")
    args = [
        "--variant",
        omp_console_producer._VARIANT,
        "--evidence-root",
        str(root),
        "--repo-root",
        str(root.parent),
        "--engine",
        str(engine),
        "--longhouse-cli",
        str(cli),
        "--provider-bin",
        str(provider_bin),
        "--provider-version",
        "fixture-1",
        "--model",
        "fixture-model",
    ]
    assert omp_console_producer.main(args) == 1
    result = _json(root / "result.json")
    assert result["status"] == "fail"
    assert result["failure_code"] == "omp_console_lifecycle_failed"
    assert _json(root / "provider-binary-receipt.json")["provider"] == "omp"
    assert _json(root / "cleanup-receipt.json")["shipper_stop_verified"] is True


_PAST_THE_NATIVE_REPLY = "fixture stops the turn after its native reply was bound"


def _console_first_turn_result(monkeypatch, tmp_path, provider: str, assistant_rows, *, with_stream: bool = False) -> dict[str, object]:
    """Run a Pi or OMP Console entry point over one faked first turn and return its result.

    Everything up to the provider's own transcript is faked; the transcript is
    read by the producer's real native-evidence step. ``assistant_rows`` builds
    the rows that follow the tool result from the turn's marker and stored
    context value. A turn whose native reply is bound stops at the next
    boundary (the transcript flush) with ``_PAST_THE_NATIVE_REPLY``.
    ``with_stream`` also writes the turn's output stream, with the same
    prompt and replies, as the provider process would have.
    """

    lifecycle = provider_console_lifecycle
    provider_bin, engine, cli = _late_provider_boundary(monkeypatch, lifecycle, tmp_path, provider)
    native = tmp_path / "native-session.jsonl"
    prompts: list[str] = []

    class Shipper:
        receipt = {"machine_name": "first-turn-machine"}

        def flush(self, _label):
            raise RuntimeError(_PAST_THE_NATIVE_REPLY)

        def stop(self):
            return {"stopped": True, "process_dead": True, "process_group_dead": True}

    def start_turn(*, message, **_kwargs):
        prompts.append(message)
        return {"run_id": "run-1", "turn_id": "turn-1"}

    def wait_claim(_path, **_kwargs):
        marker = re.search(rf"LH_{provider.upper()}_CONSOLE_[0-9a-f]{{32}}", prompts[0]).group(0)
        context = re.search(rf"LH_{provider.upper()}_CONTEXT_[0-9a-f]{{32}}", prompts[0]).group(0)
        rows = [
            {"type": "session", "id": "native-1"},
            {"type": "message", "id": "prompt", "message": {"role": "user", "content": prompts[0]}},
            {
                "type": "message",
                "id": "tool-call",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": f"I will read the file, then reply with `{marker}`."},
                        {"type": "toolCall", "id": "call-1", "name": "read", "arguments": {}},
                    ],
                },
            },
            {
                "type": "message",
                "id": "tool-result",
                "message": {"role": "toolResult", "toolCallId": "call-1", "toolName": "read", "content": marker},
            },
            *(
                {"type": "message", "id": row_id, "message": {"role": "assistant", "content": text}}
                for row_id, text in assistant_rows(marker, context)
            ),
        ]
        native.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        claim = {
            "state": "terminal",
            "result": {"terminal_state": "run_completed"},
            "source_path": str(native),
            "provider_thread_id": "native-1",
        }
        if with_stream:
            replies = [{"role": "assistant", "content": text} for _row_id, text in assistant_rows(marker, context)]
            events = [{"type": "message_end", "message": message} for message in ({"role": "user", "content": prompts[0]}, *replies)]
            stream = tmp_path / "turn-stdout.jsonl"
            stream.write_text("".join(json.dumps(event) + "\n" for event in [*events, {"type": "agent_end"}]), encoding="utf-8")
            claim["stdout_path"] = str(stream)
        return claim

    monkeypatch.setattr(lifecycle, "start_transcript_shipper", lambda *_args, **_kwargs: Shipper())
    monkeypatch.setattr(lifecycle, "_create_session", lambda **_kwargs: {"session_id": "session-1", "thread_id": "thread-1"})
    monkeypatch.setattr(lifecycle, "_start_turn", start_turn)
    monkeypatch.setattr(lifecycle, "_wait_claim", wait_claim)
    monkeypatch.setattr(lifecycle, "turn_identity_mismatches", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(lifecycle, "_claim_uses_provider_binary", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(lifecycle, "_claim_uses_selected_model", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(lifecycle, "_wait_turn_terminal", lambda **_kwargs: None)
    monkeypatch.setattr(lifecycle, "_terminate_live_qualification_session", lambda *_args: {"status": "pass", "dispatched": True})
    monkeypatch.setattr(lifecycle, "_force_cleanup", lambda _claims: None)
    monkeypatch.setattr(lifecycle, "_wait_served_run_retirement", lambda *_args: {"retired": True, "active_run_count": 0})

    producer = {"pi": pi_console_tool_producer, "omp": omp_console_producer}[provider]
    root = tmp_path / provider
    args = [
        "--variant",
        producer._VARIANT,
        "--evidence-root",
        str(root),
        "--repo-root",
        str(root.parent),
        "--engine",
        str(engine),
        "--longhouse-cli",
        str(cli),
        "--provider-bin",
        str(provider_bin),
        "--provider-version",
        "fixture-1",
        "--model",
        "fixture-model",
    ]
    assert producer.main(args) == 1
    result = _json(root / "result.json")
    assert result["status"] == "fail"
    assert result["failure_code"] == f"{producer.SCENARIO_ID}_failed"
    return result


@pytest.mark.parametrize("provider", ["pi", "omp"])
def test_console_first_turn_that_narrates_its_marker_gets_past_the_native_reply(monkeypatch, tmp_path, provider):
    # The row that calls the tool names the marker, and one later row answers
    # with the stored context value and the marker.
    result = _console_first_turn_result(
        monkeypatch, tmp_path, provider, lambda marker, context: [("reply", f"The read returned.\n\n{context} {marker}")]
    )

    assert result["error"] == f"RuntimeError: {_PAST_THE_NATIVE_REPLY}"


@pytest.mark.parametrize("provider", ["pi", "omp"])
def test_console_first_turn_answered_in_two_rows_is_refused_with_the_typed_reason(monkeypatch, tmp_path, provider):
    result = _console_first_turn_result(
        monkeypatch, tmp_path, provider, lambda marker, context: [("reply", f"{context} {marker}"), ("reply-again", marker)]
    )

    # A producer failure, never an assertion verdict; the reason opens the error.
    assert "assertions" not in result
    assert result["error"].startswith("MarkerAnswerCountError: marker_answered_more_than_once: ")
    assert "has 2 answering native assistant row(s), expected exactly 1" in result["error"]
    # The failure alone says which count this was (the provider's own rows of a
    # single-reply turn, not a served page) and which rows.
    assert "expected exactly 1 (single-reply turn: the provider itself answered in more than one row)" in result["error"]
    assert "answering row ids ['reply', 'reply-again']" in result["error"]
    assert "only mention it: ['tool-call']" in result["error"]
    assert "I will read the file" not in result["error"]


@pytest.mark.parametrize("provider", ["pi", "omp"])
def test_console_first_turn_that_never_answers_with_its_marker_is_a_producer_failure(monkeypatch, tmp_path, provider):
    result = _console_first_turn_result(
        monkeypatch, tmp_path, provider, lambda marker, context: [("reply", f"The stored value is {context} and the marker is {marker}.")]
    )

    assert "assertions" not in result
    assert result["error"] == f"RuntimeError: first Console {provider} turn has no native assistant row that answers with its marker once"

    # With the turn's own stream to read, the same reply is the model's deviation, and the artifact carries the claim.
    (tmp_path / "stream").mkdir()
    result = _console_first_turn_result(
        monkeypatch, tmp_path / "stream", provider, lambda _marker, context: [("reply", context)], with_stream=True
    )
    assert result["error"].startswith(
        f"ModelDeviation: reply_carried_another_marker: the first turn completed and its final reply carries LH_{provider.upper()}_CONTEXT "
    )
    assert set(result["model_noncompliance"]["contract_evidence"]) == {
        "run_completed",
        "provider_turn_ended",
        "provider_replied",
        "prompt_named_marker",
    }


@pytest.mark.parametrize("producer", [provider_console_lifecycle, omp_console_producer, pi_console_tool_producer])
def test_a_console_producer_stopped_by_the_models_reply_carries_the_claim_on_its_failure_artifact(monkeypatch, tmp_path, producer):
    from zerg.qa.model_compliance import ModelDeviation

    provider = {provider_console_lifecycle: "codex", omp_console_producer: "omp", pi_console_tool_producer: "pi"}[producer]
    root = tmp_path / "evidence"
    provider_bin, engine, cli = _late_provider_boundary(monkeypatch, provider_console_lifecycle, tmp_path, provider)
    deviation = ModelDeviation(
        "reply_carried_no_marker", "the resume turn completed without its marker", run_completed=True, provider_replied=True
    )

    def run_live(*_args, **_kwargs):
        raise deviation

    monkeypatch.setattr(provider_console_lifecycle, "_run_live", run_live)
    args = _provider_args(root, provider_bin, engine, cli, provider)
    if producer is not provider_console_lifecycle:
        args = ["--variant", producer._VARIANT, *args[4:]]
    assert producer.main(args) == 1
    result = _json(root / "result.json")
    assert result["error"] == "ModelDeviation: reply_carried_no_marker: the resume turn completed without its marker"
    assert result["model_noncompliance"] == deviation.entry
    assert "assertions" not in result and "observation" not in result and "waited_on" not in result


def test_product_console_lifecycle_entrypoint_retains_prior_assertion_on_late_registry_failure(monkeypatch, tmp_path):
    root = tmp_path / "product"
    original_supports = product_console_lifecycle._ConsoleRegistry.supports
    calls = {"count": 0}

    def fail_late(*, owner_id, device_id, capability):
        calls["count"] += 1
        if calls["count"] >= 3:
            return False
        return original_supports(owner_id=owner_id, device_id=device_id, capability=capability)

    monkeypatch.setattr(product_console_lifecycle._ConsoleRegistry, "supports", staticmethod(fail_late))
    assert product_console_lifecycle.main(["--evidence-root", str(root)]) == 1
    result = _json(root / "result.json")
    assert result["status"] == "fail"
    assert result["observation"]["empty_ready_live_control"] is True
    assert result["observation"]["terminal_settled_live_control"] is False
    assert result["assertions"][product_console_lifecycle.ASSERTION_ID] is False
    assert _json(root / "cleanup-receipt.json")["status"] == "pass"
    _assert_result(root, result)


def test_provider_generic_resume_run_preserves_factory_failure_observation(monkeypatch, tmp_path):
    from zerg.qa import provider_resume_factory

    root = tmp_path / "generic"
    scenario_id = provider_generic_resume.GENERIC_SCENARIOS[0]
    partial = {
        "status": "fail",
        "failure_code": "late_resume_boundary",
        "observation": {"dispatch_started": True, "resume_boundary": "provider output failed"},
        "assertions": {"resume_contract": False},
    }
    monkeypatch.setattr(provider_resume_factory, "run_provider_resume_scenario", lambda *_args: partial)
    result = provider_generic_resume.run_generic_resume(provider="codex", scenario_id=scenario_id, root=root)
    assert result["status"] == "fail"
    assert result["failure_code"] == "late_resume_boundary"
    assert result["scenario_revision"] == provider_generic_resume.REGISTRATION.scenario_revision
    assert result["observation"]["dispatch_started"] is True
    assert result["observation"]["generic_cleanup_verified"] is True
    assert _json(root / "generic-observation.json")["resume_boundary"] == "provider output failed"
    assert _json(root / "cleanup-receipt.json")["status"] == "pass"
    _assert_result(root, result)


def _failing_title_oracle(*, evidence_root: Path, **_kwargs):
    evidence_root.mkdir(parents=True, exist_ok=True)
    (evidence_root / "cleanup-receipt.json").write_text(
        json.dumps({"status": "fail", "owned_process_count": 1, "temporary_runtime_removed": False}) + "\n", encoding="utf-8"
    )
    return {"passed": False, "observation": {"runtime_started": True, "late_boundary": "title request failed"}}


def _assert_title_failure(result, root):
    assert result["status"] == "fail"
    assert result["observation"]["runtime_started"] is True
    assert result["observation"]["late_boundary"] == "title request failed"
    assert result["assertions"] == {next(iter(result["assertions"])): False}
    assert _json(root / "cleanup-receipt.json")["status"] == "fail"
    _assert_result(root, result)


def test_title_dependency_live_entrypoint_serializes_late_failure_observation(monkeypatch, tmp_path):
    monkeypatch.setattr(title_dependency_live_producer, "run_live_title_dependency_oracle", _failing_title_oracle)
    result = title_dependency_live_producer.run(tmp_path / "title-live")
    _assert_title_failure(result, tmp_path / "title-live")


def test_title_dependency_recovery_entrypoint_serializes_late_failure_observation(monkeypatch, tmp_path):
    monkeypatch.setattr(title_dependency_recovery_producer, "run_hermetic_title_dependency_oracle", _failing_title_oracle)
    result = title_dependency_recovery_producer.run(tmp_path / "title-recovery")
    _assert_title_failure(result, tmp_path / "title-recovery")


def test_workspace_suggestions_entrypoint_serializes_partial_reads_and_cleanup(monkeypatch, tmp_path):
    root = tmp_path / "workspace"
    monkeypatch.setenv(workspace_suggestions_live_producer.RUNTIME_API_URL_ENV, "http://127.0.0.1:9")
    monkeypatch.setenv(workspace_suggestions_live_producer.RUNTIME_AGENTS_TOKEN_ENV, "runtime-token")
    monkeypatch.setattr(workspace_suggestions_live_producer, "require_disposable_runtime", lambda _url: None)
    monkeypatch.setattr(workspace_suggestions_live_producer.time, "sleep", lambda _seconds: None)
    request = httpx.Request("GET", "http://127.0.0.1:9")
    responses = [
        httpx.Response(
            200,
            json={
                "machines": [
                    {"device_id": "human-a", "control_channel_status": "connected"},
                    {"device_id": "human-b", "control_channel_status": "connected"},
                ]
            },
            request=request,
        ),
        httpx.Response(200, json={"workspaces": [{"path": "/Users/david/Projects/longhouse"}]}, request=request),
        *[httpx.Response(503, json={"detail": "late workspace boundary failed"}, request=request) for _ in range(3)],
    ]
    monkeypatch.setattr(workspace_suggestions_live_producer.httpx, "get", lambda *_args, **_kwargs: responses.pop(0))
    result = workspace_suggestions_live_producer.run(root)
    assert result["status"] == "fail"
    assert result["assertions"] == {workspace_suggestions_live_producer.ASSERTION_ID: False}
    assert result["observation"]["reads"][0]["paths"] == ["/Users/david/Projects/longhouse"]
    assert result["observation"]["reads"][1]["attempts"] == 3
    assert result["observation"]["reads"][1]["status_code"] == 503
    assert _json(root / "live-runtime-observation.json")["reads"] == result["observation"]["reads"]
    assert _json(root / "cleanup-receipt.json")["status"] == "pass"
    _assert_result(root, result)


def test_transcript_search_main_retains_turn_and_cleanup_evidence_when_search_fails(monkeypatch, tmp_path):
    root = tmp_path / "transcript"
    provider_bin = tmp_path / "provider"
    provider_bin.write_text("provider fixture\n", encoding="utf-8")
    provider_bin.chmod(0o755)
    engine = tmp_path / "engine"
    engine.write_text("engine fixture\n", encoding="utf-8")
    engine.chmod(0o755)
    cli = tmp_path / "longhouse"
    cli.write_text("cli fixture\n", encoding="utf-8")
    cli.chmod(0o755)
    monkeypatch.setenv(transcript_search_producer.RUNTIME_API_URL_ENV, "http://127.0.0.1:9")
    monkeypatch.setenv(transcript_search_producer.RUNTIME_AGENTS_TOKEN_ENV, "runtime-token")
    monkeypatch.setenv("LONGHOUSE_QUALIFICATION_SANDBOX", "provider-qualification-bwrap-v3")
    qualification_home = tmp_path / "transcript-qualification-home"
    qualification_home.mkdir()
    monkeypatch.setenv("LONGHOUSE_QUALIFICATION_HOME", str(qualification_home))
    monkeypatch.setenv("HOME", str(qualification_home))
    monkeypatch.setattr(
        transcript_search_producer.console,
        "_provider_environment",
        lambda _provider, _args, home: {"LONGHOUSE_HOME": str(home), "CODEX_API_KEY": "fixture-key"},
    )
    monkeypatch.setattr(transcript_search_producer, "login_with_api_key", lambda *_args, **_kwargs: {"status": "pass"})
    monkeypatch.setattr(transcript_search_producer, "require_disposable_runtime", lambda _url: None)
    monkeypatch.setattr(transcript_search_producer.console, "_probe_version", lambda _provider, _binary: ("fixture-1", "fixture-1"))

    class Shipper:
        receipt = {"machine_name": "transcript-failure-machine"}

        def flush(self, _reason):
            return {"status": "pass", "exit_code": 0, "daemon_paused": True, "daemon_restarted": True}

        def stop(self):
            return {"stopped": True, "process_dead": True, "process_group_dead": True}

    monkeypatch.setattr(transcript_search_producer, "start_transcript_shipper", lambda *_args, **_kwargs: Shipper())
    monkeypatch.setattr(
        transcript_search_producer.console, "_create_session", lambda **_kwargs: {"session_id": "session-1", "thread_id": "thread-1"}
    )
    monkeypatch.setattr(transcript_search_producer.console, "_start_turn", lambda **_kwargs: {"run_id": "run-1", "turn_id": "turn-1"})
    monkeypatch.setattr(transcript_search_producer.console, "_claim_path", lambda _home, _run_id: Path("claim.json"))
    monkeypatch.setattr(
        transcript_search_producer.console,
        "_wait_claim",
        lambda *_args, **_kwargs: {"state": "terminal", "run_id": "run-1", "result": {"terminal_state": "run_completed"}},
    )
    monkeypatch.setattr(transcript_search_producer.console, "turn_identity_mismatches", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(transcript_search_producer.console, "_claim_uses_provider_binary", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(transcript_search_producer.console, "_wait_turn_terminal", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        transcript_search_producer.console,
        "_claim_output_evidence",
        lambda _provider, _claim, marker: {"provider_response_marker_count": 1, "provider_response_excerpt": f"echo:{marker}"},
    )
    monkeypatch.setattr(
        transcript_search_producer.console,
        "_terminate_live_qualification_session",
        lambda *_args, **_kwargs: {"status": "pass", "dispatched": True},
    )
    monkeypatch.setattr(transcript_search_producer.console, "_force_cleanup", lambda _claims: None)
    monkeypatch.setattr(transcript_search_producer.console, "_wait_owned_processes_dead", lambda _claims: True)
    monkeypatch.setattr(transcript_search_producer.console, "_owned_process_evidence", lambda _claims: {"orphan_count": 0})
    monkeypatch.setattr(
        transcript_search_producer,
        "retire_qualification_session",
        lambda *_args, **_kwargs: {"status": "pass", "archived": True, "hidden": True, "present_in_served_inventory": False},
    )
    monkeypatch.setattr(
        transcript_search_producer.oracle,
        "probe_served_search",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("late search boundary failed")),
    )

    args = [
        "--provider",
        "codex",
        "--evidence-root",
        str(root),
        "--repo-root",
        str(tmp_path),
        "--engine",
        str(engine),
        "--longhouse-cli",
        str(cli),
        "--provider-bin",
        str(provider_bin),
        "--provider-version",
        "fixture-1",
        "--model",
        "fixture-model",
    ]
    assert transcript_search_producer.main(args) == 1
    result = _json(root / "result.json")
    assert result["status"] == "fail"
    assert result["failure_code"] == "transcript_search_harness_failed"
    # The search boundary raised: no verdict, and what the run observed stays as evidence.
    assert "assertions" not in result and "observation" not in result
    assert result["partial_observation"]["provider_marker_count"] == 1
    assert result["partial_observation"]["flush_ok"] is True
    assert "search" not in result["partial_observation"]
    assert _json(root / "transcript-flush-receipt.json")["status"] == "pass"
    assert _json(root / "cleanup-receipt.json")["status"] == "pass"
    assert _json(root / "provider-binary-receipt.json")["version"] == "fixture-1"


def test_transcript_search_main_surfaces_failed_console_turn_error_immediately(monkeypatch, tmp_path):
    root = tmp_path / "transcript-run-failed"
    provider_bin = tmp_path / "codex"
    provider_bin.write_text("codex fixture\n", encoding="utf-8")
    provider_bin.chmod(0o755)
    engine = tmp_path / "engine"
    engine.write_text("engine fixture\n", encoding="utf-8")
    engine.chmod(0o755)
    cli = tmp_path / "longhouse"
    cli.write_text("cli fixture\n", encoding="utf-8")
    cli.chmod(0o755)
    monkeypatch.setenv(transcript_search_producer.RUNTIME_API_URL_ENV, "http://127.0.0.1:9")
    monkeypatch.setenv(transcript_search_producer.RUNTIME_AGENTS_TOKEN_ENV, "runtime-token")
    monkeypatch.setenv("LONGHOUSE_QUALIFICATION_SANDBOX", "provider-qualification-bwrap-v3")
    qualification_home = tmp_path / "transcript-failed-home"
    qualification_home.mkdir()
    monkeypatch.setenv("LONGHOUSE_QUALIFICATION_HOME", str(qualification_home))
    monkeypatch.setenv("HOME", str(qualification_home))
    monkeypatch.setattr(
        transcript_search_producer.console,
        "_provider_environment",
        lambda _provider, _args, home: {"LONGHOUSE_HOME": str(home), "CODEX_API_KEY": "fixture-key"},
    )
    monkeypatch.setattr(transcript_search_producer, "login_with_api_key", lambda *_args, **_kwargs: {"status": "pass"})
    monkeypatch.setattr(transcript_search_producer, "require_disposable_runtime", lambda _url: None)
    monkeypatch.setattr(transcript_search_producer.console, "_probe_version", lambda _provider, _binary: ("fixture-1", "fixture-1"))

    class Shipper:
        receipt = {"machine_name": "transcript-failure-machine"}

        def stop(self):
            return {"stopped": True, "process_dead": True, "process_group_dead": True}

    monkeypatch.setattr(transcript_search_producer, "start_transcript_shipper", lambda *_args, **_kwargs: Shipper())
    monkeypatch.setattr(
        transcript_search_producer.console, "_create_session", lambda **_kwargs: {"session_id": "session-1", "thread_id": "thread-1"}
    )
    monkeypatch.setattr(transcript_search_producer.console, "_start_turn", lambda **_kwargs: {"run_id": "run-1", "turn_id": "turn-1"})
    monkeypatch.setattr(transcript_search_producer.console, "_claim_path", lambda _home, _run_id: Path("claim.json"))
    monkeypatch.setattr(
        transcript_search_producer.console,
        "_wait_claim",
        lambda *_args, **_kwargs: {
            "state": "terminal",
            "run_id": "run-1",
            "result": {
                "terminal_state": "run_failed",
                "error": "Codex turn ended with status failed: Quota exceeded. Check your plan and billing details.",
            },
        },
    )
    monkeypatch.setattr(
        transcript_search_producer.console,
        "_terminate_live_qualification_session",
        lambda *_args, **_kwargs: {"status": "pass", "dispatched": True},
    )
    monkeypatch.setattr(transcript_search_producer.console, "_force_cleanup", lambda _claims: None)
    monkeypatch.setattr(transcript_search_producer.console, "_wait_owned_processes_dead", lambda _claims: True)
    monkeypatch.setattr(transcript_search_producer.console, "_owned_process_evidence", lambda _claims: {"orphan_count": 0})
    monkeypatch.setattr(
        transcript_search_producer,
        "retire_qualification_session",
        lambda *_args, **_kwargs: {"status": "pass", "archived": True, "hidden": True, "present_in_served_inventory": False},
    )

    args = [
        "--provider",
        "codex",
        "--evidence-root",
        str(root),
        "--repo-root",
        str(tmp_path),
        "--engine",
        str(engine),
        "--longhouse-cli",
        str(cli),
        "--provider-bin",
        str(provider_bin),
        "--provider-version",
        "fixture-1",
        "--model",
        "fixture-model",
    ]
    assert transcript_search_producer.main(args) == 1
    result = _json(root / "result.json")
    assert result["status"] == "fail"
    assert "console_run_not_completed" in result["error"]
    assert "Quota exceeded. Check your plan and billing details." in result["error"]
