"""Tests for session continuity service.

This module tests the cross-environment Claude Code session resumption
functionality, including path encoding, validation, and Life Hub integration.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest


class TestEncodeCwdForClaude:
    """Tests for encode_cwd_for_claude function."""

    def test_encodes_simple_path(self):
        """Basic path encoding replaces slashes with dashes."""
        from zerg.services.session_continuity import encode_cwd_for_claude

        result = encode_cwd_for_claude("/Users/test/git/project")
        assert result == "-Users-test-git-project"

    def test_encodes_path_with_special_chars(self):
        """Special characters are replaced with dashes."""
        from zerg.services.session_continuity import encode_cwd_for_claude

        result = encode_cwd_for_claude("/Users/test/My Project (v2)")
        assert result == "-Users-test-My-Project--v2-"

    def test_preserves_alphanumeric_and_dash(self):
        """Alphanumeric characters and dashes are preserved."""
        from zerg.services.session_continuity import encode_cwd_for_claude

        result = encode_cwd_for_claude("/abc-123-DEF")
        assert result == "-abc-123-DEF"

    def test_encodes_spaces_as_dashes(self):
        """Spaces are replaced with dashes."""
        from zerg.services.session_continuity import encode_cwd_for_claude

        result = encode_cwd_for_claude("/path with spaces/here")
        assert result == "-path-with-spaces-here"

    def test_encodes_dots_as_dashes(self):
        """Dots (except in extensions) are replaced with dashes."""
        from zerg.services.session_continuity import encode_cwd_for_claude

        result = encode_cwd_for_claude("/home/user/.config/claude")
        assert result == "-home-user--config-claude"


class TestValidateSessionId:
    """Tests for validate_session_id function - security critical."""

    def test_validates_simple_uuid(self):
        """Valid UUID-style session IDs pass validation."""
        from zerg.services.session_continuity import validate_session_id

        # Should not raise
        validate_session_id("550e8400-e29b-41d4-a716-446655440000")

    def test_validates_alphanumeric_with_dashes(self):
        """Session IDs with alphanumeric and dashes pass."""
        from zerg.services.session_continuity import validate_session_id

        validate_session_id("abc-123-def")
        validate_session_id("ABC_123_DEF")
        validate_session_id("a1b2c3")

    def test_rejects_empty_string(self):
        """Empty session ID raises ValueError."""
        from zerg.services.session_continuity import validate_session_id

        with pytest.raises(ValueError, match="cannot be empty"):
            validate_session_id("")

    def test_rejects_path_traversal_dots(self):
        """Path traversal with .. is rejected."""
        from zerg.services.session_continuity import validate_session_id

        # Pattern check fails first due to dots/slashes
        with pytest.raises(ValueError, match="Invalid session ID format"):
            validate_session_id("../../../etc/passwd")

    def test_rejects_forward_slash(self):
        """Forward slashes are rejected."""
        from zerg.services.session_continuity import validate_session_id

        with pytest.raises(ValueError, match="Invalid session ID format"):
            validate_session_id("session/id")

    def test_rejects_backslash(self):
        """Backslashes are rejected."""
        from zerg.services.session_continuity import validate_session_id

        # Pattern check fails first due to backslash
        with pytest.raises(ValueError, match="Invalid session ID format"):
            validate_session_id("session\\id")

    def test_rejects_special_characters(self):
        """Special characters that aren't alphanumeric/dash/underscore are rejected."""
        from zerg.services.session_continuity import validate_session_id

        for char in ["!", "@", "#", "$", "%", "^", "&", "*", "(", ")", " ", ":", ";"]:
            with pytest.raises(ValueError, match="Invalid session ID format"):
                validate_session_id(f"session{char}id")


