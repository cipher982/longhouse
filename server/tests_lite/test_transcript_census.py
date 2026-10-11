"""The shape catalog classifies every transcript line the goldens contain.

A shape nobody has classified is a signal nobody has looked at. The census
tool makes that a failing check instead of a thing the human notices later.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL = REPO_ROOT / "scripts" / "qa" / "transcript_census.py"


def _check(provider: str, *paths: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOL), "check", "--provider", provider, *map(str, paths)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_golden_fixtures_contain_only_classified_shapes():
    golden = REPO_ROOT / "engine" / "tests" / "fixtures" / "golden"
    checks = {
        "claude": (golden / "claude",),
        "codex": (golden / "codex",),
        "cursor": (golden / "cursor",),
        "antigravity": (golden / "antigravity", golden / "antigravity_legacy_json" / "basic.json"),
        "opencode": (golden / "opencode",),
        "pi": (golden / "pi",),
    }
    for provider, paths in checks.items():
        result = _check(provider, *paths)
        assert result.returncode == 0, result.stdout + result.stderr


def test_opencode_sqlite_census_reads_message_and_part_shapes(tmp_path: Path):
    database = tmp_path / "opencode.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE message (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE part (
                id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL,
                time_created INTEGER NOT NULL,
                data TEXT NOT NULL
            );
            INSERT INTO message (id, data) VALUES ('message-1', '{"role":"assistant"}');
            INSERT INTO part (id, message_id, time_created, data)
            VALUES ('part-1', 'message-1', 1, '{"type":"future_text","text":"future"}');
            """
        )

    result = _check("opencode", database)
    assert result.returncode == 0, result.stdout + result.stderr


def test_every_implemented_claude_signal_has_a_classified_shape():
    catalog = json.loads((REPO_ROOT / "schemas" / "transcript_shapes" / "claude.json").read_text())
    shapes = catalog["shapes"]
    for raw_source, signal in {
        "system/turn_duration": "turn.duration",
        "system/away_summary": "session.recap",
        "ai-title": "session.title",
        "system/api_error": "turn.api_error",
        "system/compact_boundary": "context.compaction",
    }.items():
        assert shapes[raw_source]["classification"] == f"signal:{signal}", raw_source
        assert shapes[raw_source]["first_seen_version"], f"{raw_source} carries no version history"
    # The catalog is a ledger: turn_duration is an interactive-only signal.
    assert shapes["system/turn_duration"]["entrypoints"] == ["cli"]


def test_every_implemented_codex_signal_has_a_classified_shape():
    catalog = json.loads((REPO_ROOT / "schemas" / "transcript_shapes" / "codex.json").read_text())
    shapes = catalog["shapes"]
    for raw_source, signal in {
        "event_msg/task_complete": "turn.duration",
        "event_msg/turn_aborted": "turn.duration",
        "event_msg/token_count": "turn.usage",
        "event_msg/error": "turn.api_error",
        "event_msg/stream_error": "turn.api_error",
        "compacted": "context.compaction",
    }.items():
        assert shapes[raw_source]["classification"] == f"signal:{signal}", raw_source
        # Protocol-backed shapes may start with no corpus version, but the current
        # checked-in rollout fixture has now observed stream_error in 0.151.0.
        assert shapes[raw_source]["first_seen_version"] or shapes[raw_source].get("evidence", "").startswith("protocol:"), (
            f"{raw_source} carries neither version history nor protocol evidence"
        )
    assert shapes["event_msg/stream_error"]["first_seen_version"] == "0.151.0"
    # Unlike Claude's turn_duration, Codex closes headless turns too.
    assert "codex_exec" in shapes["event_msg/task_complete"]["entrypoints"]
    assert "longhouse_console" in shapes["event_msg/task_complete"]["entrypoints"]


def test_unclassified_shape_fails_the_check(tmp_path: Path):
    novel = tmp_path / "novel.jsonl"
    novel.write_text('{"type":"system","subtype":"brand_new_thing","timestamp":"2026-09-03T00:00:00Z","version":"9.9.9"}\n')
    result = _check("claude", novel)
    assert result.returncode == 1
    assert "system/brand_new_thing" in result.stdout


