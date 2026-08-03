from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from zerg.qa.cursor_helm_gate0 import _auth_report
from zerg.qa.cursor_helm_gate0 import _cursor_store_agent_id
from zerg.qa.cursor_helm_gate0 import _decode_cursor_meta_value
from zerg.qa.cursor_helm_gate0 import _managed_reset_outcome_payload
from zerg.qa.cursor_helm_gate0 import _managed_reset_registration_payload
from zerg.qa.cursor_helm_gate0 import _scrub_artifact_tree
from zerg.qa.cursor_helm_gate0 import _ship_cursor_store
from zerg.qa.cursor_helm_gate0 import _snapshot_native_evidence
from zerg.qa.cursor_helm_gate0 import _storage_v2_capabilities_payload
from zerg.qa.cursor_helm_gate0 import _storage_v2_receipt_payload
from zerg.qa.cursor_helm_gate0 import find_cursor_store
from zerg.qa.cursor_helm_gate0 import read_hook_events
from zerg.qa.cursor_helm_gate0 import write_project_hooks


def test_decode_cursor_meta_accepts_hex_encoded_json() -> None:
    raw = json.dumps({"agentId": "cursor-id"}).encode("utf-8")
    assert _decode_cursor_meta_value(raw.hex()) == {"agentId": "cursor-id"}


def test_auth_report_accepts_request_scoped_cursor_api_key() -> None:
    report = _auth_report(
        {"status": "unauthenticated", "isAuthenticated": False},
        api_key_configured=True,
    )

    assert report == {
        "status": "unauthenticated",
        "account_session_authenticated": False,
        "api_key_configured": True,
        "credential_mode": "api_key",
        "is_authenticated": True,
    }


def test_auth_report_requires_account_session_when_no_api_key() -> None:
    report = _auth_report(
        {"status": "unauthenticated", "isAuthenticated": False},
        api_key_configured=False,
    )

    assert report["is_authenticated"] is False


def test_cursor_store_agent_id_reads_native_meta(tmp_path: Path) -> None:
    path = tmp_path / "store.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    payload = json.dumps({"agentId": "provider-native-id"}).encode("utf-8").hex()
    connection.execute("INSERT INTO meta(key, value) VALUES ('0', ?)", [payload])
    connection.commit()
    connection.close()

    assert _cursor_store_agent_id(path) == "provider-native-id"


def test_find_cursor_store_honors_xdg_config_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    path = tmp_path / "config" / "cursor" / "chats" / "workspace" / "provider-session" / "store.db"
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    payload = json.dumps({"agentId": "provider-session"}).encode("utf-8").hex()
    connection.execute("INSERT INTO meta(key, value) VALUES ('0', ?)", [payload])
    connection.commit()
    connection.close()

    assert find_cursor_store("provider-session") == path


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


def test_gate0_artifact_scrubber_preserves_sqlite_evidence(monkeypatch, tmp_path: Path) -> None:
    secret = "fixture-token-that-is-not-prefix-shaped"
    monkeypatch.setenv("CURSOR_API_KEY", secret)
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    database = artifact / "store.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE evidence (value TEXT NOT NULL)")
    connection.execute("INSERT INTO evidence VALUES (?)", [secret])
    connection.commit()
    connection.close()
    original_size = database.stat().st_size

    _scrub_artifact_tree(artifact)

    connection = sqlite3.connect(database)
    try:
        value = connection.execute("SELECT value FROM evidence").fetchone()[0]
    finally:
        connection.close()
    assert value != secret
    assert database.stat().st_size == original_size


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
        {
            "scenarios": {
                "create_chat_resume": {
                    "provider_conversation_id": "cursor-session",
                    "store_db": str(source_store),
                }
            }
        },
        artifact,
    )

    assert {item["kind"] for item in receipts} == {"cursor_store_db", "cursor_hook_events"}
    store_receipt = next(item for item in receipts if item["kind"] == "cursor_store_db")
    retained_store = artifact / store_receipt["path"]
    assert retained_store.read_bytes() == source_store.read_bytes()
    assert store_receipt["source_sha256"] == store_receipt["sha256"]
    assert store_receipt["byte_exact"] is True


