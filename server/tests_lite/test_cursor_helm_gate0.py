from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest

from zerg.qa import cursor_helm_gate0 as gate
from zerg.qa.cursor_helm_gate0 import _auth_report
from zerg.qa.cursor_helm_gate0 import _cursor_store_agent_id
from zerg.qa.cursor_helm_gate0 import _cursor_terminal_text
from zerg.qa.cursor_helm_gate0 import _cursor_tui_input_ready
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
from zerg.qa.pty_session import ProviderPtySession


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


def test_cursor_prompt_readiness_blocks_reconnect_after_prompt() -> None:
    assert not _cursor_tui_input_ready("Add a follow-up\nReconnecting (attempt 3, 9s)")
    assert not _cursor_tui_input_ready("Add a follow-up\nWorking")
    assert _cursor_tui_input_ready("Reconnecting (attempt 3, 9s)\nAdd a follow-up")


def test_cursor_prompt_readiness_requires_post_interrupt_bytes(tmp_path: Path) -> None:
    terminal = tmp_path / "native-resume.terminal.raw"
    terminal.write_text("Add a follow-up\n", encoding="utf-8")
    offset = terminal.stat().st_size

    assert _cursor_terminal_text(terminal, offset=offset) == ""

    terminal.write_text("Add a follow-up\nReconnecting\n", encoding="utf-8")
    assert not _cursor_tui_input_ready(_cursor_terminal_text(terminal, offset=offset))

    terminal.write_text("Add a follow-up\nReconnecting\nAdd a follow-up\n", encoding="utf-8")
    assert _cursor_tui_input_ready(_cursor_terminal_text(terminal, offset=offset))


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


def test_gate0_artifact_scrubber_checkpoints_sqlite_wal_before_redaction(monkeypatch, tmp_path: Path) -> None:
    secret = "fixture-token-that-is-not-prefix-shaped"
    monkeypatch.setenv("CURSOR_API_KEY", secret)
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    database = artifact / "store.db"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=100000")
    connection.execute("CREATE TABLE evidence (value TEXT NOT NULL)")
    connection.execute("INSERT INTO evidence VALUES (?)", [secret])
    connection.commit()

    _scrub_artifact_tree(artifact)
    connection.close()

    with sqlite3.connect(database) as retained:
        value = retained.execute("SELECT value FROM evidence").fetchone()[0]
    assert value != secret
    assert not Path(f"{database}-wal").exists() or Path(f"{database}-wal").stat().st_size == 0


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

    receipt = _storage_v2_receipt_payload(json.dumps({"expected_envelope_id": envelope_id, "render": {"records": 1}}).encode())

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
        lambda *_args, **_kwargs: CompletedProcess([], 0, json.dumps({"status": "ok", "protocol": "storage-v2", "events_shipped": 2}), ""),
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
        lambda *_args, **_kwargs: CompletedProcess([], 0, json.dumps({"status": "ok", "protocol": "storage-v2", "events_shipped": 0}), ""),
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


class _StubPtySession:
    """A Cursor PTY session that records its argv and starts nothing."""

    def __init__(self, argv: list[str], alive: bool = True) -> None:
        self.argv = argv
        self._alive = alive

    @classmethod
    def capture(cls, recorded: list[list[str]], *, alive: bool = True):
        def start(*, argv, cwd, env, terminal_path):  # noqa: ANN001 - test double
            recorded.append(list(argv))
            return cls(list(argv), alive=alive)

        return start

    def alive(self) -> bool:
        return self._alive

    def close(self) -> None:
        return None


