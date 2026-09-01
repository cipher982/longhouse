from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

import pytest

from zerg.qa import codex_coordination_native as m


def _args(tmp_path: Path) -> argparse.Namespace:
    codex_bin = tmp_path / "codex"
    codex_bin.write_text("#!/bin/sh\n")
    codex_bin.chmod(0o755)
    engine = tmp_path / "longhouse-engine"
    engine.write_text("#!/bin/sh\n")
    engine.chmod(0o755)
    return argparse.Namespace(
        engine=engine,
        codex_bin=codex_bin,
        repo_root=tmp_path,
        api_url="https://runtime.invalid",
        agents_token="test-agents-token",
        model=None,
        bridge_start_timeout_secs=5,
        live_send_timeout_secs=1,
    )


def _fake_run_version(_argv: list[object], **_kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["codex", "--version"], 0, "0.1.0-test\n", "")


def _stopped_cleanup() -> dict[str, object]:
    return {"verification": {"verified": True, "socket_absent": True, "owned_processes_dead": True}}


def test_registration_covers_exactly_the_five_schema_declared_cells() -> None:
    assert m.REGISTRATION.producer_id == "codex.coordination_awareness.v1"
    assert m.REGISTRATION.scenario_ids == (
        "codex_coordination_awareness_create",
        "codex_coordination_awareness_post_compaction",
        "codex_coordination_directed_input",
    )
    assert set(m.REGISTRATION.assertion_cells) == {
        ("coordination_instructions_model_visible", None),
        ("coordination_instructions_model_visible_after_compaction", None),
        ("no_duplicate_visible_bootstrap", None),
        ("provider_input_receipt_linked", None),
        ("attributed_input_visible", None),
    }
    assert m.REGISTRATION.evidence_classes == ("live_token",)
    assert m.REGISTRATION.required_executables == ("jq",)
    assert m.REGISTRATION.producer_revision == 8
    assert m.REGISTRATION.scenario_revision == 5
    assert m.REGISTRATION.observation_scope == "scenario"
    assert "typed_compaction_receipt" not in m.REGISTRATION.required_artifacts
    assert m.REGISTRATION.required_artifacts_by_scenario == {
        "codex_coordination_awareness_post_compaction": ("typed_compaction_receipt",),
        "codex_coordination_directed_input": ("target_send_readiness", "machine_shipper_receipt"),
    }
    assert len(m._CELL_BY_VARIANT) == 5


