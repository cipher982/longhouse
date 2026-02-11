"""Tests for the Claude Code session parser."""

import json
from pathlib import Path

from zerg.services.shipper.parser import extract_session_metadata
from zerg.services.shipper.parser import parse_session_file


class TestParseSessionFile:
    """Tests for parse_session_file."""

    def test_parse_empty_file(self, tmp_path: Path):
        """Empty file yields no events."""
        session_file = tmp_path / "empty-session.jsonl"
        session_file.write_text("")

        events = list(parse_session_file(session_file))
        assert events == []

    def test_parse_user_message(self, tmp_path: Path):
        """Parse a simple user message."""
        session_file = tmp_path / "test-session.jsonl"
        session_file.write_text(
            json.dumps(
                {
                    "type": "user",
                    "uuid": "msg-123",
                    "timestamp": "2026-01-28T10:00:00Z",
                    "message": {"role": "user", "content": "Hello, Claude!"},
                }
            )
            + "\n"
        )

        events = list(parse_session_file(session_file))

        assert len(events) == 1
        assert events[0].role == "user"
        assert events[0].content_text == "Hello, Claude!"
        assert events[0].session_id == "test-session"

    def test_parse_assistant_text(self, tmp_path: Path):
        """Parse an assistant text response."""
        session_file = tmp_path / "test-session.jsonl"
        session_file.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": "msg-456",
                    "timestamp": "2026-01-28T10:01:00Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Hello! How can I help?"}],
                    },
                }
            )
            + "\n"
        )

        events = list(parse_session_file(session_file))

        assert len(events) == 1
        assert events[0].role == "assistant"
        assert events[0].content_text == "Hello! How can I help?"

    def test_parse_assistant_tool_use(self, tmp_path: Path):
        """Parse an assistant tool call."""
        session_file = tmp_path / "test-session.jsonl"
        session_file.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": "msg-789",
                    "timestamp": "2026-01-28T10:02:00Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "tool-abc",
                                "name": "Read",
                                "input": {"file_path": "/path/to/file.py"},
                            }
                        ],
                    },
                }
            )
            + "\n"
        )

        events = list(parse_session_file(session_file))

        assert len(events) == 1
        assert events[0].role == "assistant"
        assert events[0].tool_name == "Read"
        assert events[0].tool_input_json == {"file_path": "/path/to/file.py"}

    def test_parse_tool_result(self, tmp_path: Path):
        """Parse a tool result message."""
        session_file = tmp_path / "test-session.jsonl"
        session_file.write_text(
            json.dumps(
                {
                    "type": "user",
                    "uuid": "msg-result",
                    "timestamp": "2026-01-28T10:03:00Z",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "tool-abc",
                                "content": "File contents here...",
                            }
                        ],
                    },
                }
            )
            + "\n"
        )

        events = list(parse_session_file(session_file))

        assert len(events) == 1
        assert events[0].role == "tool"
        assert events[0].tool_output_text == "File contents here..."

    def test_parse_mixed_assistant_content(self, tmp_path: Path):
        """Parse assistant message with both text and tool use."""
        session_file = tmp_path / "test-session.jsonl"
        session_file.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": "msg-mixed",
                    "timestamp": "2026-01-28T10:04:00Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "Let me read that file."},
                            {
                                "type": "tool_use",
                                "id": "tool-xyz",
                                "name": "Read",
                                "input": {"file_path": "/test.py"},
                            },
                        ],
                    },
                }
            )
            + "\n"
        )

        events = list(parse_session_file(session_file))

        assert len(events) == 2
        assert events[0].role == "assistant"
        assert events[0].content_text == "Let me read that file."
        assert events[1].role == "assistant"
        assert events[1].tool_name == "Read"

    def test_skip_metadata_types(self, tmp_path: Path):
        """Skip summary, file-history-snapshot, and progress types."""
        session_file = tmp_path / "test-session.jsonl"
        lines = [
            json.dumps({"type": "summary", "summary": "Test session"}),
            json.dumps(
                {
                    "type": "file-history-snapshot",
                    "snapshot": {"files": []},
                }
            ),
            json.dumps(
                {
                    "type": "progress",
                    "data": {"status": "running"},
                }
            ),
            json.dumps(
                {
                    "type": "user",
                    "uuid": "real-msg",
                    "timestamp": "2026-01-28T10:00:00Z",
                    "message": {"content": "Real message"},
                }
            ),
        ]
        session_file.write_text("\n".join(lines))

        events = list(parse_session_file(session_file))

        assert len(events) == 1
        assert events[0].content_text == "Real message"

    def test_parse_with_offset(self, tmp_path: Path):
        """Parse from a specific byte offset."""
        session_file = tmp_path / "test-session.jsonl"
        line1 = json.dumps(
            {
                "type": "user",
                "uuid": "msg-1",
                "timestamp": "2026-01-28T10:00:00Z",
                "message": {"content": "First message"},
            }
        )
        line2 = json.dumps(
            {
                "type": "user",
                "uuid": "msg-2",
                "timestamp": "2026-01-28T10:01:00Z",
                "message": {"content": "Second message"},
            }
        )
        session_file.write_text(f"{line1}\n{line2}\n")

        # Parse from after first line
        offset = len(line1.encode("utf-8")) + 1  # +1 for newline

        events = list(parse_session_file(session_file, offset=offset))

        assert len(events) == 1
        assert events[0].content_text == "Second message"

    def test_to_event_ingest(self, tmp_path: Path):
        """Test conversion to EventIngest format."""
        session_file = tmp_path / "test-session.jsonl"
        session_file.write_text(
            json.dumps(
                {
                    "type": "user",
                    "uuid": "msg-123",
                    "timestamp": "2026-01-28T10:00:00Z",
                    "message": {"content": "Test message"},
                }
            )
        )

        events = list(parse_session_file(session_file))
        ingest = events[0].to_event_ingest("/path/to/file.jsonl")

        assert ingest["role"] == "user"
        assert ingest["content_text"] == "Test message"
        assert ingest["source_path"] == "/path/to/file.jsonl"
        assert ingest["source_offset"] == 0

    def test_raw_line_capture(self, tmp_path: Path):
        """Test that raw JSONL line is captured for lossless archiving."""
        session_file = tmp_path / "test-session.jsonl"
        original_line = json.dumps(
            {
                "type": "user",
                "uuid": "msg-raw",
                "timestamp": "2026-01-28T10:00:00Z",
                "message": {"content": "Raw line test"},
                "extra_field": "preserved",
            }
        )
        session_file.write_text(original_line + "\n")

        events = list(parse_session_file(session_file))

        assert len(events) == 1
        assert events[0].raw_line == original_line
        ingest = events[0].to_event_ingest("/path/to/file.jsonl")
        assert ingest["raw_json"] == original_line

    def test_raw_line_capture_assistant(self, tmp_path: Path):
        """Test raw line capture for assistant messages."""
        session_file = tmp_path / "test-session.jsonl"
        original_line = json.dumps(
            {
                "type": "assistant",
                "uuid": "msg-asst",
                "timestamp": "2026-01-28T10:01:00Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Hello!"}],
                },
            }
        )
        session_file.write_text(original_line + "\n")

        events = list(parse_session_file(session_file))

        assert len(events) == 1
        assert events[0].raw_line == original_line

    def test_raw_line_capture_tool_result(self, tmp_path: Path):
        """Test raw line capture for tool results."""
        session_file = tmp_path / "test-session.jsonl"
        original_line = json.dumps(
            {
                "type": "user",
                "uuid": "msg-result",
                "timestamp": "2026-01-28T10:02:00Z",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool-abc",
                            "content": "File contents",
                        }
                    ],
                },
            }
        )
        session_file.write_text(original_line + "\n")

        events = list(parse_session_file(session_file))

        assert len(events) == 1
        assert events[0].raw_line == original_line


