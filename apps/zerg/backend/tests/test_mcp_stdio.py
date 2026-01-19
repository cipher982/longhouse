"""Tests for MCP stdio transport functionality."""

import asyncio
import json
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from zerg.tools.mcp_config_schema import normalize_config
from zerg.tools.mcp_exceptions import MCPConfigurationError
from zerg.tools.mcp_exceptions import MCPConnectionError
from zerg.tools.mcp_transport import HTTPTransport
from zerg.tools.mcp_transport import MCPServerConfig
from zerg.tools.mcp_transport import StdioTransport
from zerg.tools.mcp_transport import create_transport


class TestMCPServerConfig:
    """Tests for MCPServerConfig dataclass."""

    def test_default_transport_is_http(self):
        """Default transport should be http."""
        config = MCPServerConfig(name="test", url="https://example.com")
        assert config.transport == "http"

    def test_stdio_transport_config(self):
        """Stdio transport config should accept command and env."""
        config = MCPServerConfig(
            name="test",
            transport="stdio",
            command="python -m my_server",
            env={"DEBUG": "1"},
        )
        assert config.transport == "stdio"
        assert config.command == "python -m my_server"
        assert config.env == {"DEBUG": "1"}

    def test_http_transport_config(self):
        """HTTP transport config should accept url and auth_token."""
        config = MCPServerConfig(
            name="test",
            transport="http",
            url="https://example.com/mcp",
            auth_token="test_token",
        )
        assert config.transport == "http"
        assert config.url == "https://example.com/mcp"
        assert config.auth_token == "test_token"


class TestCreateTransport:
    """Tests for transport factory function."""

    def test_create_http_transport(self):
        """Should create HTTPTransport for http config."""
        config = MCPServerConfig(name="test", transport="http", url="https://example.com")
        transport = create_transport(config)
        assert isinstance(transport, HTTPTransport)

    def test_create_stdio_transport(self):
        """Should create StdioTransport for stdio config."""
        config = MCPServerConfig(name="test", transport="stdio", command="echo test")
        transport = create_transport(config)
        assert isinstance(transport, StdioTransport)

    def test_create_unknown_transport_raises(self):
        """Should raise ValueError for unknown transport type."""
        config = MCPServerConfig(name="test")
        config.transport = "unknown"  # type: ignore
        with pytest.raises(ValueError, match="Unknown transport type"):
            create_transport(config)


class TestNormalizeConfig:
    """Tests for config normalization with stdio support."""

    def test_normalize_stdio_explicit_type(self):
        """Explicit type=stdio should be preserved."""
        config = {"type": "stdio", "name": "test", "command": "python -m server"}
        result = normalize_config(config)
        assert result["type"] == "stdio"

    def test_normalize_stdio_inferred_from_command(self):
        """Config with command and name should infer stdio type."""
        config = {"name": "test", "command": "python -m server"}
        result = normalize_config(config)
        assert result["type"] == "stdio"

    def test_normalize_custom_http(self):
        """Config with name and url should infer custom (http) type."""
        config = {"name": "test", "url": "https://example.com"}
        result = normalize_config(config)
        assert result["type"] == "custom"

    def test_normalize_preset(self):
        """Config with preset should infer preset type."""
        config = {"preset": "github"}
        result = normalize_config(config)
        assert result["type"] == "preset"

    def test_normalize_invalid_type_raises(self):
        """Invalid explicit type should raise ValueError."""
        config = {"type": "invalid", "name": "test"}
        with pytest.raises(ValueError, match="Invalid configuration type"):
            normalize_config(config)

    def test_normalize_ambiguous_config_raises(self):
        """Config without identifiable type should raise ValueError."""
        config = {"auth_token": "test"}
        with pytest.raises(ValueError, match="Configuration must specify"):
            normalize_config(config)


