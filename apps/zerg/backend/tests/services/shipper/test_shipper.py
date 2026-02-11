"""Tests for the session shipper."""

import json
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from zerg.services.shipper import SessionShipper
from zerg.services.shipper import ShipperConfig
from zerg.services.shipper.state import ShipperState


@pytest.fixture
def mock_projects_dir(tmp_path: Path) -> Path:
    """Create a mock projects directory structure."""
    projects_dir = tmp_path / ".claude" / "projects"
    projects_dir.mkdir(parents=True)

    # Create a project directory
    project_dir = projects_dir / "-Users-test-project"
    project_dir.mkdir()

    # Create a session file
    session_file = project_dir / "test-session-123.jsonl"
    session_file.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "msg-1",
                "timestamp": "2026-01-28T10:00:00Z",
                "cwd": "/Users/test/project",
                "gitBranch": "main",
                "message": {"content": "Hello Claude"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "assistant",
                "uuid": "msg-2",
                "timestamp": "2026-01-28T10:01:00Z",
                "message": {
                    "content": [{"type": "text", "text": "Hello! How can I help?"}],
                },
            }
        )
    )

    return tmp_path / ".claude"


@pytest.fixture
def shipper_config(mock_projects_dir: Path) -> ShipperConfig:
    """Create shipper config pointing to mock directory."""
    return ShipperConfig(
        api_url="http://test:47300",
        claude_config_dir=mock_projects_dir,
    )


@pytest.fixture
def shipper_state(tmp_path: Path) -> ShipperState:
    """Create shipper state with temp file."""
    return ShipperState(state_path=tmp_path / "shipper-state.json")


class TestShipperConfig:
    """Tests for ShipperConfig."""

    def test_default_config(self):
        """Default config uses ~/.claude."""
        config = ShipperConfig()
        assert config.api_url == "http://localhost:8080"
        assert config.scan_interval_seconds == 30
        assert config.batch_size == 100

    def test_projects_dir(self, mock_projects_dir: Path):
        """Projects dir is derived from claude_config_dir."""
        config = ShipperConfig(claude_config_dir=mock_projects_dir)
        assert config.projects_dir == mock_projects_dir / "projects"

    def test_config_propagates_to_state_and_spool(self, tmp_path: Path):
        """ShipperConfig's claude_config_dir propagates to state and spool."""
        config_dir = tmp_path / "custom-claude"
        config_dir.mkdir()
        (config_dir / "projects").mkdir()

        config = ShipperConfig(claude_config_dir=config_dir)
        shipper = SessionShipper(config=config)

        # State and spool should use the custom config dir
        assert shipper.state.state_path == config_dir / "zerg-shipper-state.json"
        assert shipper.spool.db_path == config_dir / "zerg-shipper-spool.db"