def _run_permission_scenario(
    monkeypatch,
    tmp_path: Path,
    decision: str,
    *,
    hook_events: list[dict] | None = None,
    alive: bool = True,
) -> tuple[list[str], dict]:
    from zerg.qa import cursor_helm_gate0 as gate

    recorded: list[list[str]] = []
    monkeypatch.setattr(gate.CursorPtySession, "start", _StubPtySession.capture(recorded, alive=alive))
    # The scenario counts existing events before the run and reads again after,
    # so the stub must start empty and only then report what the run produced.
    reads = {"count": 0}

    def _read_hook_events(*_args, **_kwargs):
        reads["count"] += 1
        return [] if reads["count"] == 1 else list(hook_events or [])

    monkeypatch.setattr(gate, "read_hook_events", _read_hook_events)
    monkeypatch.setattr(gate, "wait_for_hook", lambda *_args, **_kwargs: {"generation_id": "gen-1"})
    workspace = tmp_path / decision
    workspace.mkdir()
    if decision == "allow":
        # permission=allow must let the command through, so the side effect is present.
        (workspace / f"permission-{decision}.txt").write_text("ALLOWED", encoding="utf-8")
    result = gate._permission_scenario(
        decision=decision,
        binary="/fixture/cursor-agent",
        workspace=workspace,
        events_path=tmp_path / "events.jsonl",
        terminal_path=tmp_path / f"{decision}.raw",
        provider_id="conversation-1",
        timeout=1.0,
        model=None,
    )
    return recorded[0], result


@pytest.mark.parametrize("decision", ["allow", "deny"])
def test_permission_allow_and_deny_force_past_provider_approval(monkeypatch, tmp_path: Path, decision: str) -> None:
    """The hook decision must be the only gate, not Cursor's own prompt."""

    argv, result = _run_permission_scenario(monkeypatch, tmp_path, decision)

    assert "--force" in argv
    assert result["provider_auto_approval"] == "force"
    assert result["status"] == "passed"


def test_permission_ask_does_not_force_its_own_answer(monkeypatch, tmp_path: Path) -> None:
    """--force answers the very question `ask` exists to pose.

    Cursor 2026.09.10 executed the command under `permission=ask` because
    --force auto-approved it, which failed Gate 0 on a promise the provider
    never made. Unforced, an unattended ask executes nothing.
    """

    argv, result = _run_permission_scenario(monkeypatch, tmp_path, "ask")

    assert "--force" not in argv
    assert result["provider_auto_approval"] == "prompt"
    assert result["side_effect_present"] is False
    assert result["status"] == "passed"


def test_permission_ask_needs_more_than_an_absent_side_effect(monkeypatch, tmp_path: Path) -> None:
    """A blocked command, a dead session, and a shell that ran elsewhere all
    leave the same empty marker path.

    `ask` runs unforced, so an absent side effect is not by itself evidence
    that the decision was honoured. Both other readings must fail.
    """

    from uuid import UUID

    from zerg.qa import cursor_helm_gate0 as gate

    fixed = UUID("00000000-0000-4000-8000-000000000001")
    monkeypatch.setattr(gate, "uuid4", lambda: fixed)

    dead_root = tmp_path / "dead"
    dead_root.mkdir()
    with pytest.raises(RuntimeError, match="died"):
        _run_permission_scenario(monkeypatch, dead_root, "ask", alive=False)

    executed = [
        {
            "longhouse_session_id": str(fixed),
            "event": "afterShellExecution",
            "conversation_id": "conversation-1",
        }
    ]
    executed_root = tmp_path / "executed"
    executed_root.mkdir()
    with pytest.raises(RuntimeError, match="reported shell execution"):
        _run_permission_scenario(monkeypatch, executed_root, "ask", hook_events=executed)


