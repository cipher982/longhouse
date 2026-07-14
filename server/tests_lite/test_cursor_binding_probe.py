from __future__ import annotations

import json
import sqlite3

from zerg.services import cursor_binding_probe


def _store(path, agent_id: str) -> None:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
    con.execute("INSERT INTO meta VALUES ('0', ?)", (json.dumps({"agentId": agent_id}),))
    con.commit()
    con.close()


def _live_helm_state(tmp_path) -> None:
    state_dir = tmp_path / "longhouse" / "managed-local" / "cursor-helm"
    state_dir.mkdir(parents=True)
    (state_dir / "launch-token.json").write_text(json.dumps({"launcher_pid": 1, "cursor_pid": 2}))


def test_probe_fails_closed_when_cursor_generated_agent_id_differs(monkeypatch, tmp_path):
    monkeypatch.setenv("LONGHOUSE_HOME", str(tmp_path / "longhouse"))
    store = tmp_path / "store.db"
    _store(store, "cursor-generated")
    for phase in ("before_launch", "after_prompt", "after_tool_turn", "at_exit"):
        result = cursor_binding_probe.record_probe_observation(
            "launch-token", phase, None if phase == "before_launch" else store
        )
    assert result["status"] == "failed"
    assert "deterministic" in result["failure_reason"]


def test_probe_emits_expiring_claim_only_for_exact_provider_native_evidence(monkeypatch, tmp_path):
    monkeypatch.setenv("LONGHOUSE_HOME", str(tmp_path / "longhouse"))
    store = tmp_path / "store.db"
    _store(store, "launch-token")
    _live_helm_state(tmp_path)
    for phase in ("before_launch", "after_prompt", "after_tool_turn", "at_exit"):
        result = cursor_binding_probe.record_probe_observation(
            "launch-token", phase, None if phase == "before_launch" else store
        )
    assert result["status"] == "passed"
    assert result["conversation_uuid"] == "launch-token"
    assert result["expires_at"]