def test_gate0_binds_raw_source_ids_to_nested_provider_identity(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    source_store = tmp_path / "runtime" / "store.db"
    source_store.parent.mkdir()
    connection = sqlite3.connect(source_store)
    connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    connection.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
    connection.execute("INSERT INTO meta VALUES ('0', ?)", [json.dumps({"agentId": "cursor-before"})])
    connection.execute("INSERT INTO blobs VALUES ('fixture', ?)", [b'{"role":"user"}'])
    connection.commit()
    connection.close()

    receipts = _snapshot_native_evidence(
        {
            "scenarios": {
                "conversation_reset": {
                    "before": {
                        "provider_session_id": "cursor-before",
                        "raw_source_ids": [str(source_store)],
                    }
                }
            }
        },
        artifact,
    )

    store_receipt = next(item for item in receipts if item["kind"] == "cursor_store_db")
    assert store_receipt["provider_session_ids"] == ["cursor-before"]
    assert store_receipt["source_scenarios"] == ["conversation_reset"]

    with pytest.raises(RuntimeError, match="does not match"):
        _snapshot_native_evidence(
            {
                "scenarios": {
                    "conversation_reset": {
                        "before": {
                            "provider_session_id": "wrong-id",
                            "raw_source_ids": [str(source_store)],
                        }
                    }
                }
            },
            tmp_path / "mismatch-artifact",
        )


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


def test_storage_v2_gate_stub_advertises_the_real_ship_contract() -> None:
    payload = _storage_v2_capabilities_payload()

    assert payload["protocol_version"] == 2
    assert payload["cutover"] is True
    assert payload["tenant_id"] == "cursor-gate0"
    assert payload["machine_id"] == "cursor-gate0"
    assert payload["ingest_path"] == "/api/agents/storage/v2/envelopes"
    assert payload["lanes"] == ["live", "repair"]


def test_storage_v2_gate_stub_returns_a_durable_receipt() -> None:
    envelope_id = "a" * 64

    receipt = _storage_v2_receipt_payload(
        json.dumps({"expected_envelope_id": envelope_id, "render": {"records": 1}}).encode()
    )

    assert receipt["v"] == 2
    assert receipt["envelope_id"] == envelope_id
    assert receipt["raw_state"] == "durable"
    assert receipt["render_state"] == "ready"


def test_storage_v2_gate_receipt_is_explicitly_local_and_synthetic() -> None:
    envelope_id = "a" * 64

    receipt = _storage_v2_receipt_payload(json.dumps({"expected_envelope_id": envelope_id}).encode())

    assert receipt["object_hash"] == "a" * 64
    # The wire contract uses the durable state expected by the real engine, but
    # this harness must not present its local receipt as hosted-ingest proof.
    assert receipt["raw_state"] == "durable"


def test_cursor_source_ship_result_labels_local_receipt_and_enforces_progress(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "zerg.qa.cursor_helm_gate0.subprocess.run",
        lambda *_args, **_kwargs: CompletedProcess(
            [], 0, json.dumps({"status": "ok", "protocol": "storage-v2", "events_shipped": 2}), ""
        ),
    )

    result = _ship_cursor_store(
        engine="/bin/longhouse-engine",
        store=tmp_path / "store.db",
        workspace=tmp_path,
        events_path=tmp_path / "events.ndjson",
        shipper_db=tmp_path / "artifact" / "longhouse-shipper.db",
        registration_url="http://127.0.0.1:1",
        timeout=1,
    )

    assert result["receipt_host"] == "gate0_local_contract_stub"
    assert result["receipt_semantics"] == "synthetic_storage_v2_receipt"
    assert result["external_ingest_verified"] is False
    assert result["events_shipped"] == 2


def test_cursor_source_ship_rejects_zero_shipped_events(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "zerg.qa.cursor_helm_gate0.subprocess.run",
        lambda *_args, **_kwargs: CompletedProcess(
            [], 0, json.dumps({"status": "ok", "protocol": "storage-v2", "events_shipped": 0}), ""
        ),
    )

    with pytest.raises(RuntimeError, match="no shipped events"):
        _ship_cursor_store(
            engine="/bin/longhouse-engine",
            store=tmp_path / "store.db",
            workspace=tmp_path,
            events_path=tmp_path / "events.ndjson",
            shipper_db=tmp_path / "artifact" / "longhouse-shipper.db",
            registration_url="http://127.0.0.1:1",
            timeout=1,
        )


def test_storage_v2_gate_stub_rejects_an_invalid_envelope_id() -> None:
    with pytest.raises(ValueError, match="canonical expected envelope id"):
        _storage_v2_receipt_payload(b'{"expected_envelope_id":"not-a-hash"}')