class TestExtractSessionMetadata:
    """Tests for extract_session_metadata."""

    def test_extract_metadata(self, tmp_path: Path):
        """Extract session metadata from file."""
        session_file = tmp_path / "abc123-def456.jsonl"
        session_file.write_text(
            json.dumps(
                {
                    "type": "user",
                    "uuid": "msg-1",
                    "timestamp": "2026-01-28T10:00:00Z",
                    "cwd": "/Users/test/project",
                    "gitBranch": "main",
                    "version": "2.1.0",
                    "message": {"content": "Test"},
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "user",
                    "uuid": "msg-2",
                    "timestamp": "2026-01-28T12:00:00Z",
                    "message": {"content": "Later message"},
                }
            )
        )

        metadata = extract_session_metadata(session_file)

        assert metadata.session_id == "abc123-def456"
        assert metadata.cwd == "/Users/test/project"
        assert metadata.git_branch == "main"
        assert metadata.project == "project"  # Derived from cwd
        assert metadata.version == "2.1.0"
        assert metadata.started_at is not None
        assert metadata.ended_at is not None
        assert metadata.started_at < metadata.ended_at

    def test_extract_empty_file(self, tmp_path: Path):
        """Handle empty file gracefully."""
        session_file = tmp_path / "empty.jsonl"
        session_file.write_text("")

        metadata = extract_session_metadata(session_file)

        assert metadata.session_id == "empty"
        assert metadata.cwd is None
        assert metadata.started_at is None