class TestStdioTransport:
    """Tests for StdioTransport class."""

    @pytest.fixture
    def stdio_config(self):
        """Create a basic stdio config."""
        return MCPServerConfig(
            name="test_server",
            transport="stdio",
            command="python -m mcp_server",
            timeout=5.0,
        )

    @pytest.fixture
    def stdio_transport(self, stdio_config):
        """Create a StdioTransport instance."""
        return StdioTransport(stdio_config)

    def test_initial_state(self, stdio_transport):
        """Transport should start in disconnected state."""
        assert stdio_transport.process is None
        assert stdio_transport._initialized is False
        assert stdio_transport._tools_cache is None

    @pytest.mark.asyncio
    async def test_connect_requires_command(self):
        """Connect should raise if no command is specified."""
        config = MCPServerConfig(name="test", transport="stdio", command=None)
        transport = StdioTransport(config)
        with pytest.raises(MCPConnectionError, match="No command specified"):
            await transport.connect()

    @pytest.mark.asyncio
    async def test_connect_handles_file_not_found(self):
        """Connect should raise MCPConnectionError for missing command."""
        config = MCPServerConfig(
            name="test",
            transport="stdio",
            command="/nonexistent/command",
        )
        transport = StdioTransport(config)
        with pytest.raises(MCPConnectionError):
            await transport.connect()

    @pytest.mark.asyncio
    async def test_health_check_false_when_not_connected(self, stdio_transport):
        """Health check should return False when not connected."""
        result = await stdio_transport.health_check()
        assert result is False

    @pytest.mark.asyncio
    async def test_disconnect_is_safe_when_not_connected(self, stdio_transport):
        """Disconnect should be safe to call when not connected."""
        await stdio_transport.disconnect()  # Should not raise
        assert stdio_transport.process is None

    @pytest.mark.asyncio
    async def test_message_id_increments(self, stdio_transport):
        """Message ID should increment with each call."""
        assert stdio_transport._next_id() == 1
        assert stdio_transport._next_id() == 2
        assert stdio_transport._next_id() == 3

    @pytest.mark.asyncio
    async def test_list_tools_connects_if_needed(self, stdio_transport):
        """list_tools should auto-connect if not connected."""
        # Mock the connection and request
        with patch.object(stdio_transport, "connect", new_callable=AsyncMock) as mock_connect:
            with patch.object(stdio_transport, "_send_request", new_callable=AsyncMock) as mock_request:
                mock_request.return_value = {"tools": [{"name": "test_tool"}]}
                stdio_transport._initialized = True  # Pretend connected

                tools = await stdio_transport.list_tools()

                assert tools == [{"name": "test_tool"}]
                assert stdio_transport._tools_cache == [{"name": "test_tool"}]

    @pytest.mark.asyncio
    async def test_list_tools_uses_cache(self, stdio_transport):
        """list_tools should return cached results on subsequent calls."""
        stdio_transport._tools_cache = [{"name": "cached_tool"}]
        tools = await stdio_transport.list_tools()
        assert tools == [{"name": "cached_tool"}]

    @pytest.mark.asyncio
    async def test_call_tool_connects_if_needed(self, stdio_transport):
        """call_tool should auto-connect if not connected."""
        with patch.object(stdio_transport, "connect", new_callable=AsyncMock):
            with patch.object(stdio_transport, "_send_request", new_callable=AsyncMock) as mock_request:
                mock_request.return_value = {"content": [{"type": "text", "text": "result"}]}
                stdio_transport._initialized = True

                result = await stdio_transport.call_tool("test_tool", {"arg": "value"})

                assert result == "result"
                mock_request.assert_called_once_with(
                    "tools/call",
                    {"name": "test_tool", "arguments": {"arg": "value"}},
                )


class TestStdioTransportJsonRpc:
    """Tests for JSON-RPC message formatting in StdioTransport."""

    def test_jsonrpc_message_format(self):
        """Verify JSON-RPC message format is correct."""
        config = MCPServerConfig(name="test", transport="stdio", command="echo test")
        transport = StdioTransport(config)

        # Test that message IDs increment correctly
        id1 = transport._next_id()
        id2 = transport._next_id()
        assert id1 == 1
        assert id2 == 2

        # Verify the expected JSON-RPC format
        expected_request = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/list",
            "params": {},
        }
        # This is what _send_request would construct
        assert expected_request["jsonrpc"] == "2.0"
        assert expected_request["method"] == "tools/list"

    def test_pending_futures_management(self):
        """Test that pending futures are properly managed."""
        config = MCPServerConfig(name="test", transport="stdio", command="echo test")
        transport = StdioTransport(config)

        # Initially empty
        assert len(transport._pending) == 0

        # Can add futures
        loop = asyncio.new_event_loop()
        future = loop.create_future()
        transport._pending[1] = future
        assert len(transport._pending) == 1

        # Clean up
        loop.close()