def test_main_registration_mode_prints_registration_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert m.main(["--registration"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["producer_id"] == "codex.coordination_awareness.v1"
    assert len(payload["assertion_cells"]) == 5


def test_run_awareness_create_pass_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = _args(tmp_path)
    holder: dict[str, Path] = {}

    def fake_start(*_a: object, **kwargs: object) -> tuple[dict[str, object], subprocess.CompletedProcess[str], Path]:
        isolation_root = kwargs["isolation_root"]
        assert isinstance(isolation_root, Path)
        state_file = isolation_root / "state.json"
        thread_path = isolation_root / "rollout.jsonl"
        thread_path.write_text("", encoding="utf-8")
        state_file.write_text(json.dumps({"session_id": "session-1", "thread_path": str(thread_path)}), encoding="utf-8")
        holder["state_file"] = state_file
        holder["thread_path"] = thread_path
        summary = {"session_id": "session-1", "state_file": str(state_file)}
        return summary, subprocess.CompletedProcess(["start"], 0, "{}", ""), isolation_root

    def fake_run(argv: list[object], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        argv_str = [str(item) for item in argv]
        if "--version" in argv_str:
            return _fake_run_version(argv_str)
        if "send" in argv_str:
            prompt = argv_str[argv_str.index("--text") + 1]
            marker = re.search(r"reply with exactly (\S+) and", prompt, re.IGNORECASE).group(1)
            state_file = holder["state_file"]
            thread_path = holder["thread_path"]
            state = json.loads(state_file.read_text(encoding="utf-8"))
            state["last_turn_status"] = "completed"
            state_file.write_text(json.dumps(state), encoding="utf-8")
            reply = marker
            thread_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "payload": {
                                    "type": "mcp_tool_call_end",
                                    "invocation": {"server_name": "longhouse", "tool_name": "peers"},
                                    "arguments": {"repo": "probe", "active_only": False},
                                }
                            }
                        ),
                        json.dumps({"payload": {"type": "agent_message", "message": reply}}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(argv_str, 0, "{}", "")
        return subprocess.CompletedProcess(argv_str, 0, "", "")

    monkeypatch.setattr(m.bridge_canary, "_start_bridge", fake_start)
    monkeypatch.setattr(m.bridge_canary, "_run", fake_run)
    monkeypatch.setattr(m.bridge_canary, "_stop_bridge", lambda *_a, **_k: _stopped_cleanup())

    root = tmp_path / "evidence"
    root.mkdir()
    observation, assertions = m._run_awareness_create(args, root)

    assert assertions == {"coordination_instructions_model_visible": True}
    assert observation["coordination_instructions_model_visible"] is True
    assert observation["coordination_mcp_tool_invoked"] is True
    cleanup = json.loads((root / "cleanup-receipt.json").read_text())
    assert cleanup["required_cleanup"] == {
        "final_bridge_stopped": True,
        "final_socket_absent": True,
        "no_orphan_provider_processes": True,
    }


def test_run_awareness_create_fails_when_the_reply_does_not_show_visibility(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = _args(tmp_path)
    holder: dict[str, Path] = {}

    def fake_start(*_a: object, **kwargs: object) -> tuple[dict[str, object], subprocess.CompletedProcess[str], Path]:
        isolation_root = kwargs["isolation_root"]
        assert isinstance(isolation_root, Path)
        state_file = isolation_root / "state.json"
        thread_path = isolation_root / "rollout.jsonl"
        thread_path.write_text("", encoding="utf-8")
        state_file.write_text(json.dumps({"session_id": "session-1", "thread_path": str(thread_path)}), encoding="utf-8")
        holder["state_file"] = state_file
        holder["thread_path"] = thread_path
        summary = {"session_id": "session-1", "state_file": str(state_file)}
        return summary, subprocess.CompletedProcess(["start"], 0, "{}", ""), isolation_root

    def fake_run(argv: list[object], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        argv_str = [str(item) for item in argv]
        if "--version" in argv_str:
            return _fake_run_version(argv_str)
        if "send" in argv_str:
            state_file = holder["state_file"]
            thread_path = holder["thread_path"]
            state = json.loads(state_file.read_text(encoding="utf-8"))
            state["last_turn_status"] = "completed"
            state_file.write_text(json.dumps(state), encoding="utf-8")
            thread_path.write_text(
                json.dumps({"payload": {"type": "agent_message", "message": "I am not sure which tools are available."}}) + "\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(argv_str, 0, "{}", "")
        return subprocess.CompletedProcess(argv_str, 0, "", "")

    monkeypatch.setattr(m.bridge_canary, "_start_bridge", fake_start)
    monkeypatch.setattr(m.bridge_canary, "_run", fake_run)
    monkeypatch.setattr(m.bridge_canary, "_stop_bridge", lambda *_a, **_k: _stopped_cleanup())

    root = tmp_path / "evidence"
    root.mkdir()
    observation, assertions = m._run_awareness_create(args, root)

    assert assertions == {"coordination_instructions_model_visible": False}
    assert observation["coordination_instructions_model_visible"] is False


class _FakeAppServerSocket:
    def __init__(self, messages: list[dict[str, object]]) -> None:
        self.messages = [json.dumps(message) for message in messages]
        self.sent: list[dict[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    def recv(self, *, timeout: float) -> str:
        assert timeout > 0
        if not self.messages:
            raise TimeoutError
        return self.messages.pop(0)


def test_typed_compaction_requires_the_app_server_completion_item(monkeypatch: pytest.MonkeyPatch) -> None:
    socket = _FakeAppServerSocket(
        [
            {"id": 1, "result": {}},
            {"id": 2, "result": {"thread": {"id": "thread-1"}}},
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-compact",
                    "item": {"id": "item-compact", "type": "contextCompaction"},
                },
            },
            {"id": 3, "result": {}},
        ]
    )
    connection: dict[str, object] = {}

    def connect(*args: object, **kwargs: object):
        connection["args"] = args
        connection["kwargs"] = kwargs
        return socket

    monkeypatch.setattr(m, "websocket_connect", connect)

    receipt = m._typed_compact_thread(
        "ws://127.0.0.1:1234",
        "thread-1",
        ws_auth_token="relay-secret",
        timeout=1,
    )

    assert receipt == {
        "subscription_method": "thread/resume",
        "subscription_completed": True,
        "request_method": "thread/compact/start",
        "request_completed": True,
        "completion_method": "item/completed",
        "context_compaction_completed": True,
        "thread_id": "thread-1",
        "turn_id": "turn-compact",
        "item_id": "item-compact",
        "item_type": "contextCompaction",
    }
    assert [message.get("method") for message in socket.sent] == [
        "initialize",
        "initialized",
        "thread/resume",
        "thread/compact/start",
    ]
    assert connection["kwargs"]["additional_headers"] == {"Authorization": "Bearer relay-secret"}


def test_run_awareness_post_compaction_pass_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = _args(tmp_path)
    holder: dict[str, object] = {"send_count": 0}

    def fake_start(*_a: object, **kwargs: object) -> tuple[dict[str, object], subprocess.CompletedProcess[str], Path]:
        isolation_root = kwargs["isolation_root"]
        assert isinstance(isolation_root, Path)
        state_file = isolation_root / "state.json"
        thread_path = isolation_root / "rollout.jsonl"
        thread_path.write_text("", encoding="utf-8")
        state_file.write_text(
            json.dumps({"thread_id": "thread-1", "thread_path": str(thread_path)}),
            encoding="utf-8",
        )
        holder["thread_path"] = thread_path
        summary = {
            "session_id": "session-1",
            "ws_url": "ws://127.0.0.1:1234",
            "state_file": str(state_file),
        }
        return summary, subprocess.CompletedProcess(["start"], 0, "{}", ""), isolation_root

    def fake_send(*_args: object, **_kwargs: object) -> dict[str, object]:
        holder["send_count"] = int(holder["send_count"]) + 1
        prompt = str(_args[4])
        thread_path = holder["thread_path"]
        assert isinstance(thread_path, Path)
        if holder["send_count"] == 1:
            marker = re.search(r"exactly (\S+) and", prompt, re.IGNORECASE).group(1)
            rows = [{"payload": {"type": "agent_message", "message": marker}}]
        else:
            marker = re.search(r"exactly (\S+) and", prompt, re.IGNORECASE).group(1)
            rows = [
                {"payload": {"type": "function_call", "name": "mcp__longhouse__inbox", "arguments": "{}"}},
                {"payload": {"type": "agent_message", "message": marker}},
            ]
        with thread_path.open("a", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row) + "\n")
        return {"last_turn_status": "completed", "thread_id": "thread-1", "thread_path": str(thread_path)}

    monkeypatch.setattr(m.bridge_canary, "_start_bridge", fake_start)
    monkeypatch.setattr(m.bridge_canary, "_run", _fake_run_version)
    monkeypatch.setattr(m.bridge_canary, "_stop_bridge", lambda *_a, **_k: _stopped_cleanup())
    monkeypatch.setattr(m, "_live_send_and_wait", fake_send)
    monkeypatch.setattr(
        m,
        "_typed_compact_thread",
        lambda *_a, **_k: {
            "request_completed": True,
            "context_compaction_completed": True,
            "item_type": "contextCompaction",
        },
    )
    monkeypatch.setattr(
        m,
        "observe_codex_post_compaction_bootstrap",
        lambda **_kwargs: {
            "coordination_instructions_model_visible_after_compaction": False,
            "visible_bootstrap_count": 1,
            "mcp_coordination_instructions_present": True,
        },
    )
    root = tmp_path / "evidence"
    root.mkdir()
    observation, assertions = m._run_awareness_post_compaction(args, root)

    assert assertions == {
        "coordination_instructions_model_visible_after_compaction": True,
        "no_duplicate_visible_bootstrap": True,
    }
    assert observation["compaction_signal_observed"] is True
    assert observation["post_compact_question_answered"] is True
    assert observation["assistant_evidence_source"] == "native_rollout_mcp_and_assistant_events"
    assert observation["post_compaction_inbox_invoked"] is True
    assert observation["visible_bootstrap_count"] == 1
    assert (root / "typed-compaction-receipt.json").is_file()


def test_run_awareness_post_compaction_rejects_user_prompt_echo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = _args(tmp_path)
    holder: dict[str, object] = {"send_count": 0}

    def fake_start(*_a: object, **kwargs: object) -> tuple[dict[str, object], subprocess.CompletedProcess[str], Path]:
        isolation_root = kwargs["isolation_root"]
        assert isinstance(isolation_root, Path)
        state_file = isolation_root / "state.json"
        thread_path = isolation_root / "rollout.jsonl"
        thread_path.write_text("", encoding="utf-8")
        state_file.write_text(
            json.dumps({"thread_id": "thread-1", "thread_path": str(thread_path)}),
            encoding="utf-8",
        )
        holder["thread_path"] = thread_path
        summary = {
            "session_id": "session-1",
            "ws_url": "ws://127.0.0.1:1234",
            "state_file": str(state_file),
        }
        return summary, subprocess.CompletedProcess(["start"], 0, "{}", ""), isolation_root

    def fake_send(*_args: object, **_kwargs: object) -> dict[str, object]:
        holder["send_count"] = int(holder["send_count"]) + 1
        prompt = str(_args[4])
        thread_path = holder["thread_path"]
        assert isinstance(thread_path, Path)
        if holder["send_count"] == 1:
            marker = re.search(r"exactly (\S+) and", prompt, re.IGNORECASE).group(1)
            rows = [{"payload": {"type": "agent_message", "message": marker}}]
        else:
            rows = [
                {"payload": {"type": "message", "role": "user", "content": prompt}},
                {"payload": {"type": "agent_message", "message": "I do not know."}},
            ]
        with thread_path.open("a", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row) + "\n")
        return {"last_turn_status": "completed", "thread_id": "thread-1", "thread_path": str(thread_path)}

    monkeypatch.setattr(m.bridge_canary, "_start_bridge", fake_start)
    monkeypatch.setattr(m.bridge_canary, "_run", _fake_run_version)
    monkeypatch.setattr(m.bridge_canary, "_stop_bridge", lambda *_a, **_k: _stopped_cleanup())
    monkeypatch.setattr(m, "_live_send_and_wait", fake_send)
    monkeypatch.setattr(
        m,
        "_typed_compact_thread",
        lambda *_a, **_k: {
            "request_completed": True,
            "context_compaction_completed": True,
            "item_type": "contextCompaction",
        },
    )
    monkeypatch.setattr(
        m,
        "observe_codex_post_compaction_bootstrap",
        lambda **_kwargs: {"coordination_instructions_model_visible_after_compaction": False, "visible_bootstrap_count": 0},
    )
    root = tmp_path / "evidence"
    root.mkdir()
    observation, assertions = m._run_awareness_post_compaction(args, root)

    assert observation["compaction_signal_observed"] is True
    assert observation["post_compact_question_answered"] is False
    assert assertions["coordination_instructions_model_visible_after_compaction"] is False


def test_run_directed_input_pass_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = _args(tmp_path)
    store: dict[str, object] = {}

    def fake_start(*_a: object, **kwargs: object) -> tuple[dict[str, object], subprocess.CompletedProcess[str], Path]:
        evidence_root = kwargs["evidence_root"]
        isolation_root = kwargs["isolation_root"]
        assert isinstance(isolation_root, Path)
        session_id = "source-session-1" if "source" in str(evidence_root) else "target-session-1"
        summary = {"session_id": session_id, "state_file": str(isolation_root / "state.json")}
        return summary, subprocess.CompletedProcess(["start"], 0, "{}", ""), isolation_root

    def fake_issue_token(_args: object, session_id: str) -> str:
        return f"token-for-{session_id}"

    def fake_api_call(
        _api_url: str,
        token: str,
        path: str,
        *,
        method: str = "GET",
        json_body: dict[str, object] | None = None,
        extra_headers: dict[str, str] | None = None,
    ):
        expected_session = "source-session-1" if token.endswith("source-session-1") else "target-session-1"
        assert extra_headers == {m._SESSION_HEADER: expected_session}
        if path == "directed-inputs" and method == "POST":
            assert json_body is not None
            store.clear()
            store.update(
                {
                    "id": 1,
                    "source_session_id": "source-session-1",
                    "target_session_id": json_body["target_session_id"],
                    "text": json_body["text"],
                    "input_receipt": 42,
                }
            )
            return dict(store)
        if path.startswith("directed-inputs?direction="):
            return {"directed_inputs": [dict(store)] if store else []}
        raise AssertionError(f"unexpected call: {method} {path} token={token}")

    monkeypatch.setattr(m.bridge_canary, "_start_bridge", fake_start)
    monkeypatch.setattr(m.bridge_canary, "_run", _fake_run_version)
    monkeypatch.setattr(m.bridge_canary, "_stop_bridge", lambda *_a, **_k: _stopped_cleanup())
    monkeypatch.setattr(
        m,
        "start_transcript_shipper",
        lambda *_a, **_k: type("FakeShipper", (), {"stop": lambda self: {"status": "pass"}})(),
    )
    monkeypatch.setattr(m, "_issue_coordination_token", fake_issue_token)
    monkeypatch.setattr(m, "_api_call", fake_api_call)

    root = tmp_path / "evidence"
    root.mkdir()
    observation, assertions = m._run_directed_input(args, root)

    assert assertions == {
        "directed_input_persisted": True,
        "provider_input_receipt_linked": True,
        "attributed_input_visible": True,
    }
    assert observation["source_session_id"] == "source-session-1"
    cleanup = json.loads((root / "cleanup-receipt.json").read_text())
    assert cleanup["status"] == "pass"
    assert set(cleanup["sessions"]) == {"source", "target"}
    readiness = json.loads((root / "target-send-readiness.json").read_text())
    assert readiness["delivery_receipt_observed"] is True


def test_run_directed_input_fails_closed_when_receipt_never_links(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = _args(tmp_path)
    args.live_send_timeout_secs = 1
    store: dict[str, object] = {}

    def fake_start(*_a: object, **kwargs: object) -> tuple[dict[str, object], subprocess.CompletedProcess[str], Path]:
        evidence_root = kwargs["evidence_root"]
        isolation_root = kwargs["isolation_root"]
        assert isinstance(isolation_root, Path)
        session_id = "source-session-1" if "source" in str(evidence_root) else "target-session-1"
        summary = {"session_id": session_id, "state_file": str(isolation_root / "state.json")}
        return summary, subprocess.CompletedProcess(["start"], 0, "{}", ""), isolation_root

    def fake_api_call(
        _api_url: str,
        token: str,
        path: str,
        *,
        method: str = "GET",
        json_body: dict[str, object] | None = None,
        extra_headers: dict[str, str] | None = None,
    ):
        expected_session = "source-session-1" if token.endswith("source-session-1") else "target-session-1"
        assert extra_headers == {m._SESSION_HEADER: expected_session}
        if path == "directed-inputs" and method == "POST":
            assert json_body is not None
            store.clear()
            store.update(
                {
                    "id": 1,
                    "source_session_id": "source-session-1",
                    "target_session_id": json_body["target_session_id"],
                    "text": json_body["text"],
                    "input_receipt": None,  # delivery never succeeds
                }
            )
            return dict(store)
        if path.startswith("directed-inputs?direction="):
            return {"directed_inputs": [dict(store)] if store else []}
        raise AssertionError(f"unexpected call: {method} {path} token={token}")

    monkeypatch.setattr(m.bridge_canary, "_start_bridge", fake_start)
    monkeypatch.setattr(m.bridge_canary, "_run", _fake_run_version)
    monkeypatch.setattr(m.bridge_canary, "_stop_bridge", lambda *_a, **_k: _stopped_cleanup())
    monkeypatch.setattr(
        m,
        "start_transcript_shipper",
        lambda *_a, **_k: type("FakeShipper", (), {"stop": lambda self: {"status": "pass"}})(),
    )
    monkeypatch.setattr(m, "_issue_coordination_token", lambda _args, session_id: f"token-for-{session_id}")
    monkeypatch.setattr(m, "_api_call", fake_api_call)

    root = tmp_path / "evidence"
    root.mkdir()
    observation, assertions = m._run_directed_input(args, root)

    assert observation["input_receipt_linked"] is False
    assert assertions["provider_input_receipt_linked"] is False
    # Visibility is independent of receipt linkage and should still be true.
    assert assertions["attributed_input_visible"] is True


def test_run_coordination_dispatches_by_variant_and_uses_pass_not_passed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end through run_coordination(): exercises the exact result.json
    shape the real validator checks (status == "pass", per-assertion boolean
    in "assertions"), not just the internal _run_* helpers."""

    args = _args(tmp_path)
    args.evidence_root = tmp_path / "evidence"
    create_variant = next(variant for variant, cell in m._CELL_BY_VARIANT.items() if cell[0] == "coordination_instructions_model_visible")
    args.variant = create_variant

    monkeypatch.setattr(
        m,
        "_run_awareness_create",
        lambda _args, _root: ({"coordination_instructions_model_visible": True}, {"coordination_instructions_model_visible": True}),
    )
    monkeypatch.setattr(m.bridge_canary, "_run", _fake_run_version)

    result = m.run_coordination(args)

    assert result["status"] == "pass"
    assert result["observation_scope"] == "scenario"
    assert result["assertions"] == {"coordination_instructions_model_visible": True}
    assert result["scenario_id"] == "codex_coordination_awareness_create"
    assert result["variant"] is None
    on_disk = json.loads((args.evidence_root / "result.json").read_text(encoding="utf-8"))
    assert on_disk == result


def test_run_coordination_mixed_scenario_status_matches_complete_assertion_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = _args(tmp_path)
    args.evidence_root = tmp_path / "evidence-mixed"
    args.variant = next(variant for variant, cell in m._CELL_BY_VARIANT.items() if cell[0] == "no_duplicate_visible_bootstrap")
    assertions = {
        "coordination_instructions_model_visible_after_compaction": False,
        "no_duplicate_visible_bootstrap": True,
    }
    monkeypatch.setattr(m, "_run_awareness_post_compaction", lambda _args, _root: ({}, assertions))
    monkeypatch.setattr(m.bridge_canary, "_run", _fake_run_version)

    result = m.run_coordination(args)

    assert result["status"] == "fail"
    assert result["assertions"] == assertions
