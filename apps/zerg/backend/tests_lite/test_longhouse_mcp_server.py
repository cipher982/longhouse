"""Tests for the public Longhouse MCP tool surface."""

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
    }
