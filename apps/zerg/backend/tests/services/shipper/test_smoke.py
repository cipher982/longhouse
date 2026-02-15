"""Shipper smoke test for public launch checklist.

Verifies the critical shipper path end-to-end without a running server:
  1. Create a realistic JSONL session file (user, assistant, tool use, tool result)
  2. Parse it with the session parser
  3. Build an ingest payload via SessionShipper
  4. Ship it (mocked HTTP) and verify the full round-trip

Updated for shipper v2:
  - Only first event per JSONL line carries raw_json
  - Spool stores pointers (file_path + byte range), not payloads
  - Failed ship + replay cycle works with pointer spool
  - State persists in SQLite (not JSON)

This is the "shipper smoke test passes" gate for the launch checklist.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import patch

import httpx
import pytest

from zerg.services.shipper import SessionShipper
from zerg.services.shipper import ShipperConfig
from zerg.services.shipper.parser import extract_session_metadata
from zerg.services.shipper.parser import parse_session_file
from zerg.services.shipper.spool import OfflineSpool
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
        """Parser extracts all 5 meaningful events from a realistic session."""
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

    def test_raw_json_dedup_across_events(self, tmp_path: Path):
        """Only first event per JSONL line carries raw_json (v2 fix)."""
        session_file = _write_realistic_session(tmp_path)
        events = list(parse_session_file(session_file))

        # Line 1 (user msg) → 1 event → should have raw_line
        assert events[0].raw_line != ""
        ingest_0 = events[0].to_event_ingest(str(session_file))
        assert ingest_0["raw_json"] is not None

        # Line 2 (assistant with text + tool_use) → 2 events
        # Only the first should have raw_line
        assert events[1].raw_line != ""  # first event from line 2
        assert events[2].raw_line == ""  # second event from line 2
        ingest_1 = events[1].to_event_ingest(str(session_file))
        ingest_2 = events[2].to_event_ingest(str(session_file))
        assert ingest_1["raw_json"] is not None
        assert ingest_2["raw_json"] is None

        # Line 3 (tool result) → 1 event → should have raw_line
        assert events[3].raw_line != ""

        # Line 4 (assistant text) → 1 event → should have raw_line
        assert events[4].raw_line != ""

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
        state = ShipperState(db_path=tmp_path / "shipper-state.db")
        spool = OfflineSpool(db_path=tmp_path / "shipper-spool.db")
        shipper = SessionShipper(config=config, state=state, spool=spool)

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

        # Verify raw_json dedup: not all events should have raw_json
        events_with_raw = [e for e in captured_payload["events"] if e.get("raw_json")]
        events_without_raw = [e for e in captured_payload["events"] if not e.get("raw_json")]
        # 4 JSONL lines, but line 2 produces 2 events. Only 4 should have raw_json.
        assert len(events_with_raw) == 4
        assert len(events_without_raw) == 1

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

    @pytest.mark.asyncio
    async def test_state_persists_in_sqlite(self, tmp_path: Path):
        """State persists across ShipperState instances (SQLite, not JSON)."""
        session_file = _write_realistic_session(tmp_path)
        db_path = tmp_path / "shipper-state.db"

        # Create state, set offset
        state1 = ShipperState(db_path=db_path)
        state1.set_offset(str(session_file), 1000, "session-abc", "provider-xyz")

        # Create new instance pointing to same DB — should see the data
        state2 = ShipperState(db_path=db_path)
        assert state2.get_offset(str(session_file)) == 1000
        session = state2.get_session(str(session_file))
        assert session is not None
        assert session.session_id == "session-abc"

    @pytest.mark.asyncio
    async def test_failed_ship_replay_cycle(self, tmp_path: Path):
        """Failed ship → pointer spool → replay cycle works."""
        session_file = _write_realistic_session(tmp_path)
        claude_dir = tmp_path / ".claude"

        config = ShipperConfig(
            api_url="http://localhost:47300",
            claude_config_dir=claude_dir,
        )
        state = ShipperState(db_path=tmp_path / "state.db")
        spool = OfflineSpool(db_path=tmp_path / "spool.db")
        shipper = SessionShipper(config=config, state=state, spool=spool)

        # Phase 1: Ship fails → pointer spooled
        async def fail_post(payload):
            raise httpx.ConnectError("Connection refused")

        shipper._post_ingest = fail_post
        result1 = await shipper.scan_and_ship()

        assert result1.events_spooled == 5
        assert spool.pending_count() == 1

        # Verify spool stores pointer, not payload
        entries = spool.dequeue_batch()
        assert len(entries) == 1
        entry = entries[0]
        assert entry.file_path == str(session_file)
        assert entry.start_offset == 0
        assert entry.end_offset > 0

        # Phase 2: Replay succeeds
        replay_payload = None

        async def succeed_post(payload):
            nonlocal replay_payload
            replay_payload = payload
            return {
                "session_id": payload["id"],
                "events_inserted": len(payload["events"]),
                "events_skipped": 0,
            }

        shipper._post_ingest = succeed_post
        result2 = await shipper.replay_spool()

        assert result2["replayed"] == 1
        assert result2["remaining"] == 0

        # Verify the replayed payload has the right events
        assert replay_payload is not None
        assert len(replay_payload["events"]) == 5
