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
        ("codex", lifecycle.UNSUPPORTED_VARIANT),
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

    with pytest.raises(RuntimeError, match="exactly one occurrence"):
        lifecycle._wait_exact_assistant_marker("https://runtime.example", "token", "session-1", marker)


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
    "projected_id",
    ["longhouse-event-9", pytest.param("pi-message-42", id="equal-native-and-projected")],
)
def test_pi_continuation_linkage_accepts_native_projection_identity_relationships(tmp_path, projected_id):
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

    native_evidence = lifecycle._pi_native_marker_evidence(native, marker)
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
    assert linkage["native_and_projected_ids_bound"] is True
    assert linkage["proven"] is True


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

    evidence = lifecycle._pi_native_marker_evidence(native, marker, maximum_source_offset=boundary)
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

    first = lifecycle._omp_native_marker_evidence(native, marker, maximum_source_offset=first_boundary)
    second = lifecycle._omp_native_marker_evidence(
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
    assert evidence["terminal_event_index"] > evidence["marker_event_index"]


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
