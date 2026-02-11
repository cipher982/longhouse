"""Tests for Gemini CLI session provider."""

from __future__ import annotations

import json
import time
from datetime import datetime
from datetime import timezone
from pathlib import Path

from zerg.services.shipper.providers.gemini import GeminiProvider

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

HASH_DIR_NAME = "a" * 64  # valid SHA-256 hex string

STANDARD_SESSION = {
    "sessionId": "test-session-1",
    "projectHash": "abc123def456" + "0" * 52,
    "startTime": "2026-01-08T21:12:00Z",
    "lastUpdated": "2026-01-08T22:00:00Z",
    "messages": [
        {
            "id": "msg-1",
            "timestamp": "2026-01-08T21:12:30Z",
            "type": "user",
            "content": "list files in this directory",
        },
        {
            "id": "msg-2",
            "timestamp": "2026-01-08T21:12:35Z",
            "type": "gemini",
            "content": "Here are the files:",
            "toolCalls": [
                {
                    "id": "tc-1",
                    "name": "list_directory",
                    "displayName": "List Directory",
                    "args": {"path": "/Users/test/project"},
                    "result": [
                        {
                            "functionResponse": {
                                "name": "list_directory",
                                "response": {"output": "src/\ntests/\nREADME.md"},
                            }
                        }
                    ],
                    "status": "success",
                    "timestamp": "2026-01-08T21:12:33Z",
                }
            ],
        },
    ],
}


def _write_session(
    tmp_path: Path,
    data: dict | list,
    *,
    in_chats: bool = True,
    filename: str = "session-001.json",
    hash_dir: str = HASH_DIR_NAME,
) -> Path:
    """Helper to write a session file in the expected directory structure."""
    if in_chats:
        dest = tmp_path / "tmp" / hash_dir / "chats"
    else:
        dest = tmp_path / "tmp" / hash_dir
    dest.mkdir(parents=True, exist_ok=True)
    fp = dest / filename
    fp.write_text(json.dumps(data))
    return fp


# ---------------------------------------------------------------------------
# discover_files
# ---------------------------------------------------------------------------


class TestDiscoverFiles:
    def test_discover_files_empty(self, tmp_path: Path) -> None:
        """No tmp dir -> empty list."""
        provider = GeminiProvider(config_dir=tmp_path)
        assert provider.discover_files() == []

    def test_discover_files_finds_in_chats(self, tmp_path: Path) -> None:
        """Standard path: <hash>/chats/session-*.json."""
        _write_session(tmp_path, STANDARD_SESSION, in_chats=True)
        provider = GeminiProvider(config_dir=tmp_path)
        files = provider.discover_files()
        assert len(files) == 1
        assert files[0].name == "session-001.json"

    def test_discover_files_finds_fallback(self, tmp_path: Path) -> None:
        """Fallback path: <hash>/session-*.json (no chats subdir)."""
        _write_session(tmp_path, STANDARD_SESSION, in_chats=False, filename="session-002.json")
        provider = GeminiProvider(config_dir=tmp_path)
        files = provider.discover_files()
        assert len(files) == 1
        assert files[0].name == "session-002.json"

    def test_discover_files_skips_non_hex_dirs(self, tmp_path: Path) -> None:
        """Directories that don't look like hex hashes are skipped."""
        # Create a valid one
        _write_session(tmp_path, STANDARD_SESSION, in_chats=True)
        # Create an invalid one (non-hex name)
        bad_dir = tmp_path / "tmp" / "not-a-hash" / "chats"
        bad_dir.mkdir(parents=True)
        (bad_dir / "session-bad.json").write_text("{}")

        provider = GeminiProvider(config_dir=tmp_path)
        files = provider.discover_files()
        assert len(files) == 1
        assert files[0].name == "session-001.json"

    def test_discover_files_deduplicates(self, tmp_path: Path) -> None:
        """Same file matched by both patterns is deduplicated."""
        # Write in chats/ — the glob("session-*.json") on hash_dir
        # won't match because the file is in chats/ subdir
        _write_session(tmp_path, STANDARD_SESSION, in_chats=True)
        provider = GeminiProvider(config_dir=tmp_path)
        files = provider.discover_files()
        assert len(files) == 1

    def test_discover_files_sorted_newest_first(self, tmp_path: Path) -> None:
        """Files sorted by mtime, newest first."""
        f1 = _write_session(
            tmp_path,
            STANDARD_SESSION,
            in_chats=True,
            filename="session-old.json",
        )
        time.sleep(0.05)
        f2 = _write_session(
            tmp_path,
            STANDARD_SESSION,
            in_chats=True,
            filename="session-new.json",
        )
        provider = GeminiProvider(config_dir=tmp_path)
        files = provider.discover_files()
        assert len(files) == 2
        assert files[0].name == "session-new.json"
        assert files[1].name == "session-old.json"


