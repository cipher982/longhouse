"""The shape catalog classifies every transcript line the goldens contain.

A shape nobody has classified is a signal nobody has looked at. The census
tool makes that a failing check instead of a thing the human notices later.
"""

from __future__ import annotations

import inspect
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

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
        "omp": (golden / "omp",),
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


def _codex_rollout(path: Path, *, token_count: bool, version: str = "0.163.0-alpha.5") -> Path:
    """One completed Codex turn as 0.162 and later write it, with or without the `token_count` event."""
    usage = {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 2, "reasoning_output_tokens": 0, "total_tokens": 12}
    ids = {"thread_id": "t", "turn_id": "u", "session_id": "s", "root_turn_id": "u", "response_id": "r"}
    lines = [
        {"type": "session_meta", "payload": {"id": "s", "cli_version": version, "originator": "codex_exec", "cwd": "/tmp"}},
        {"type": "token_usage_record", "payload": {**ids, "usage": usage, "turn_token_usage": usage, "thread_token_usage": usage}},
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"last_token_usage": usage, "total_token_usage": usage, "model_context_window": 100},
            },
        },
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "u", "last_agent_message": "ok", "duration_ms": 5}},
    ]
    if not token_count:
        del lines[2]
    path.write_text("".join(json.dumps({"timestamp": "2026-10-11T00:00:00.000Z", **line}) + "\n" for line in lines))
    return path


def test_a_codex_turn_that_completes_without_token_count_is_reported(tmp_path: Path):
    """The parser makes a turn's usage fact from `token_count`; the per-response usage record does not replace it.

    A provider that stops writing a line leaves nothing to carry a missing key, so the catalog
    requires the event beside the line that closes a turn (`with:event_msg/token_count`).
    """
    written = _codex_rollout(tmp_path / "rollout-2026-10-11T00-00-00-written.jsonl", token_count=True)
    result = _check("codex", written)
    assert result.returncode == 0, result.stdout
    assert "with:event_msg/token_count" in _census("codex", written)["event_msg/task_complete"]["keys"]

    dropped = _codex_rollout(tmp_path / "rollout-2026-10-11T00-00-01-dropped.jsonl", token_count=False)
    assert sorted(_census("codex", dropped)) == ["event_msg/task_complete", "session_meta", "token_usage_record"]
    result = _check("codex", dropped)
    assert result.returncode == 1
    assert "event_msg/task_complete  with:event_msg/token_count absent in 1 of 1 lines" in result.stdout
    assert "unclassified" not in result.stdout

    # It is counted per transcript: one that has the event does not cover one that lacks it.
    result = _check("codex", written, dropped)
    assert "event_msg/task_complete  with:event_msg/token_count absent in 1 of 2 lines" in result.stdout

    shapes = json.loads((REPO_ROOT / "schemas" / "transcript_shapes" / "codex.json").read_text())["shapes"]
    assert shapes["token_usage_record"]["classification"] == "state:token usage record"
    assert shapes["event_msg/token_count"]["classification"] == "signal:turn.usage"


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
            "event_msg/task_complete": ["duration_ms", "turn_id", "with:event_msg/token_count"],
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
        "omp": ("unknown_future_record", "provider_drift:unknown_record"),
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