class TestSessionShipper:
    """Tests for SessionShipper."""

    def test_find_session_files(
        self,
        mock_projects_dir: Path,
        shipper_config: ShipperConfig,
        shipper_state: ShipperState,
    ):
        """Find session files in projects directory."""
        shipper = SessionShipper(config=shipper_config, state=shipper_state)
        files = shipper._find_session_files()

        assert len(files) == 1
        assert files[0].name == "test-session-123.jsonl"

    def test_has_new_content(
        self,
        mock_projects_dir: Path,
        shipper_config: ShipperConfig,
        shipper_state: ShipperState,
    ):
        """Check if file has new content."""
        shipper = SessionShipper(config=shipper_config, state=shipper_state)
        files = shipper._find_session_files()

        # New file should have new content
        assert shipper._has_new_content(files[0]) is True

        # After setting offset to file size, no new content
        shipper_state.set_offset(
            str(files[0]),
            files[0].stat().st_size,
            "session-id",
            "test-session-123",
        )
        assert shipper._has_new_content(files[0]) is False

    @pytest.mark.asyncio
    async def test_ship_session(
        self,
        mock_projects_dir: Path,
        shipper_config: ShipperConfig,
        shipper_state: ShipperState,
    ):
        """Ship a session to Zerg API."""
        shipper = SessionShipper(config=shipper_config, state=shipper_state)
        files = shipper._find_session_files()

        # Mock the API call
        mock_response = {
            "session_id": "zerg-session-abc",
            "events_inserted": 2,
            "events_skipped": 0,
            "session_created": True,
        }

        with patch.object(shipper, "_post_ingest", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            result = await shipper.ship_session(files[0])

        assert result["events_inserted"] == 2
        assert result["events_skipped"] == 0

        # Check that payload was correct
        call_args = mock_post.call_args[0][0]
        assert call_args["provider"] == "claude"
        assert call_args["cwd"] == "/Users/test/project"
        assert call_args["git_branch"] == "main"
        assert len(call_args["events"]) == 2
        # Verify provider_session_id is included (from filename)
        assert call_args["provider_session_id"] == "test-session-123"
        # Verify raw_json is included in events
        for event in call_args["events"]:
            assert "raw_json" in event
            assert event["raw_json"] is not None
            # Verify it's valid JSON
            import json

            parsed = json.loads(event["raw_json"])
            assert "type" in parsed

    @pytest.mark.asyncio
    async def test_scan_and_ship(
        self,
        mock_projects_dir: Path,
        shipper_config: ShipperConfig,
        shipper_state: ShipperState,
    ):
        """Scan and ship all sessions."""
        shipper = SessionShipper(config=shipper_config, state=shipper_state)

        mock_response = {
            "session_id": "zerg-session-abc",
            "events_inserted": 2,
            "events_skipped": 0,
            "session_created": True,
        }

        with patch.object(shipper, "_post_ingest", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            result = await shipper.scan_and_ship()

        assert result.sessions_scanned == 1
        assert result.sessions_shipped == 1
        assert result.events_shipped == 2
        assert result.errors == []

    @pytest.mark.asyncio
    async def test_incremental_ship(
        self,
        mock_projects_dir: Path,
        shipper_config: ShipperConfig,
        shipper_state: ShipperState,
    ):
        """Second ship only sends new events."""
        shipper = SessionShipper(config=shipper_config, state=shipper_state)
        files = shipper._find_session_files()

        mock_response = {
            "session_id": "zerg-session-abc",
            "events_inserted": 2,
            "events_skipped": 0,
            "session_created": True,
        }

        with patch.object(shipper, "_post_ingest", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response

            # First ship
            result1 = await shipper.scan_and_ship()
            assert result1.events_shipped == 2

            # Second ship - no new content
            result2 = await shipper.scan_and_ship()
            assert result2.sessions_shipped == 0
            assert result2.events_shipped == 0

        # Add new content
        with open(files[0], "a") as f:
            f.write(
                "\n"
                + json.dumps(
                    {
                        "type": "user",
                        "uuid": "msg-3",
                        "timestamp": "2026-01-28T10:02:00Z",
                        "message": {"content": "New message"},
                    }
                )
            )

        mock_response["events_inserted"] = 1

        with patch.object(shipper, "_post_ingest", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response

            # Third ship - only new event
            result3 = await shipper.scan_and_ship()
            assert result3.sessions_shipped == 1
            assert result3.events_shipped == 1

    @pytest.mark.asyncio
    async def test_ship_error_handling(
        self,
        mock_projects_dir: Path,
        shipper_config: ShipperConfig,
        shipper_state: ShipperState,
    ):
        """Handle API errors gracefully."""
        shipper = SessionShipper(config=shipper_config, state=shipper_state)

        with patch.object(shipper, "_post_ingest", new_callable=AsyncMock) as mock_post:
            mock_post.side_effect = Exception("Connection refused")
            result = await shipper.scan_and_ship()

        assert result.sessions_scanned == 1
        assert result.sessions_shipped == 0
        assert len(result.errors) == 1
        assert "Connection refused" in result.errors[0]

    @pytest.mark.asyncio
    async def test_api_token_in_headers(
        self,
        mock_projects_dir: Path,
        shipper_state: ShipperState,
    ):
        """API token is included in request headers when configured."""
        config = ShipperConfig(
            api_url="http://test:47300",
            claude_config_dir=mock_projects_dir,
            api_token="test-secret-token",
        )
        shipper = SessionShipper(config=config, state=shipper_state)

        # Mock httpx.AsyncClient to capture headers
        captured_headers = {}

        async def mock_post(url, content, headers):
            captured_headers.update(headers)
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "session_id": "zerg-session-abc",
                "events_inserted": 2,
                "events_skipped": 0,
            }
            return mock_resp

        mock_client = AsyncMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("zerg.services.shipper.shipper.httpx.AsyncClient", return_value=mock_client):
            await shipper.scan_and_ship()

        assert "X-Agents-Token" in captured_headers
        assert captured_headers["X-Agents-Token"] == "test-secret-token"

    @pytest.mark.asyncio
    async def test_no_token_when_not_configured(
        self,
        mock_projects_dir: Path,
        shipper_state: ShipperState,
    ):
        """No auth header when api_token is not configured."""
        config = ShipperConfig(
            api_url="http://test:47300",
            claude_config_dir=mock_projects_dir,
            api_token=None,  # Explicitly no token
        )
        shipper = SessionShipper(config=config, state=shipper_state)

        # Mock httpx.AsyncClient to capture headers
        captured_headers = {}

        async def mock_post(url, content, headers):
            captured_headers.update(headers)
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "session_id": "zerg-session-abc",
                "events_inserted": 2,
                "events_skipped": 0,
            }
            return mock_resp

        mock_client = AsyncMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("zerg.services.shipper.shipper.httpx.AsyncClient", return_value=mock_client):
            with patch.dict("os.environ", {}, clear=False):
                # Ensure AGENTS_API_TOKEN is not set in env for this test
                import os

                orig_token = os.environ.pop("AGENTS_API_TOKEN", None)
                try:
                    await shipper.scan_and_ship()
                finally:
                    if orig_token is not None:
                        os.environ["AGENTS_API_TOKEN"] = orig_token

        assert "X-Agents-Token" not in captured_headers


class TestGzipCompression:
    """Tests for gzip compression in shipper."""

    @pytest.mark.asyncio
    async def test_gzip_compression_enabled(
        self,
        mock_projects_dir: Path,
        shipper_state: ShipperState,
    ):
        """Payloads are gzip compressed when enable_gzip is True."""
        import gzip as gzip_module

        config = ShipperConfig(
            api_url="http://test:47300",
            claude_config_dir=mock_projects_dir,
            enable_gzip=True,
        )
        shipper = SessionShipper(config=config, state=shipper_state)

        captured_content = None
        captured_headers = {}

        async def mock_post(url, content, headers):
            nonlocal captured_content, captured_headers
            captured_content = content
            captured_headers = dict(headers)
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "session_id": "zerg-session-abc",
                "events_inserted": 2,
                "events_skipped": 0,
            }
            return mock_resp

        mock_client = AsyncMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("zerg.services.shipper.shipper.httpx.AsyncClient", return_value=mock_client):
            await shipper.scan_and_ship()

        # Verify Content-Encoding header is set
        assert captured_headers.get("Content-Encoding") == "gzip"

        # Verify content is gzip compressed (can be decompressed)
        assert captured_content is not None
        decompressed = gzip_module.decompress(captured_content)
        payload = json.loads(decompressed)
        assert "events" in payload

    @pytest.mark.asyncio
    async def test_gzip_compression_disabled(
        self,
        mock_projects_dir: Path,
        shipper_state: ShipperState,
    ):
        """Payloads are not compressed when enable_gzip is False."""
        config = ShipperConfig(
            api_url="http://test:47300",
            claude_config_dir=mock_projects_dir,
            enable_gzip=False,
        )
        shipper = SessionShipper(config=config, state=shipper_state)

        captured_content = None
        captured_headers = {}

        async def mock_post(url, content, headers):
            nonlocal captured_content, captured_headers
            captured_content = content
            captured_headers = dict(headers)
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "session_id": "zerg-session-abc",
                "events_inserted": 2,
                "events_skipped": 0,
            }
            return mock_resp

        mock_client = AsyncMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("zerg.services.shipper.shipper.httpx.AsyncClient", return_value=mock_client):
            await shipper.scan_and_ship()

        # Verify no Content-Encoding header
        assert "Content-Encoding" not in captured_headers

        # Verify content is plain JSON
        assert captured_content is not None
        payload = json.loads(captured_content.decode("utf-8"))
        assert "events" in payload