def test_a_dropped_required_key_is_drift_not_a_clean_census(tmp_path: Path):
    """A provider that renames durationMs keeps the discriminator; only the key set can say the fact will stop."""
    renamed = tmp_path / "renamed.jsonl"
    renamed.write_text(
        '{"type":"system","subtype":"turn_duration","duration":129299,"messageCount":898,'
        '"timestamp":"2026-09-03T14:20:39.100Z","version":"9.9.9","uuid":"u","sessionId":"s","cwd":"/","gitBranch":"main"}\n'
    )
    result = _check("claude", renamed)
    assert result.returncode == 1
    assert "system/turn_duration" in result.stdout
    assert "durationMs absent in 1 of 1 lines" in result.stdout
    assert "+duration" in result.stdout, "the new name is reported as a key the catalog has never seen"


def test_a_new_key_on_a_known_shape_is_reported_until_seeded(tmp_path: Path):
    """A field the provider added is something to classify, not something to ignore."""
    widened = tmp_path / "widened.jsonl"
    widened.write_text(
        '{"timestamp":"2026-09-03T11:25:31.445Z","type":"event_msg","payload":{"type":"task_complete",'
        '"turn_id":"t-1","last_agent_message":"ok","started_at":1,"completed_at":2,"duration_ms":18391,'
        '"time_to_first_token_ms":8662,"brand_new_field":true}}\n'
    )
    result = _check("codex", widened)
    assert result.returncode == 1
    assert "event_msg/task_complete  +brand_new_field" in result.stdout


def test_required_keys_name_what_the_parser_reads():
    for provider, expected in {
        "claude": {"system/turn_duration": ["durationMs"], "ai-title": ["aiTitle"], "system/away_summary": ["content"]},
        "codex": {
            "event_msg/task_complete": ["duration_ms", "turn_id"],
            "event_msg/token_count": [
                "info",
                "info.last_token_usage",
                "info.last_token_usage.total_tokens",
                "info.last_token_usage.output_tokens",
                "info.model_context_window",
            ],
        },
    }.items():
        shapes = json.loads((REPO_ROOT / "schemas" / "transcript_shapes" / f"{provider}.json").read_text())["shapes"]
        for shape, keys in expected.items():
            assert shapes[shape]["required_keys"] == keys, (provider, shape)
            assert set(keys) <= set(shapes[shape]["keys"]), "a required key must be one the provider has actually written"


def test_every_launch_provider_has_boundary_and_drift_catalog_rows():
    expected = {
        "claude": ("user/origin_task_notification+string", "provider_notification:task_notification"),
        "codex": ("response_item/message/user/provider_system", "provider_system:context_injection"),
        "cursor": ("user/provider_context+text", "provider_injection:context"),
        "antigravity": ("UI/USER_INPUT", "transcript:user"),
        "opencode": ("part/future_text/assistant", "provider_drift:unknown_text"),
        "pi": ("message/user/text", "transcript:user"),
    }
    for provider, (shape, classification) in expected.items():
        catalog = json.loads((REPO_ROOT / "schemas" / "transcript_shapes" / f"{provider}.json").read_text())
        assert catalog["shapes"][shape]["classification"] == classification
        assert catalog["shapes"][shape]["required_keys"]


