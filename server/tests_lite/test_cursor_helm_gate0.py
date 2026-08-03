from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from zerg.qa.cursor_helm_gate0 import _cursor_store_agent_id
from zerg.qa.cursor_helm_gate0 import _decode_cursor_meta_value
from zerg.qa.cursor_helm_gate0 import _managed_reset_outcome_payload
from zerg.qa.cursor_helm_gate0 import _managed_reset_registration_payload
from zerg.qa.cursor_helm_gate0 import _scrub_artifact_tree
from zerg.qa.cursor_helm_gate0 import _snapshot_native_evidence
from zerg.qa.cursor_helm_gate0 import read_hook_events
from zerg.qa.cursor_helm_gate0 import write_project_hooks


def test_decode_cursor_meta_accepts_hex_encoded_json() -> None:
    raw = json.dumps({"agentId": "cursor-id"}).encode("utf-8")
    assert _decode_cursor_meta_value(raw.hex()) == {"agentId": "cursor-id"}


def test_cursor_store_agent_id_reads_native_meta(tmp_path: Path) -> None:
    path = tmp_path / "store.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    payload = json.dumps({"agentId": "provider-native-id"}).encode("utf-8").hex()
    connection.execute("INSERT INTO meta(key, value) VALUES ('0', ?)", [payload])
    connection.commit()
    connection.close()

    assert _cursor_store_agent_id(path) == "provider-native-id"


def test_project_hooks_cover_identity_transcript_and_control_events(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    events = tmp_path / "events.ndjson"

    script = write_project_hooks(workspace, events)
    config = json.loads((workspace / ".cursor" / "hooks.json").read_text())

    assert script.is_file()
    assert script.stat().st_mode & 0o111
    assert set(config["hooks"]) >= {
        "sessionStart",
        "beforeSubmitPrompt",
        "afterAgentThought",
        "afterAgentResponse",
        "preToolUse",
        "beforeShellExecution",
        "stop",
    }


def test_gate0_artifact_scrubber_removes_exact_and_structured_provider_keys(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CURSOR_API_KEY", "fixture-token-that-is-not-prefix-shaped")
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    payload = artifact / "terminal.raw"
    payload.write_bytes(b"fixture-token-that-is-not-prefix-shaped crsr_secret123")

    _scrub_artifact_tree(artifact)

    retained = payload.read_bytes()
    assert b"fixture-token-that-is-not-prefix-shaped" not in retained
    assert b"crsr_secret123" not in retained


def test_gate0_snapshots_native_cursor_store_and_hook_evidence(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    events = artifact / "events.ndjson"
    events.write_text('{"event":"sessionStart"}\n', encoding="utf-8")
    source_store = tmp_path / "runtime" / "store.db"
    source_store.parent.mkdir()
    connection = sqlite3.connect(source_store)
    connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    connection.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
    connection.execute("INSERT INTO meta VALUES ('0', ?)", [json.dumps({"agentId": "cursor-session"})])
    connection.execute("INSERT INTO blobs VALUES ('fixture', ?)", [b'{"role":"user"}'])
    connection.commit()
    connection.close()

    receipts = _snapshot_native_evidence(
        {"scenarios": {"create_chat_resume": {"store_db": str(source_store)}}},
        artifact,
    )

    assert {item["kind"] for item in receipts} == {"cursor_store_db", "cursor_hook_events"}
    store_receipt = next(item for item in receipts if item["kind"] == "cursor_store_db")
    retained_store = artifact / store_receipt["path"]
    assert retained_store.read_bytes() == source_store.read_bytes()
    assert store_receipt["byte_exact"] is True


def test_hook_event_reader_ignores_partial_or_invalid_lines(tmp_path: Path) -> None:
    path = tmp_path / "events.ndjson"
    path.write_text('{"event":"sessionStart"}\nnot-json\n{"partial":', encoding="utf-8")

    assert read_hook_events(path) == [{"event": "sessionStart"}]


def test_reset_registration_stub_matches_cursor_helm_contract() -> None:
    payload = _managed_reset_registration_payload({"session_id": "session-id"}, "run-id")

    assert payload["session_id"] == "session-id"
    assert payload["run_id"] == "run-id"
    assert payload["managed_transport"] == "cursor_helm"
    assert payload["coordination_token"] == "test-coordination-authority"


def test_reset_registration_stub_acknowledges_launch_outcome() -> None:
    assert _managed_reset_outcome_payload() == {"recorded": True}
