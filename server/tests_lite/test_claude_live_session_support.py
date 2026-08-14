from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from zerg.qa import claude_live_session_support as m
from zerg.qa import managed_claude_live


def test_managed_claude_helpers_respect_an_explicit_isolated_home(tmp_path: Path) -> None:
    home = tmp_path / "isolated-home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_id = "11111111-1111-4111-8111-111111111111"
    provider_session_id = "22222222-2222-4222-8222-222222222222"
    state_path = home / ".claude" / "channels" / "longhouse" / "sessions" / f"{session_id}.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "ready": True,
                "session_id": session_id,
                "provider_session_id": provider_session_id,
                "cwd": str(workspace.resolve()),
            }
        ),
        encoding="utf-8",
    )
    transcript = home / ".claude" / "projects" / "probe" / f"{provider_session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n", encoding="utf-8")

    assert managed_claude_live.wait_for_channel_ready(session_id, timeout_secs=0.1, home=home) is True
    assert managed_claude_live.find_channel_session_id(workspace, home=home) == session_id
    assert managed_claude_live.read_provider_session_id(session_id, home=home) == provider_session_id
    assert managed_claude_live.transcript_paths(provider_session_id, home=home) == [transcript]


def test_channel_send_passes_the_isolated_environment_to_the_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(managed_claude_live.subprocess, "run", fake_run)
    environment = {"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin"}

    result = managed_claude_live.channel_send(
        "11111111-1111-4111-8111-111111111111",
        "hello",
        repo_root=tmp_path,
        env=environment,
    )

    assert result.returncode == 0
    assert captured["kwargs"]["env"] == environment  # type: ignore[index]


def test_claude_launch_environment_aliases_a_generic_staged_binary(tmp_path: Path) -> None:
    staged = tmp_path / "release" / "provider"
    staged.parent.mkdir()
    staged.write_text("binary", encoding="utf-8")

    environment = m.claude_launch_environment(
        {"HOME": str(tmp_path / "home")},
        claude_bin=staged,
        engine=tmp_path / "longhouse-engine",
        model="claude-test",
        longhouse_home=tmp_path / "longhouse",
    )

    alias = Path(environment["LONGHOUSE_CLAUDE_BIN"])
    assert alias.name == "claude"
    assert alias.is_symlink()
    assert alias.resolve() == staged.resolve()


def test_local_managed_control_fact_returns_only_safe_attached_send_evidence(tmp_path: Path) -> None:
    longhouse_home = tmp_path / "longhouse"
    status_path = longhouse_home / "agent" / "engine-status.json"
    status_path.parent.mkdir(parents=True)
    session_id = "11111111-1111-4111-8111-111111111111"
    status_path.write_text(
        json.dumps(
            {
                "machine_evidence": {
                    "control": [
                        {
                            "provider": "claude",
                            "session_id": session_id,
                            "state": "attached",
                            "granted_operations": ["send_input", "interrupt"],
                            "connection_id": "connection-1",
                            "auth_token": "must-not-leak",
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    fact = m.local_managed_control_fact(longhouse_home, session_id)

    assert fact == {
        "provider": "claude",
        "session_id": session_id,
        "connection_id": "connection-1",
        "state": "attached",
        "granted_operations": ["send_input", "interrupt"],
    }


def test_local_managed_control_fact_rejects_non_send_control(tmp_path: Path) -> None:
    longhouse_home = tmp_path / "longhouse"
    status_path = longhouse_home / "agent" / "engine-status.json"
    status_path.parent.mkdir(parents=True)
    status_path.write_text(
        json.dumps(
            {
                "machine_evidence": {
                    "control": [
                        {
                            "session_id": "session-1",
                            "state": "degraded",
                            "granted_operations": [],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    assert m.local_managed_control_fact(longhouse_home, "session-1") is None


def test_find_compaction_boundary_requires_a_new_structured_transcript_row(tmp_path: Path) -> None:
    home = tmp_path / "home"
    session_id = "22222222-2222-4222-8222-222222222222"
    transcript = home / ".claude" / "projects" / "probe" / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        "\n".join(
            [
                json.dumps({"type": "assistant", "message": {"content": []}}),
                json.dumps({"type": "system", "subtype": "compact_boundary"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert m.find_compaction_boundary(session_id, after_line_counts={str(transcript): 2}, home=home) is None
    assert m.find_compaction_boundary(session_id, after_line_counts={str(transcript): 1}, home=home) == {
        "transcript_path": str(transcript),
        "line": 2,
        "type": "system",
        "subtype": "compact_boundary",
    }


def test_wait_for_served_quiescent_reads_the_canonical_session_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "11111111-1111-4111-8111-111111111111"
    payloads = iter(
        [
            {"served_path": "canonical_session_detail", "shadow": {"activity": {"state": "thinking"}}},
            {"served_path": "canonical_session_detail", "shadow": {"activity": {"state": "quiescent"}}},
        ]
    )
    requested_paths: list[str] = []

    def fake_api(_url: str, _token: str, path: str) -> dict[str, object]:
        requested_paths.append(path)
        return next(payloads)

    def fake_wait(predicate: object, **_kwargs: object) -> object:
        assert callable(predicate)
        assert predicate() is None
        return predicate()

    monkeypatch.setattr(m, "api_json_tolerant", fake_api)
    monkeypatch.setattr(m, "wait_until", fake_wait)

    settled, _elapsed, samples = m.wait_for_served_quiescent(
        api_url="https://runtime.invalid",
        token="test-token",
        session_id=session_id,
        timeout=5,
    )

    assert settled is True
    assert samples == ["thinking", "quiescent"]
    assert requested_paths == [
        f"sessions/{session_id}/state-diagnostics",
        f"sessions/{session_id}/state-diagnostics",
    ]
