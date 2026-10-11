from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from zerg.qa import provider_console_lifecycle as lifecycle
from zerg.qa.console_served_state_core import MarkerAnswerCountError
from zerg.services.provider_capability_schema import load_capability_assertions


def _receipts(provider: str = "claude") -> tuple[dict, dict, dict, dict]:
    identity = {
        "provider": provider,
        "session_id": "session-1",
        "thread_id": "thread-1",
        "run_id": "run-1",
        "prompt_digest": "sha256:" + "a" * 64,
    }
    dispatch = {"status": "pass", "qualification_model_bound": True, **identity}
    binding = {
        "status": "pass",
        **identity,
        "marker": "LH_MARKER",
        "provider_response_marker_count": 1,
        "provider_response_excerpt": '{"assistant":"LH_MARKER"}',
        "bound_assistant_event_id": "event-1",
        "bound_assistant_event_origin": "durable",
        "bound_assistant_event_excerpt": "LH_MARKER",
        "bound_assistant_marker_count": 1,
        "marker_in_provider_response": True,
        "marker_in_bound_assistant_event": True,
        "assistant_event_count": 1,
        "transcript_converged_exactly_once": True,
    }
    interrupt = {
        "status": "pass",
        "expectation": "supported",
        "interrupt_dispatched": True,
        "active_run_cancelled": True,
        "provider_process_dead": True,
        "post_interrupt_turn_completed": True,
    }
    cleanup = {
        "status": "pass",
        "provider_process_dead": True,
        "process_group_dead": True,
        "orphan_count": 0,
        "process_stop_verified": True,
        "source_retention_verified": True,
    }
    return dispatch, binding, interrupt, cleanup


def test_registration_covers_each_launch_provider_with_least_authority_credentials():
    registration = lifecycle.REGISTRATION.to_dict()

    assert registration["producer_id"] == "provider.console_lifecycle.v1"
    assert registration["providers"] == list(lifecycle.PROVIDERS)
    assert registration["subject_kind"] == "provider_release"
    assert registration["provider_artifact_required"] is True
    assert registration["credential_binding_ids"] == []
    assert "console_continuation_receipt" in registration["required_artifacts"]
    for provider in lifecycle.PROVIDERS:
        assert registration["credential_binding_ids_by_provider"][provider] == [
            f"{provider}_provider_token",
            "runtime_host_control",
        ]
        assert f"{provider}_console_adapter_lifecycle" in registration["scenario_ids"]


def test_artifact_manifest_covers_nested_shipper_diagnostics(tmp_path):
    (tmp_path / "cleanup-receipt.json").write_text("{}\n", encoding="utf-8")
    nested = tmp_path / "shipper" / "engine-logs" / "engine.log"
    nested.parent.mkdir(parents=True)
    nested.write_text("diagnostic\n", encoding="utf-8")
    (tmp_path / "result.json").write_text("not self-bound\n", encoding="utf-8")

    manifest = lifecycle.artifact_manifest(tmp_path)

    assert {item["path"] for item in manifest} == {
        "cleanup-receipt.json",
        "shipper/engine-logs/engine.log",
    }


def test_artifact_manifest_stops_shipper_before_hashing_mutable_log(tmp_path):
    log = tmp_path / "shipper" / "engine-logs" / "engine.log"
    log.parent.mkdir(parents=True)
    log.write_bytes(b"before-stop\n")

    class AppendingShipper:
        stopped = False

        def stop(self):
            self.stopped = True
            with log.open("ab") as stream:
                stream.write(b"stopped\n")

    shipper = AppendingShipper()

    manifest = lifecycle._artifact_manifest_after_shipper_stopped(tmp_path, shipper)

    assert shipper.stopped is True
    entry = next(item for item in manifest if item["path"] == "shipper/engine-logs/engine.log")
    assert entry["size"] == log.stat().st_size
    assert entry["sha256"] == lifecycle._sha256_file(log)


def test_schema_gates_every_console_adapter_on_its_typed_release_assertion():
    assertions = {
        (item.provider, item.variant, item.scenario_id)
        for item in load_capability_assertions()
        if item.capability == "session.turn.start" and item.assertion_id == lifecycle.ASSERTION_ID
    }

    assert assertions == {
        (provider, lifecycle._expected_variant(provider), lifecycle._scenario_id(provider)) for provider in lifecycle.PROVIDERS
    }


def test_console_oracle_accepts_complete_independent_receipts():
    dispatch, binding, interrupt, cleanup = _receipts()
    observation = lifecycle._observation_from_receipts(
        dispatch=dispatch,
        binding=binding,
        interrupt=interrupt,
        cleanup=cleanup,
    )

    assert lifecycle.console_lifecycle_assertions(observation) == {lifecycle.ASSERTION_ID: True}


@pytest.mark.parametrize(
    ("receipt_index", "field", "value"),
    [
        (0, "run_id", "run-other"),
        (1, "marker_in_provider_response", False),
        (1, "bound_assistant_event_id", None),
        (1, "bound_assistant_marker_count", 2),
        (1, "assistant_event_count", 2),
        (2, "status", "fail"),
        (3, "orphan_count", 1),
        (3, "process_stop_verified", False),
    ],
)
def test_console_oracle_fails_closed_on_missing_binding_or_cleanup(receipt_index: int, field: str, value: object):
    receipts = list(_receipts())
    receipts[receipt_index] = copy.deepcopy(receipts[receipt_index])
    receipts[receipt_index][field] = value

    observation = lifecycle._observation_from_receipts(
        dispatch=receipts[0],
        binding=receipts[1],
        interrupt=receipts[2],
        cleanup=receipts[3],
    )

    assert lifecycle.console_lifecycle_assertions(observation) == {lifecycle.ASSERTION_ID: False}


@pytest.mark.parametrize(
    ("provider", "variant"),
    [
        ("codex", lifecycle.SUPPORTED_VARIANT),
        ("claude", lifecycle.SUPPORTED_VARIANT),
        ("opencode", lifecycle.SUPPORTED_VARIANT),
        ("cursor", lifecycle.SUPPORTED_VARIANT),
    ],
)
def test_interrupt_expectation_is_provider_typed(provider: str, variant: str):
    assert lifecycle._expected_variant(provider) == variant


@pytest.mark.parametrize("provider", ["pi", "claude", "cursor", "opencode"])
def test_interrupt_output_terminal_contract_is_omp_only(provider: str):
    assert lifecycle._expected_variant(provider) == lifecycle.SUPPORTED_VARIANT
    assert lifecycle._interrupt_output_contract_applies(provider) is False
    evidence = lifecycle._retained_post_interrupt_output_evidence(provider, {}, "MARKER", [], Path("/tmp"))
    assert evidence["applicable"] is False
    assert evidence["valid"] is None


@pytest.mark.parametrize(
    ("is_terminal", "will_continue", "valid"),
    [(False, False, False), (True, True, True), (None, False, True)],
)
def test_omp_interrupt_output_preserves_terminal_precedence(tmp_path, is_terminal, will_continue, valid):
    source = tmp_path / "post.jsonl"
    terminal = {"type": "agent_end", "willContinue": will_continue}
    if is_terminal is not None:
        terminal["isTerminal"] = is_terminal
    source.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "message_end",
                        "message": {"role": "assistant", "content": [{"type": "text", "text": "MARKER"}]},
                    }
                ),
                json.dumps(terminal),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    evidence = lifecycle._retained_post_interrupt_output_evidence(
        "omp",
        {"stdout_path": str(source)},
        "MARKER",
        [{"source": str(source), "kind": "stdout_path", "path": "post.jsonl", "retained": True}],
        tmp_path,
    )

    assert evidence["valid"] is valid


def test_codex_model_argument_controls_spawned_machine_agent_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_MODEL", "ambient-model")
    args = argparse.Namespace(
        engine=tmp_path / "longhouse-engine",
        provider_bin=tmp_path / "codex",
        model="qualified-model",
    )

    environment = lifecycle._provider_environment("codex", args, tmp_path / "home")

    assert environment["CODEX_MODEL"] == "qualified-model"
    assert environment["LONGHOUSE_CODEX_BIN"] == str(args.provider_bin)
    assert environment["CODEX_HOME"] == str(tmp_path / "home" / ".codex")
    assert environment["XDG_CONFIG_HOME"] == str(tmp_path / "home" / ".config")


def test_pi_console_routes_its_model_through_the_qualification_routing(tmp_path):
    import json

    from zerg.qa.openrouter_routing import OPENROUTER_QUALIFICATION_ROUTING

    args = argparse.Namespace(
        engine=tmp_path / "longhouse-engine",
        provider_bin=tmp_path / "pi",
        model="deepseek/deepseek-v4.1-flash:off",
    )

    environment = lifecycle._provider_environment("pi", args, tmp_path / "home")

    models = json.loads((tmp_path / "home" / ".pi" / "models.json").read_text(encoding="utf-8"))
    assert environment["PI_CODING_AGENT_DIR"] == str(tmp_path / "home" / ".pi")
    override = models["providers"]["openrouter"]["modelOverrides"]["deepseek/deepseek-v4.1-flash"]
    assert override["compat"]["openRouterRouting"] == OPENROUTER_QUALIFICATION_ROUTING


def test_claude_console_configures_the_real_staged_lifecycle_hook(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    args = argparse.Namespace(longhouse_cli=tmp_path / "longhouse")
    environment = {
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "LONGHOUSE_ENGINE_BIN": str(tmp_path / "longhouse-engine"),
    }
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(lifecycle.subprocess, "run", run)

    lifecycle._configure_claude_hook(args, environment)

    assert calls == [
        (
            [str(args.longhouse_cli), "claude", "configure", "--claude-dir", environment["CLAUDE_CONFIG_DIR"]],
            {
                "cwd": home,
                "env": environment,
                "capture_output": True,
                "text": True,
                "timeout": 30,
                "check": False,
            },
        )
    ]


@pytest.mark.parametrize(
    ("provider", "raw", "normalized"),
    [
        ("codex", "codex-cli 1.2.3", "1.2.3"),
        ("claude", "2.1.0 (Claude Code)", "2.1.0"),
        ("opencode", "1.17.20", "1.17.20"),
        ("cursor", "2026.07.23-e383d2b", "2026.07.23-e383d2b"),
    ],
)
def test_provider_version_probe_normalizes_the_staged_release(monkeypatch, tmp_path, provider: str, raw: str, normalized: str):
    binary = tmp_path / provider
    binary.write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, raw + "\n", ""),
    )

    assert lifecycle._probe_version(provider, binary) == (normalized, raw)


def test_provider_version_probe_rejects_unrecognized_output(monkeypatch, tmp_path):
    binary = tmp_path / "codex"
    binary.write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "unknown build\n", ""),
    )

    with pytest.raises(RuntimeError, match="release grammar"):
        lifecycle._probe_version("codex", binary)


def test_dispatch_claim_must_name_the_exact_staged_binary(tmp_path):
    staged = tmp_path / "provider"
    staged.write_text("fixture", encoding="utf-8")

    assert lifecycle._claim_uses_provider_binary({"result": {"argv": [str(staged), "--print"]}}, staged)
    assert not lifecycle._claim_uses_provider_binary({"result": {"argv": [str(tmp_path / "other"), "--print"]}}, staged)


def test_pi_console_model_binding_uses_native_openrouter_model_id():
    claim = {
        "result": {
            "argv": [
                "/opt/pi",
                "-p",
                "prompt",
                "--provider",
                "openrouter",
                "--model",
                "deepseek/deepseek-v4-flash",
            ]
        }
    }

    assert lifecycle._claim_uses_selected_model(
        claim,
        provider="pi",
        model="deepseek/deepseek-v4-flash",
    )