def test_submit_active_steers_with_two_enters_not_one(tmp_path: Path) -> None:
    """Gate 0's steer must send the sequence that actually steers Cursor.

    Text plus one Enter only *queues* a follow-up; Enter on the now-empty
    prompt injects it into the running generation. One Enter is exactly the
    `cursor_steer_queue_only` negative control (the engine pops the second
    CR to simulate a broken steer), so a one-Enter harness reports a working
    provider as unsupported -- which is what factory run aa7770ad did against
    2026.09.10, a version with native steer. Proven live 2026-09-18: same
    binary and credential, one Enter -> response_was_steered false, two
    Enters -> true. No ESC: that clears Cursor's follow-up queue.
    """

    read_fd, write_fd = os.pipe()
    session = ProviderPtySession(
        process=cast(Any, SimpleNamespace(poll=lambda: None, returncode=None)),
        master_fd=write_fd,
        terminal_path=tmp_path / "terminal.raw",
        _reader=cast(Any, SimpleNamespace(join=lambda timeout=None: None)),
        _stop_reader=threading.Event(),
        _write_lock=threading.Lock(),
        text_settle_seconds=0.0,
        escape_settle_seconds=0.0,
        steer_queue_settle_seconds=0.0,
    )
    try:
        session.submit_active("STEER")
    finally:
        os.close(write_fd)
    written = os.read(read_fd, 4096)
    os.close(read_fd)

    assert written == b"STEER\r\r"
    assert written.count(b"\r") == 2
    assert b"\x1b" not in written


class _HookReplay:
    """A clock that replays hook rows: sleeping advances it and appends the rows
    that have come due, so a full-length wait costs no real time."""

    def __init__(self) -> None:
        self.now = 0.0
        self._pending: list[tuple[float, Path, dict[str, Any]]] = []

    @classmethod
    def install(cls, monkeypatch) -> _HookReplay:
        replay = cls()
        monkeypatch.setattr(gate, "time", replay)
        return replay

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self._flush()

    def emit(self, path: Path, rows: list[tuple[float, dict[str, Any]]], **identity: str) -> None:
        self._pending.extend((self.now + offset, path, {**row, **identity}) for offset, row in rows)
        self._flush()

    def _flush(self) -> None:
        due = sorted((item for item in self._pending if item[0] <= self.now + 1e-9), key=lambda item: item[0])
        self._pending = [item for item in self._pending if item[0] > self.now + 1e-9]
        for _, path, row in due:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row) + "\n")


def _hook(offset: float, event: str, generation_id: str = "gen-1", **fields: Any) -> tuple[float, dict[str, Any]]:
    return offset, {"event": event, "generation_id": generation_id, **fields}


class _FakeCursor:
    """A Cursor PTY session that emits scripted hook rows instead of running.

    The first batch is emitted at launch and the next one on each input the
    harness sends (a prompt, a steer or Ctrl-C), stamped with the session and
    conversation the harness launched it with.
    """

    def __init__(
        self, replay: _HookReplay, batches: list[list[tuple[float, dict[str, Any]]]], env: dict[str, str], argv: list[str]
    ) -> None:
        self._replay = replay
        self._batches = batches
        self._events_path = Path(env["LONGHOUSE_CURSOR_GATE0_EVENTS"])
        identity_flag = "--resume" if "--resume" in argv else "--new-session-id"
        self._identity = {
            "longhouse_session_id": env["LONGHOUSE_MANAGED_SESSION_ID"],
            "conversation_id": argv[argv.index(identity_flag) + 1],
        }
        self.process = SimpleNamespace(pid=4242, returncode=None)
        self.survives = True

    @classmethod
    def launcher(cls, replay: _HookReplay, *batches: list[tuple[float, dict[str, Any]]], survives: bool = True):
        def start(*, argv, cwd, env, terminal_path):  # noqa: ANN001 - test double
            terminal_path.touch()
            session = cls(replay, list(batches), env, list(argv))
            session.survives = survives
            session._next_batch()
            return session

        return start

    def _next_batch(self) -> None:
        if self._batches:
            self._replay.emit(self._events_path, self._batches.pop(0), **self._identity)

    def submit_idle(self, _text: str) -> None:
        self._next_batch()

    def submit_active(self, _text: str) -> None:
        self._next_batch()

    def interrupt(self) -> None:
        self._next_batch()

    def alive(self) -> bool:
        return self.survives

    def close(self) -> None:
        return None


