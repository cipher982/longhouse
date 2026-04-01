"""Tests for the public Longhouse MCP tool surface."""

from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from zerg.mcp_server.server import create_server


def test_create_server_exposes_only_continuity_and_oikos_tools():
    server = create_server("http://example.com", None)

    tool_names = set(server._tool_manager._tools.keys())

    assert tool_names == {
        "search_sessions",
        "get_session_detail",
        "get_session_events",
        "notify_oikos",
        "log_insight",
        "query_insights",
        "recall",
        "check_wall",
        "session_tail",
        "poke",
        "check_pokes",
    }


@pytest.mark.asyncio
async def test_query_insights_uses_machine_agents_route():
    server = create_server("http://example.com", "test-token")
    tool = server._tool_manager._tools["query_insights"]
    response = type("Resp", (), {"status_code": 200, "text": '{"insights":[],"total":0}'})()

    with patch(
        "zerg.mcp_server.server.LonghouseAPIClient.get",
        new=AsyncMock(return_value=response),
    ) as mock_get:
        result = await tool.run({"project": "zerg", "limit": 5})

    assert result == '{"insights":[],"total":0}'
    mock_get.assert_awaited_once_with(
        "/api/agents/insights",
        params={"since_hours": 168, "limit": 5, "project": "zerg"},
    )