def test_codex_console_model_binding_reads_native_turn_context_not_argv(tmp_path):
    """Reproduces the 2026-09-26 factory false failure (interrupt_unsupported).

    The retained rollout on clifford (assurance root
    c1299af216b10063c7f8ce145d77dccbcc27681240f38d88e211f088639776e1) carried a
    ``turn_context`` row with ``payload.model == "gpt-5.6-luna"`` -- exactly the
    qualification model that was requested -- while argv never mentions a
    model, because Codex's app-server sends it over ``turn/start`` JSON-RPC
    (engine/src/codex_exec.rs). The old check scanned argv for a
    ``model="..."`` token that Codex no longer emits and failed a run whose
    Codex transcript shows the turn completed successfully.
    """
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(
        "\n".join(
            [
                json.dumps({"type": "session_meta", "payload": {}}),
                json.dumps(
                    {
                        "type": "turn_context",
                        "payload": {
                            "turn_id": "01a0dbf3-ad8b-7de3-9933-8e48bc1928d0",
                            "cwd": "/run/lhq/sandbox-home/c/w",
                            "approval_policy": "never",
                            "model": "gpt-5.6-luna",
                            "collaboration_mode": {"settings": {"model": "gpt-5.6-luna"}},
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "LH_CODEX_CONSOLE_marker"}],
                        },
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    claim = {
        "result": {"argv": ["/opt/codex", "-c", "tui.notifications=false", "app-server", "--listen", "stdio://"]},
        "source_path": str(rollout),
    }

    assert lifecycle._claim_uses_selected_model(claim, provider="codex", model="gpt-5.6-luna")
    assert not lifecycle._claim_uses_selected_model(claim, provider="codex", model="gpt-5.3-codex-low")
    assert not lifecycle._claim_uses_selected_model({"result": claim["result"]}, provider="codex", model="gpt-5.6-luna")


def test_codex_local_output_evidence_ignores_prompt_echo(tmp_path):
    marker = "LH_CODEX_CONSOLE_" + "c" * 32
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": f"Reply with {marker}"}],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "different answer"}],
                        },
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    without_marker = lifecycle._claim_output_evidence("codex", {"source_path": str(rollout)}, marker)

    assert without_marker is not None
    assert without_marker["provider_response_marker_count"] == 0
    assert without_marker["provider_response_excerpt"] == ""

    with rollout.open("a", encoding="utf-8") as stream:
        stream.write(
            "\n"
            + json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": marker}],
                    },
                }
            )
        )
    with_marker = lifecycle._claim_output_evidence("codex", {"source_path": str(rollout)}, marker)

    assert with_marker is not None
    assert with_marker["provider_response_marker_count"] == 1
    assert marker in str(with_marker["provider_response_excerpt"])


def test_start_turn_retries_transient_admission_with_stable_request(monkeypatch):
    calls = []

    def request(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) == 1:
            raise RuntimeError('POST /turns returned HTTP 503: {"detail":"Request timed out"}')
        if len(calls) == 2:
            raise RuntimeError("adapter_unavailable")
        if len(calls) == 3:
            return {"state": "queued", "turn_id": "turn-1", "run_id": None}
        return {"state": "starting", "turn_id": "turn-1", "run_id": "run-1"}

    monkeypatch.setattr(lifecycle, "_request", request)
    monkeypatch.setattr(lifecycle.time, "sleep", lambda _seconds: None)

    result = lifecycle._start_turn(
        api_url="https://runtime.example",
        token="token",
        session_id="session-1",
        message="hello",
        request_id="stable-request",
    )

    assert result["run_id"] == "run-1"
    assert len(calls) == 4
    assert all(
        call[0][-1]
        == {
            "message": "hello",
            "client_request_id": "stable-request",
        }
        for call in calls
    )


def test_wait_turn_terminal_requires_runtime_host_terminal_before_next_turn(monkeypatch):
    calls = []
    responses = [
        {"state": "active", "turn_id": "turn-1", "run_id": "run-1"},
        {"state": "completed", "turn_id": "turn-1", "run_id": "run-1"},
    ]

    def request(*args, **kwargs):
        calls.append((args, kwargs))
        return responses.pop(0)

    monkeypatch.setattr(lifecycle, "_request", request)
    monkeypatch.setattr(lifecycle.time, "sleep", lambda _seconds: None)

    result = lifecycle._wait_turn_terminal(
        api_url="https://runtime.example",
        token="token",
        session_id="session-1",
        message="hello",
        request_id="stable-request",
        turn_id="turn-1",
        run_id="run-1",
    )

    assert result["state"] == "completed"
    assert len(calls) == 2
    assert all(
        call[0][-1]
        == {
            "message": "hello",
            "client_request_id": "stable-request",
        }
        for call in calls
    )


