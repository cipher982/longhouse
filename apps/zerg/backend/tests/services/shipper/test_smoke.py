"""Shipper smoke test for public launch checklist.

Verifies the critical shipper path end-to-end without a running server:
  1. Create a realistic JSONL session file (user, assistant, tool use, tool result)
  2. Parse it with the session parser
  3. Build an ingest payload via SessionShipper
  4. Ship it (mocked HTTP) and verify the full round-trip

This is the "shipper smoke test passes" gate for the launch checklist.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from zerg.services.shipper import SessionShipper, ShipperConfig
from zerg.services.shipper.parser import extract_session_metadata, parse_session_file
from zerg.services.shipper.state import ShipperState


def _write_realistic_session(tmp_path: Path) -> Path:
    """Write a realistic 4-event JSONL session file.

    Mimics a real Claude Code session: user asks a question, assistant calls
    a tool, tool returns a result, assistant responds with text.
    """
    projects_dir = tmp_path / ".claude" / "projects" / "-Users-test-myproject"
    projects_dir.mkdir(parents=True)

    session_file = projects_dir / "smoke-test-session-abc123.jsonl"
    events = [
        {
            "type": "user",
            "uuid": "msg-001",
            "timestamp": "2026-02-10T10:00:00Z",
            "cwd": "/Users/test/myproject",
            "gitBranch": "main",
            "version": "2.3.0",
            "message": {"role": "user", "content": "Read the README file"},
        },
        {
            "type": "assistant",
            "uuid": "msg-002",
            "timestamp": "2026-02-10T10:00:05Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Let me read that file."},
                    {
                        "type": "tool_use",
                        "id": "tool-read-001",
                        "name": "Read",
                        "input": {"file_path": "/Users/test/myproject/README.md"},
                    },
                ],
            },
        },
        {
            "type": "user",
            "uuid": "msg-003",
            "timestamp": "2026-02-10T10:00:06Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-read-001",
                        "content": "# My Project\nA sample project.",
                    }
                ],
            },
        },
        {
            "type": "assistant",
            "uuid": "msg-004",
            "timestamp": "2026-02-10T10:00:10Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": "The README describes a sample project called My Project.",
                    }
                ],
            },
        },
    ]

    lines = [json.dumps(e) for e in events]
    session_file.write_text("\n".join(lines) + "\n")
    return session_file


class TestShipperSmoke:
    """Smoke test: parse -> build payload -> ship (mocked) in one pass."""

    def test_parse_realistic_session(self, tmp_path: Path):
        """Parser extracts all 4 meaningful events from a realistic session."""
        session_file = _write_realistic_session(tmp_path)
        events = list(parse_session_file(session_file))

        # 1 user msg + 1 assistant text + 1 tool_use + 1 tool_result + 1 assistant text = 5
        assert len(events) == 5

        roles = [e.role for e in events]
        assert roles == ["user", "assistant", "assistant", "tool", "assistant"]

        # First event: user message
        assert events[0].content_text == "Read the README file"

        # Second event: assistant text before tool call
        assert events[1].content_text == "Let me read that file."

        # Third event: tool use
        assert events[2].tool_name == "Read"
        assert events[2].tool_input_json == {"file_path": "/Users/test/myproject/README.md"}

        # Fourth event: tool result
        assert events[3].tool_output_text == "# My Project\nA sample project."

        # Fifth event: assistant final response
        assert "sample project" in events[4].content_text

        # All events carry raw_line for lossless archiving
        for event in events:
            assert event.raw_line, f"Event {event.uuid} missing raw_line"

    def test_metadata_extraction(self, tmp_path: Path):
        """Metadata extraction captures cwd, branch, project, and timestamps."""
        session_file = _write_realistic_session(tmp_path)
        metadata = extract_session_metadata(session_file)

        assert metadata.session_id == "smoke-test-session-abc123"
        assert metadata.cwd == "/Users/test/myproject"
        assert metadata.git_branch == "main"
        assert metadata.project == "myproject"
        assert metadata.version == "2.3.0"
        assert metadata.started_at is not None
        assert metadata.ended_at is not None
        assert metadata.started_at < metadata.ended_at

    @pytest.mark.asyncio
    async def test_full_ship_round_trip(self, tmp_path: Path):
        """Full round-trip: parse, build payload, ship (mocked), verify state."""
        session_file = _write_realistic_session(tmp_path)
        claude_dir = tmp_path / ".claude"

        config = ShipperConfig(
            api_url="http://localhost:47300",
            claude_config_dir=claude_dir,
        )
        state = ShipperState(state_path=tmp_path / "shipper-state.json")
        shipper = SessionShipper(config=config, state=state)

        # Capture the payload sent to the API
        captured_payload = None

        async def mock_post(payload: dict) -> dict:
            nonlocal captured_payload
            captured_payload = payload
            return {
                "session_id": payload["id"],
                "events_inserted": len(payload["events"]),
                "events_skipped": 0,
                "session_created": True,
            }

        with patch.object(shipper, "_post_ingest", new_callable=AsyncMock) as mock:
            mock.side_effect = mock_post
            result = await shipper.scan_and_ship()

        # Ship succeeded
        assert result.sessions_shipped == 1
        assert result.events_shipped == 5
        assert result.errors == []

        # Payload structure matches the ingest API contract
        assert captured_payload is not None
        assert captured_payload["provider"] == "claude"
        assert captured_payload["cwd"] == "/Users/test/myproject"
        assert captured_payload["git_branch"] == "main"
        assert captured_payload["provider_session_id"] == "smoke-test-session-abc123"
        assert len(captured_payload["events"]) == 5

        # Events carry required fields
        for event in captured_payload["events"]:
            assert "role" in event
            assert "timestamp" in event
            assert "raw_json" in event
            assert event["raw_json"] is not None

        # Verify role distribution in payload
        event_roles = [e["role"] for e in captured_payload["events"]]
        assert event_roles.count("user") == 1
        assert event_roles.count("assistant") == 3  # 2 text + 1 tool_use
        assert event_roles.count("tool") == 1

        # State is updated -- second ship should find nothing new
        with patch.object(shipper, "_post_ingest", new_callable=AsyncMock) as mock2:
            result2 = await shipper.scan_and_ship()

        assert result2.sessions_shipped == 0
        assert result2.events_shipped == 0
        mock2.assert_not_called()