class TestGetClaudeConfigDir:
    """Tests for get_claude_config_dir function."""

    def test_returns_default_when_no_env_var(self, monkeypatch):
        """Returns ~/.claude when CLAUDE_CONFIG_DIR is not set."""
        from zerg.services.session_continuity import get_claude_config_dir

        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        result = get_claude_config_dir()
        assert result == Path.home() / ".claude"

    def test_respects_claude_config_dir_env_var(self, monkeypatch):
        """Returns path from CLAUDE_CONFIG_DIR when set."""
        from zerg.services.session_continuity import get_claude_config_dir

        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/custom/claude/config")
        result = get_claude_config_dir()
        assert result == Path("/custom/claude/config")

    def test_respects_empty_env_var_as_default(self, monkeypatch):
        """Empty CLAUDE_CONFIG_DIR falls back to default."""
        from zerg.services.session_continuity import get_claude_config_dir

        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "")
        result = get_claude_config_dir()
        assert result == Path.home() / ".claude"


class TestFetchSessionFromLifeHub:
    """Tests for fetch_session_from_life_hub function."""

    @pytest.mark.asyncio
    async def test_raises_without_api_key(self, monkeypatch):
        """Raises ValueError when LIFE_HUB_API_KEY is not configured."""
        from zerg.services.session_continuity import fetch_session_from_life_hub

        monkeypatch.setattr("zerg.services.session_continuity.LIFE_HUB_API_KEY", None)

        with pytest.raises(ValueError, match="LIFE_HUB_API_KEY not configured"):
            await fetch_session_from_life_hub("test-session-id")

    @pytest.mark.asyncio
    async def test_raises_on_404(self, monkeypatch):
        """Raises ValueError when session not found."""
        from zerg.services.session_continuity import fetch_session_from_life_hub

        monkeypatch.setattr("zerg.services.session_continuity.LIFE_HUB_API_KEY", "test-key")

        mock_response = MagicMock()
        mock_response.status_code = 404

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None

        with patch("httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(ValueError, match="not found in Life Hub"):
                await fetch_session_from_life_hub("nonexistent-session")

    @pytest.mark.asyncio
    async def test_validates_provider_session_id_from_response(self, monkeypatch):
        """Validates provider_session_id from response headers to prevent path traversal."""
        from zerg.services.session_continuity import fetch_session_from_life_hub

        monkeypatch.setattr("zerg.services.session_continuity.LIFE_HUB_API_KEY", "test-key")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b'{"test": "data"}'
        mock_response.headers = {
            "X-Session-CWD": "/test/path",
            "X-Provider-Session-ID": "../../../etc/passwd",  # Malicious!
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None

        with patch("httpx.AsyncClient", return_value=mock_client):
            # Pattern check fails first due to dots/slashes
            with pytest.raises(ValueError, match="Invalid session ID format"):
                await fetch_session_from_life_hub("test-session")

    @pytest.mark.asyncio
    async def test_returns_session_data_on_success(self, monkeypatch):
        """Returns tuple of (content, cwd, provider_session_id) on success."""
        from zerg.services.session_continuity import fetch_session_from_life_hub

        monkeypatch.setattr("zerg.services.session_continuity.LIFE_HUB_API_KEY", "test-key")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b'{"event": "test"}\n'
        mock_response.headers = {
            "X-Session-CWD": "/Users/test/project",
            "X-Provider-Session-ID": "abc123-def456",
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None

        with patch("httpx.AsyncClient", return_value=mock_client):
            content, cwd, provider_id = await fetch_session_from_life_hub("test-session")

        assert content == b'{"event": "test"}\n'
        assert cwd == "/Users/test/project"
        assert provider_id == "abc123-def456"


class TestPrepareSessionForResume:
    """Tests for prepare_session_for_resume function."""

    @pytest.mark.asyncio
    async def test_raises_without_provider_session_id(self, monkeypatch, tmp_path):
        """Raises ValueError when session has no provider_session_id."""
        from zerg.services.session_continuity import prepare_session_for_resume

        monkeypatch.setattr("zerg.services.session_continuity.LIFE_HUB_API_KEY", "test-key")

        # Mock fetch to return empty provider_session_id
        async def mock_fetch(*args, **kwargs):
            return (b'{"test": "data"}', "/test/path", "")

        with patch("zerg.services.session_continuity.fetch_session_from_life_hub", mock_fetch):
            with pytest.raises(ValueError, match="no provider_session_id"):
                await prepare_session_for_resume("test-session", tmp_path)

    @pytest.mark.asyncio
    async def test_creates_session_file_in_correct_location(self, monkeypatch, tmp_path):
        """Creates session file at {config_dir}/projects/{encoded_cwd}/{session_id}.jsonl."""
        from zerg.services.session_continuity import prepare_session_for_resume

        monkeypatch.setattr("zerg.services.session_continuity.LIFE_HUB_API_KEY", "test-key")

        session_content = b'{"event": "init"}\n{"event": "user_message"}\n'
        provider_session_id = "abc123-session"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        config_dir = tmp_path / "claude_config"

        async def mock_fetch(*args, **kwargs):
            return (session_content, str(workspace), provider_session_id)

        with patch("zerg.services.session_continuity.fetch_session_from_life_hub", mock_fetch):
            result = await prepare_session_for_resume(
                "test-session",
                workspace,
                claude_config_dir=config_dir,
            )

        assert result == provider_session_id

        # Verify file was created
        from zerg.services.session_continuity import encode_cwd_for_claude

        encoded_cwd = encode_cwd_for_claude(str(workspace.absolute()))
        expected_file = config_dir / "projects" / encoded_cwd / f"{provider_session_id}.jsonl"
        assert expected_file.exists()
        assert expected_file.read_bytes() == session_content

    @pytest.mark.asyncio
    async def test_validates_provider_session_id_defense_in_depth(self, monkeypatch, tmp_path):
        """Double-validates provider_session_id even after fetch validation."""
        from zerg.services.session_continuity import prepare_session_for_resume

        monkeypatch.setattr("zerg.services.session_continuity.LIFE_HUB_API_KEY", "test-key")

        # Mock fetch to return a malicious session ID (simulating a bypassed validation)
        async def mock_fetch(*args, **kwargs):
            return (b'{"test": "data"}', "/test/path", "../../malicious")

        with patch("zerg.services.session_continuity.fetch_session_from_life_hub", mock_fetch):
            # Pattern check fails first due to dots/slashes
            with pytest.raises(ValueError, match="Invalid session ID format"):
                await prepare_session_for_resume("test-session", tmp_path)


class TestShipSessionToLifeHub:
    """Tests for ship_session_to_life_hub function."""

    @pytest.mark.asyncio
    async def test_returns_none_without_api_key(self, monkeypatch, tmp_path):
        """Returns None when LIFE_HUB_API_KEY is not configured."""
        from zerg.services.session_continuity import ship_session_to_life_hub

        monkeypatch.setattr("zerg.services.session_continuity.LIFE_HUB_API_KEY", None)

        result = await ship_session_to_life_hub(tmp_path, "worker-1")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_sessions_found(self, monkeypatch, tmp_path):
        """Returns None when no session files exist for workspace."""
        from zerg.services.session_continuity import ship_session_to_life_hub

        monkeypatch.setattr("zerg.services.session_continuity.LIFE_HUB_API_KEY", "test-key")

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        config_dir = tmp_path / "claude_config"
        config_dir.mkdir()

        result = await ship_session_to_life_hub(workspace, "worker-1", claude_config_dir=config_dir)
        assert result is None

    @pytest.mark.asyncio
    async def test_ships_most_recent_session(self, monkeypatch, tmp_path):
        """Ships the most recently modified session file."""
        from zerg.services.session_continuity import encode_cwd_for_claude
        from zerg.services.session_continuity import ship_session_to_life_hub

        monkeypatch.setattr("zerg.services.session_continuity.LIFE_HUB_API_KEY", "test-key")

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        config_dir = tmp_path / "claude_config"

        # Create session directory
        encoded_cwd = encode_cwd_for_claude(str(workspace.absolute()))
        session_dir = config_dir / "projects" / encoded_cwd
        session_dir.mkdir(parents=True)

        # Create two session files
        old_session = session_dir / "old-session.jsonl"
        old_session.write_text('{"event": "old"}\n')

        import time

        time.sleep(0.1)  # Ensure different mtime

        new_session = session_dir / "new-session.jsonl"
        new_session.write_text('{"event": "new"}\n')

        # Mock the HTTP client
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"session_id": "life-hub-session-123"}
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await ship_session_to_life_hub(workspace, "worker-1", claude_config_dir=config_dir)

        assert result == "life-hub-session-123"

        # Verify the correct session was shipped
        call_args = mock_client.post.call_args
        payload = call_args.kwargs["json"]
        assert payload["provider_session_id"] == "new-session"

    @pytest.mark.asyncio
    async def test_handles_shipping_failure_gracefully(self, monkeypatch, tmp_path):
        """Returns None on network failure without raising."""
        from zerg.services.session_continuity import encode_cwd_for_claude
        from zerg.services.session_continuity import ship_session_to_life_hub

        monkeypatch.setattr("zerg.services.session_continuity.LIFE_HUB_API_KEY", "test-key")

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        config_dir = tmp_path / "claude_config"

        # Create session directory and file
        encoded_cwd = encode_cwd_for_claude(str(workspace.absolute()))
        session_dir = config_dir / "projects" / encoded_cwd
        session_dir.mkdir(parents=True)
        (session_dir / "test-session.jsonl").write_text('{"event": "test"}\n')

        # Mock the HTTP client to raise an exception
        mock_client = AsyncMock()
        mock_client.post.side_effect = Exception("Network error")
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await ship_session_to_life_hub(workspace, "worker-1", claude_config_dir=config_dir)

        # Should return None, not raise
        assert result is None


class TestSessionContinuityIntegration:
    """Integration tests for full session continuity flow."""

    @pytest.mark.asyncio
    async def test_round_trip_prepare_and_ship(self, monkeypatch, tmp_path):
        """Session can be prepared for resume and shipped after completion."""
        from zerg.services.session_continuity import encode_cwd_for_claude
        from zerg.services.session_continuity import prepare_session_for_resume
        from zerg.services.session_continuity import ship_session_to_life_hub

        monkeypatch.setattr("zerg.services.session_continuity.LIFE_HUB_API_KEY", "test-key")

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        config_dir = tmp_path / "claude_config"

        original_session_content = b'{"event": "init"}\n{"event": "user_message"}\n'
        original_provider_id = "original-session-123"

        # Mock fetch for prepare
        async def mock_fetch(*args, **kwargs):
            return (original_session_content, str(workspace), original_provider_id)

        # Prepare the session
        with patch("zerg.services.session_continuity.fetch_session_from_life_hub", mock_fetch):
            result_id = await prepare_session_for_resume(
                "life-hub-session-id",
                workspace,
                claude_config_dir=config_dir,
            )

        assert result_id == original_provider_id

        # Verify session file exists
        encoded_cwd = encode_cwd_for_claude(str(workspace.absolute()))
        session_file = config_dir / "projects" / encoded_cwd / f"{original_provider_id}.jsonl"
        assert session_file.exists()

        # Simulate Claude Code modifying the session
        with open(session_file, "ab") as f:
            f.write(b'{"event": "assistant_response"}\n')

        # Now ship the session back
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"session_id": "shipped-session-456"}
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None

        with patch("httpx.AsyncClient", return_value=mock_client):
            shipped_id = await ship_session_to_life_hub(workspace, "worker-1", claude_config_dir=config_dir)

        assert shipped_id == "shipped-session-456"

        # Verify the shipped payload contains the modified session
        call_args = mock_client.post.call_args
        payload = call_args.kwargs["json"]
        assert payload["provider_session_id"] == original_provider_id
        assert len(payload["events"]) == 3  # Original 2 + 1 new