# ---------------------------------------------------------------------------
# parse_file — message types
# ---------------------------------------------------------------------------


class TestParseMessages:
    def test_parse_user_message(self, tmp_path: Path) -> None:
        """type=user -> role=user."""
        fp = _write_session(tmp_path, STANDARD_SESSION)
        provider = GeminiProvider(config_dir=tmp_path)
        events = list(provider.parse_file(fp))

        user_events = [e for e in events if e.role == "user"]
        assert len(user_events) == 1
        assert user_events[0].content_text == "list files in this directory"
        assert user_events[0].raw_type == "gemini-user"

    def test_parse_gemini_message(self, tmp_path: Path) -> None:
        """type=gemini -> role=assistant."""
        fp = _write_session(tmp_path, STANDARD_SESSION)
        provider = GeminiProvider(config_dir=tmp_path)
        events = list(provider.parse_file(fp))

        assistant_text = [e for e in events if e.role == "assistant" and e.content_text is not None]
        assert len(assistant_text) == 1
        assert assistant_text[0].content_text == "Here are the files:"
        assert assistant_text[0].raw_type == "gemini-gemini"

    def test_parse_info_message_skipped(self, tmp_path: Path) -> None:
        """type=info -> no events emitted."""
        data = {
            "sessionId": "s1",
            "messages": [
                {
                    "id": "info-1",
                    "timestamp": "2026-01-08T21:12:00Z",
                    "type": "info",
                    "content": "Session started",
                }
            ],
        }
        fp = _write_session(tmp_path, data)
        provider = GeminiProvider(config_dir=tmp_path)
        events = list(provider.parse_file(fp))
        assert events == []

    def test_parse_empty_content_skipped(self, tmp_path: Path) -> None:
        """Messages with empty/whitespace content don't emit content events."""
        data = {
            "sessionId": "s1",
            "messages": [
                {
                    "id": "msg-1",
                    "timestamp": "2026-01-08T21:12:00Z",
                    "type": "user",
                    "content": "   ",
                }
            ],
        }
        fp = _write_session(tmp_path, data)
        provider = GeminiProvider(config_dir=tmp_path)
        events = list(provider.parse_file(fp))
        assert events == []


# ---------------------------------------------------------------------------
# parse_file — tool calls
# ---------------------------------------------------------------------------