def _wait_in_turn(replay: _HookReplay, events: Path, rows: list[tuple[float, dict[str, Any]]], **overrides: Any) -> dict[str, Any]:
    replay.emit(events, rows, longhouse_session_id="session-1", conversation_id="conversation-1")
    return gate.wait_for_hook(
        events,
        **{
            "longhouse_session_id": "session-1",
            "event": "afterAgentResponse",
            "conversation_id": "conversation-1",
            "generation_id": "gen-1",
            "timeout": 90.0,
            **overrides,
        },
    )


def test_wait_for_hook_fails_typed_when_the_turn_errors_before_the_awaited_event(monkeypatch, tmp_path: Path) -> None:
    """A turn the provider ended in error cannot still produce its response.

    The steered turn's shell ends, the provider reports `stop status=error`
    twenty seconds in, and no response follows. Waiting out the remaining
    seventy seconds and calling it a timeout misreports a provider error.
    """

    replay = _HookReplay.install(monkeypatch)

    with pytest.raises(gate.Gate0Failure) as failure:
        _wait_in_turn(
            replay,
            tmp_path / "events.ndjson",
            [
                _hook(0.0, "beforeShellExecution"),
                _hook(0.5, "beforeSubmitPrompt", "gen-2"),
                _hook(1.0, "afterShellExecution"),
                _hook(20.4, "stop", status="error"),
            ],
        )

    assert failure.value.failure_code == "cursor_turn_ended_in_error"
    assert failure.value.facts["awaited_event"] == "afterAgentResponse"
    assert failure.value.facts["stop_status"] == "error"
    assert failure.value.facts["generation_id"] == "gen-1"
    assert failure.value.facts["turn"] is None
    assert 20.4 <= replay.now < 20.4 + gate._HOOK_TIMEOUT_SECONDS + 1


def test_wait_for_hook_still_times_out_when_the_turn_never_ends(monkeypatch, tmp_path: Path) -> None:
    replay = _HookReplay.install(monkeypatch)

    with pytest.raises(TimeoutError, match="afterAgentResponse"):
        _wait_in_turn(
            replay,
            tmp_path / "events.ndjson",
            [_hook(0.0, "beforeShellExecution"), _hook(1.0, "afterShellExecution")],
        )

    assert replay.now >= 90.0


def test_wait_for_hook_accepts_an_event_that_lands_after_its_turns_stop(monkeypatch, tmp_path: Path) -> None:
    """Hooks are separate processes: a turn's stop row can precede its response row."""

    replay = _HookReplay.install(monkeypatch)

    row = _wait_in_turn(
        replay,
        tmp_path / "events.ndjson",
        [_hook(2.0, "stop", status="completed"), _hook(2.3, "afterAgentResponse", text_sha256="digest")],
    )

    assert row["event"] == "afterAgentResponse"
    assert replay.now < 3.0


def test_wait_for_hook_names_a_completed_turn_that_never_produced_the_event(monkeypatch, tmp_path: Path) -> None:
    replay = _HookReplay.install(monkeypatch)

    with pytest.raises(gate.Gate0Failure) as failure:
        _wait_in_turn(
            replay,
            tmp_path / "events.ndjson",
            [_hook(3.0, "afterAgentResponse"), _hook(3.0, "stop", status="completed")],
            event="beforeShellExecution",
            turn="permission",
        )

    assert failure.value.failure_code == "cursor_turn_ended_without_event"
    assert failure.value.facts["stop_status"] == "completed"
    assert failure.value.facts["turn"] == "permission"
    assert "the permission turn" in str(failure.value)
    assert replay.now < 3.0 + gate._HOOK_TIMEOUT_SECONDS + 1