def _cursor_store(path: Path, *, message_id_bytes: int = 32, meta_encoding: str = "hex") -> Path:
    """A Cursor conversation store: root metadata, a protobuf root and JSON message blobs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    messages = {
        "a" * 64: {"role": "user", "content": [{"type": "text", "text": "private prompt"}], "providerOptions": {}},
        "b" * 64: {"role": "assistant", "id": "m1", "content": [{"type": "text", "text": "private reply"}], "providerOptions": {}},
    }
    root = b"".join(b"\x0a" + bytes([message_id_bytes]) + bytes.fromhex(blob_id)[:message_id_bytes] for blob_id in messages)
    root += b"\x50\x01"  # field 10, a varint the reader skips
    meta = json.dumps({"agentId": "agent-1", "latestRootBlobId": "c" * 64, "createdAt": 1, "name": "private title"})
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT); CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB);"
        )
        connection.execute("INSERT INTO meta VALUES ('0', ?)", (meta.encode().hex() if meta_encoding == "hex" else meta,))
        connection.execute("INSERT INTO blobs VALUES (?, ?)", ("c" * 64, root))
        connection.execute("INSERT INTO blobs VALUES (?, ?)", ("d" * 64, b"\x0a\x20" + b"\x00" * 32))  # a superseded root
        for blob_id, message in messages.items():
            connection.execute("INSERT INTO blobs VALUES (?, ?)", (blob_id, json.dumps(message).encode()))
    connection.close()
    return path


def test_cursor_store_census_names_the_meta_row_the_root_blob_and_each_message(tmp_path: Path):
    store = _cursor_store(tmp_path / "chats" / "workspace" / "agent-1" / "store.db")
    shapes = _census("cursor", tmp_path)

    assert sorted(shapes) == ["store/assistant/text", "store/meta/0", "store/root", "store/user/text"]
    # The metadata row is named by its keys and by how it was written.
    assert shapes["store/meta/0"]["keys"] == ["agentId", "createdAt", "encoding:hex", "latestRootBlobId", "name"]
    # The root is named by its protobuf fields and the length of the ids it lists; a superseded root is not a record.
    assert shapes["store/root"]["count"] == 1
    assert shapes["store/root"]["keys"] == ["field1.len32", "field1.wire2", "field10.wire0"]
    assert shapes["store/assistant/text"]["keys"] == ["content", "content[].text", "content[].type", "id", "providerOptions", "role"]
    assert "private" not in json.dumps(shapes)

    result = _check("cursor", store)
    assert result.returncode == 0, result.stdout + result.stderr


def test_cursor_root_blob_whose_message_ids_change_length_is_drift(tmp_path: Path):
    """The reader takes field 1 as 32-byte blob ids; another length keeps the field and loses the conversation."""
    store = _cursor_store(tmp_path / "store.db", message_id_bytes=16)
    result = _check("cursor", store)
    assert result.returncode == 1
    assert "store/root  field1.len32 absent in 1 of 1 lines" in result.stdout
    assert "store/root  +field1.len16" in result.stdout


def test_cursor_metadata_written_as_plain_json_is_read_and_reported(tmp_path: Path):
    """The server's reader takes plain JSON too, so the census finds the root; the engine takes hex only, so it is drift."""
    store = _cursor_store(tmp_path / "store.db", meta_encoding="json")
    shapes = _census("cursor", store)
    assert shapes["store/root"]["count"] == 1
    assert "encoding:json" in shapes["store/meta/0"]["keys"]

    result = _check("cursor", store)
    assert result.returncode == 1
    assert "store/meta/0  encoding:hex absent in 1 of 1 lines" in result.stdout
    assert "store/meta/0  +encoding:json" in result.stdout