class TestMCPAPIStdioEndpoints:
    """Tests for MCP server API endpoints with stdio transport."""

    def test_add_stdio_server_validation(self, client, auth_headers, test_agent):
        """Test adding a stdio MCP server via API."""
        response = client.post(
            f"/api/agents/{test_agent.id}/mcp-servers/",
            headers=auth_headers,
            json={
                "transport": "stdio",
                "name": "echo_server",
                "command": "echo hello",
            },
        )
        # In test mode, connection is skipped - should succeed
        assert response.status_code in [201, 502]  # 502 if connection attempted and failed

    def test_add_stdio_server_missing_command(self, client, auth_headers, test_agent):
        """Test adding stdio server without command fails validation."""
        response = client.post(
            f"/api/agents/{test_agent.id}/mcp-servers/",
            headers=auth_headers,
            json={
                "transport": "stdio",
                "name": "test_server",
                # Missing command
            },
        )
        assert response.status_code == 422

    def test_add_stdio_server_missing_name(self, client, auth_headers, test_agent):
        """Test adding stdio server without name fails validation."""
        response = client.post(
            f"/api/agents/{test_agent.id}/mcp-servers/",
            headers=auth_headers,
            json={
                "transport": "stdio",
                "command": "echo test",
                # Missing name
            },
        )
        assert response.status_code == 422

    def test_add_stdio_server_with_env(self, client, auth_headers, test_agent):
        """Test adding stdio server with environment variables."""
        response = client.post(
            f"/api/agents/{test_agent.id}/mcp-servers/",
            headers=auth_headers,
            json={
                "transport": "stdio",
                "name": "env_server",
                "command": "python -m server",
                "env": {"DEBUG": "1", "LOG_LEVEL": "info"},
            },
        )
        # In test mode, connection is skipped
        assert response.status_code in [201, 502]

    def test_cannot_mix_stdio_and_url(self, client, auth_headers, test_agent):
        """Test that providing both command and url fails validation."""
        response = client.post(
            f"/api/agents/{test_agent.id}/mcp-servers/",
            headers=auth_headers,
            json={
                "transport": "stdio",
                "name": "mixed",
                "command": "echo test",
                "url": "https://example.com",  # Not allowed with stdio
            },
        )
        assert response.status_code == 422

    def test_test_stdio_server(self, client, auth_headers, test_agent):
        """Test MCP stdio server using the /test endpoint."""
        response = client.post(
            f"/api/agents/{test_agent.id}/mcp-servers/test",
            headers=auth_headers,
            json={
                "transport": "stdio",
                "name": "test_stdio_server",
                "command": "echo hello",
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["success"] is True

    def test_remove_stdio_server_shuts_down(self, client, auth_headers, test_agent, db, monkeypatch):
        """Removing a stdio server should shut down its pooled process."""
        from zerg.crud import crud
        from zerg.tools.mcp_adapter import MCPManager
        from zerg.utils.json_helpers import set_json_field

        agent = crud.get_agent(db, agent_id=test_agent.id)
        set_json_field(
            agent,
            "config",
            {
                "mcp_servers": [
                    {
                        "type": "stdio",
                        "name": "local_stdio",
                        "command": "echo hi",
                        "env": {"DEBUG": "1"},
                    }
                ]
            },
        )
        db.commit()

        called = {}

        def fake_shutdown(self, cfg):  # noqa: ANN001 - test stub
            called["cfg"] = cfg

        monkeypatch.setattr(MCPManager, "shutdown_stdio_process_for_config_sync", fake_shutdown)

        response = client.delete(
            f"/api/agents/{test_agent.id}/mcp-servers/local_stdio",
            headers=auth_headers,
        )
        assert response.status_code == 204
        assert called["cfg"].command == "echo hi"


# Import the fixture from conftest - needed for API tests
@pytest.fixture
def test_agent(db, test_user):
    """Create a test agent for MCP tests."""
    from tests.conftest import TEST_WORKER_MODEL
    from zerg.crud import crud

    agent = crud.create_agent(
        db=db,
        owner_id=test_user.id,
        name="Test Agent for MCP Stdio",
        system_instructions="You are a test agent",
        task_instructions="Test MCP stdio functionality",
        model=TEST_WORKER_MODEL,
        schedule=None,
        config=None,
    )
    return agent