def _census(provider: str, *paths: Path) -> dict:
    result = subprocess.run(
        [sys.executable, str(TOOL), "census", "--json", "--provider", provider, *map(str, paths)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)["shapes"]


def _cursor_store(path: Path, *, message_id_bytes: int = 32) -> Path:
    """A Cursor conversation store: hex-encoded root metadata, a protobuf root and JSON message blobs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    messages = {
        "a" * 64: {"role": "user", "content": [{"type": "text", "text": "private prompt"}], "providerOptions": {}},
        "b" * 64: {"role": "assistant", "id": "m1", "content": [{"type": "text", "text": "private reply"}], "providerOptions": {}},
    }
    root = b"".join(b"\x0a" + bytes([message_id_bytes]) + bytes.fromhex(blob_id)[:message_id_bytes] for blob_id in messages)
    root += b"\x50\x01"  # field 10, a varint the reader skips
    meta = {"agentId": "agent-1", "latestRootBlobId": "root-1", "createdAt": 1, "name": "private title"}
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT); CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB);"
        )
        connection.execute("INSERT INTO meta VALUES ('0', ?)", (json.dumps(meta).encode().hex(),))
        connection.execute("INSERT INTO blobs VALUES ('root-1', ?)", (root,))
        connection.execute("INSERT INTO blobs VALUES ('superseded-root', ?)", (b"\x0a\x20" + b"\x00" * 32,))
        for blob_id, message in messages.items():
            connection.execute("INSERT INTO blobs VALUES (?, ?)", (blob_id, json.dumps(message).encode()))
    connection.close()
    return path


def test_cursor_store_census_names_the_meta_row_the_root_blob_and_each_message(tmp_path: Path):
    store = _cursor_store(tmp_path / "chats" / "workspace" / "agent-1" / "store.db")
    shapes = _census("cursor", tmp_path)

    assert sorted(shapes) == ["store/assistant/text", "store/meta/0", "store/root", "store/user/text"]
    assert shapes["store/meta/0"]["keys"] == ["agentId", "createdAt", "latestRootBlobId", "name"]
    # The root is named by its protobuf fields and the length of the ids it lists; a superseded root is not a record.
    assert shapes["store/root"]["count"] == 1
    assert shapes["store/root"]["keys"] == ["field1.len32", "field1.wire2", "field10.wire0"]
    assert shapes["store/assistant/text"]["keys"] == ["content", "content[].text", "content[].type", "id", "providerOptions", "role"]
    assert "private" not in json.dumps(shapes)

    # Every shape but the root metadata row is classified; that row stays a finding.
    result = _check("cursor", store)
    assert result.returncode == 1
    assert "1 unclassified shape(s)" in result.stdout and "store/meta/0" in result.stdout
    assert "missing a key" not in result.stdout and "never seen" not in result.stdout


def test_cursor_root_blob_whose_message_ids_change_length_is_drift(tmp_path: Path):
    """The reader takes field 1 as 32-byte blob ids; another length keeps the field and loses the conversation."""
    store = _cursor_store(tmp_path / "store.db", message_id_bytes=16)
    result = _check("cursor", store)
    assert result.returncode == 1
    assert "store/root  field1.len32 absent in 1 of 1 lines" in result.stdout
    assert "store/root  +field1.len16" in result.stdout


def test_cursor_projection_lifecycle_line_is_named_by_its_type(tmp_path: Path):
    projection = tmp_path / "agent.jsonl"
    projection.write_text(
        '{"role":"assistant","message":{"content":[{"type":"text","text":"hi"},{"type":"tool_use","name":"Shell","input":{}}]}}\n'
        '{"type":"turn_ended","status":"success"}\n'
    )
    assert sorted(_census("cursor", projection)) == ["assistant/text+tool_use", "event/turn_ended"]
    assert _check("cursor", projection).returncode == 0


def test_omp_sessions_are_shaped_like_pi_and_checked_against_their_own_catalog(tmp_path: Path):
    session = tmp_path / "session.jsonl"
    lines = [
        {"type": "title", "v": 1, "title": "t", "source": "auto", "updatedAt": "2026-10-11T00:00:00.000Z", "pad": ""},
        {"type": "session", "version": 3, "id": "s", "timestamp": "2026-10-11T00:00:00.000Z", "cwd": "/tmp"},
        {"type": "service_tier_change", "id": "a", "parentId": None, "timestamp": "2026-10-11T00:00:00.000Z", "serviceTier": None},
        {
            "type": "message",
            "id": "b",
            "parentId": "a",
            "timestamp": "2026-10-11T00:00:00.000Z",
            "message": {"role": "user", "content": [{"type": "text", "text": "hi"}], "timestamp": 1},
        },
    ]
    session.write_text("".join(json.dumps(line) + "\n" for line in lines))
    assert sorted(_census("omp", session)) == ["message/user/text", "service_tier_change", "session", "title"]
    assert _check("omp", session).returncode == 0, "every line type above is one the OMP catalog classifies"

    # A line type nobody has classified is reported under OMP's own catalog, not Pi's.
    session.write_text(json.dumps({"type": "ttsr_injection", "id": "c", "parentId": "b", "timestamp": "2026-10-11T00:00:00.000Z"}) + "\n")
    result = _check("omp", session)
    assert result.returncode == 1 and "ttsr_injection" in result.stdout