class TestParseToolCalls:
    def test_parse_tool_calls(self, tmp_path: Path) -> None:
        """Embedded toolCalls -> assistant tool events."""
        fp = _write_session(tmp_path, STANDARD_SESSION)
        provider = GeminiProvider(config_dir=tmp_path)
        events = list(provider.parse_file(fp))

        tool_events = [e for e in events if e.raw_type == "gemini-tool_call"]
        assert len(tool_events) == 1
        assert tool_events[0].role == "assistant"
        assert tool_events[0].tool_name == "List Directory"
        assert tool_events[0].tool_input_json == {"path": "/Users/test/project"}

    def test_parse_tool_results(self, tmp_path: Path) -> None:
        """functionResponse.response.output extracted as tool result."""
        fp = _write_session(tmp_path, STANDARD_SESSION)
        provider = GeminiProvider(config_dir=tmp_path)
        events = list(provider.parse_file(fp))

        result_events = [e for e in events if e.raw_type == "gemini-tool_result"]
        assert len(result_events) == 1
        assert result_events[0].role == "tool"
        assert result_events[0].tool_output_text == "src/\ntests/\nREADME.md"

    def test_tool_call_uses_name_when_no_displayname(self, tmp_path: Path) -> None:
        """Falls back to name when displayName is missing."""
        data = {
            "sessionId": "s1",
            "messages": [
                {
                    "id": "msg-1",
                    "timestamp": "2026-01-08T21:12:35Z",
                    "type": "gemini",
                    "content": "",
                    "toolCalls": [
                        {
                            "id": "tc-1",
                            "name": "read_file",
                            "args": {"file_path": "/tmp/test.py"},
                            "result": [],
                            "status": "success",
                        }
                    ],
                }
            ],
        }
        fp = _write_session(tmp_path, data)
        provider = GeminiProvider(config_dir=tmp_path)
        events = list(provider.parse_file(fp))

        tool_events = [e for e in events if e.raw_type == "gemini-tool_call"]
        assert len(tool_events) == 1
        assert tool_events[0].tool_name == "read_file"


# ---------------------------------------------------------------------------
# parse_file — JSON shapes
# ---------------------------------------------------------------------------


class TestJsonShapes:
    def test_parse_flat_array_format(self, tmp_path: Path) -> None:
        """Flat array shape: [...] handled."""
        data = [
            {
                "id": "msg-1",
                "timestamp": "2026-01-08T21:12:30Z",
                "type": "user",
                "content": "hello from flat array",
            }
        ]
        fp = _write_session(tmp_path, data)
        provider = GeminiProvider(config_dir=tmp_path)
        events = list(provider.parse_file(fp))

        assert len(events) == 1
        assert events[0].content_text == "hello from flat array"
        assert events[0].role == "user"

    def test_parse_history_format(self, tmp_path: Path) -> None:
        """History key shape: {"history": [...]} handled."""
        data = {
            "history": [
                {
                    "id": "msg-1",
                    "timestamp": "2026-01-08T21:12:30Z",
                    "type": "gemini",
                    "content": "from history format",
                }
            ]
        }
        fp = _write_session(tmp_path, data)
        provider = GeminiProvider(config_dir=tmp_path)
        events = list(provider.parse_file(fp))

        assert len(events) == 1
        assert events[0].content_text == "from history format"
        assert events[0].role == "assistant"

    def test_parse_role_field_instead_of_type(self, tmp_path: Path) -> None:
        """Messages with 'role' field instead of 'type' are handled."""
        data = {
            "sessionId": "s1",
            "messages": [
                {
                    "id": "msg-1",
                    "timestamp": "2026-01-08T21:12:30Z",
                    "role": "model",
                    "content": "using role field",
                }
            ],
        }
        fp = _write_session(tmp_path, data)
        provider = GeminiProvider(config_dir=tmp_path)
        events = list(provider.parse_file(fp))

        assert len(events) == 1
        assert events[0].role == "assistant"


# ---------------------------------------------------------------------------
# extract_metadata
# ---------------------------------------------------------------------------