def test_wait_for_hook_only_ends_on_a_stop_of_the_awaited_turn(monkeypatch, tmp_path: Path) -> None:
    """An earlier turn's stop neither ends the wait for a later turn nor fails it."""

    replay = _HookReplay.install(monkeypatch)
    events = tmp_path / "events.ndjson"
    rows = [
        _hook(0.2, "stop", "gen-0", status="error"),
        _hook(9.0, "beforeSubmitPrompt"),
        _hook(12.0, "afterAgentResponse"),
        _hook(12.0, "stop", status="completed"),
    ]

    assert _wait_in_turn(replay, events, rows)["generation_id"] == "gen-1"
    # A wait for the event that opens a turn has no turn of its own to end.
    opened = gate.wait_for_hook(
        events,
        longhouse_session_id="session-1",
        event="beforeSubmitPrompt",
        conversation_id="conversation-1",
        timeout=90.0,
    )
    assert opened["generation_id"] == "gen-1"
    # A stop is returned whatever its status when it is the awaited event.
    stopped = gate.wait_for_hook(
        events,
        longhouse_session_id="session-1",
        event="stop",
        conversation_id="conversation-1",
        timeout=90.0,
    )
    assert stopped["status"] == "error"


def _scenario_paths(tmp_path: Path) -> dict[str, Any]:
    return {
        "binary": "/fixture/cursor-agent",
        "workspace": tmp_path,
        "events_path": tmp_path / "events.ndjson",
        "terminal_path": tmp_path / "terminal.raw",
        "timeout": 90.0,
        "model": None,
    }


_STEER_LAUNCH = [_hook(0.0, "beforeSubmitPrompt"), _hook(1.7, "beforeShellExecution")]
_STEER_ERRORED = [
    _hook(0.5, "beforeSubmitPrompt", "gen-2"),
    _hook(1.0, "afterShellExecution"),
    _hook(20.4, "stop", status="error"),
]


def test_active_steer_reports_the_provider_error_not_a_timeout(monkeypatch, tmp_path: Path) -> None:
    replay = _HookReplay.install(monkeypatch)
    monkeypatch.setattr(gate.CursorPtySession, "start", _FakeCursor.launcher(replay, _STEER_LAUNCH, _STEER_ERRORED))

    with pytest.raises(gate.Gate0Failure) as failure:
        gate._active_steer_scenario(provider_id="conversation-1", **_scenario_paths(tmp_path))

    assert failure.value.failure_code == "cursor_turn_ended_in_error"
    assert failure.value.facts["stop_status"] == "error"
    assert failure.value.facts["turn"] == "steered"
    assert replay.now < 30.0


def test_active_steer_passes_when_the_stop_row_precedes_the_steered_response(monkeypatch, tmp_path: Path) -> None:
    replay = _HookReplay.install(monkeypatch)
    steered = [
        _hook(0.5, "beforeSubmitPrompt", "gen-2"),
        _hook(1.0, "afterShellExecution"),
        _hook(2.2, "stop", status="completed"),
        _hook(2.3, "afterAgentResponse", text_sha256=gate._marker_digest("STEERED")),
    ]
    monkeypatch.setattr(gate.CursorPtySession, "start", _FakeCursor.launcher(replay, _STEER_LAUNCH, steered))

    result = gate._active_steer_scenario(provider_id="conversation-1", **_scenario_paths(tmp_path))

    assert result["status"] == "passed"
    assert result["response_was_steered"] is True


_CANCEL_LAUNCH = [_hook(0.0, "beforeSubmitPrompt"), _hook(2.6, "beforeShellExecution")]
# Cursor reports an interrupted generation twice: aborted, then error.
_CANCEL_STOPS = [_hook(0.3, "stop", status="aborted"), _hook(0.31, "stop", status="error")]