def test_the_census_reads_every_cursor_store_the_servers_reader_decodes(tmp_path: Path):
    """Over the stores the reader's own tests build, in each form of metadata and root id it accepts."""
    from tests_lite.test_cursor_transcript import _basic_messages
    from tests_lite.test_cursor_transcript import _write_store
    from zerg.services.cursor_transcript import decode_store_db
    from zerg.services.cursor_transcript import peek_cursor_session

    def rewrite_meta(store: Path, change) -> None:
        with sqlite3.connect(store) as connection:
            (raw,) = connection.execute("SELECT value FROM meta WHERE key = '0'").fetchone()
            connection.execute("UPDATE meta SET value = ? WHERE key = '0'", (change(json.loads(bytes.fromhex(raw))),))
        connection.close()

    # Each form, and whether the engine's reader takes it too: plain JSON metadata it does not, which is drift.
    forms = {
        "as cursor-agent writes it": (None, 0),
        "plain JSON metadata": (lambda meta: json.dumps(meta), 1),
        "upper-case root id": (lambda meta: json.dumps({**meta, "latestRootBlobId": meta["latestRootBlobId"].upper()}).encode().hex(), 0),
    }
    for form, (change, check_exit) in forms.items():
        store = _write_store(
            tmp_path / form.replace(" ", "-") / "agent",
            agent_id="agent-1",
            created_at_ms=1_760_000_000_000,
            updated_at_ms=1_760_000_060_000,
            messages=_basic_messages(),
        )
        if change is not None:
            rewrite_meta(store, change)

        summary = peek_cursor_session(store)
        decoded = decode_store_db(store)
        assert summary is not None and decoded.session is not None, form
        shapes = _census("cursor", store)

        assert shapes["store/root"]["count"] == 1, form
        assert shapes["store/meta/0"]["count"] == 1, form
        messages = sum(shape["count"] for name, shape in shapes.items() if name not in {"store/root", "store/meta/0"})
        assert messages == len(_basic_messages()), form
        assert "<unparseable>" not in shapes, form
        result = _check("cursor", store)
        assert result.returncode == check_exit, (form, result.stdout)
        assert "unclassified" not in result.stdout, (form, result.stdout)


def test_check_agrees_with_the_servers_reader_on_every_store_its_tests_build(tmp_path: Path):
    """Each of the reader's tests is run here, and every store it leaves behind is checked.

    Where the reader decodes a store and knows every block in it, the check
    is clean. Where the reader meets a block it has no arm for, or cannot
    decode the store at all, the check says so too.
    """
    import tests_lite.test_cursor_transcript as reader_tests
    from zerg.services.cursor_transcript import decode_store_db

    outcomes: dict[str, list[str]] = {"clean": [], "unknown block": [], "not decoded": []}
    for name, test in sorted(vars(reader_tests).items()):
        if not name.startswith("test_") or not callable(test):
            continue
        fixtures = list(inspect.signature(test).parameters)
        assert set(fixtures) <= {"tmp_path", "monkeypatch"}, f"{name} takes a fixture this test does not supply: {fixtures}"
        directory = tmp_path / name
        directory.mkdir()
        with pytest.MonkeyPatch.context() as patch:
            try:
                test(**{fixture: {"tmp_path": directory, "monkeypatch": patch}[fixture] for fixture in fixtures})
            except pytest.skip.Exception:
                pass  # the stores it built before skipping are still checked

        for store in sorted(directory.rglob("store.db")):
            decoded = decode_store_db(store)
            result = _check("cursor", store)
            where = f"{name}: {store.relative_to(directory)}\n{result.stdout}{result.stderr}"
            if decoded.session is None:
                outcomes["not decoded"].append(name)
                assert result.returncode == 1, where
                # A message the root lists that is not JSON, or a root that lists no message.
                assert "<unparseable>" in result.stdout or "store/root  field1.wire2 absent" in result.stdout, where
            elif decoded.diagnostics.unknown_block_types:
                outcomes["unknown block"].append(name)
                assert result.returncode == 1, where
                unclassified = [line for line in result.stdout.splitlines() if line.startswith("  store/")]
                for block in decoded.diagnostics.unknown_block_types:
                    assert any(block in line.split()[0].rsplit("/", 1)[1].split("+") for line in unclassified), where
            else:
                outcomes["clean"].append(name)
                assert result.returncode == 0, where

    # Every kind of outcome was met, so none of the branches above passed on nothing.
    assert len(outcomes["clean"]) >= 10, outcomes
    assert outcomes["unknown block"] == ["test_unknown_block_type_preserved_not_dropped"]
    assert outcomes["not decoded"] == [
        "test_empty_session_reported_as_gap",
        "test_legacy_chunked_format_reported_as_gap",
        "test_peek_cursor_session_legacy_flag",
    ]