def test_provider_console_registration_cli_is_hermetic_without_database_env(tmp_path):
    server_root = Path(__file__).resolve().parents[1]
    home = tmp_path / "home"
    home.mkdir()
    environment = {
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(server_root),
    }

    completed = subprocess.run(
        [sys.executable, "-m", "zerg.qa.provider_console_lifecycle", "--registration"],
        cwd=server_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert json.loads(completed.stdout)["producer_id"] == "provider.console_lifecycle.v1"


def test_console_runtime_wake_socket_stays_below_linux_path_limit():
    _runtime, _evidence, _workspace, longhouse_home = lifecycle._console_runtime_paths(Path("/run/lhq/sandbox-home"))
    wake_socket = longhouse_home / "agent" / "transcript-wake.sock"

    assert len(os.fsencode(wake_socket)) <= 90


def test_archive_assistant_markers_exclude_tools_and_non_text_content(monkeypatch):
    marker = "LH_ARCHIVE_MARKER"
    events = [
        {"id": "prompt", "role": "user", "content_text": marker},
        {"id": "tool", "role": "assistant", "tool_name": "shell", "content_text": marker},
        {"id": "metadata", "role": "assistant", "content_text": {"marker": marker}},
        {"id": "preview", "role": "assistant", "event_origin": "live_provisional", "content_text": marker},
        {"id": "reply", "role": "assistant", "content_text": marker},
    ]
    monkeypatch.setattr(lifecycle, "_request", lambda *_args: {"events": events})

    matches = lifecycle._assistant_marker_events("https://runtime.example", "token", "session-1", marker)

    assert [event["id"] for event in matches] == ["reply"]


def test_archive_convergence_rejects_duplicate_markers_within_one_reply(monkeypatch):
    marker = "LH_ARCHIVE_MARKER"
    monkeypatch.setattr(
        lifecycle,
        "_request",
        lambda *_args: {"events": [{"id": "reply", "role": "assistant", "content_text": f"{marker} {marker}"}]},
    )

    # Typed, and raised on sight: nothing served later makes this reply say its marker once.
    with pytest.raises(MarkerAnswerCountError, match="^marker_repeated_in_one_reply: .*expected each to say its marker once"):
        lifecycle._wait_served_answers("https://runtime.example", "token", "session-1", marker)


def _served(monkeypatch, events: list[dict[str, object]]) -> None:
    monkeypatch.setattr(lifecycle, "_request", lambda *_args: {"events": events})
    # A refusal has to stand; these tests wait for the second read, not for the window.
    monkeypatch.setattr("zerg.qa.console_served_state_core.SERVED_ANSWER_SETTLE_SECONDS", 0.0)


def _wait(marker: str, native: dict[str, int] | None, **kwargs: object):
    """The served wait against a fixed archive page; ``native`` is the provider's own answering rows."""

    return lifecycle._wait_served_answers(
        "https://runtime.example",
        "token",
        "session-1",
        marker,
        native_answers=None if native is None else (lambda: (dict(native), "read")),
        **kwargs,
    )


def test_a_steer_answered_twice_converges_when_both_served_replies_are_the_providers_rows(monkeypatch):
    """OMP answers a steer once as it lands and again when the background job's notice arrives."""

    marker = "LH_OMP_STEERED_abc"
    twice = [
        {"id": "first", "role": "assistant", "content_text": marker},
        {"id": "after-job", "role": "assistant", "content_text": marker},
    ]
    _served(monkeypatch, twice)

    answers, account = _wait(marker, {"first": 1, "after-job": 1}, one_reply=False)
    assert [event["id"] for event in answers] == ["first", "after-job"]
    assert account["converged"] is True and account["served_answer_rows"] == {"first": 1, "after-job": 1}
    # However often: a third reply the provider wrote is its own business too.
    thrice = [*twice, {"id": "again", "role": "assistant", "content_text": marker}]
    _served(monkeypatch, thrice)
    answers, _account = _wait(marker, {"first": 1, "after-job": 1, "again": 1}, one_reply=False)
    assert len(answers) == 3

    # A reply the provider did not write is a finding about Longhouse, not a count.
    with pytest.raises(MarkerAnswerCountError, match="^served_answer_without_native_row: ") as stray:
        _wait(marker, {"first": 1, "after-job": 1}, one_reply=False)
    assert stray.value.about == ("again",)
    assert stray.value.observation["answer_correspondence"]["unbound_served_event_ids"] == ["again"]


def test_a_turn_that_binds_one_reply_tells_a_provider_that_answered_twice_from_a_reply_served_twice(monkeypatch):
    marker = "LH_PI_CONSOLE_abc"
    reply = {"id": "reply", "role": "assistant", "content_text": marker}

    # One native row, served once: the reply.
    _served(monkeypatch, [reply])
    answers, account = _wait(marker, {"reply": 1})
    assert [event["id"] for event in answers] == ["reply"] and account["converged"] is True
    # A later text block of the row is served under the row's id plus a suffix.
    _served(monkeypatch, [{"id": "reply-text-1", "role": "assistant", "content_text": marker}])
    assert _wait(marker, {"reply": 1})[1]["converged"] is True

    # The provider wrote one reply and the archive serves it twice: Longhouse's fault, typed as such.
    _served(monkeypatch, [reply, dict(reply)])
    with pytest.raises(MarkerAnswerCountError, match="^native_answer_served_more_than_once: "):
        _wait(marker, {"reply": 1})
    _served(monkeypatch, [reply, {"id": "other", "role": "assistant", "content_text": marker}])
    with pytest.raises(MarkerAnswerCountError, match="^served_answer_without_native_row: "):
        _wait(marker, {"reply": 1})

    # The provider itself answered in two rows. The receipts of this turn bind one
    # reply, so that is said of the provider's rows, whatever was served.
    for served in ([reply], [reply, {"id": "second", "role": "assistant", "content_text": marker}], []):
        _served(monkeypatch, served)
        with pytest.raises(MarkerAnswerCountError, match="^marker_answered_more_than_once: .* 2 answering native assistant row") as twice:
            _wait(marker, {"reply": 1, "second": 1})
        assert twice.value.answer_event_ids == ["reply", "second"]
        # The message alone says which of the two remaining counts this was, and which rows.
        assert "expected exactly 1 (single-reply turn: the provider itself answered in more than one row)" in str(twice.value)
        assert "(answering row ids ['reply', 'second']; " in str(twice.value)


def test_a_reply_not_served_yet_is_lag_and_the_expired_wait_says_what_the_provider_wrote(monkeypatch):
    marker = "LH_CLAUDE_CONSOLE_abc"
    _served(monkeypatch, [{"id": "prompt", "role": "user", "content_text": f"Reply with exactly {marker}."}])

    # The wait that gives up hands its last count to the caller's read of the provider's stream.
    with pytest.raises(
        RuntimeError, match=r"^from the stream \(count=0, .*native answering rows=\{'reply': 1\}, not yet served=\['reply'\]"
    ):
        _wait(marker, {"reply": 1}, timeout=0.6, unanswered=lambda served: RuntimeError(f"from the stream ({served})"))

    # The provider's record could not be read as rows: nothing is refused, nothing
    # converges, and the stream is not asked about a reply that was served.
    _served(monkeypatch, [{"id": "reply", "role": "assistant", "content_text": marker}])
    with pytest.raises(lifecycle.ConsoleTurnFailure, match=r"^provider_reply_not_judged: .*count=1, .*native record unreadable"):
        lifecycle._wait_served_answers(
            "https://runtime.example", "token", "session-1", marker, native_answers=lambda: (None, "unreadable"), timeout=0.6
        )


def test_an_adapter_whose_served_ids_are_not_the_providers_is_counted_to_one(monkeypatch):
    marker = "LH_CODEX_CONSOLE_abc"
    twice = [
        {"id": "5a2f", "role": "assistant", "content_text": marker},
        {"id": "9c1d", "role": "assistant", "content_text": marker},
    ]
    _served(monkeypatch, twice[:1])
    answers, account = _wait(marker, None)
    assert [event["id"] for event in answers] == ["5a2f"] and account is None
    _served(monkeypatch, twice)
    with pytest.raises(MarkerAnswerCountError, match="marker_answered_more_than_once: .* has 2 answering .* expected exactly 1") as counted:
        _wait(marker, None)
    # The message alone says which of the two remaining counts this was, and which events.
    assert "2 answering served assistant event(s), expected exactly 1 (counted adapter: its served ids are not" in str(counted.value)
    assert "(answering event ids ['5a2f', '9c1d']; " in str(counted.value)
    # A turn that binds no single reply (the steer) takes the provider's repeated reply on a
    # counted adapter as it does on one held by id; nothing served is still not converged.
    answers, account = _wait(marker, None, one_reply=False)
    assert [event["id"] for event in answers] == ["5a2f", "9c1d"] and account is None
    _served(monkeypatch, [])
    with pytest.raises(RuntimeError, match=r"count=0"):
        _wait(marker, None, one_reply=False, timeout=0.6)

    # Every Console adapter is on one side or the other, and each counted one says why.
    adapters = {*lifecycle.PROVIDERS, *lifecycle.CAN_RESUME}
    assert set(lifecycle._NATIVE_ROWS_IN_CLAIM) | set(lifecycle._SERVED_IDS_NOT_NATIVE) == adapters
    assert not set(lifecycle._NATIVE_ROWS_IN_CLAIM) & set(lifecycle._SERVED_IDS_NOT_NATIVE)
    assert all(len(reason) > 40 for reason in lifecycle._SERVED_IDS_NOT_NATIVE.values())
    for provider in lifecycle._SERVED_IDS_NOT_NATIVE:
        assert lifecycle._claim_native_answers(provider, {"stdout_path": "/unused", "source_path": "/unused"}, marker) is None


def _jsonl(path: Path, rows: list[dict[str, object]], *, complete: bool = True) -> str:
    path.write_text("\n".join(json.dumps(row) for row in rows) + ("\n" if complete else ""), encoding="utf-8")
    return str(path)


def test_a_record_still_being_written_gives_its_complete_rows_and_says_so(tmp_path):
    """The row the provider is writing must not hide the complete rows before it."""

    marker = "LH_CONSOLE_abc"

    def text(value: str) -> dict[str, object]:
        return {"role": "assistant", "content": [{"type": "text", "text": value}]}

    session = [
        {"type": "session", "id": "native-1", "version": 3, "cwd": "/workspace"},
        {"type": "message", "id": "a1", "message": {"role": "user", "content": f"Reply with exactly {marker}."}},
        {"type": "message", "id": "a2", "message": text(marker)},
        # The next turn's row, complete as JSON and not yet ended with a newline.
        {"type": "message", "id": "a3", "message": text("Working on the next prompt.")},
    ]
    for provider in ("pi", "omp"):
        path = Path(_jsonl(tmp_path / f"{provider}.jsonl", session, complete=False))
        rows, state, sessions = lifecycle._native_assistant_rows(provider, path)
        assert state == "mid_write" and sessions == ["native-1"]
        assert [row["id"] for row in rows] == ["a2"]
        # The turn's reply is a complete row: its native evidence is there.
        evidence = lifecycle._native_marker_evidence(provider, path, marker)
        assert evidence is not None and evidence["native_message_id"] == "a2"
        # A served page is held to every answering row, which this read cannot yet give.
        assert lifecycle._claim_native_answers(provider, {"source_path": str(path)}, marker)() == (None, "mid_write")
        # Once the provider ends the row, the same record is read in full.
        path.write_bytes(path.read_bytes() + b"\n")
        rows, state, _sessions = lifecycle._native_assistant_rows(provider, path)
        assert state == "read" and [row["id"] for row in rows] == ["a2", "a3"]
        assert lifecycle._claim_native_answers(provider, {"source_path": str(path)}, marker)() == ({"a2": 1}, "read")

    stream = [
        {"type": "assistant", "uuid": "u-1", "message": text(marker)},
        {"type": "assistant", "uuid": "u-2", "message": text("Still writing.")},
    ]
    path = Path(_jsonl(tmp_path / "claude.stdout", stream, complete=False))
    rows, state, _sessions = lifecycle._native_assistant_rows("claude", path)
    assert state == "mid_write" and [row["id"] for row in rows] == ["u-1"]
    # A record with nothing in it, or that cannot be read, has no rows to give.
    (tmp_path / "empty.jsonl").write_bytes(b"")
    for provider in ("pi", "omp", "claude"):
        assert lifecycle._native_assistant_rows(provider, tmp_path / "empty.jsonl")[:2] == (None, "empty")
        assert lifecycle._native_assistant_rows(provider, tmp_path / "absent.jsonl")[:2] == (None, "unreadable")


def test_the_providers_own_answering_rows_are_read_from_the_record_its_claim_names(tmp_path):
    marker = "LH_CONSOLE_abc"
    narration = f"I will run the command, then reply with `{marker}`."

    def text(value: str) -> dict[str, object]:
        return {"role": "assistant", "content": [{"type": "text", "text": value}]}

    # Pi and OMP: the session file's assistant entries, by entry id.
    session = [
        {"type": "session", "id": "native-1", "version": 3, "cwd": "/workspace"},
        {"type": "message", "id": "a1", "message": {"role": "user", "content": f"Reply with exactly {marker}."}},
        {"type": "message", "id": "a2", "message": text(narration)},
        {"type": "message", "id": "a3", "message": text(f"The command finished.\n\n{marker}")},
    ]
    for provider in ("pi", "omp"):
        claim = {"source_path": _jsonl(tmp_path / f"{provider}.jsonl", session), "stdout_path": "/not/the/rows"}
        assert lifecycle._claim_native_answers(provider, claim, marker)() == ({"a3": 1}, "read")
        # A record that ends inside a row is one whose answers are not known yet.
        claim = {"source_path": _jsonl(tmp_path / f"{provider}-writing.jsonl", session, complete=False)}
        assert lifecycle._claim_native_answers(provider, claim, marker)() == (None, "mid_write")
        assert lifecycle._claim_native_answers(provider, {}, marker)() == (None, "unreadable")
        assert lifecycle._claim_native_answers(provider, {"source_path": str(tmp_path / "absent.jsonl")}, marker)()[0] is None

    # Claude: the print stream's assistant events, by uuid.
    stream = [
        {"type": "system", "subtype": "init", "session_id": "s-1"},
        {"type": "assistant", "uuid": "u-1", "message": text(narration)},
        {"type": "assistant", "uuid": "u-2", "message": text(marker)},
        {"type": "result", "subtype": "success", "result": marker},
    ]
    claim = {"stdout_path": _jsonl(tmp_path / "claude.stdout", stream), "source_path": "/not/the/rows"}
    assert lifecycle._claim_native_answers("claude", claim, marker)() == ({"u-2": 1}, "read")
    # A reply the stream does not name cannot be held to anything by id.
    unnamed = [stream[0], {"type": "assistant", "message": text(marker)}]
    claim = {"stdout_path": _jsonl(tmp_path / "claude-unnamed.stdout", unnamed)}
    assert lifecycle._claim_native_answers("claude", claim, marker)() == (None, "unreadable")
    # A row without an id that does not answer is nobody's reply and hides nothing.
    mixed = [{"type": "assistant", "message": text("Working.")}, stream[2]]
    claim = {"stdout_path": _jsonl(tmp_path / "claude-mixed.stdout", mixed)}
    assert lifecycle._claim_native_answers("claude", claim, marker)() == ({"u-2": 1}, "read")


def test_archive_convergence_counts_the_answer_not_the_narration_that_quotes_it(monkeypatch):
    marker = "LH_CLAUDE_CONSOLE_abc"
    events = [
        {"id": "prompt", "role": "user", "content_text": f"Reply with exactly {marker} and nothing else."},
        # Narration ahead of a tool call names the marker inside a sentence.
        {"id": "narration", "role": "assistant", "content_text": f"I will run the command, then reply with `{marker}`."},
        {"id": "reply", "role": "assistant", "content_text": f"The command finished.\n\n{marker}"},
    ]
    monkeypatch.setattr(lifecycle, "_request", lambda *_args: {"events": events})

    answers, _account = lifecycle._wait_served_answers("https://runtime.example", "token", "session-1", marker)
    assert [event["id"] for event in answers] == ["reply"]

    # Marker only in narration: never an answer, and the failure says a mention was seen.
    monkeypatch.setattr(lifecycle, "_request", lambda *_args: {"events": events[:2]})
    with pytest.raises(RuntimeError, match=r"count=0, events that only mention it=1"):
        lifecycle._wait_served_answers("https://runtime.example", "token", "session-1", marker, timeout=0.6)


def test_archive_convergence_reads_a_reply_asked_to_carry_the_stored_context(monkeypatch):
    marker, context = "LH_OMP_CONSOLE_abc", "LH_OMP_CONTEXT_def"

    # On one line, on two, or run together: all three are what models send.
    for text in (f"{context} {marker}", f"{context}\n{marker}", f"{context}{marker}"):
        monkeypatch.setattr(
            lifecycle, "_request", lambda *_args, text=text: {"events": [{"id": "reply", "role": "assistant", "content_text": text}]}
        )
        answers = lifecycle._assistant_marker_events("https://runtime.example", "token", "session-1", marker, alongside=(context,))
        assert [event["id"] for event in answers] == ["reply"]

    # The wait takes the same companions; without them the run-together reply is not an answer.
    answers, _account = lifecycle._wait_served_answers("https://runtime.example", "token", "session-1", marker, alongside=(context,))
    assert [event["id"] for event in answers] == ["reply"]
    with pytest.raises(RuntimeError, match=r"count=0, events that only mention it=1"):
        lifecycle._wait_served_answers("https://runtime.example", "token", "session-1", marker, timeout=0.6)


def test_omp_job_notice_completes_the_steered_tool_and_a_miss_says_what_was_served() -> None:
    marker = "LH_OMP_STEER_TOOL_42_abc"
    first = "LH_OMP_STEER_FIRST_abc"
    notice = {"role": "system", "content_text": f"<system-notice>\nBackground job bg_1 has completed.\n{marker}\n</system-notice>"}
    assert lifecycle._tool_ran_to_completion([notice], tool_marker=marker, first_marker=first)

    served = [{"role": "assistant", "tool_name": "bash", "content_text": "sleep 15 && echo LH_OMP_STEER_TOOL_$((40+2))_abc"}]
    assert not lifecycle._tool_ran_to_completion(served, tool_marker=marker, first_marker=first)
    assert lifecycle._tool_marker_rows([notice, *served], marker) == [
        {"role": "system", "tool_name": None, "origin": None, "in_text": True, "in_tool_output": False}
    ]


# One line of a real OMP run's captured stdout (factory epoch 076529750, steer cell):
# the background job's result as OMP handed it to the model. The label in `details`
# carries the command's uncomputed text; only `content` holds the computed marker.
_OMP_REAL_JOB_NOTICE = json.loads(
    r"""{"type":"message_end","message":{"role":"custom","customType":"async-result","content":"<system-notice>\nBackground job bg_1 has completed. Resume your work using the result below.\nLH_OMP_STEER_TOOL_42_467d351979c44995bf597a86ee9dd285\nWall time: 15.03 seconds\n</system-notice>","display":true,"attribution":"agent","details":{"meta":{"source":{"type":"report","value":"background job delivery"}},"jobs":[{"jobId":"bg_1","type":"bash","label":"sleep 15 && echo LH_OMP_STEER_TOOL_$((40+2))_467d351979c44995bf597a86ee9dd285","durationMs":15030}]},"timestamp":1790663323476},"messageId":"msg-6"}"""
)


def test_the_real_omp_job_notice_satisfies_the_stream_check(tmp_path) -> None:
    tag = "467d351979c44995bf597a86ee9dd285"
    path = tmp_path / "stdout.log"
    path.write_text(json.dumps(_OMP_REAL_JOB_NOTICE) + "\n", encoding="utf-8")
    claim = {"stdout_path": str(path)}
    marker, first = f"LH_OMP_STEER_TOOL_42_{tag}", f"LH_OMP_STEER_FIRST_{tag}"

    assert lifecycle._provider_stream_reports_tool_result(claim, tool_marker=marker, first_marker=first)
    # The command text in the job's label is not its result.
    assert not lifecycle._provider_stream_reports_tool_result(claim, tool_marker=f"LH_OMP_STEER_TOOL_$((40+2))_{tag}", first_marker=first)
    assert not lifecycle._provider_stream_reports_tool_result(claim, tool_marker=marker.replace("42", "43"), first_marker=first)


def test_omp_steered_command_result_is_read_from_the_provider_stream(tmp_path) -> None:
    marker = "LH_OMP_STEER_TOOL_42_abc"
    first = "LH_OMP_STEER_FIRST_abc"
    notice = {
        "type": "message_end",
        "message": {"role": "custom", "customType": "async-result", "content": f"<system-notice>\n{marker}\n</system-notice>"},
    }

    def stream(*records: dict) -> dict:
        path = tmp_path / f"stdout-{len(list(tmp_path.iterdir()))}.log"
        path.write_text("\n".join(json.dumps(record) for record in records) + "\nnot json\n", encoding="utf-8")
        return {"stdout_path": str(path)}

    assert "omp" in lifecycle._STEER_TOOL_RESULT_IN_PROVIDER_STREAM
    assert "pi" not in lifecycle._STEER_TOOL_RESULT_IN_PROVIDER_STREAM
    assert lifecycle._provider_stream_reports_tool_result(stream(notice), tool_marker=marker, first_marker=first)
    # A synchronous tool result counts the same way.
    result = {"type": "message_end", "message": {"role": "toolResult", "content": [{"type": "text", "text": marker}]}}
    assert lifecycle._provider_stream_reports_tool_result(stream(result), tool_marker=marker, first_marker=first)
    # The prompt and the model's own replies quote markers; only the provider's messages are evidence.
    quoted = [
        {"type": "message_end", "message": {"role": role, "content": [{"type": "text", "text": marker}]}} for role in ("user", "assistant")
    ]
    assert not lifecycle._provider_stream_reports_tool_result(stream(*quoted), tool_marker=marker, first_marker=first)
    # The first turn's marker means an earlier command, not the steered one.
    both = {"type": "message_end", "message": {"role": "custom", "content": f"{marker} {first}"}}
    assert not lifecycle._provider_stream_reports_tool_result(stream(both), tool_marker=marker, first_marker=first)
    assert not lifecycle._provider_stream_reports_tool_result({}, tool_marker=marker, first_marker=first)
    assert not lifecycle._provider_stream_reports_tool_result(
        {"stdout_path": str(tmp_path / "missing.log")}, tool_marker=marker, first_marker=first
    )


def test_served_run_inventory_accepts_canonical_ended_terminal_state(monkeypatch):
    monkeypatch.setattr(
        lifecycle,
        "_request",
        lambda *_args, **_kwargs: {
            "session_id": "session-1",
            "served_path": "canonical_session_detail",
            "shadow": {
                "run": {"id": "run-1", "lifecycle": "ended"},
                "activity": {"state": "quiescent"},
            },
        },
    )

    evidence = lifecycle._served_run_inventory_evidence(
        "https://runtime.example",
        "token",
        "session-1",
        [{"session_id": "session-1", "run_id": "run-1", "state": "terminal"}],
    )

    assert evidence["retired"] is True
    assert evidence["active_run_count"] == 0


def test_failed_console_cleanup_dispatches_live_termination(monkeypatch):
    calls = []

    def request(api_url, token, method, path, payload=None):
        calls.append((api_url, token, method, path, payload))
        return {"terminate_dispatched": True, "session_id": "session-1"}

    monkeypatch.setattr(lifecycle, "_request", request)

    receipt = lifecycle._terminate_live_qualification_session(
        "https://runtime.example",
        "token",
        "session-1",
    )

    assert receipt == {
        "status": "pass",
        "dispatched": True,
        "session_id": "session-1",
        "response": {"terminate_dispatched": True, "session_id": "session-1"},
    }
    assert calls == [
        (
            "https://runtime.example",
            "token",
            "POST",
            "/api/agents/sessions/session-1/terminate-live",
            None,
        )
    ]


def test_served_run_retirement_wait_returns_without_sleep_when_already_terminal(monkeypatch):
    monkeypatch.setattr(
        lifecycle,
        "_served_run_inventory_evidence",
        lambda *_args: {
            "retired": True,
            "active_run_count": 0,
            "session_id": "session-1",
        },
    )

    evidence = lifecycle._wait_served_run_retirement(
        "https://runtime.example",
        "token",
        "session-1",
        [{"session_id": "session-1", "run_id": "run-1", "state": "terminal"}],
    )

    assert evidence["retired"] is True
    assert evidence["retirement_wait_attempts"] == 0
    assert evidence["retirement_wait_status"] == "pass"


def test_served_run_inventory_uses_terminal_facts_when_activity_head_is_unknown(monkeypatch):
    monkeypatch.setattr(
        lifecycle,
        "_request",
        lambda *_args, **_kwargs: {
            "session_id": "session-1",
            "served_path": "canonical_session_detail",
            "shadow": {
                "run": {"id": "run-1", "lifecycle": "ended"},
                "activity": {"state": "unknown"},
            },
        },
    )

    evidence = lifecycle._served_run_inventory_evidence(
        "https://runtime.example",
        "token",
        "session-1",
        [{"session_id": "session-1", "run_id": "run-1", "state": "terminal"}],
    )

    assert evidence["retired"] is True
    assert evidence["active_run_count"] == 0
    assert evidence["activity_state"] == "unknown"
    assert evidence["activity_state_authority"] == "diagnostic_head"


def test_served_run_inventory_rejects_explicitly_active_activity_head(monkeypatch):
    monkeypatch.setattr(
        lifecycle,
        "_request",
        lambda *_args: {
            "session_id": "session-1",
            "served_path": "canonical_session_detail",
            "shadow": {
                "run": {"id": "run-1", "lifecycle": "ended"},
                "activity": {"state": "executing"},
            },
        },
    )

    evidence = lifecycle._served_run_inventory_evidence(
        "https://runtime.example",
        "token",
        "session-1",
        [{"session_id": "session-1", "run_id": "run-1", "state": "terminal"}],
    )

    assert evidence["retired"] is False
    assert evidence["active_run_count"] is None


@pytest.mark.parametrize(
    ("served_session_id", "served_run_id"),
    [("other-session", "run-1"), ("session-1", "old-run")],
)
def test_served_run_inventory_rejects_wrong_session_or_run_identity(monkeypatch, served_session_id, served_run_id):
    monkeypatch.setattr(
        lifecycle,
        "_request",
        lambda *_args: {
            "session_id": served_session_id,
            "served_path": "canonical_session_detail",
            "shadow": {
                "run": {"id": served_run_id, "lifecycle": "ended"},
                "activity": {"state": "quiescent"},
            },
        },
    )

    evidence = lifecycle._served_run_inventory_evidence(
        "https://runtime.example",
        "token",
        "session-1",
        [{"session_id": "session-1", "run_id": "run-1", "state": "terminal"}],
    )

    assert evidence["retired"] is False
    assert evidence["active_run_count"] is None


@pytest.mark.parametrize(
    ("projected_id", "bound"),
    [
        pytest.param("pi-message-42", True, id="the-native-row"),
        pytest.param("pi-message-42-text-1", True, id="a-later-text-block-of-the-row"),
        # A served event that is not the native row is not its projection, whatever it says.
        pytest.param("longhouse-event-9", False, id="another-event"),
        pytest.param("pi-message-421", False, id="another-row-with-a-longer-id"),
    ],
)
def test_pi_continuation_linkage_binds_the_projected_reply_to_the_native_row_by_id(tmp_path, projected_id, bound):
    marker = "PI_RESUME_MARKER"
    native = tmp_path / "pi-session.jsonl"
    native.write_text(
        "\n".join(
            [
                json.dumps({"type": "session", "id": "pi-session"}),
                json.dumps(
                    {
                        "type": "message",
                        "id": "pi-message-42",
                        "message": {"role": "assistant", "content": [{"type": "text", "text": marker}]},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    native_evidence = lifecycle._native_marker_evidence("pi", native, marker)
    linkage = lifecycle._pi_continuation_linkage(
        native_evidence,
        projected_id,
        projected_marker_count=1,
        same_session=True,
        same_thread=True,
        native_provider_thread_id="pi-session",
        projected_provider_thread_id="pi-session",
    )

    assert native_evidence["native_message_id"] == "pi-message-42"
    assert linkage["native_message_id_present"] is True
    assert linkage["projected_assistant_event_id_present"] is True
    assert linkage["native_and_projected_ids_bound"] is bound
    assert linkage["proven"] is bound


def test_pi_native_marker_evidence_stops_at_the_pre_interrupt_boundary(tmp_path):
    marker = "PI_RESUME_MARKER"
    native = tmp_path / "pi-session.jsonl"
    native.write_text(
        "\n".join(
            [
                json.dumps({"type": "session", "id": "pi-session"}),
                json.dumps({"type": "message", "id": "resume-before-interrupt", "message": {"role": "assistant", "content": marker}}),
                json.dumps({"type": "message", "id": "post-interrupt", "message": {"role": "assistant", "content": marker}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    lines = native.read_bytes().splitlines(keepends=True)
    boundary = len(lines[0]) + len(lines[1])

    evidence = lifecycle._native_marker_evidence("pi", native, marker, maximum_source_offset=boundary)
    assert evidence is not None
    assert evidence["native_message_id"] == "resume-before-interrupt"


def test_omp_native_marker_evidence_respects_turn_boundaries(tmp_path):
    marker = "OMP_RESUME_MARKER"
    native = tmp_path / "omp-session.jsonl"
    rows = [
        {"type": "session", "id": "omp-session"},
        {
            "type": "message",
            "id": "omp-first",
            "message": {"role": "assistant", "content": [{"type": "text", "text": marker}]},
        },
        {
            "type": "message",
            "id": "omp-second",
            "message": {"role": "assistant", "content": [{"type": "text", "text": marker}]},
        },
    ]
    native.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    lines = native.read_bytes().splitlines(keepends=True)
    first_boundary = sum(len(line) for line in lines[:2])

    first = lifecycle._native_marker_evidence("omp", native, marker, maximum_source_offset=first_boundary)
    second = lifecycle._native_marker_evidence(
        "omp",
        native,
        marker,
        minimum_source_offset=first_boundary,
        maximum_source_offset=native.stat().st_size,
    )

    assert first is not None
    assert first["native_message_id"] == "omp-first"
    assert first["provider_session_id"] == "omp-session"
    assert second is not None
    assert second["native_message_id"] == "omp-second"


_NATIVE_MARKER = "LH_FIXTURE_CONSOLE_abc"
_NATIVE_CONTEXT = "LH_FIXTURE_CONTEXT_def"
_NATIVE_NARRATION = f"I will read the file, then reply with `{_NATIVE_MARKER}`."


def _write_native_turn(path: Path, *assistant_rows: tuple[str, object]) -> None:
    """One native Pi or OMP tool turn: the row that calls the tool, its result, then ``assistant_rows`` (id, content)."""
    rows = [
        {"type": "session", "id": "native-session"},
        {"type": "message", "id": "prompt", "message": {"role": "user", "content": f"Read the file, then reply with {_NATIVE_MARKER}."}},
        {
            "type": "message",
            "id": "tool-call",
            "message": {
                "role": "assistant",
                # A turn that works through a tool says what it will reply before it calls the tool.
                "content": [{"type": "text", "text": _NATIVE_NARRATION}, {"type": "toolCall", "id": "call-1", "name": "read"}],
                "stopReason": "toolUse",
            },
        },
        # The proof file holds the marker, so the tool's output names it too.
        {"type": "message", "id": "tool-result", "message": {"role": "toolResult", "toolCallId": "call-1", "content": _NATIVE_MARKER}},
        *({"type": "message", "id": row_id, "message": {"role": "assistant", "content": content}} for row_id, content in assistant_rows),
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


@pytest.mark.parametrize("provider", ["pi", "omp"])
def test_native_marker_evidence_names_the_row_that_answers_not_the_narration(tmp_path, provider):
    native = tmp_path / "session.jsonl"
    _write_native_turn(native, ("reply", f"The read returned.\n\n{_NATIVE_CONTEXT} {_NATIVE_MARKER}"))

    evidence = lifecycle._native_marker_evidence(provider, native, _NATIVE_MARKER, alongside=(_NATIVE_CONTEXT,))

    assert evidence == {
        "native_message_id": "reply",
        "provider_session_id": "native-session",
        "source_offset": evidence["source_offset"],
        "marker_count": 1,
    }
    # The reply carries the stored context value on the marker's line, so it
    # answers only for a caller that names that value.
    assert lifecycle._native_marker_evidence(provider, native, _NATIVE_MARKER) is None


@pytest.mark.parametrize("provider", ["pi", "omp"])
def test_native_marker_evidence_reads_text_blocks_as_separate_lines(tmp_path, provider):
    native = tmp_path / "session.jsonl"
    # The marker ends one text block and the next begins with a word.
    _write_native_turn(native, ("reply", [{"type": "text", "text": _NATIVE_MARKER}, {"type": "text", "text": "Done."}]))

    evidence = lifecycle._native_marker_evidence(provider, native, _NATIVE_MARKER)

    assert evidence is not None and evidence["native_message_id"] == "reply"


@pytest.mark.parametrize("provider", ["pi", "omp"])
def test_native_marker_answered_in_two_rows_is_refused_with_the_typed_reason(tmp_path, provider):
    native = tmp_path / "session.jsonl"
    _write_native_turn(native, ("reply", _NATIVE_MARKER), ("reply-again", f"`{_NATIVE_MARKER}`"))

    with pytest.raises(MarkerAnswerCountError) as raised:
        lifecycle._native_marker_evidence(provider, native, _NATIVE_MARKER)

    assert raised.value.reason == "marker_answered_more_than_once"
    assert raised.value.answer_event_ids == ["reply", "reply-again"]
    assert raised.value.mention_event_ids == ["tool-call"]
    assert "native assistant row(s), expected exactly 1 (single-reply turn: " in str(raised.value)
    assert "(answering row ids ['reply', 'reply-again']; 1 other event(s) only mention it: ['tool-call'])" in str(raised.value)
    message = str(raised.value)
    assert message.startswith("marker_answered_more_than_once: ")
    assert "has 2 answering native assistant row(s), expected exactly 1" in message
    # Counts and ids only: nothing the model wrote reaches the classifier.
    assert "I will read the file" not in message


@pytest.mark.parametrize("provider", ["pi", "omp"])
@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(f"The marker is {_NATIVE_MARKER}, as asked.", id="in-a-sentence"),
        pytest.param(f"{_NATIVE_MARKER}\n\nThat was {_NATIVE_MARKER}.", id="answered-then-repeated"),
    ],
)
def test_native_marker_evidence_needs_a_row_that_answers_once(tmp_path, provider, reply):
    native = tmp_path / "session.jsonl"
    _write_native_turn(native, ("reply", reply))

    assert lifecycle._native_marker_evidence(provider, native, _NATIVE_MARKER) is None


def test_pi_claim_output_evidence_ignores_non_assistant_message_end(tmp_path):
    marker = "PI_OUTPUT_MARKER"
    source = tmp_path / "pi-output.jsonl"
    source.write_text(
        "\n".join(
            [
                json.dumps({"type": "message_end", "message": {"role": "user", "content": marker}}),
                json.dumps({"type": "message_end", "message": {"role": "assistant", "content": marker}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    evidence = lifecycle._claim_output_evidence("pi", {"source_path": str(source)}, marker)

    assert evidence is not None
    assert evidence["provider_response_marker_count"] == 1


def test_console_cleanup_cannot_pass_on_a_failure_path(tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle, "_pid_dead", lambda _pid: True)
    monkeypatch.setattr(lifecycle, "_process_group_dead", lambda _pgid: True)
    source = tmp_path / "native.jsonl"
    source.write_bytes(b"complete source\n")
    claims = [
        {
            "pid": 1,
            "process_group_id": 1,
            "boot_id": "boot-1",
            "process_start_time": "start-1",
            "state": "terminal",
            "provider": "omp",
            "session_id": "session-1",
            "thread_id": "thread-1",
            "run_id": "run-1",
            "source_path": str(source),
        }
    ]
    retained = lifecycle._retain_claim_sources(tmp_path, claims, {}, complete=True)
    ready = {
        "process_stop_wait_completed": True,
        "shipper_stop": {"stopped": True, "process_dead": True, "process_group_dead": True},
        "served_run_inventory": {"retired": True, "active_run_count": 0, "session_id": "session-1"},
        "session_retirement": {
            "status": "pass",
            "session_id": "session-1",
            "hidden": True,
            "archived": True,
            "present_in_served_inventory": False,
        },
        "expected_session_id": "session-1",
    }

    assert lifecycle._console_cleanup_receipt(claims, retained, **ready)["status"] == "pass"
    assert lifecycle._console_cleanup_receipt(claims, retained, **ready, run_failed=True)["status"] == "fail"
    assert lifecycle._console_cleanup_receipt(claims, retained, **(ready | {"session_retirement": None}))["status"] == "fail"


def test_omp_interrupt_recovery_is_bound_to_retained_marker_and_terminal_output(tmp_path):
    source = tmp_path / "post.jsonl"
    source.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "message_end",
                        "message": {"role": "assistant", "content": [{"type": "text", "text": "POST_MARKER"}]},
                    }
                ),
                json.dumps({"type": "agent_end", "isTerminal": True, "willContinue": False}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    evidence = lifecycle._retained_post_interrupt_output_evidence(
        "omp",
        {"stdout_path": str(source)},
        "POST_MARKER",
        [{"source": str(source), "kind": "stdout_path", "path": "post.jsonl", "retained": True}],
        tmp_path,
    )

    assert evidence["valid"] is True
    assert evidence["assistant_marker_count"] == 1
    assert evidence["assistant_reply_count"] == 1
    assert evidence["terminal_event_index"] > evidence["marker_event_index"]


def _post_interrupt_output(tmp_path: Path, *messages: object) -> dict[str, object]:
    source = tmp_path / "post.jsonl"
    events = [{"type": "message_end", "message": {"role": "assistant", "content": content}} for content in messages]
    events.append({"type": "agent_end", "isTerminal": True, "willContinue": False})
    source.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
    return lifecycle._retained_post_interrupt_output_evidence(
        "omp",
        {"stdout_path": str(source)},
        "POST_MARKER",
        [{"source": str(source), "kind": "stdout_path", "path": "post.jsonl", "retained": True}],
        tmp_path,
    )


def test_omp_interrupt_recovery_binds_the_message_that_answers_not_the_one_that_narrates(tmp_path):
    evidence = _post_interrupt_output(
        tmp_path,
        [{"type": "text", "text": "I will check the job first, then reply with `POST_MARKER`."}, {"type": "toolCall", "id": "call-1"}],
        [{"type": "text", "text": "The job is gone.\n\nPOST_MARKER"}],
    )

    assert evidence["valid"] is True
    # Every message that names the marker is counted; one of them replied.
    assert evidence["assistant_marker_count"] == 2
    assert evidence["assistant_reply_count"] == 1
    assert evidence["marker_event_index"] == 1
    assert evidence["terminal_event_index"] == 2


def test_omp_interrupt_recovery_without_a_message_that_answers_is_not_valid(tmp_path):
    evidence = _post_interrupt_output(tmp_path, "I would reply with POST_MARKER, but the job is still running.")

    assert evidence["valid"] is False
    assert evidence["assistant_marker_count"] == 1
    assert evidence["assistant_reply_count"] == 0
    assert evidence["marker_event_index"] is None


def test_omp_interrupt_recovery_answered_in_two_messages_is_refused_with_the_typed_reason(tmp_path):
    with pytest.raises(MarkerAnswerCountError) as raised:
        _post_interrupt_output(tmp_path, "POST_MARKER", "POST_MARKER")

    assert raised.value.reason == "marker_answered_more_than_once"
    assert "has 2 answering provider output message(s), expected exactly 1" in str(raised.value)


def test_source_retention_rejects_a_source_changed_during_copy(tmp_path, monkeypatch):
    source = tmp_path / "native.jsonl"
    source.write_bytes(b"original source\n")
    write_bytes = Path.write_bytes

    def append_during_retention(path, content):
        written = write_bytes(path, content)
        if path.parent.name == "provider-sources":
            write_bytes(source, b"original source\nlate source bytes\n")
        return written

    monkeypatch.setattr(Path, "write_bytes", append_during_retention)
    retained = lifecycle._retain_claim_sources(tmp_path, [{"run_id": "run-1", "source_path": str(source)}], {}, complete=True)

    assert retained[0]["retained"] is False
    assert source.read_bytes() == b"original source\nlate source bytes\n"


def test_steer_cell_requires_every_steer_fact_and_stays_out_of_the_turn_start_gate():
    passing = {
        "steer_accepted": True,
        "same_run_completed": True,
        "steer_marker_answered": True,
        "no_new_turn": True,
        "tool_ran_to_completion": True,
    }
    assert lifecycle.console_steer_assertion(passing) is True
    for fact in passing:
        assert lifecycle.console_steer_assertion(passing | {fact: False}) is False, fact
    assert lifecycle.console_steer_assertion(None) is False
    # A steer failure never reaches the strict session.turn.start assertion.
    assert "steer" not in " ".join(lifecycle.OBSERVED_ACTIVITY)
    assert lifecycle.STEER_ASSERTION_ID != lifecycle.ASSERTION_ID
    assert (lifecycle.STEER_ASSERTION_ID, None) in lifecycle.REGISTRATION.assertion_cells


def test_steer_cell_is_its_own_factory_invocation():
    # The factory runs one execution per cell and passes its execution key as
    # --variant, so each steer cell needs a key the producer's parser accepts.
    from zerg.qa import omp_console_producer
    from zerg.qa import pi_console_tool_producer

    base = ["--model", "m", "--provider", "codex"]
    steer = lifecycle.STEER_VARIANTS["codex"]
    assert steer not in (lifecycle.SUPPORTED_VARIANT, lifecycle.UNSUPPORTED_VARIANT)
    assert lifecycle._parser().parse_args([*base, "--variant", steer]).variant == steer
    assert set(lifecycle.STEER_VARIANTS) == {"codex", "claude", "opencode"}
    for producer in (pi_console_tool_producer, omp_console_producer):
        assert producer._STEER_VARIANT != producer._VARIANT
        assert producer._STEER_VARIANT.endswith(f":{lifecycle.STEER_ASSERTION_ID}:{producer.SCENARIO_ID}")


@pytest.mark.parametrize(
    ("fires", "steer_verdict", "failure_code", "expected"),
    [
        (True, False, None, "rejected"),
        (True, True, None, "undetected"),
        (False, False, None, "inconclusive"),
        (True, False, "provider_console_lifecycle_failed", "inconclusive"),
    ],
)
def test_console_steer_negative_control_verdict(tmp_path, monkeypatch, fires, steer_verdict, failure_code, expected):
    monkeypatch.delenv("LONGHOUSE_QA_FAULT", raising=False)

    def execute():
        assert os.environ["LONGHOUSE_QA_FAULT"] == lifecycle.CONSOLE_STEER_FAULT
        if fires:
            Path(os.environ["LONGHOUSE_QA_FAULT_RECEIPT"]).write_text(
                json.dumps({"fault": lifecycle.CONSOLE_STEER_FAULT, "session_id": "s1"}) + "\n", encoding="utf-8"
            )
        steer = dict.fromkeys(("steer_accepted", "no_new_turn", "same_run_completed", "tool_ran_to_completion"), True)
        result = {"assertions": {lifecycle.STEER_ASSERTION_ID: steer_verdict}, "steer": steer}
        return result | ({"failure_code": failure_code} if failure_code else {})

    result = lifecycle.run_with_console_steer_fault(execute, tmp_path)

    assert result["negative_control"]["verdict"] == expected
    assert result["status"] == ("pass" if expected == "rejected" else "fail")
    assert "LONGHOUSE_QA_FAULT" not in os.environ
    assert json.loads((tmp_path / "result.json").read_text())["negative_control"]["fault"] == "console_steer_noop"


def test_console_steer_negative_control_is_accepted_by_every_steer_producer():
    from zerg.qa import omp_console_producer
    from zerg.qa import pi_console_tool_producer

    fault = ["--negative-control", lifecycle.CONSOLE_STEER_FAULT]
    assert lifecycle._parser().parse_args(["--model", "m", *fault]).negative_control == lifecycle.CONSOLE_STEER_FAULT
    for producer in (pi_console_tool_producer, omp_console_producer):
        action = next(a for a in producer._parser()._actions if a.dest == "negative_control")
        assert action.choices == (lifecycle.CONSOLE_STEER_FAULT,)


def _steer_409_turn_ended() -> RuntimeError:
    # The route's real body: an adapter's turn_not_steerable arrives as turn_ended.
    detail = {"detail": {"error_code": "turn_ended", "retry_with_intent": "queue"}}
    return RuntimeError(f"POST /api/agents/sessions/s1/input returned HTTP 409: {json.dumps(detail)}")


def _fake_steer_route(monkeypatch, answers):
    calls: list[dict] = []
    answers = iter(answers)

    def fake_request(api_url, token, method, path, payload=None, **_kwargs):
        calls.append(dict(payload))
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(lifecycle, "_request", fake_request)
    monkeypatch.setattr(lifecycle.time, "sleep", lambda _seconds: None)
    return calls


def test_steer_post_resends_the_same_request_id_when_delivery_is_unknown(monkeypatch):
    calls = _fake_steer_route(
        monkeypatch,
        [RuntimeError("POST /input returned HTTP 502: delivery unknown"), {"outcome": "sent", "disposition": "accepted"}],
    )

    steered, attempts = lifecycle._post_steer("http://127.0.0.1:1", "t", "s1", text="x", turn_running=lambda: True)

    assert steered["outcome"] == "sent" and attempts == 2
    assert calls[0]["client_request_id"] == calls[1]["client_request_id"]


def test_steer_post_retries_the_start_race_under_a_fresh_request_id(monkeypatch):
    calls = _fake_steer_route(monkeypatch, [_steer_409_turn_ended(), {"outcome": "sent", "disposition": "accepted"}])

    steered, attempts = lifecycle._post_steer("http://127.0.0.1:1", "t", "s1", text="x", turn_running=lambda: True)

    assert steered["outcome"] == "sent" and attempts == 2
    assert calls[0]["client_request_id"] != calls[1]["client_request_id"]


def test_steer_post_takes_turn_ended_as_the_answer_once_the_turn_is_over(monkeypatch):
    _fake_steer_route(monkeypatch, [_steer_409_turn_ended()])

    steered, attempts = lifecycle._post_steer("http://127.0.0.1:1", "t", "s1", text="x", turn_running=lambda: False)

    assert attempts == 1
    assert "turn_ended" in steered["error"]


def test_console_steer_negative_control_is_inconclusive_when_the_turn_itself_failed(tmp_path, monkeypatch):
    def execute():
        Path(os.environ["LONGHOUSE_QA_FAULT_RECEIPT"]).write_text(
            json.dumps({"fault": lifecycle.CONSOLE_STEER_FAULT}) + "\n", encoding="utf-8"
        )
        steer = {"steer_accepted": True, "no_new_turn": True, "same_run_completed": False, "tool_ran_to_completion": True}
        return {"assertions": {lifecycle.STEER_ASSERTION_ID: False}, "steer": steer}

    result = lifecycle.run_with_console_steer_fault(execute, tmp_path)

    assert result["negative_control"]["verdict"] == "inconclusive"
    assert result["negative_control"]["preconditions_held"] is False
    assert result["status"] == "fail"


def test_steer_tool_completion_is_read_from_the_tool_output_not_only_content_text() -> None:
    marker = "LH_CLAUDE_STEER_TOOL_42_abc"
    first = "LH_CLAUDE_FIRST_abc"
    # How the parsers serve a finished command: the output is in tool_output_text
    # and content_text is empty, so reading content alone never saw it.
    served = {"role": "tool", "content_text": None, "tool_output_text": marker}
    assert lifecycle._tool_ran_to_completion([served], tool_marker=marker, first_marker=first)
    # A command that never ran serves nothing; the setup text quotes the
    # arithmetic, never the computed marker.
    setup = {"role": "assistant", "content_text": "sleep 15 && echo LH_CLAUDE_STEER_TOOL_$((40+2))_abc"}
    assert not lifecycle._tool_ran_to_completion([setup], tool_marker=marker, first_marker=first)
    # The user's own message and the first turn's marker do not count.
    assert not lifecycle._tool_ran_to_completion([{"role": "user", "content_text": marker}], tool_marker=marker, first_marker=first)
    assert not lifecycle._tool_ran_to_completion(
        [{"role": "tool", "tool_output_text": f"{marker} {first}"}], tool_marker=marker, first_marker=first
    )


def test_opencode_model_binding_reads_the_model_the_adapter_sent_not_argv() -> None:
    claim = {"result": {"argv": ["opencode", "serve", "--port", "1"], "model": "openrouter/deepseek/x"}}
    assert lifecycle._claim_uses_selected_model(claim, provider="opencode", model="deepseek/x")  # noqa: SLF001
    assert not lifecycle._claim_uses_selected_model(  # noqa: SLF001
        {"result": {"argv": ["opencode", "serve"], "model": "openrouter/other"}}, provider="opencode", model="deepseek/x"
    )
    # A claim that recorded no model proves nothing.
    assert not lifecycle._claim_uses_selected_model(  # noqa: SLF001
        {"result": {"argv": ["opencode", "serve"]}}, provider="opencode", model="deepseek/x"
    )


def test_turn_identity_mismatches_names_each_differing_field() -> None:
    from zerg.qa import provider_console_lifecycle as console

    expected = dict(provider="opencode", session_id="s1", thread_id="t1", run_id="r1")
    good = {
        "provider": "opencode",
        "session_id": "s1",
        "thread_id": "t1",
        "run_id": "r1",
        "adapter": console.ADAPTERS["opencode"],
        "provider_identity_confirmed": True,
    }
    assert console.turn_identity_mismatches(good, **expected) == {}
    assert console._turn_identity_ok(good, **expected)

    drifted = {**good, "thread_id": "t2", "provider_identity_confirmed": None}
    assert console.turn_identity_mismatches(drifted, **expected) == {
        "thread_id": {"expected": "t1", "observed": "t2"},
        "provider_identity_confirmed": {"expected": True, "observed": None},
    }
    assert not console._turn_identity_ok(drifted, **expected)
    summary = console.claim_identity_summary({**drifted, "state": "terminal", "result": {"terminal_state": "run_completed", "argv": ["x"]}})
    assert summary["thread_id"] == "t2" and summary["terminal_state"] == "run_completed"
    assert "argv" not in summary and "result" not in summary
    unknown = console.turn_identity_mismatches({**good, "provider": "nope", "adapter": None}, **{**expected, "provider": "nope"})
    assert "adapter" in unknown


# --- A Console turn's failure is decided from the engine's claim and the provider's own stream ---

_ASKED = "LH_OMP_POST_INTERRUPT_" + "a" * 32
_EARLIER = "LH_OMP_INTERRUPT_" + "b" * 32


def _ask(marker: str = _ASKED) -> str:
    return f"Reply with exactly {marker} and nothing else."


def _turn_claim(
    tmp_path: Path, provider: str, prompt: str, replies: list[str], *, ended: bool = True, **result: object
) -> dict[str, object]:
    """A terminal claim over one turn's stream, in the shape that adapter's provider writes it."""

    def message(role: str, text: str, block: str = "text") -> dict[str, object]:
        return {"role": role, "content": [{"type": block, "text": text}]}

    if provider in {"pi", "omp"}:
        events: list[dict[str, object]] = [{"type": "message_end", "message": message("user", prompt)}]
        events += [{"type": "message_end", "message": message("assistant", text)} for text in replies]
        end: dict[str, object] = {"type": "agent_end", "isTerminal": True}
    elif provider in {"claude", "cursor"}:
        events = [{"type": "user", "message": message("user", prompt)}]
        events += [{"type": "assistant", "uuid": f"reply-{n}", "message": message("assistant", text)} for n, text in enumerate(replies)]
        end = {"type": "result", "subtype": "success"}
    elif provider == "opencode":
        # OpenCode echoes no prompt, and writes a text part again as it grows.
        events = [{"type": "text", "part": {"type": "text", "text": text[:cut]}} for text in replies for cut in (len(text) // 2, len(text))]
        end = {"type": "step_finish", "part": {"type": "step-finish", "reason": "stop"}}
    else:
        # Codex's rollout holds the session: an earlier, finished task comes first.
        events = [
            {"type": "response_item", "payload": {"type": "message", **message("assistant", _EARLIER, "output_text")}},
            {"type": "event_msg", "payload": {"type": "task_complete"}},
            {"type": "event_msg", "payload": {"type": "task_started"}},
            {"type": "response_item", "payload": {"type": "message", **message("user", prompt, "input_text")}},
        ]
        events += [
            {"type": "response_item", "payload": {"type": "message", **message("assistant", text, "output_text")}} for text in replies
        ]
        end = {"type": "event_msg", "payload": {"type": "task_complete"}}
    stream = tmp_path / f"{provider}-turn.jsonl"
    stream.write_text("".join(json.dumps(event) + "\n" for event in [*events, *([end] if ended else [])]), encoding="utf-8")
    return {
        "run_id": "run-1",
        "state": "terminal",
        "result": {"terminal_state": "run_completed", **result},
        lifecycle._TURN_STREAM_IN_CLAIM[provider]: str(stream),
    }


@pytest.mark.parametrize("provider", ["pi", "omp", "claude", "cursor", "opencode", "codex"])
def test_every_adapters_stream_gives_the_turns_replies_its_prompt_and_its_end(tmp_path, provider):
    account = lifecycle._turn_account(provider, _turn_claim(tmp_path, provider, _ask(), ["Reading first.", _ASKED]), _ASKED)
    assert account["assistant_replies"] == 2 and account["answering_replies"] == 1 and account["turn_ended"] is True
    # OpenCode's stream echoes no prompt, so nothing is known of it either way.
    assert account["prompt_named_marker"] is (None if provider == "opencode" else True)

    other = lifecycle._turn_account(provider, _turn_claim(tmp_path, provider, _ask(), [_EARLIER], ended=False), _ASKED)
    assert other["answering_replies"] == 0 and other["turn_ended"] is False
    assert other["final_reply_names_marker"] is False and other["final_reply_other_markers"] == ["LH_OMP_INTERRUPT"]


def test_a_turn_that_had_to_complete_fails_at_once_on_the_engines_claim(tmp_path):
    failed = _turn_claim(tmp_path, "omp", _ask(), [_ASKED], terminal_state="run_failed", error="native session source did not drain")
    with pytest.raises(lifecycle.ConsoleTurnFailure) as raised:
        lifecycle._require_run_completed("omp", failed, turn="post-interrupt", marker=_ASKED)
    # The engine's own error, and beside it what the provider wrote; no model text and no marker's random part.
    assert str(raised.value) == (
        "console_run_not_completed: the engine ended the post-interrupt turn as state=terminal terminal_state=run_failed "
        "with error 'native session source did not drain'; the provider's own stream for the turn holds the reply that "
        "answers LH_OMP_POST_INTERRUPT"
    )
    # The engine's reason code is carried when the claim keeps its terminal event: a provider
    # that moved the session to another file reads as that, in the engine's words.
    moved = {**failed, "error": "OMP stopped saving to this session's file: Session a.jsonl is open in another process"}
    moved["terminal_event"] = {"payload": {"terminal_state": "run_failed", "terminal_reason": "provider_session_moved"}}
    with pytest.raises(lifecycle.ConsoleTurnFailure, match="terminal_state=run_failed terminal_reason=provider_session_moved with error"):
        lifecycle._require_run_completed("omp", moved, turn="resume", marker=_ASKED)
    # A claim the engine wrote as failed without a result is the same failure; a completed one is none.
    with pytest.raises(
        lifecycle.ConsoleTurnFailure, match="state=failed terminal_state=None with error 'spawn failed'; .* could not be read"
    ):
        lifecycle._require_run_completed("omp", {"state": "failed", "error": "spawn failed"}, turn="first", marker=_ASKED)
    lifecycle._require_run_completed("omp", _turn_claim(tmp_path, "omp", _ask(), []), turn="first", marker=_ASKED)


_CONTEXT = "LH_OMP_CONTEXT_" + "c" * 32


def _session_record(tmp_path: Path, resumed_parent: str | None) -> str:
    """A Pi or OMP session file: a turn that stores the context, then the continuation prompt under ``resumed_parent``."""

    entries = [
        ("u1", None, "user", f"Store the exact value {_CONTEXT!r}."),
        ("a1", "u1", "assistant", "Stored."),
        ("u2", resumed_parent, "user", _ask()),
    ]
    path = tmp_path / "session.jsonl"
    path.write_text(
        "".join(
            json.dumps({"type": "message", "id": i, "parentId": parent, "message": {"role": r, "content": t}}) + "\n"
            for i, parent, r, t in entries
        ),
        encoding="utf-8",
    )
    return str(path)


# What the provider wrote for a turn the engine completed, and what an unserved reply to it is therefore called.
_UNANSWERED = {
    # The model sent something else, and Longhouse's part is shown: the model's deviation, by what the reply carried.
    "another marker": (dict(replies=[_EARLIER]), "ModelDeviation", "reply_carried_another_marker", None),
    "no marker": (dict(replies=["I would rather not."]), "ModelDeviation", "reply_carried_no_marker", None),
    "marker with other text": (
        dict(replies=[f"Sure: {_ASKED}. Anything else?"]),
        "ModelDeviation",
        "reply_named_marker_with_other_text",
        None,
    ),
    "context held": (dict(replies=["No value was stored."], resumed_parent="a1"), "ModelDeviation", "reply_carried_no_marker", None),
    # The provider answered and Longhouse never served it.
    "answered": (dict(replies=[_ASKED]), "ConsoleTurnFailure", "provider_reply_not_served", "longhouse_ingest"),
    # The provider's own stream holds no end of turn.
    "turn open": (dict(replies=["Working."], ended=False), "ConsoleTurnFailure", "provider_turn_not_ended", "provider_turn"),
    # The resumed process started a chain of its own: the context was never loaded. A finding, never a deviation.
    "context missing": (
        dict(replies=["No value was stored."], resumed_parent=None),
        "ConsoleTurnFailure",
        "console_resume_context_missing",
        None,
    ),
    # Whose doing it is cannot be read.
    "other prompt": (dict(prompt=_ask(_EARLIER), replies=[_EARLIER]), "ConsoleTurnFailure", "provider_reply_not_judged", None),
    "no reply": (dict(replies=[]), "ConsoleTurnFailure", "provider_reply_not_judged", None),
}


@pytest.mark.parametrize("case", sorted(_UNANSWERED))
def test_an_unserved_reply_is_named_from_what_the_provider_itself_wrote(tmp_path, case):
    from zerg.qa.model_compliance import failure_claim
    from zerg.qa.wait_kind import waited_on

    turn, kind, reason, declared = _UNANSWERED[case]
    claim = _turn_claim(tmp_path, "omp", turn.get("prompt", _ask()), turn["replies"], ended=turn.get("ended", True))
    context = {}
    if "resumed_parent" in turn:
        claim["source_path"] = _session_record(tmp_path, turn["resumed_parent"])
        context = {"context_marker": _CONTEXT}

    error = lifecycle._unanswered_turn_error("omp", claim, _ASKED, turn="resume", served="count=0", **context)

    assert (type(error).__name__, error.reason, waited_on(error)) == (kind, reason, declared)
    assert str(error).startswith(f"{reason}: ") and str(error).endswith(" (served: count=0)")
    # Marker names only: no marker's random part and none of the model's words.
    assert "a" * 32 not in str(error) and "b" * 32 not in str(error) and "rather not" not in str(error)
    held = failure_claim(error).get("model_noncompliance", {}).get("contract_evidence")
    if kind != "ModelDeviation":
        assert held is None
    else:
        proven = {"run_completed", "provider_turn_ended", "provider_replied", "prompt_named_marker"}
        assert set(held) == proven | ({"resumed_session_holds_context"} if "resumed_parent" in turn else set())


def test_a_stream_that_echoes_no_prompt_or_cannot_be_read_claims_only_what_it_shows(tmp_path):
    from zerg.qa.model_compliance import failure_claim

    # OpenCode echoes no prompt: the deviation is claimed on what is shown, and the prompt is not among it.
    opencode = lifecycle._unanswered_turn_error("opencode", _turn_claim(tmp_path, "opencode", _ask(), [_EARLIER]), _ASKED, turn="resume")
    assert set(failure_claim(opencode)["model_noncompliance"]["contract_evidence"]) == {
        "run_completed",
        "provider_turn_ended",
        "provider_replied",
    }
    assert "carries LH_OMP_INTERRUPT and not the LH_OMP_POST_INTERRUPT it was asked for" in str(opencode)
    # No stream, or a session record without the continuation prompt: nothing is said about the model.
    absent = {"state": "terminal", "result": {"terminal_state": "run_completed"}, "stdout_path": str(tmp_path / "absent")}
    assert lifecycle._unanswered_turn_error("omp", absent, _ASKED, turn="resume").reason == "provider_reply_not_judged"
    unrecorded = _turn_claim(tmp_path, "omp", _ask(), ["No value was stored."])
    assert (
        lifecycle._unanswered_turn_error("omp", unrecorded, _ASKED, turn="resume", context_marker=_CONTEXT).reason
        == "provider_reply_not_judged"
    )


def test_the_flush_waits_for_the_provider_the_engine_reported_finished(monkeypatch, tmp_path):
    from zerg.qa.wait_kind import waited_on

    child = os.fork()
    if child == 0:
        os._exit(0)
    os.waitpid(child, 0)
    closed = lifecycle._await_provider_closed({"run_id": "run-1", "pid": child, "process_group_id": child}, tmp_path, turn="resume")
    assert closed["provider_closed"] is True and closed["close_wait_seconds"] < 1

    # Still running when the bound runs out: the engine reported terminal and did not close it.
    monkeypatch.setattr(lifecycle, "PROVIDER_CLOSE_SECONDS", 0.3)
    alive = {"run_id": "run-2", "pid": os.getpid(), "process_group_id": os.getpgrp()}
    with pytest.raises(
        lifecycle.ConsoleTurnFailure, match="^provider_outlived_terminal_run: the engine reported the steer turn's run terminal"
    ) as raised:
        lifecycle._await_provider_closed(alive, tmp_path, turn="steer")
    assert waited_on(raised.value) == "longhouse_engine" and f"pid {os.getpid()} alive=True" in str(raised.value)

    # An invocation the engine parked keeps its provider on purpose, whenever its claim came to say so.
    assert lifecycle._await_provider_closed({**alive, "invocation_state": "parked"}, tmp_path, turn="steer") == {"provider_closed": None}
    claim_path = lifecycle._claim_path(tmp_path, "run-2")
    claim_path.parent.mkdir(parents=True)
    claim_path.write_text(json.dumps({**alive, "invocation_state": "parked", "pending_count": 1}), encoding="utf-8")
    assert lifecycle._await_provider_closed(alive, tmp_path, turn="steer") == {"provider_closed": None}
    # A claim that names no process is nothing to observe.
    assert lifecycle._await_provider_closed({"run_id": "run-3"}, tmp_path, turn="first") == {"provider_closed": None}


def test_a_session_persistence_notice_is_kept_with_the_turn_that_raised_it(tmp_path):
    message = "Session ~/sessions/example/first.jsonl is open for writing in another process, so this one continues elsewhere."
    forked = tmp_path / "forked.jsonl"
    forked.write_text(
        json.dumps({"type": "notice", "level": "warning", "source": "session-persistence", "message": message})
        + "\n"
        + json.dumps({"type": "notice", "source": "other", "message": "unrelated.jsonl"})
        + "\n",
        encoding="utf-8",
    )
    claims = [_turn_claim(tmp_path, "omp", _ask(), [_ASKED]), {"run_id": "run-2", "stdout_path": str(forked)}]

    # Written with the retained sources, which a passing run and a failing one both do.
    lifecycle._retain_claim_sources(tmp_path / "evidence", claims, {})
    retention = json.loads((tmp_path / "evidence" / "provider-source-retention.json").read_text(encoding="utf-8"))
    assert retention["session_persistence_notices"] == [
        {"turn_index": 1, "run_id": "run-2", "level": "warning", "session_file": "~/sessions/example/first.jsonl", "message": message}
    ]
    assert len(retention["sources"]) == 2


# --- A wait for a served reply ends early only when the provider's stream shows a finished turn that did not answer ---


class _Clock:
    """The producer's clock, so that how long a wait took is read and not sat through."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_a_served_wait_stops_early_only_on_what_is_already_certain(monkeypatch):
    from zerg.qa.wait_kind import waited_on

    marker = "LH_CODEX_STEERED_abc"
    clock = _Clock()
    monkeypatch.setattr(lifecycle, "time", clock)
    _served(monkeypatch, [{"id": "other", "role": "assistant", "content_text": "LH_CODEX_STEER_abc"}])

    def wait(stream_unanswered, native: dict[str, int] | None = None, state: str = "read") -> RuntimeError:
        clock.now = 0.0
        with pytest.raises(RuntimeError) as gave_up:
            lifecycle._wait_served_answers(
                "https://runtime.example",
                "token",
                "session-1",
                marker,
                timeout=120,
                native_answers=None if native is None else (lambda: (dict(native) if state == "read" else None, state)),
                unanswered=lambda served: RuntimeError(f"from the stream ({served})"),
                stream_unanswered=stream_unanswered,
            )
        return gave_up.value

    # The stream shows a finished turn that did not answer: nothing is coming, and the wait says so from the stream.
    # It stands for the two seconds a converged page must, from a clock that starts at zero too.
    assert str(wait(lambda: True)) == "from the stream (count=0, events that only mention it=0)" and 2 <= clock.now < 5
    assert str(wait(lambda: True, native={})).startswith("from the stream (count=0, ") and 2 <= clock.now < 5
    # Without that reading, or with none to ask, the wait runs to its deadline as it always did.
    for undecided in (lambda: False, None):
        wait(undecided)
        assert clock.now >= 120
    # Nor does the stream outrank the provider's own record: a row of it that answers is still to be
    # served, and a record that could not be read may hold one.
    assert "from the stream" in str(wait(lambda: True, native={"row": 1})) and clock.now >= 120
    unread = wait(lambda: True, native={}, state="unreadable")
    assert str(unread).startswith("provider_reply_not_judged: ") and clock.now >= 120 and waited_on(unread) is None


def _served_from(monkeypatch, clock: _Clock, at: float, events: list[dict[str, object]]) -> None:
    """An archive that serves ``events`` only once the clock has reached ``at``."""

    monkeypatch.setattr(lifecycle, "_request", lambda *_args: {"events": events if clock.now >= at else []})


def _turn_wait(provider: str, claim: dict[str, object], **wait: object):
    return lifecycle._wait_turn_served("https://runtime.example", "token", "session-1", provider, claim, _ASKED, turn="resume", **wait)


@pytest.mark.parametrize("provider", ["codex", "cursor", "opencode"])
def test_a_counted_adapters_wait_is_cut_short_only_by_a_finished_turn_whose_replies_were_read(monkeypatch, tmp_path, provider):
    """These adapters have no row of the provider's record to wait on, so the stream alone must be certain."""

    from zerg.qa.wait_kind import waited_on

    clock = _Clock()
    monkeypatch.setattr(lifecycle, "time", clock)
    answer = [{"id": "served", "role": "assistant", "content_text": _ASKED}]

    # The turn ended and its replies were read; none answers. Certain: the wait ends on the settle time.
    _served_from(monkeypatch, clock, 0, [])
    with pytest.raises(RuntimeError, match=r"^reply_carried_another_marker: the resume turn completed .*\(served: count=0, "):
        _turn_wait(provider, _turn_claim(tmp_path, provider, _ask(), ["Reading first.", _EARLIER]), timeout=120)
    assert 2 <= clock.now < 5

    # The provider answered in a shape the stream's reader does not know, so no reply was recognised at all.
    # That is not a turn without an answer: the page serves the reply late and the wait takes it.
    unknown = _turn_claim(tmp_path, provider, _ask(), [])
    with Path(str(unknown[lifecycle._TURN_STREAM_IN_CLAIM[provider]])).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": "reply.v2", "delta": {"value": _ASKED}}) + "\n")
    clock.now = 0.0
    _served_from(monkeypatch, clock, 30, answer)
    answers, _account = _turn_wait(provider, unknown, timeout=120)
    assert [event["id"] for event in answers] == ["served"] and 30 <= clock.now < 40
    # Served nothing, it runs to the deadline, and nothing is said about the model.
    clock.now = 0.0
    _served_from(monkeypatch, clock, 0, [])
    with pytest.raises(lifecycle.ConsoleTurnFailure, match="^provider_reply_not_judged: .* without producing an assistant reply"):
        _turn_wait(provider, unknown, timeout=120)
    assert clock.now >= 120

    # A stream with a reply and no end of turn is not finished: waited for, and declared a wait on the provider.
    clock.now = 0.0
    with pytest.raises(lifecycle.ConsoleTurnFailure, match="^provider_turn_not_ended: ") as still_open:
        _turn_wait(provider, _turn_claim(tmp_path, provider, _ask(), [_EARLIER], ended=False), timeout=120)
    assert clock.now >= 120 and waited_on(still_open.value) == "provider_turn"
    # And a late answer to a turn the stream calls unfinished is taken, as before.
    clock.now = 0.0
    _served_from(monkeypatch, clock, 30, answer)
    assert _turn_wait(provider, _turn_claim(tmp_path, provider, _ask(), [_EARLIER], ended=False), timeout=120)[0] == answer


# --- The steer cell: whose doing a reply without the steered marker is ---

_TAG = "0123456789abcdef0123456789abcdef"


def _steer_markers(provider: str) -> tuple[str, str, str]:
    """The first, steered and tool markers the steer step mints for ``provider`` under `_TAG`."""

    stem = f"LH_{provider.upper()}_"
    return f"{stem}STEER_FIRST_{_TAG}", f"{stem}STEERED_{_TAG}", f"{stem}STEER_TOOL_42_{_TAG}"


def _steer_page(provider: str, final_reply: str, *, steer_at: str | None = "2026-01-01T00:00:27+00:00") -> list[dict[str, object]]:
    """What the archive serves of a steered turn; ``steer_at`` is when the provider recorded the steer, None when it never did."""

    first, steered, tool = _steer_markers(provider)
    page: list[dict[str, object]] = [
        {"id": "prompt", "role": "user", "content_text": _ask(first), "timestamp": "2026-01-01T00:00:20+00:00"},
        {"id": "call", "role": "assistant", "tool_name": "bash", "content_text": None, "timestamp": "2026-01-01T00:00:31+00:00"},
        {"id": "result", "role": "tool", "tool_output_text": tool, "timestamp": "2026-01-01T00:00:46+00:00"},
        {"id": "reply-1", "role": "assistant", "content_text": final_reply, "timestamp": "2026-01-01T00:00:49+00:00"},
    ]
    if steer_at is not None:
        page.append({"id": "steer", "role": "user", "content_text": f"Change of plan: {_ask(steered)}", "timestamp": steer_at})
    return sorted(page, key=lambda event: str(event["timestamp"]))


def _run_steer(monkeypatch, tmp_path: Path, provider: str, final_reply: str, page: list[dict[str, object]]) -> tuple[dict, _Clock]:
    """The steer step over a finished turn: the provider's own stream for it, and the page the archive serves."""

    from uuid import UUID

    first, _steered, tool = _steer_markers(provider)
    replies = ["Running the command.", final_reply]
    claim = _turn_claim(tmp_path, provider, _ask(first), replies)
    if provider == "opencode":
        # As OpenCode writes a turn steered mid-tool: each text part again as it grows, the
        # hand-off to the tool, the reply in a part of its own, and no prompt at all.
        def part(part_id: str, text: str) -> dict[str, object]:
            return {"type": "text", "part": {"id": part_id, "type": "text", "text": text}}

        def finish(reason: str) -> dict[str, object]:
            return {"type": "step_finish", "part": {"type": "step-finish", "reason": reason}}

        stream = [
            *(part("p1", replies[0][:cut]) for cut in (3, len(replies[0]))),
            {"type": "tool_use", "part": {"type": "tool", "tool": "bash", "state": {"status": "completed", "output": tool}}},
            finish("tool-calls"),
            *(part("p2", "The command has finished."[:cut]) for cut in (3, 25)),
            *(part("p3", final_reply[:cut]) for cut in (0, 14, len(final_reply))),
            finish("stop"),
        ]
        Path(str(claim["stdout_path"])).write_text("".join(json.dumps(event) + "\n" for event in stream), encoding="utf-8")
    if provider in {"pi", "omp"}:
        # Their served replies are held to the session file's rows, by id.
        record = tmp_path / f"{provider}-session.jsonl"
        rows = [{"type": "session", "id": "native-session"}]
        rows += [
            {"type": "message", "id": f"reply-{n}", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}
            for n, text in enumerate(replies)
        ]
        record.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        claim["source_path"] = str(record)
    clock = _Clock()
    claims = iter([{**claim, "state": "spawned"}, claim])
    monkeypatch.setattr(lifecycle, "time", clock)
    monkeypatch.setattr(lifecycle, "uuid4", lambda: UUID(hex=_TAG))
    monkeypatch.setattr(lifecycle, "_start_turn", lambda **_kwargs: {"run_id": "run-1", "turn_id": "turn-1"})
    monkeypatch.setattr(lifecycle, "_wait_claim", lambda *_args, **_kwargs: next(claims))
    monkeypatch.setattr(lifecycle, "_post_steer", lambda *_args, **_kwargs: ({"outcome": "sent", "disposition": "accepted"}, 12))
    monkeypatch.setattr(lifecycle, "_wait_turn_terminal", lambda **_kwargs: {})
    monkeypatch.setattr(lifecycle, "_request", lambda *_args, **_kwargs: {"events": page})
    receipt = lifecycle._run_steer_step(
        api_url="https://runtime.example",
        token="token",
        session_id="session-1",
        provider=provider,
        longhouse_home=tmp_path,
        claims=[],
        flush_for_projection=lambda *_args: {},
    )
    return receipt, clock


def _steer_observation(receipt: dict) -> dict:
    dispatch, binding, interrupt, cleanup = _receipts()
    return lifecycle._observation_from_receipts(dispatch=dispatch, binding=binding, interrupt=interrupt, cleanup=cleanup, steer=receipt)


@pytest.mark.parametrize("provider", sorted(lifecycle.STEER_PROVIDERS))
def test_a_wrong_reply_to_a_steer_the_provider_recorded_is_the_models(monkeypatch, tmp_path, provider):
    first, steered, _tool = _steer_markers(provider)
    miscopied = steered.replace("STEERED", "STEER")

    receipt, clock = _run_steer(monkeypatch, tmp_path, provider, miscopied, _steer_page(provider, miscopied))

    assert receipt["status"] == "fail" and receipt["steer_accepted"] is True and receipt["steer_marker_answered"] is False
    assert receipt["steer_marker_error"].startswith(
        f"reply_carried_another_marker: the steer turn completed and its final reply carries LH_{provider.upper()}_STEER and not"
    )
    # What was found, where, and its time against the final reply's.
    assert receipt["steer_delivery"] == {
        "source": "served_events",
        "steer_event_id": "steer",
        "steer_event_at": "2026-01-01T00:00:27+00:00",
        "final_reply_event_id": "reply-1",
        "final_reply_at": "2026-01-01T00:00:49+00:00",
        "delivered_before_final_reply": True,
        "missing": None,
    }
    # The same claim for every provider, whatever its stream echoes of a steer.
    assert receipt["model_noncompliance"] == {
        "reason": "reply_carried_another_marker",
        "contract_evidence": dict.fromkeys(
            ("run_completed", "provider_turn_ended", "provider_replied", "steer_delivered_before_final_reply"), True
        ),
    }
    # Where the factory reads it: on the observation, under the assertion that is false.
    assert _steer_observation(receipt)["model_noncompliance"] == {lifecycle.STEER_ASSERTION_ID: receipt["model_noncompliance"]}
    # The stream had ended without the reply before the wait began: it stood for the
    # settle time and not for its 120 seconds (6 of these are the pause before the steer).
    assert clock.now < 6 + 5

    # The first marker, with the steer on record ahead of it: delivery is shown, so not following it was the model's.
    ignored, _clock = _run_steer(monkeypatch, tmp_path, provider, first, _steer_page(provider, first))
    assert ignored["model_noncompliance"]["reason"] == "reply_carried_another_marker"
    assert ignored["steer_delivery"]["delivered_before_final_reply"] is True


@pytest.mark.parametrize("provider", sorted(lifecycle.STEER_PROVIDERS))
def test_an_accepted_steer_the_provider_never_recorded_stays_a_finding(monkeypatch, tmp_path, provider):
    first, _steered, _tool = _steer_markers(provider)

    # Acknowledged and dropped: the model answers the first marker, which reads exactly like a reply that carried another.
    receipt, clock = _run_steer(monkeypatch, tmp_path, provider, first, _steer_page(provider, first, steer_at=None))

    assert receipt["status"] == "fail" and receipt["steer_accepted"] is True and receipt["steer_marker_answered"] is False
    assert receipt["steer_marker_error"].startswith("reply_carried_another_marker: ")
    assert receipt["steer_delivery"]["delivered_before_final_reply"] is False
    assert receipt["steer_delivery"]["missing"] == "no served user event carries the steer"
    assert "model_noncompliance" not in receipt and "model_noncompliance" not in _steer_observation(receipt)
    assert clock.now < 6 + 5

    # Recorded only once the provider had finished replying, or timed after the reply it is served ahead of: not delivered.
    late, _clock = _run_steer(monkeypatch, tmp_path, provider, first, _steer_page(provider, first, steer_at="2026-01-01T00:00:50+00:00"))
    assert late["steer_delivery"]["missing"] == "no served assistant reply follows the steer" and "model_noncompliance" not in late
    mistimed = _steer_page(provider, first)
    mistimed[1]["timestamp"] = "2026-01-01T00:00:55+00:00"
    assert lifecycle._steer_delivery(mistimed, _steer_markers(provider)[1])["missing"] == "the steer is not timed before the final reply"


@pytest.mark.parametrize("provider", sorted(lifecycle.STEER_PROVIDERS))
def test_a_steer_the_model_followed_passes_and_keeps_its_delivery_on_the_receipt(monkeypatch, tmp_path, provider):
    _first, steered, _tool = _steer_markers(provider)

    receipt, _clock = _run_steer(monkeypatch, tmp_path, provider, steered, _steer_page(provider, steered))

    assert receipt["status"] == "pass" and lifecycle.console_steer_assertion(receipt) is True
    assert receipt["steer_answer_event_ids"] == ["reply-1"] and "steer_marker_error" not in receipt
    assert receipt["steer_delivery"]["delivered_before_final_reply"] is True
    assert "model_noncompliance" not in receipt and "model_noncompliance" not in _steer_observation(receipt)


def test_a_dropped_steer_put_down_to_the_model_is_not_a_detected_drop(tmp_path, monkeypatch):
    monkeypatch.delenv("LONGHOUSE_QA_FAULT", raising=False)
    steer = dict.fromkeys(("steer_accepted", "no_new_turn", "same_run_completed", "tool_ran_to_completion"), True)

    def execute():
        Path(os.environ["LONGHOUSE_QA_FAULT_RECEIPT"]).write_text(
            json.dumps({"fault": lifecycle.CONSOLE_STEER_FAULT}) + "\n", encoding="utf-8"
        )
        excused = {"reason": "reply_carried_another_marker", "contract_evidence": {"steer_delivered_before_final_reply": True}}
        return {"assertions": {lifecycle.STEER_ASSERTION_ID: False}, "steer": steer | {"model_noncompliance": excused}}

    result = lifecycle.run_with_console_steer_fault(execute, tmp_path)

    # The assertion is false, and the factory would still have retried the run and kept it off the findings.
    assert result["negative_control"]["verdict"] == "undetected" and result["negative_control"]["target_outcome"] == "model_deviation"
    assert result["status"] == "fail"
