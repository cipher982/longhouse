"""Tests for the session shipper."""

import json
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from zerg.services.shipper import SessionShipper
from zerg.services.shipper import ShipperConfig
from zerg.services.shipper.spool import OfflineSpool
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
    """Create shipper state with temp DB."""
    return ShipperState(db_path=tmp_path / "test-shipper.db")


@pytest.fixture
def shipper_spool(tmp_path: Path) -> OfflineSpool:
    """Create spool with temp DB."""
    return OfflineSpool(db_path=tmp_path / "test-spool.db")


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


class TestSessionShipper:
    """Tests for SessionShipper."""

    def test_find_session_files(
        self,
        mock_projects_dir: Path,
        shipper_config: ShipperConfig,
        shipper_state: ShipperState,
        shipper_spool: OfflineSpool,
    ):
        """Find session files in projects directory."""
        shipper = SessionShipper(config=shipper_config, state=shipper_state, spool=shipper_spool)
        files = shipper._find_session_files()

        assert len(files) == 1
        assert files[0].name == "test-session-123.jsonl"

    def test_has_new_content(
        self,
        mock_projects_dir: Path,
        shipper_config: ShipperConfig,
        shipper_state: ShipperState,
        shipper_spool: OfflineSpool,
    ):
        """Check if file has new content."""
        shipper = SessionShipper(config=shipper_config, state=shipper_state, spool=shipper_spool)
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
        shipper_spool: OfflineSpool,
    ):
        """Ship a session to Zerg API."""
        shipper = SessionShipper(config=shipper_config, state=shipper_state, spool=shipper_spool)
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
        assert call_args["provider_session_id"] == "test-session-123"

    @pytest.mark.asyncio
    async def test_scan_and_ship(
        self,
        mock_projects_dir: Path,
        shipper_config: ShipperConfig,
        shipper_state: ShipperState,
        shipper_spool: OfflineSpool,
    ):
        """Scan and ship all sessions."""
        shipper = SessionShipper(config=shipper_config, state=shipper_state, spool=shipper_spool)

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
        shipper_spool: OfflineSpool,
    ):
        """Second ship only sends new events."""
        shipper = SessionShipper(config=shipper_config, state=shipper_state, spool=shipper_spool)
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
        shipper_spool: OfflineSpool,
    ):
        """Handle API errors gracefully."""
        shipper = SessionShipper(config=shipper_config, state=shipper_state, spool=shipper_spool)

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
        shipper_spool: OfflineSpool,
    ):
        """API token is included in request headers when configured."""
        config = ShipperConfig(
            api_url="http://test:47300",
            claude_config_dir=mock_projects_dir,
            api_token="test-secret-token",
        )
        shipper = SessionShipper(config=config, state=shipper_state, spool=shipper_spool)

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
        shipper_spool: OfflineSpool,
    ):
        """No auth header when api_token is not configured."""
        config = ShipperConfig(
            api_url="http://test:47300",
            claude_config_dir=mock_projects_dir,
            api_token=None,
        )
        shipper = SessionShipper(config=config, state=shipper_state, spool=shipper_spool)

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
        shipper_spool: OfflineSpool,
    ):
        """Payloads are gzip compressed when enable_gzip is True."""
        import gzip as gzip_module

        config = ShipperConfig(
            api_url="http://test:47300",
            claude_config_dir=mock_projects_dir,
            enable_gzip=True,
        )
        shipper = SessionShipper(config=config, state=shipper_state, spool=shipper_spool)

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

        assert captured_headers.get("Content-Encoding") == "gzip"
        assert captured_content is not None
        decompressed = gzip_module.decompress(captured_content)
        payload = json.loads(decompressed)
        assert "events" in payload

    @pytest.mark.asyncio
    async def test_gzip_compression_disabled(
        self,
        mock_projects_dir: Path,
        shipper_state: ShipperState,
        shipper_spool: OfflineSpool,
    ):
        """Payloads are not compressed when enable_gzip is False."""
        config = ShipperConfig(
            api_url="http://test:47300",
            claude_config_dir=mock_projects_dir,
            enable_gzip=False,
        )
        shipper = SessionShipper(config=config, state=shipper_state, spool=shipper_spool)

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

        assert "Content-Encoding" not in captured_headers
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
        shipper_spool: OfflineSpool,
    ):
        """Shipper respects Retry-After header on 429 response."""
        config = ShipperConfig(
            api_url="http://test:47300",
            claude_config_dir=mock_projects_dir,
            max_retries_429=2,
            base_backoff_seconds=0.01,
        )
        shipper = SessionShipper(config=config, state=shipper_state, spool=shipper_spool)

        call_count = 0

        async def mock_post(url, content, headers):
            nonlocal call_count
            call_count += 1

            mock_resp = MagicMock()

            if call_count == 1:
                mock_resp.status_code = 429
                mock_resp.headers = {"Retry-After": "0.01"}
                return mock_resp
            else:
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

        assert call_count == 2
        assert result.events_shipped == 2

    @pytest.mark.asyncio
    async def test_429_exponential_backoff(
        self,
        mock_projects_dir: Path,
        shipper_state: ShipperState,
        shipper_spool: OfflineSpool,
    ):
        """Shipper uses exponential backoff on repeated 429 without Retry-After."""
        config = ShipperConfig(
            api_url="http://test:47300",
            claude_config_dir=mock_projects_dir,
            max_retries_429=2,
            base_backoff_seconds=0.01,
        )
        shipper = SessionShipper(config=config, state=shipper_state, spool=shipper_spool)

        call_count = 0

        async def mock_post(url, content, headers):
            nonlocal call_count
            call_count += 1

            mock_resp = MagicMock()

            if call_count <= 2:
                mock_resp.status_code = 429
                mock_resp.headers = {}
                return mock_resp
            else:
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

        assert call_count == 3
        assert result.events_shipped == 2

    @pytest.mark.asyncio
    async def test_429_max_retries_exceeded(
        self,
        mock_projects_dir: Path,
        shipper_state: ShipperState,
        shipper_spool: OfflineSpool,
    ):
        """Shipper spools events after max retries on persistent 429."""
        config = ShipperConfig(
            api_url="http://test:47300",
            claude_config_dir=mock_projects_dir,
            max_retries_429=2,
            base_backoff_seconds=0.01,
        )
        shipper = SessionShipper(config=config, state=shipper_state, spool=shipper_spool)

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
        # After max retries, events are spooled for later retry
        assert result.events_spooled > 0
        assert result.events_skipped == 0
        # Verify pointer is in the spool
        assert shipper_spool.pending_count() > 0


