"""Integration tests for session continuity with real Life Hub API.

These tests require LIFE_HUB_API_KEY to be set and hit the actual Life Hub
production API. They verify the full end-to-end flow works, not just mocked units.

Run with: uv run pytest tests/test_session_continuity_integration.py -v

Skip in CI without credentials:
  pytest ... -m "not integration"
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# Skip entire module if LIFE_HUB_API_KEY not set
pytestmark = pytest.mark.skipif(
    not os.getenv("LIFE_HUB_API_KEY"),
    reason="LIFE_HUB_API_KEY not set - skipping integration tests",
)


class TestPrepareSessionIntegration:
    """Integration tests for prepare_session_for_resume against real Life Hub."""

    @pytest.mark.asyncio
    async def test_fetch_real_session_from_life_hub(self):
        """Fetch a real session from Life Hub and verify the response structure."""
        from zerg.services.session_continuity import fetch_session_from_life_hub

        # Get a recent session ID from Life Hub
        session_id = await self._get_recent_session_id()
        if not session_id:
            pytest.skip("No sessions available in Life Hub for testing")

        # Fetch the session
        content, cwd, provider_session_id = await fetch_session_from_life_hub(session_id)

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
        assert "type" in first_event or "message" in first_event, (
            f"First event missing expected fields: {first_event.keys()}"
        )

        print(f"\n✓ Fetched real session from Life Hub")
        print(f"  Life Hub session ID: {session_id}")
        print(f"  Provider session ID: {provider_session_id}")
        print(f"  CWD: {cwd}")
        print(f"  Content size: {len(content):,} bytes")
        print(f"  Event count: {len(lines)}")

    @pytest.mark.asyncio
    async def test_prepare_session_creates_correct_file(self):
        """Full E2E: fetch from Life Hub and write to correct Claude Code path."""
        from zerg.services.session_continuity import (
            encode_cwd_for_claude,
            prepare_session_for_resume,
        )

        session_id = await self._get_recent_session_id()
        if not session_id:
            pytest.skip("No sessions available in Life Hub for testing")

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
        from zerg.services.session_continuity import fetch_session_from_life_hub

        with pytest.raises(ValueError, match="not found in Life Hub"):
            await fetch_session_from_life_hub("00000000-0000-0000-0000-000000000000")

    @pytest.mark.asyncio
    async def test_invalid_session_id_format(self):
        """Verify proper error handling for invalid session ID format."""
        from zerg.services.session_continuity import fetch_session_from_life_hub

        # Life Hub should return 400 or 404 for invalid UUID format
        with pytest.raises((ValueError, Exception)):
            await fetch_session_from_life_hub("not-a-valid-uuid")

    async def _get_recent_session_id(self) -> str | None:
        """Helper to get a recent session ID from Life Hub for testing.

        Filters out tiny sessions (< 10 events) to avoid picking up test data.
        """
        import httpx

        api_key = os.getenv("LIFE_HUB_API_KEY")
        url = os.getenv("LIFE_HUB_URL", "https://data.drose.io")

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"{url}/query/fiches/sessions",
                headers={"X-API-Key": api_key},
                params={"limit": 10, "provider": "claude"},  # Get more to filter
            )
            if response.status_code != 200:
                return None

            data = response.json()
            sessions = data.get("data", [])

            # Filter to real sessions (not test data)
            for session in sessions:
                if session.get("events_total", 0) >= 10:
                    return session.get("id")

            return None


class TestShipSessionIntegration:
    """Integration tests for shipping sessions back to Life Hub."""

    @pytest.mark.asyncio
    async def test_ship_empty_workspace_returns_none(self):
        """Shipping from workspace with no sessions returns None gracefully."""
        from zerg.services.session_continuity import ship_session_to_life_hub

        with tempfile.TemporaryDirectory() as workspace:
            with tempfile.TemporaryDirectory() as config_dir:
                result = await ship_session_to_life_hub(
                    workspace_path=Path(workspace),
                    commis_id="test-commis",
                    claude_config_dir=Path(config_dir),
                )
                assert result is None

    @pytest.mark.asyncio
    async def test_ship_real_session_to_life_hub(self):
        """Ship a test session to Life Hub and verify it was ingested."""
        from zerg.services.session_continuity import (
            encode_cwd_for_claude,
            ship_session_to_life_hub,
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

                # Write minimal valid session data
                import json

                test_events = [
                    {"type": "user", "message": {"role": "user", "content": "test"}},
                    {"type": "assistant", "message": {"role": "assistant", "content": "ok"}},
                ]
                session_file.write_text("\n".join(json.dumps(e) for e in test_events))

                # Ship it
                result = await ship_session_to_life_hub(
                    workspace_path=workspace_path,
                    commis_id="integration-test-commis",
                    claude_config_dir=config_path,
                )

                # Should get back a Life Hub session ID
                assert result is not None, "Expected session ID from Life Hub"
                assert len(result) > 10, f"Session ID looks invalid: {result}"


class TestCommisJobProcessorIntegration:
    """Commis job processor integration is now tested in E2E.

    See: apps/zerg/e2e/tests/core/session-continuity.spec.ts

    These tests required:
    1. CommisJobProcessor running (available in E2E mode)
    2. Real Life Hub API (LIFE_HUB_API_KEY in CI secrets)
    3. Mock hatch CLI (creates session files without running real Claude Code)

    Run with: make test-e2e-core

    The E2E tests provide full coverage including:
    - Workspace commis execution with mock hatch
    - Session fetch from real Life Hub API
    - Graceful fallback when session not found
    """

    @pytest.mark.skip(reason="Covered by E2E: session-continuity.spec.ts::workspace commis with resume_session_id")
    @pytest.mark.asyncio
    async def test_commis_with_resume_session_id(self, db_session):
        """Test that passing resume_session_id to a commis prepares the session.

        E2E test sends a chat message with a real Life Hub session ID,
        and verifies the workspace commis completes successfully.
        """
        pass

    @pytest.mark.skip(reason="Covered by E2E: session-continuity.spec.ts::workspace commis executes with mock hatch")
    @pytest.mark.asyncio
    async def test_successful_commis_ships_session(self, db_session):
        """Test that successful commis completion ships session to Life Hub.

        E2E test triggers workspace commis, verifies commis_complete event,
        and can query Life Hub to verify session was shipped.
        """
        pass


class TestEndToEndResumeFlow:
    """Full end-to-end test of the resume flow.

    This tests the complete scenario:
    1. Get a real session from Life Hub
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
        session_id = await self._get_recent_session_id()
        if not session_id:
            pytest.skip("No sessions available in Life Hub for testing")

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

    async def _get_recent_session_id(self) -> str | None:
        """Helper to get a recent session ID from Life Hub for testing.

        Filters out tiny sessions (< 10 events) to avoid picking up test data.
        """
        import httpx

        api_key = os.getenv("LIFE_HUB_API_KEY")
        url = os.getenv("LIFE_HUB_URL", "https://data.drose.io")

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"{url}/query/fiches/sessions",
                headers={"X-API-Key": api_key},
                params={"limit": 10, "provider": "claude"},  # Get more to filter
            )
            if response.status_code != 200:
                return None

            data = response.json()
            sessions = data.get("data", [])

            # Filter to real sessions (not test data)
            for session in sessions:
                if session.get("events_total", 0) >= 10:
                    return session.get("id")

            return None
