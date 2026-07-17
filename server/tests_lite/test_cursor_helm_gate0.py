from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from zerg.qa.cursor_helm_gate0 import _cursor_store_agent_id
from zerg.qa.cursor_helm_gate0 import _decode_cursor_meta_value
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


def test_hook_event_reader_ignores_partial_or_invalid_lines(tmp_path: Path) -> None:
    path = tmp_path / "events.ndjson"
    path.write_text('{"event":"sessionStart"}\nnot-json\n{"partial":', encoding="utf-8")

    assert read_hook_events(path) == [{"event": "sessionStart"}]