class TestShipperSpoolIntegration:
    """Tests for shipper + spool integration with pointer-based spool."""

    @pytest.fixture
    def temp_env(self, tmp_path: Path):
        """Create temporary environment for testing."""
        # Create projects directory with a test session
        projects_dir = tmp_path / ".claude" / "projects" / "test-project"
        projects_dir.mkdir(parents=True)

        session_file = projects_dir / "test-session.jsonl"
        event_data = {
            "type": "user",
            "uuid": "user-1",
            "timestamp": "2026-02-15T10:00:00Z",
            "message": {"role": "user", "content": "Hello"},
        }
        session_file.write_text(json.dumps(event_data) + "\n")

        config = ShipperConfig(
            api_url="http://localhost:47300",
            claude_config_dir=tmp_path / ".claude",
        )

        state = ShipperState(db_path=tmp_path / "state.db")
        spool = OfflineSpool(db_path=tmp_path / "spool.db")

        yield {
            "tmpdir": tmp_path,
            "session_file": session_file,
            "config": config,
            "state": state,
            "spool": spool,
        }

    @pytest.mark.asyncio
    async def test_ship_spools_pointer_on_connection_error(self, temp_env):
        """ship_session should spool a pointer when API is unreachable."""
        import httpx

        shipper = SessionShipper(
            config=temp_env["config"],
            state=temp_env["state"],
            spool=temp_env["spool"],
        )

        async def mock_post_ingest(payload):
            raise httpx.ConnectError("Connection refused")

        shipper._post_ingest = mock_post_ingest

        result = await shipper.ship_session(temp_env["session_file"])

        assert result["events_inserted"] == 0
        assert result["events_spooled"] == 1

        # Verify pointer is in spool (not payload)
        assert temp_env["spool"].pending_count() == 1
        entries = temp_env["spool"].dequeue_batch()
        assert len(entries) == 1
        assert entries[0].file_path == str(temp_env["session_file"])
        assert entries[0].start_offset == 0
        assert entries[0].end_offset > 0

    @pytest.mark.asyncio
    async def test_ship_spools_pointer_on_timeout(self, temp_env):
        """ship_session should spool pointer on timeout."""
        import httpx

        shipper = SessionShipper(
            config=temp_env["config"],
            state=temp_env["state"],
            spool=temp_env["spool"],
        )

        async def mock_post_ingest(payload):
            raise httpx.TimeoutException("Request timed out")

        shipper._post_ingest = mock_post_ingest

        result = await shipper.ship_session(temp_env["session_file"])

        assert result["events_spooled"] == 1
        assert temp_env["spool"].pending_count() == 1

    @pytest.mark.asyncio
    async def test_ship_raises_on_auth_error(self, temp_env):
        """ship_session should raise on 401/403 auth errors, not spool."""
        import httpx

        shipper = SessionShipper(
            config=temp_env["config"],
            state=temp_env["state"],
            spool=temp_env["spool"],
        )

        async def mock_post_ingest(payload):
            response = MagicMock()
            response.status_code = 401
            raise httpx.HTTPStatusError(
                "Unauthorized",
                request=MagicMock(),
                response=response,
            )

        shipper._post_ingest = mock_post_ingest

        with pytest.raises(httpx.HTTPStatusError):
            await shipper.ship_session(temp_env["session_file"])

        assert temp_env["spool"].pending_count() == 0

    @pytest.mark.asyncio
    async def test_ship_spools_pointer_on_server_error(self, temp_env):
        """ship_session should spool pointer on 5xx server errors."""
        import httpx

        shipper = SessionShipper(
            config=temp_env["config"],
            state=temp_env["state"],
            spool=temp_env["spool"],
        )

        async def mock_post_ingest(payload):
            response = MagicMock()
            response.status_code = 503
            raise httpx.HTTPStatusError(
                "Service unavailable",
                request=MagicMock(),
                response=response,
            )

        shipper._post_ingest = mock_post_ingest

        result = await shipper.ship_session(temp_env["session_file"])

        assert result["events_spooled"] == 1
        assert temp_env["spool"].pending_count() == 1

    @pytest.mark.asyncio
    async def test_ship_skips_on_4xx_client_error(self, temp_env):
        """ship_session should skip (not spool) on 4xx client errors."""
        import httpx

        shipper = SessionShipper(
            config=temp_env["config"],
            state=temp_env["state"],
            spool=temp_env["spool"],
        )

        async def mock_post_ingest(payload):
            response = MagicMock()
            response.status_code = 400
            raise httpx.HTTPStatusError(
                "Bad request",
                request=MagicMock(),
                response=response,
            )

        shipper._post_ingest = mock_post_ingest

        result = await shipper.ship_session(temp_env["session_file"])

        assert result["events_skipped"] == 1
        assert result["events_spooled"] == 0
        assert temp_env["spool"].pending_count() == 0

    @pytest.mark.asyncio
    async def test_startup_recovery_enqueues_gaps(self, temp_env):
        """startup_recovery should enqueue gaps between queued and acked offsets."""
        shipper = SessionShipper(
            config=temp_env["config"],
            state=temp_env["state"],
            spool=temp_env["spool"],
        )

        # Simulate a gap: queued_offset > acked_offset
        file_path = str(temp_env["session_file"])
        temp_env["state"].set_queued_offset(file_path, 500, session_id="sess-1")
        # acked_offset stays at 0 (default)

        count = shipper.startup_recovery()
        assert count == 1
        assert temp_env["spool"].pending_count() == 1

        entries = temp_env["spool"].dequeue_batch()
        assert entries[0].start_offset == 0
        assert entries[0].end_offset == 500

    @pytest.mark.asyncio
    async def test_backpressure_does_not_advance_offset(self, temp_env):
        """Bug 1: When spool is full, queued_offset must NOT advance."""
        import httpx

        shipper = SessionShipper(
            config=temp_env["config"],
            state=temp_env["state"],
            spool=temp_env["spool"],
        )

        # Fill the spool to capacity
        from zerg.services.shipper.spool import MAX_QUEUE_SIZE

        now_iso = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
        temp_env["spool"].conn.executemany(
            "INSERT INTO spool_queue (provider, file_path, start_offset, end_offset, session_id, created_at, next_retry_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
            [("claude", f"/tmp/f{i}.jsonl", 0, 100, None, now_iso, now_iso) for i in range(MAX_QUEUE_SIZE)],
        )
        temp_env["spool"].conn.commit()

        async def mock_post_ingest(payload):
            raise httpx.ConnectError("Connection refused")

        shipper._post_ingest = mock_post_ingest

        file_path = str(temp_env["session_file"])
        result = await shipper.ship_session(temp_env["session_file"])

        # Offset should NOT have advanced
        assert temp_env["state"].get_queued_offset(file_path) == 0
        assert result["events_spooled"] == 0
        assert result.get("errors")  # Should contain backpressure error

    @pytest.mark.asyncio
    async def test_rescan_does_not_respool_same_range(self, temp_env):
        """Bug 2: After failed ship+spool, next scan must NOT re-spool same range."""
        import httpx

        shipper = SessionShipper(
            config=temp_env["config"],
            state=temp_env["state"],
            spool=temp_env["spool"],
        )

        async def mock_post_ingest(payload):
            raise httpx.ConnectError("Connection refused")

        shipper._post_ingest = mock_post_ingest

        # First ship — spools the data
        result1 = await shipper.ship_session(temp_env["session_file"])
        assert result1["events_spooled"] == 1
        assert temp_env["spool"].pending_count() == 1

        # Second scan — should NOT find new content (queued_offset advanced)
        assert shipper._has_new_content(temp_env["session_file"]) is False

        # If we do force a ship_session, it should find no events
        result2 = await shipper.ship_session(temp_env["session_file"])
        assert result2["events_spooled"] == 0
        assert result2["events_inserted"] == 0

        # Spool should still have just 1 entry, not 2
        assert temp_env["spool"].pending_count() == 1

    @pytest.mark.asyncio
    async def test_partial_line_at_eof_not_lost(self, temp_env):
        """Bug 3: Partial last line at EOF must not be skipped."""
        shipper = SessionShipper(
            config=temp_env["config"],
            state=temp_env["state"],
            spool=temp_env["spool"],
        )

        # Write a complete line + a partial (incomplete JSON) line at end
        complete_line = json.dumps({
            "type": "user",
            "uuid": "msg-complete",
            "timestamp": "2026-02-15T10:00:00Z",
            "message": {"content": "Complete message"},
        })
        partial_line = '{"type": "user", "uuid": "msg-partial", "timestamp": "2026-02-15T10:01:00Z"'

        temp_env["session_file"].write_text(complete_line + "\n" + partial_line)

        mock_response = {
            "session_id": "zerg-session-abc",
            "events_inserted": 1,
            "events_skipped": 0,
        }

        with patch.object(shipper, "_post_ingest", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            result = await shipper.ship_session(temp_env["session_file"])

        # Should have shipped the complete line
        assert result["events_inserted"] == 1

        # new_offset should be at the end of the complete line, NOT at file size
        file_size = temp_env["session_file"].stat().st_size
        assert result["new_offset"] < file_size
        assert result["new_offset"] == len(complete_line.encode("utf-8")) + 1  # +1 for newline

    @pytest.mark.asyncio
    async def test_file_truncation_resets_offsets(self, temp_env):
        """Bug 4: File truncation/rotation should reset offsets and re-read."""
        shipper = SessionShipper(
            config=temp_env["config"],
            state=temp_env["state"],
            spool=temp_env["spool"],
        )

        file_path = str(temp_env["session_file"])

        # Simulate previously shipped to offset 1000
        temp_env["state"].set_offset(file_path, 1000, "sess-1", "p-1")

        # But file is smaller than 1000 bytes (truncated)
        assert temp_env["session_file"].stat().st_size < 1000

        # _has_new_content should detect truncation and reset
        assert shipper._has_new_content(temp_env["session_file"]) is True

        # Offsets should have been reset
        assert temp_env["state"].get_offset(file_path) == 0
        assert temp_env["state"].get_queued_offset(file_path) == 0

    @pytest.mark.asyncio
    async def test_empty_replay_advances_acked_offset(self, temp_env):
        """Bug 7: Replay with no parseable events should still advance acked_offset."""
        shipper = SessionShipper(
            config=temp_env["config"],
            state=temp_env["state"],
            spool=temp_env["spool"],
        )

        file_path = str(temp_env["session_file"])

        # Create a file that has only metadata lines (no parseable events)
        content = json.dumps({"type": "summary", "summary": "Test"}) + "\n"
        temp_env["session_file"].write_text(content)
        file_size = temp_env["session_file"].stat().st_size

        # Manually set up a spool entry for this file range (use actual file size)
        temp_env["state"].set_queued_offset(file_path, file_size, session_id="sess-1")
        temp_env["spool"].enqueue("claude", file_path, 0, file_size, "sess-1")

        result = await shipper.replay_spool()
        assert result["replayed"] == 1

        # acked_offset should have advanced to close the gap
        assert temp_env["state"].get_offset(file_path) == file_size

        # No more unacked files
        assert len(temp_env["state"].get_unacked_files()) == 0

    @pytest.mark.asyncio
    async def test_startup_recovery_uses_provider_and_session_id(self, temp_env):
        """Bug 8: startup_recovery should use provider/session_id from state, not hardcode claude."""
        shipper = SessionShipper(
            config=temp_env["config"],
            state=temp_env["state"],
            spool=temp_env["spool"],
        )

        file_path = str(temp_env["session_file"])
        temp_env["state"].set_queued_offset(
            file_path, 500, provider="gemini", session_id="gemini-sess-1", provider_session_id="p1"
        )

        count = shipper.startup_recovery()
        assert count == 1

        entries = temp_env["spool"].dequeue_batch()
        assert len(entries) == 1
        assert entries[0].provider == "gemini"
        assert entries[0].session_id == "gemini-sess-1"