def _run_cancel_scenario(monkeypatch, tmp_path: Path, recovery_turn: list[tuple[float, dict[str, Any]]]) -> dict[str, Any]:
    replay = _HookReplay.install(monkeypatch)
    monkeypatch.setattr(gate, "_wait_cursor_post_interrupt_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gate.CursorPtySession, "start", _FakeCursor.launcher(replay, _CANCEL_LAUNCH, _CANCEL_STOPS, recovery_turn))
    return gate._cancel_scenario(provider_id="conversation-1", **_scenario_paths(tmp_path))


def test_ctrl_c_cancel_still_expects_a_stop_that_is_not_a_success(monkeypatch, tmp_path: Path) -> None:
    result = _run_cancel_scenario(
        monkeypatch,
        tmp_path,
        [
            _hook(1.5, "beforeSubmitPrompt", "gen-2"),
            _hook(2.8, "stop", "gen-2", status="completed"),
            _hook(2.8, "afterAgentResponse", "gen-2"),
        ],
    )

    assert result["status"] == "passed"
    assert result["cancel_status"] == "aborted"
    assert result["next_turn_completed"] is True


def test_permission_deny_is_unaffected_by_how_the_denied_turn_ends(monkeypatch, tmp_path: Path) -> None:
    replay = _HookReplay.install(monkeypatch)
    denied = [
        _hook(0.0, "beforeSubmitPrompt"),
        _hook(2.1, "beforeShellExecution"),
        _hook(2.2, "postToolUseFailure"),
        _hook(2.6, "stop", status="error"),
    ]
    monkeypatch.setattr(gate.CursorPtySession, "start", _FakeCursor.launcher(replay, denied))

    result = gate._permission_scenario(decision="deny", provider_id="conversation-1", **_scenario_paths(tmp_path))

    assert result["status"] == "passed"
    assert result["side_effect_present"] is False


@pytest.mark.parametrize(
    ("steered_turn", "failure_code", "stop_status"),
    [
        (_STEER_ERRORED, "cursor_turn_ended_in_error", "error"),
        ([_hook(1.0, "afterShellExecution")], "TimeoutError", None),
    ],
)
def test_gate0_report_names_the_failed_scenario_and_its_typed_code(
    monkeypatch,
    tmp_path: Path,
    steered_turn: list[tuple[float, dict[str, Any]]],
    failure_code: str,
    stop_status: str | None,
) -> None:
    replay = _HookReplay.install(monkeypatch)
    binary = tmp_path / "cursor-agent"
    binary.write_bytes(b"fixture")

    def passed(*_args, **_kwargs):
        return {"status": "passed", "longhouse_session_id": "session-1"}

    for name in (
        "_trust_workspace",
        "_identity_scenario",
        "_resume_scenario",
        "_managed_conversation_reset_scenario",
        "_cancel_scenario",
        "_permission_scenario",
    ):
        monkeypatch.setattr(gate, name, passed)
    monkeypatch.setattr(gate, "_provider_version", lambda *_args: "0.0.0-fixture")
    monkeypatch.setattr(gate, "_run_json", lambda *_args, **_kwargs: {"isAuthenticated": True})
    monkeypatch.setattr(gate, "_create_chat", lambda *_args: "conversation-1")
    monkeypatch.setattr(gate, "_git_commit", lambda: None)
    monkeypatch.setattr(gate, "evaluate_reset_observation", lambda _observation: {"status": "pass", "failed_assertions": []})
    monkeypatch.setattr(gate.CursorPtySession, "start", _FakeCursor.launcher(replay, _STEER_LAUNCH, steered_turn))

    report = gate.run_gate0(gate.build_parser().parse_args(["--cursor-bin", str(binary), "--artifact-root", str(tmp_path / "artifacts")]))

    assert report["status"] == "failed"
    assert report["failure_code"] == failure_code
    assert report["failed_scenario"] == "active_steer"
    assert report["scenarios"]["permission_ask"]["status"] == "passed"
    failed = report["scenarios"]["active_steer"]
    assert failed["status"] == "failed"
    assert failed["failure_code"] == failure_code
    assert failed.get("stop_status") == stop_status
    assert failed.get("turn") == ("steered" if stop_status else None)
    assert report == json.loads((Path(report["artifact_root"]) / "gate0.json").read_text(encoding="utf-8"))


def _stub_native_store(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(gate, "wait_for_store", lambda *_args, **_kwargs: tmp_path / "store.db")
    monkeypatch.setattr(gate, "_cursor_store_agent_id", lambda _path: "conversation-1")


def test_native_resume_fails_when_the_resumed_turn_ends_in_error(monkeypatch, tmp_path: Path) -> None:
    """A resumed conversation whose only turn errored has not shown continuity.

    The hook naming the same conversation proves the identity was rebound; it
    does not prove the conversation still answers.
    """

    replay = _HookReplay.install(monkeypatch)
    _stub_native_store(monkeypatch, tmp_path)
    errored = [_hook(0.0, "beforeSubmitPrompt"), _hook(1.2, "afterAgentThought"), _hook(23.7, "stop", status="error")]
    monkeypatch.setattr(gate.CursorPtySession, "start", _FakeCursor.launcher(replay, errored))

    with pytest.raises(gate.Gate0Failure) as failure:
        gate._resume_scenario(provider_id="conversation-1", longhouse_session_id="session-1", **_scenario_paths(tmp_path))

    assert failure.value.failure_code == "cursor_turn_ended_in_error"
    assert failure.value.facts["turn"] == "resumed"
    assert failure.value.facts["stop_status"] == "error"


_RESUMED_COMPLETED = [_hook(0.0, "beforeSubmitPrompt"), _hook(1.3, "afterAgentResponse"), _hook(1.3, "stop", status="completed")]


def test_native_resume_passes_on_a_completed_resumed_turn(monkeypatch, tmp_path: Path) -> None:
    replay = _HookReplay.install(monkeypatch)
    _stub_native_store(monkeypatch, tmp_path)
    monkeypatch.setattr(gate.CursorPtySession, "start", _FakeCursor.launcher(replay, _RESUMED_COMPLETED))

    result = gate._resume_scenario(provider_id="conversation-1", longhouse_session_id="session-1", **_scenario_paths(tmp_path))

    assert result["status"] == "passed"
    assert result["stop_status"] == "completed"
    assert result["store_agent_id"] == "conversation-1"


def test_native_resume_fails_when_the_resumed_process_did_not_survive_the_turn(monkeypatch, tmp_path: Path) -> None:
    replay = _HookReplay.install(monkeypatch)
    _stub_native_store(monkeypatch, tmp_path)
    monkeypatch.setattr(gate.CursorPtySession, "start", _FakeCursor.launcher(replay, _RESUMED_COMPLETED, survives=False))

    with pytest.raises(RuntimeError, match="process_alive_after_turn"):
        gate._resume_scenario(provider_id="conversation-1", longhouse_session_id="session-1", **_scenario_paths(tmp_path))


_PROBE_PROMPT = "Reply with exactly PROBE and nothing else."


def _run_identity_scenario(monkeypatch, tmp_path: Path, probe_turn: list[tuple[float, dict[str, Any]]]) -> dict[str, Any]:
    replay = _HookReplay.install(monkeypatch)
    _stub_native_store(monkeypatch, tmp_path)
    launch_turn = [_hook(0.0, "beforeSubmitPrompt", "gen-0"), _hook(22.0, "stop", "gen-0", status="error")]
    monkeypatch.setattr(gate.CursorPtySession, "start", _FakeCursor.launcher(replay, launch_turn, probe_turn))
    return gate._identity_scenario(
        name="create_chat_resume",
        provider_id="conversation-1",
        launch_args=["--resume", "conversation-1"],
        probe_prompt=_PROBE_PROMPT,
        **_scenario_paths(tmp_path),
    )


def test_identity_scenario_records_an_errored_launch_turn_it_does_not_rely_on(monkeypatch, tmp_path: Path) -> None:
    """The launch turn is a readiness barrier; the proof is the probe turn's."""

    result = _run_identity_scenario(
        monkeypatch,
        tmp_path,
        [
            _hook(0.6, "beforeSubmitPrompt", prompt_sha256=gate._marker_digest(_PROBE_PROMPT)),
            _hook(5.0, "stop", status="completed"),
            _hook(5.0, "afterAgentResponse", text_sha256="digest"),
        ],
    )

    assert result["status"] == "passed"
    assert result["stop_status"] == "completed"
    assert result["launch_turn"] == {"role": "readiness_barrier", "generation_id": "gen-0", "stop_status": "error"}


def test_identity_scenario_fails_when_the_probe_turn_ends_in_error(monkeypatch, tmp_path: Path) -> None:
    with pytest.raises(gate.Gate0Failure) as failure:
        _run_identity_scenario(
            monkeypatch,
            tmp_path,
            [
                _hook(0.6, "beforeSubmitPrompt", prompt_sha256=gate._marker_digest(_PROBE_PROMPT)),
                _hook(5.0, "afterAgentResponse", text_sha256="digest"),
                _hook(5.1, "stop", status="error"),
            ],
        )

    assert failure.value.failure_code == "cursor_turn_ended_in_error"
    assert failure.value.facts["turn"] == "probe"


def test_identity_scenario_finds_a_launch_stop_that_landed_before_the_store_and_ahead_of_its_response(monkeypatch, tmp_path: Path) -> None:
    """The launch turn's stop is found by its generation, not by where its row sits.

    The launch turn can finish before the native store appears, and its stop
    row can precede its response row. A window that starts at the last row
    present when the store appeared then holds only the response.
    """

    replay = _HookReplay.install(monkeypatch)
    _stub_native_store(monkeypatch, tmp_path)

    def late_store(*_args: Any, **_kwargs: Any) -> Path:
        replay.sleep(3.0)
        return tmp_path / "store.db"

    monkeypatch.setattr(gate, "wait_for_store", late_store)
    launch_turn = [
        _hook(0.0, "beforeSubmitPrompt", "gen-0"),
        _hook(1.4, "stop", "gen-0", status="completed"),
        _hook(1.42, "afterAgentResponse", "gen-0"),
    ]
    probe_turn = [
        _hook(0.6, "beforeSubmitPrompt", prompt_sha256=gate._marker_digest(_PROBE_PROMPT)),
        _hook(5.0, "afterAgentResponse", text_sha256="digest"),
        _hook(5.0, "stop", status="completed"),
    ]
    monkeypatch.setattr(gate.CursorPtySession, "start", _FakeCursor.launcher(replay, launch_turn, probe_turn))

    result = gate._identity_scenario(
        name="new_session_id",
        provider_id="conversation-1",
        launch_args=["--new-session-id", "conversation-1"],
        probe_prompt=_PROBE_PROMPT,
        **_scenario_paths(tmp_path),
    )

    assert result["status"] == "passed"
    assert result["launch_turn"] == {"role": "readiness_barrier", "generation_id": "gen-0", "stop_status": "completed"}
    assert result["probe_generation_id"] == "gen-1"
    assert replay.now < 30.0


def test_identity_scenario_names_the_probe_turn_that_ended_before_its_response(monkeypatch, tmp_path: Path) -> None:
    with pytest.raises(gate.Gate0Failure) as failure:
        _run_identity_scenario(
            monkeypatch,
            tmp_path,
            [
                _hook(0.6, "beforeSubmitPrompt", prompt_sha256=gate._marker_digest(_PROBE_PROMPT)),
                _hook(21.0, "stop", status="error"),
            ],
        )

    assert failure.value.failure_code == "cursor_turn_ended_in_error"
    assert failure.value.facts["turn"] == "probe"
    assert failure.value.facts["awaited_event"] == "afterAgentResponse"


def test_ctrl_c_cancel_fails_typed_when_the_recovery_turn_ends_in_error(monkeypatch, tmp_path: Path) -> None:
    with pytest.raises(gate.Gate0Failure) as failure:
        _run_cancel_scenario(
            monkeypatch,
            tmp_path,
            [_hook(1.5, "beforeSubmitPrompt", "gen-2"), _hook(9.0, "stop", "gen-2", status="error")],
        )

    assert failure.value.failure_code == "cursor_turn_ended_in_error"
    assert failure.value.facts["turn"] == "post-cancel"
