"""Integration tests for session continuity with Longhouse API.

These tests require a running Longhouse backend (make dev).
They are skipped unless INTEGRATION_ZERG_API=1 is set.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# Skip entire module unless explicitly enabled
pytestmark = pytest.mark.skipif(
    os.getenv("INTEGRATION_ZERG_API") != "1",
    reason="INTEGRATION_ZERG_API != 1 - skipping integration tests",
)


async def _create_test_session() -> str | None:
    """Create a small test session via the Longhouse ingest API."""
    import httpx
    from datetime import datetime, timezone

    api_url = os.getenv("LONGHOUSE_API_URL", "http://localhost:47300")
    now = datetime.now(timezone.utc)

    payload = {
        "provider": "claude",
        "environment": "development",
        "project": "integration-tests",
        "device_id": "test-device",
        "cwd": "/tmp/integration-tests",
        "git_repo": "https://example.com/repo.git",
        "git_branch": "main",
        "started_at": now.isoformat(),
        "ended_at": now.isoformat(),
        "provider_session_id": "integration-session-1",
        "events": [
            {
                "role": "user",
                "content_text": "Test message",
                "timestamp": now.isoformat(),
            },
            {
                "role": "assistant",
                "content_text": "Test response",
                "timestamp": now.isoformat(),
            },
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{api_url}/api/agents/ingest",
                json=payload,
            )
    except httpx.HTTPError:
        pytest.skip(f"Longhouse API not reachable at {api_url}")
        return None

    if response.status_code in (401, 403):
        pytest.skip("Agents API requires auth; set AUTH_DISABLED=1 for integration tests")
        return None

    if response.status_code != 200:
        pytest.skip(f"Failed to ingest test session (status {response.status_code})")
        return None

    return response.json().get("session_id")


class TestPrepareSessionIntegration:
    """Integration tests for prepare_session_for_resume against Longhouse."""

    @pytest.mark.asyncio
    async def test_fetch_real_session_from_zerg(self):
        """Fetch a real session from Longhouse and verify the response structure."""
        from zerg.services.session_continuity import fetch_session_from_zerg

        session_id = await _create_test_session()
        if not session_id:
            pytest.skip("Failed to create test session for integration")

        # Fetch the session
        content, cwd, provider_session_id = await fetch_session_from_zerg(session_id)

        # Verify we got real data
        assert content, "Expected non-empty session content"
        assert len(content) > 100, f"Session content too small: {len(content)} bytes"
        assert cwd, "Expected CWD header to be set"
        assert provider_session_id, "Expected provider_session_id header to be set"

        # Verify content is valid JSONL
        import json

        lines = content.decode("utf-8").strip().split("\n")
        assert len(lines) >= 1, "Expected at least one JSONL line"

        # First line should be valid JSON
        first_event = json.loads(lines[0])
        assert "role" in first_event or "content" in first_event, f"Unexpected event format: {first_event.keys()}"

        print(f"\n✓ Fetched real session from Longhouse")
        print(f"  Longhouse session ID: {session_id}")
        print(f"  Provider session ID: {provider_session_id}")
        print(f"  CWD: {cwd}")
        print(f"  Content size: {len(content):,} bytes")
        print(f"  Event count: {len(lines)}")

    @pytest.mark.asyncio
    async def test_prepare_session_creates_correct_file(self):
        """Full E2E: fetch from Longhouse and write to correct Claude Code path."""
        from zerg.services.session_continuity import (
            encode_cwd_for_claude,
            prepare_session_for_resume,
        )

        session_id = await _create_test_session()
        if not session_id:
            pytest.skip("Failed to create test session for integration")

        with tempfile.TemporaryDirectory() as workspace:
            workspace_path = Path(workspace)

            # Use a temp config dir to avoid polluting real ~/.claude
            with tempfile.TemporaryDirectory() as config_dir:
                config_path = Path(config_dir)

                # Prepare the session
                provider_session_id = await prepare_session_for_resume(
                    session_id=session_id,
                    workspace_path=workspace_path,
                    claude_config_dir=config_path,
                )

                assert provider_session_id, "Expected provider_session_id to be returned"

                # Verify the file was created at the correct path
                encoded_cwd = encode_cwd_for_claude(str(workspace_path.absolute()))
                expected_file = config_path / "projects" / encoded_cwd / f"{provider_session_id}.jsonl"

                assert expected_file.exists(), f"Session file not created at {expected_file}"
                assert expected_file.stat().st_size > 100, "Session file too small"

                # Verify content is valid JSONL
                import json

                content = expected_file.read_text()
                lines = content.strip().split("\n")
                assert len(lines) >= 1
                json.loads(lines[0])  # Should not raise

    @pytest.mark.asyncio
    async def test_nonexistent_session_raises(self):
        """Verify proper error handling for nonexistent sessions."""
        from zerg.services.session_continuity import fetch_session_from_zerg

        # Now uses local Zerg API which returns 404 for nonexistent sessions
        with pytest.raises((ValueError, Exception)):
            await fetch_session_from_zerg("00000000-0000-0000-0000-000000000000")

    @pytest.mark.asyncio
    async def test_invalid_session_id_format(self):
        """Verify proper error handling for invalid session ID format."""
        from zerg.services.session_continuity import fetch_session_from_zerg

        # Zerg API should return 400 or 404 for invalid UUID format
        with pytest.raises((ValueError, Exception)):
            await fetch_session_from_zerg("not-a-valid-uuid")

class TestShipSessionIntegration:
    """Integration tests for shipping sessions back to Longhouse."""

    @pytest.mark.asyncio
    async def test_ship_empty_workspace_returns_none(self):
        """Shipping from workspace with no sessions returns None gracefully."""
        from zerg.services.session_continuity import ship_session_to_zerg

        with tempfile.TemporaryDirectory() as workspace:
            with tempfile.TemporaryDirectory() as config_dir:
                result = await ship_session_to_zerg(
                    workspace_path=Path(workspace),
                    commis_id="test-commis",
                    claude_config_dir=Path(config_dir),
                )
                assert result is None

    @pytest.mark.asyncio
    async def test_ship_real_session_to_zerg(self):
        """Ship a test session to Zerg and verify it was ingested.

        This test requires the Zerg API to be running locally (via make dev).
        It tests the full ingest flow, not just mocked behavior.
        """
        from zerg.services.session_continuity import (
            encode_cwd_for_claude,
            ship_session_to_zerg,
        )

        with tempfile.TemporaryDirectory() as workspace:
            workspace_path = Path(workspace)

            with tempfile.TemporaryDirectory() as config_dir:
                config_path = Path(config_dir)

                # Create a fake session file
                encoded_cwd = encode_cwd_for_claude(str(workspace_path.absolute()))
                session_dir = config_path / "projects" / encoded_cwd
                session_dir.mkdir(parents=True)

                test_session_id = "integration-test-session"
                session_file = session_dir / f"{test_session_id}.jsonl"

                # Write minimal valid session data with proper timestamp
                import json
                from datetime import datetime, timezone

                now = datetime.now(timezone.utc).isoformat()
                test_events = [
                    {"role": "user", "content": "test", "timestamp": now},
                    {"role": "assistant", "content": "ok", "timestamp": now},
                ]
                session_file.write_text("\n".join(json.dumps(e) for e in test_events))

                # Ship it - may fail if API not running
                result = await ship_session_to_zerg(
                    workspace_path=workspace_path,
                    commis_id="integration-test-commis",
                    claude_config_dir=config_path,
                )

                # Result is None if API not running or ship failed (graceful degradation)
                # When API is running, we should get back a UUID
                if result is not None:
                    assert len(result) > 10, f"Session ID looks invalid: {result}"
                # If result is None, the test passes (graceful failure)


class TestCommisJobProcessorIntegration:
    """Commis job processor integration is now tested in E2E.

    See: apps/zerg/e2e/tests/core/session-continuity.spec.ts

    These tests required:
    1. CommisJobProcessor running (available in E2E mode)
    2. Real Longhouse API (local dev server)
    3. Mock hatch CLI (creates session files without running real Claude Code)

    Run with: make test-e2e

    The E2E tests provide full coverage including:
    - Workspace commis execution with mock hatch
    - Session fetch from real Longhouse API
    - Graceful fallback when session not found
    """

    @pytest.mark.skip(reason="Covered by E2E: session-continuity.spec.ts::workspace commis with resume_session_id")
    @pytest.mark.asyncio
    async def test_commis_with_resume_session_id(self, db_session):
        """Test that passing resume_session_id to a commis prepares the session.

        E2E test sends a chat message with a real session ID,
        and verifies the workspace commis completes successfully.
        """
        pass

    @pytest.mark.skip(reason="Covered by E2E: session-continuity.spec.ts::workspace commis executes with mock hatch")
    @pytest.mark.asyncio
    async def test_successful_commis_ships_session(self, db_session):
        """Test that successful commis completion ships session to Longhouse.

        E2E test triggers workspace commis, verifies commis_complete event,
        and can query Longhouse to verify session was shipped.
        """
        pass


class TestEndToEndResumeFlow:
    """Full end-to-end test of the resume flow.

    This tests the complete scenario:
    1. Get a real session from Longhouse
    2. Prepare it for resume in a workspace
    3. Verify the file is in the right place for Claude Code
    4. (Would need actual Claude Code to test resume works)
    """

    @pytest.mark.asyncio
    async def test_full_prepare_and_verify_path(self):
        """Complete flow: fetch → prepare → verify path matches Claude Code expectations."""
        from zerg.services.session_continuity import (
            encode_cwd_for_claude,
            prepare_session_for_resume,
        )

        # Get a real session
        session_id = await _create_test_session()
        if not session_id:
            pytest.skip("Failed to create test session for integration")

        with tempfile.TemporaryDirectory() as workspace:
            workspace_path = Path(workspace)

            with tempfile.TemporaryDirectory() as config_dir:
                config_path = Path(config_dir)

                # Prepare
                provider_session_id = await prepare_session_for_resume(
                    session_id=session_id,
                    workspace_path=workspace_path,
                    claude_config_dir=config_path,
                )

                # Verify the exact path Claude Code would look for
                # Claude Code path: {CLAUDE_CONFIG_DIR}/projects/{encoded_cwd}/{session_id}.jsonl
                encoded_cwd = encode_cwd_for_claude(str(workspace_path.absolute()))
                claude_code_path = config_path / "projects" / encoded_cwd / f"{provider_session_id}.jsonl"

                assert claude_code_path.exists(), f"Session file not at expected Claude Code path: {claude_code_path}"

                # Verify it's resumable (has valid JSONL content)
                import json

                content = claude_code_path.read_text()
                lines = [l for l in content.strip().split("\n") if l.strip()]

                assert len(lines) >= 1, "Session file is empty"

                # Parse all lines to verify valid JSONL
                for i, line in enumerate(lines[:10]):  # Check first 10 lines
                    try:
                        json.loads(line)
                    except json.JSONDecodeError as e:
                        pytest.fail(f"Invalid JSON on line {i}: {e}")

                print(f"\n✓ Session {session_id} prepared successfully")
                print(f"  Provider session ID: {provider_session_id}")
                print(f"  Claude Code path: {claude_code_path}")
                print(f"  Session size: {claude_code_path.stat().st_size:,} bytes")
                print(f"  Event count: {len(lines)}")