class TestExtractMetadata:
    def test_extract_metadata_basic(self, tmp_path: Path) -> None:
        """sessionId, startTime, lastUpdated extracted."""
        fp = _write_session(tmp_path, STANDARD_SESSION)
        provider = GeminiProvider(config_dir=tmp_path)
        meta = provider.extract_metadata(fp)

        assert meta.session_id == "test-session-1"
        assert meta.started_at == datetime(2026, 1, 8, 21, 12, 0, tzinfo=timezone.utc)
        assert meta.ended_at == datetime(2026, 1, 8, 22, 0, 0, tzinfo=timezone.utc)

    def test_extract_metadata_falls_back_to_message_timestamps(self, tmp_path: Path) -> None:
        """When top-level timestamps missing, infer from messages."""
        data = {
            "sessionId": "s1",
            "messages": [
                {
                    "id": "m1",
                    "timestamp": "2026-01-08T10:00:00Z",
                    "type": "user",
                    "content": "first",
                },
                {
                    "id": "m2",
                    "timestamp": "2026-01-08T11:00:00Z",
                    "type": "gemini",
                    "content": "last",
                },
            ],
        }
        fp = _write_session(tmp_path, data)
        provider = GeminiProvider(config_dir=tmp_path)
        meta = provider.extract_metadata(fp)

        assert meta.started_at == datetime(2026, 1, 8, 10, 0, 0, tzinfo=timezone.utc)
        assert meta.ended_at == datetime(2026, 1, 8, 11, 0, 0, tzinfo=timezone.utc)

    def test_extract_metadata_infers_cwd(self, tmp_path: Path) -> None:
        """CWD inferred from tool call args containing absolute paths."""
        # Create a fake .git dir so _infer_cwd_from_messages finds it
        project_dir = tmp_path / "fake_project"
        (project_dir / ".git").mkdir(parents=True)

        data = {
            "sessionId": "s1",
            "projectHash": "abc123" + "0" * 58,
            "messages": [
                {
                    "id": "msg-1",
                    "timestamp": "2026-01-08T21:12:35Z",
                    "type": "gemini",
                    "content": "",
                    "toolCalls": [
                        {
                            "id": "tc-1",
                            "name": "read_file",
                            "args": {"file_path": str(project_dir / "src" / "main.py")},
                            "result": [],
                        }
                    ],
                }
            ],
        }
        fp = _write_session(tmp_path, data)
        provider = GeminiProvider(config_dir=tmp_path)
        meta = provider.extract_metadata(fp)

        assert meta.cwd == str(project_dir)
        assert meta.project == "fake_project"

    def test_extract_metadata_bad_json(self, tmp_path: Path) -> None:
        """Gracefully handles invalid JSON."""
        dest = tmp_path / "tmp" / HASH_DIR_NAME / "chats"
        dest.mkdir(parents=True)
        fp = dest / "session-bad.json"
        fp.write_text("not valid json {{{")

        provider = GeminiProvider(config_dir=tmp_path)
        meta = provider.extract_metadata(fp)
        assert meta.session_id == "session-bad"

    def test_extract_metadata_filename_as_session_id(self, tmp_path: Path) -> None:
        """When sessionId missing from JSON, uses filename stem."""
        data = {"messages": []}
        fp = _write_session(tmp_path, data, filename="session-my-custom-name.json")
        provider = GeminiProvider(config_dir=tmp_path)
        meta = provider.extract_metadata(fp)
        assert meta.session_id == "session-my-custom-name"


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------


class TestTimestampParsing:
    def test_timestamp_unix_epoch(self, tmp_path: Path) -> None:
        """Integer timestamps (Unix epoch) are handled."""
        epoch_ts = 1736370750  # 2025-01-08T21:12:30Z
        data = {
            "sessionId": "epoch-session",
            "messages": [
                {
                    "id": "msg-1",
                    "timestamp": epoch_ts,
                    "type": "user",
                    "content": "epoch timestamp test",
                }
            ],
        }
        fp = _write_session(tmp_path, data)
        provider = GeminiProvider(config_dir=tmp_path)
        events = list(provider.parse_file(fp))

        assert len(events) == 1
        assert events[0].timestamp.tzinfo is not None
        # Verify it's close to expected (exact depends on epoch value)
        assert events[0].timestamp.year == 2025

    def test_timestamp_iso_with_timezone(self, tmp_path: Path) -> None:
        """ISO timestamps with timezone offset are handled."""
        data = {
            "sessionId": "tz-session",
            "messages": [
                {
                    "id": "msg-1",
                    "timestamp": "2026-01-08T16:12:30-05:00",
                    "type": "user",
                    "content": "timezone test",
                }
            ],
        }
        fp = _write_session(tmp_path, data)
        provider = GeminiProvider(config_dir=tmp_path)
        events = list(provider.parse_file(fp))

        assert len(events) == 1
        assert events[0].timestamp.year == 2026