@pytest.mark.parametrize("provider", ["cursor", "opencode"])
def test_a_closed_store_in_a_directory_that_cannot_be_written_is_still_read(tmp_path: Path, provider: str):
    """Reading through the WAL needs a new index file beside the store; the census takes the main file alone."""
    if provider == "cursor":
        store = _cursor_store(tmp_path / "cold" / "store.db")
        expected = {"store/assistant/text", "store/meta/0", "store/root", "store/user/text"}
    else:
        store = tmp_path / "cold" / "opencode.db"
        store.parent.mkdir()
        with sqlite3.connect(store) as connection:
            connection.executescript(
                "CREATE TABLE message (id TEXT PRIMARY KEY, data TEXT);"
                "CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, time_created INTEGER, data TEXT);"
            )
            connection.execute("INSERT INTO message VALUES ('m1', ?)", (json.dumps({"role": "user"}),))
            connection.execute("INSERT INTO part VALUES ('p1', 'm1', 1, ?)", (json.dumps({"type": "text", "text": "private"}),))
        connection.close()
        expected = {"part/text/user"}
    connection = sqlite3.connect(store)
    connection.execute("PRAGMA journal_mode=wal")
    connection.close()
    assert sorted(path.name for path in store.parent.iterdir()) == [store.name]

    store.parent.chmod(0o555)
    try:
        if os.access(store.parent, os.W_OK):
            pytest.skip("this user can write the WAL index into a read-only directory")
        shapes = _census(provider, store)
    finally:
        store.parent.chmod(0o755)
    assert set(shapes) == expected


def test_the_census_sqlite_reader_falls_back_when_the_first_read_fails(tmp_path: Path, monkeypatch):
    """`mode=ro` opens a store it cannot read through its log; only the first read says so."""
    store = tmp_path / "store.db"
    with sqlite3.connect(store) as connection:
        connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    connection.close()
    from tests_lite.test_transcript_census_families import _tool

    tool = _tool()
    real_connect = sqlite3.connect
    opened: list[str] = []

    class Unreadable:
        def execute(self, *_args):
            raise sqlite3.OperationalError("attempt to write a readonly database")

        def close(self):
            opened.append("closed")

    def connect(database: str, **kwargs):
        opened.append(database.rsplit("?", 1)[1])
        return Unreadable() if database.endswith("?mode=ro") else real_connect(database, **kwargs)

    monkeypatch.setattr(tool.sqlite3, "connect", connect)
    # The tool has no opener of its own: it is the QA producers', loaded by path.
    assert Path(tool._open_sqlite_readonly.__code__.co_filename) == REPO_ROOT / "server" / "zerg" / "qa" / "provider_sqlite.py"
    connection = tool._open_sqlite_readonly(store)
    try:
        assert opened == ["mode=ro", "closed", "mode=ro&immutable=1"]
        assert connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall() == [("meta",)]
    finally:
        connection.close()


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

    # A line type the parser has no arm for is reported under OMP's own catalog, not Pi's.
    session.write_text(json.dumps({"type": "brand_new_line", "id": "c", "parentId": "b", "timestamp": "2026-10-11T00:00:00.000Z"}) + "\n")
    result = _check("omp", session)
    assert result.returncode == 1 and "brand_new_line" in result.stdout


def test_a_census_orders_prerelease_and_release_versions_of_one_provider(tmp_path: Path):
    """Codex ships `-alpha.N` tags between releases; seeing both must not break the version range."""
    rollouts = []
    for index, version in enumerate(("0.163.0", "0.163.0-alpha.5", "0.151.0")):
        rollout = tmp_path / f"rollout-{index}.jsonl"
        rollout.write_text(json.dumps({"type": "session_meta", "payload": {"id": str(index), "cli_version": version}}) + "\n")
        rollouts.append(rollout)
    shape = _census("codex", *rollouts)["session_meta"]
    assert (shape["first_seen_version"], shape["last_seen_version"]) == ("0.151.0", "0.163.0")