class TestHttp429Handling:
    """Tests for HTTP 429 rate limit handling."""

    @pytest.mark.asyncio
    async def test_429_with_retry_after_header(
        self,
        mock_projects_dir: Path,
        shipper_state: ShipperState,
    ):
        """Shipper respects Retry-After header on 429 response."""
        config = ShipperConfig(
            api_url="http://test:47300",
            claude_config_dir=mock_projects_dir,
            max_retries_429=2,
            base_backoff_seconds=0.01,  # Fast backoff for testing
        )
        shipper = SessionShipper(config=config, state=shipper_state)

        call_count = 0

        async def mock_post(url, content, headers):
            nonlocal call_count
            call_count += 1

            mock_resp = MagicMock()

            if call_count == 1:
                # First call returns 429
                mock_resp.status_code = 429
                mock_resp.headers = {"Retry-After": "0.01"}
                return mock_resp
            else:
                # Second call succeeds
                mock_resp.status_code = 200
                mock_resp.json.return_value = {
                    "session_id": "zerg-session-abc",
                    "events_inserted": 2,
                    "events_skipped": 0,
                }
                return mock_resp

        mock_client = AsyncMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("zerg.services.shipper.shipper.httpx.AsyncClient", return_value=mock_client):
            result = await shipper.scan_and_ship()

        # Should have retried and succeeded
        assert call_count == 2
        assert result.events_shipped == 2

    @pytest.mark.asyncio
    async def test_429_exponential_backoff(
        self,
        mock_projects_dir: Path,
        shipper_state: ShipperState,
    ):
        """Shipper uses exponential backoff on repeated 429 without Retry-After."""
        config = ShipperConfig(
            api_url="http://test:47300",
            claude_config_dir=mock_projects_dir,
            max_retries_429=2,
            base_backoff_seconds=0.01,  # Fast backoff for testing
        )
        shipper = SessionShipper(config=config, state=shipper_state)

        call_count = 0

        async def mock_post(url, content, headers):
            nonlocal call_count
            call_count += 1

            mock_resp = MagicMock()

            if call_count <= 2:
                # First two calls return 429 without Retry-After
                mock_resp.status_code = 429
                mock_resp.headers = {}
                return mock_resp
            else:
                # Third call succeeds
                mock_resp.status_code = 200
                mock_resp.json.return_value = {
                    "session_id": "zerg-session-abc",
                    "events_inserted": 2,
                    "events_skipped": 0,
                }
                return mock_resp

        mock_client = AsyncMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("zerg.services.shipper.shipper.httpx.AsyncClient", return_value=mock_client):
            result = await shipper.scan_and_ship()

        # Should have retried twice and succeeded
        assert call_count == 3
        assert result.events_shipped == 2

    @pytest.mark.asyncio
    async def test_429_max_retries_exceeded(
        self,
        mock_projects_dir: Path,
        shipper_state: ShipperState,
    ):
        """Shipper spools events after max retries on persistent 429."""
        config = ShipperConfig(
            api_url="http://test:47300",
            claude_config_dir=mock_projects_dir,
            max_retries_429=2,
            base_backoff_seconds=0.01,
        )
        shipper = SessionShipper(config=config, state=shipper_state)

        call_count = 0

        async def mock_post(url, content, headers):
            nonlocal call_count
            call_count += 1

            mock_resp = MagicMock()
            mock_resp.status_code = 429
            mock_resp.headers = {"Retry-After": "0.01"}
            return mock_resp

        mock_client = AsyncMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("zerg.services.shipper.shipper.httpx.AsyncClient", return_value=mock_client):
            result = await shipper.scan_and_ship()

        # Should have retried max_retries_429 times (2) + 1 initial = 3 total
        assert call_count == 3
        # After max retries, events are spooled for later retry (rate limits are temporary)
        assert result.events_spooled > 0
        assert result.events_skipped == 0
        # Verify events are in the spool
        assert shipper.spool.pending_count() > 0
