"""Failure/cleanup conformance for the Claude and Codex producer entrypoints."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from zerg.qa import claude_coordination_awareness_create as claude_create
from zerg.qa import claude_coordination_awareness_post_compaction as claude_compaction
from zerg.qa import claude_coordination_directed_input as claude_directed
from zerg.qa import claude_launch_helm_real_print as claude_launch
from zerg.qa import claude_native_resume
from zerg.qa import claude_turn_boundary_quiescent as claude_boundary
from zerg.qa import claude_turn_start_real_print as claude_turn_start
from zerg.qa import codex_coordination_native as codex_coordination
from zerg.qa import codex_helm_launch_visibility as codex_launch
from zerg.qa import codex_native_resume
from zerg.qa import codex_turn_boundary_native as codex_boundary
from zerg.qa import provider_native_resume
from zerg.qa.provider_semantic_qualification import AssertionOutcome

COVERED_PRODUCERS = frozenset(
    {
        "zerg.qa.claude_coordination_awareness_create",
        "zerg.qa.claude_coordination_awareness_post_compaction",
        "zerg.qa.claude_coordination_directed_input",
        "zerg.qa.claude_launch_helm_real_print",
        "zerg.qa.claude_native_resume",
        "zerg.qa.claude_turn_boundary_quiescent",
        "zerg.qa.claude_turn_start_real_print",
        "zerg.qa.codex_coordination_native",
        "zerg.qa.codex_helm_launch_visibility",
        "zerg.qa.codex_native_resume",
        "zerg.qa.codex_turn_boundary_native",
    }
)


class _FakeShipper:
    receipt = {"status": "pass", "machine_name": "conformance-machine"}

    def __init__(self) -> None:
        self.stop_calls = 0

    def stop(self) -> dict[str, object]:
        self.stop_calls += 1
        return {"status": "pass", "stopped": True}


class _FakeSession:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.submitted: list[str] = []

    def submit_line(self, line: str) -> None:
        self.submitted.append(line)


def _executable(path: Path, output: str = "test-provider 1.0") -> Path:
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{output}'\\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _fail_first_result_write(module: object, monkeypatch: pytest.MonkeyPatch) -> None:
    original = module.write_json
    raised = False

    def controlled_write(path: Path, payload: object) -> None:
        nonlocal raised
        if path.name == "result.json" and not raised:
            raised = True
            raise RuntimeError("controlled failure after producer judgment")
        original(path, payload)

    monkeypatch.setattr(module, "write_json", controlled_write)


def _assert_persisted_failure(root: Path, result: dict[str, object]) -> dict[str, object]:
    assert result["status"] == "fail"
    persisted = json.loads((root / "result.json").read_text(encoding="utf-8"))
    assert persisted["status"] == "fail"
    assert persisted["failure_code"]
    return persisted


def _install_claude_session_boundaries(
    module: object, monkeypatch: pytest.MonkeyPatch, root: Path
) -> tuple[_FakeShipper, list[_FakeSession]]:
    shipper = _FakeShipper()
    sessions: list[_FakeSession] = []

    def start_machine(*_args: object, **_kwargs: object):
        return shipper, {"HOME": str(root), "LONGHOUSE_HOME": str(root / "longhouse")}

    def launch_session(*_args: object, **kwargs: object):
        session = _FakeSession(f"session-{len(sessions) + 1}")
        sessions.append(session)
        return session, session.session_id, f"provider-{len(sessions)}"

    monkeypatch.setattr(module, "start_machine_and_shipper", start_machine)
    monkeypatch.setattr(module, "prepare_claude_profile", lambda **_kwargs: {"status": "pass"})
    monkeypatch.setattr(module, "launch_claude_session", launch_session)
    if hasattr(module, "read_coordination_token"):
        monkeypatch.setattr(module, "read_coordination_token", lambda *_args: "coordination-token")
    monkeypatch.setattr(module, "close_session", lambda _session: {"exit_code": 0, "alive_after_close": False})
    return shipper, sessions


def _claude_args(root: Path, binary: Path, **changes: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "evidence_root": root,
        "provider_bin": binary,
        "engine": binary,
        "model": "test-model",
        "project": "test-project",
        "launch_timeout_secs": 1,
        "response_timeout_secs": 1,
        "compaction_timeout_secs": 1,
        "receipt_timeout_secs": 1,
        "inbox_timeout_secs": 1,
        "quiescent_timeout_secs": 1,
        "variant": "test-variant",
        "api_url": "http://127.0.0.1:9",
        "agents_token": "test-token",
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _codex_args(root: Path, binary: Path, **changes: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "evidence_root": root,
        "codex_bin": binary,
        "engine": binary,
        "repo_root": root,
        "api_url": "http://127.0.0.1:9",
        "agents_token": "test-token",
        "model": "test-model",
        "variant": "test-variant",
        "live_send_timeout_secs": 1,
        "bridge_start_timeout_secs": 1,
        "tui_record_secs": 1,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_claude_awareness_create_late_persistence_keeps_judgment_and_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "evidence"
    binary = _executable(tmp_path / "claude")
    shipper, sessions = _install_claude_session_boundaries(claude_create, monkeypatch, root)
    monkeypatch.setattr(claude_create, "await_assistant_marker", lambda **_kwargs: None)
    monkeypatch.setattr(
        claude_create,
        "find_tool_invocation",
        lambda *_args, **_kwargs: {"tool_result_line": "peers result", "is_error": False},
    )
    _fail_first_result_write(claude_create, monkeypatch)
    result = claude_create.run_awareness_create_scenario(_claude_args(root, binary))
    persisted = _assert_persisted_failure(root, result)
    assert persisted["observation"]["coordination_instructions_model_visible"] is True
    assert persisted["assertions"]["coordination_instructions_model_visible"] is True
    assert sessions and sessions[0].submitted
    assert shipper.stop_calls == 1
    assert json.loads((root / "cleanup-receipt.json").read_text())["status"] == "pass"


def test_claude_awareness_post_compaction_late_persistence_keeps_assertions_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "evidence"
    binary = _executable(tmp_path / "claude")
    shipper, sessions = _install_claude_session_boundaries(claude_compaction, monkeypatch, root)
    monkeypatch.setattr(claude_compaction, "await_assistant_marker", lambda **_kwargs: None)
    monkeypatch.setattr(
        claude_compaction,
        "find_tool_invocation",
        lambda *_args, **_kwargs: {"tool_result_line": "peers result", "is_error": False},
    )
    monkeypatch.setattr(claude_compaction, "mcp_bootstrap_config_paths", lambda *_args, **_kwargs: [root / "mcp.json"])
    monkeypatch.setattr(claude_compaction, "transcript_line_counts", lambda *_args, **_kwargs: {"visible": 1})
    monkeypatch.setattr(claude_compaction, "wait_for_terminal_quiescence", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(claude_compaction, "wait_until", lambda *_args, **_kwargs: {"compaction": "completed"})
    _fail_first_result_write(claude_compaction, monkeypatch)
    variant = next(iter(claude_compaction._CELL_BY_VARIANT))
    result = claude_compaction.run_awareness_post_compaction_scenario(_claude_args(root, binary, variant=variant))
    persisted = _assert_persisted_failure(root, result)
    assert persisted["observation"]["coordination_instructions_model_visible_after_compaction"] is True
    assert all(persisted["assertions"].values())
    assert sessions and sessions[0].submitted
    assert shipper.stop_calls == 1
    assert json.loads((root / "cleanup-receipt.json").read_text())["status"] == "pass"


def test_claude_directed_input_late_persistence_keeps_assertions_and_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "evidence"
    binary = _executable(tmp_path / "claude")
    shipper, sessions = _install_claude_session_boundaries(claude_directed, monkeypatch, root)
    monkeypatch.setattr(claude_directed, "local_managed_control_fact", lambda *_args, **_kwargs: {"attached": True})
    item = {
        "id": "input-1",
        "client_request_id": "request-1",
        "source_session_id": "session-1",
        "input_receipt": {"status": "accepted"},
    }

    def api_json(_url: str, _token: str, path: str, **_kwargs: object) -> dict[str, object]:
        if "direction=inbound" in path:
            return {"directed_inputs": [item]}
        if path == "directed-inputs":
            return item
        raise AssertionError(path)

    monkeypatch.setattr(claude_directed, "api_json", api_json)
    monkeypatch.setattr(claude_directed, "wait_until", lambda fn, **_kwargs: fn())
    _fail_first_result_write(claude_directed, monkeypatch)
    variant = next(iter(claude_directed._CELL_BY_VARIANT))
    result = claude_directed.run_directed_input_scenario(_claude_args(root, binary, variant=variant))
    persisted = _assert_persisted_failure(root, result)
    observation = persisted["observation"]
    assert observation["input_persisted"] is True
    assert observation["input_receipt_linked"] is True
    assert observation["input_visible"] is True
    assert all(persisted["assertions"].values())
    assert len(sessions) == 2 and all(session.submitted == [] for session in sessions)
    assert shipper.stop_calls == 1
    assert json.loads((root / "cleanup-receipt.json").read_text())["status"] == "pass"


def test_claude_launch_late_failure_keeps_real_canary_artifact_and_verdict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "evidence"
    binary = _executable(tmp_path / "claude")

    def canary(options: dict[str, object]) -> dict[str, object]:
        artifact = Path(str(options["artifact"]))
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(json.dumps({"verdict": "green"}), encoding="utf-8")
        return {"verdict": "green", "canaries": {"provider_live": {"status": "pass"}}}

    monkeypatch.setattr(claude_launch, "run_provider_live_canary", canary)
    _fail_first_result_write(claude_launch, monkeypatch)
    result = claude_launch.run_launch_helm_scenario(_claude_args(root, binary, wait_ready_secs=1))
    persisted = _assert_persisted_failure(root, result)
    assert (root / "no-token-canary" / "provider-live-canary.json").is_file()
    assert persisted["artifact_manifest"]


def test_claude_turn_start_late_failure_keeps_semantic_observation_and_false_harness_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "evidence"
    binary = _executable(tmp_path / "claude")

    def execute(_binary: Path, evidence_root: Path):
        evidence_root.mkdir(parents=True, exist_ok=True)
        observation = {
            "no_token_canary": {"verdict": "green"},
            "real_print_canary": {"status": "pass"},
        }
        assertion = SimpleNamespace(assertion_id=claude_turn_start._ASSERTION_ID, outcome=AssertionOutcome.PASS)
        return observation, (assertion,), ()

    monkeypatch.setattr(claude_turn_start.real_print_qual, "_execute", execute)
    _fail_first_result_write(claude_turn_start, monkeypatch)
    result = claude_turn_start.run_turn_start_scenario(_claude_args(root, binary))
    persisted = _assert_persisted_failure(root, result)
    assert json.loads((root / "semantic-observation.json").read_text())["no_token_canary"]["verdict"] == "green"
    assert persisted["status"] == "fail"


def test_claude_turn_boundary_late_persistence_keeps_observation_and_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "evidence"
    binary = _executable(tmp_path / "claude")
    shipper, sessions = _install_claude_session_boundaries(claude_boundary, monkeypatch, root)
    monkeypatch.setattr(claude_boundary, "await_assistant_marker", lambda **_kwargs: ("transcript.jsonl", "reply", "2026-01-01T00:00:00Z"))
    monkeypatch.setattr(claude_boundary, "wait_for_served_quiescent", lambda **_kwargs: (True, 0.01, [{"active": False}]))
    _fail_first_result_write(claude_boundary, monkeypatch)
    result = claude_boundary.run_turn_boundary_scenario(_claude_args(root, binary))
    persisted = _assert_persisted_failure(root, result)
    assert persisted["observation"]["returned_to_quiescent"] is True
    assert persisted["assertions"]["activity_returns_to_quiescent_at_turn_boundary"] is True
    assert sessions and sessions[0].submitted
    assert shipper.stop_calls == 1
    assert json.loads((root / "cleanup-receipt.json").read_text())["status"] == "pass"


def test_claude_native_resume_bounded_provider_failure_is_typed_and_shipper_is_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "evidence"
    binary = _executable(tmp_path / "claude")
    engine = _executable(tmp_path / "engine")
    cli = _executable(tmp_path / "longhouse")
    shipper = _FakeShipper()
    monkeypatch.setenv("LONGHOUSE_RUNTIME_API_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("LONGHOUSE_RUNTIME_AGENTS_TOKEN", "test-token")
    provider_home = tmp_path / "provider-home"
    provider_home.mkdir()
    monkeypatch.setenv("HOME", str(provider_home))
    monkeypatch.setenv("LONGHOUSE_QUALIFICATION_HOME", str(provider_home))
    monkeypatch.setenv("LONGHOUSE_QUALIFICATION_SANDBOX", "provider-qualification-bwrap-v3")
    monkeypatch.setattr(provider_native_resume.live_session_toolkit, "prepare_claude_profile", lambda **_kwargs: {"status": "pass"})
    monkeypatch.setattr(provider_native_resume.live_session_toolkit, "start_transcript_shipper", lambda *_args, **_kwargs: shipper)
    # launch_command is the native provider boundary; fail it after the enrolled shipper exists.
    monkeypatch.setattr(
        provider_native_resume.live_session_toolkit,
        "launch_command",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("provider launch failed")),
    )
    argv = [
        "--variant",
        "clean_exit",
        "--evidence-root",
        str(root),
        "--repo-root",
        str(tmp_path),
        "--engine",
        str(engine),
        "--longhouse-cli",
        str(cli),
        "--provider-bin",
        str(binary),
    ]
    claude_native_resume.main_for("claude", argv)
    persisted = json.loads((root / "result.json").read_text(encoding="utf-8"))
    assert persisted["status"] == "fail"
    assert persisted["failure_code"]
    assert shipper.stop_calls >= 1


def test_codex_coordination_late_persistence_keeps_native_assertion_and_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "evidence"
    binary = _executable(tmp_path / "codex")
    fixture_root = tmp_path / "codex-fixture"
    thread = fixture_root / "thread.jsonl"
    thread.parent.mkdir(parents=True, exist_ok=True)
    thread.write_text(
        json.dumps(
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "toolCall", "name": "peers"}, {"type": "text", "text": "LONGHOUSE_COORD_CREATE"}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    state_file = fixture_root / "state.json"
    state_file.write_text(json.dumps({"thread_path": str(thread), "last_turn_status": "completed"}), encoding="utf-8")

    def start_bridge(*_args: object, **kwargs: object):
        return (
            {"session_id": "session-1", "state_file": str(state_file)},
            SimpleNamespace(returncode=0),
            Path(str(kwargs["isolation_root"])),
        )

    monkeypatch.setattr(codex_coordination.bridge_canary, "_start_bridge", start_bridge)

    def live_send(_args: object, _isolation: Path, _session_id: str, _state: Path, prompt: str, **_kwargs: object):
        marker = prompt.split("exactly ", 1)[1].split(" ", 1)[0]
        thread.write_text(
            json.dumps({"payload": {"type": "function_call", "name": "peers"}})
            + "\n"
            + json.dumps({"payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": marker}]}})
            + "\n",
            encoding="utf-8",
        )
        return {"thread_path": str(thread), "last_turn_status": "completed"}

    monkeypatch.setattr(codex_coordination, "_live_send_and_wait", live_send)
    monkeypatch.setattr(
        codex_coordination.bridge_canary,
        "_stop_bridge",
        lambda *_args, **_kwargs: {"verification": {"verified": True, "socket_absent": True, "owned_processes_dead": True}},
    )
    _fail_first_result_write(codex_coordination, monkeypatch)
    variant = next(key for key, value in codex_coordination._CELL_BY_VARIANT.items() if value[1] == "codex_coordination_awareness_create")
    result = codex_coordination.run_coordination(_codex_args(root, binary, variant=variant))
    persisted = _assert_persisted_failure(root, result)
    assert persisted["observation"]["coordination_instructions_model_visible"] is True
    assert persisted["assertions"]["coordination_instructions_model_visible"] is True
    assert json.loads((root / "cleanup-receipt.json").read_text())["status"] == "pass"


def test_codex_launch_bounded_auth_failure_is_typed_and_proxy_is_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "evidence"
    binary = _executable(tmp_path / "codex")
    monkeypatch.setenv("CODEX_API_KEY", "test-api-key")
    monkeypatch.setattr(codex_launch, "login_with_api_key", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("auth failed")))
    result = codex_launch.run_scenario(
        _codex_args(root, binary, variant=codex_launch._EXECUTION_VARIANT, wait_ready_secs=1, cleanup_timeout_secs=1)
    )
    persisted = _assert_persisted_failure(root, result)
    assert persisted["failure_code"] == "codex_helm_launch_visibility_failed"


def test_codex_native_resume_bounded_bridge_failure_is_typed_and_shipper_is_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "evidence"
    binary = _executable(tmp_path / "codex")
    shipper = _FakeShipper()
    monkeypatch.setattr(codex_native_resume, "start_transcript_shipper", lambda *_args, **_kwargs: shipper)
    monkeypatch.setattr(
        codex_native_resume.bridge_canary, "_start_bridge", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("bridge failed"))
    )
    result = codex_native_resume.run_native_resume(_codex_args(root, binary, variant="clean_exit"))
    persisted = _assert_persisted_failure(root, result)
    assert persisted["failure_code"]
    assert shipper.stop_calls >= 1


def test_codex_turn_boundary_bounded_bridge_failure_is_typed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "evidence"
    binary = _executable(tmp_path / "codex")
    monkeypatch.setattr(
        codex_boundary.bridge_canary, "_start_bridge", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("bridge failed"))
    )
    result = codex_boundary.run_turn_boundary_quiescent(_codex_args(root, binary, variant=codex_boundary._EXECUTION_VARIANT))
    persisted = _assert_persisted_failure(root, result)
    assert persisted["failure_code"]
