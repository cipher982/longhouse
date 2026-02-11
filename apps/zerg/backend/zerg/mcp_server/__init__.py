"""Longhouse MCP server for CLI agent integration.

Exposes Longhouse capabilities (session search, memory, notifications)
as MCP tools that Claude Code and other agents can call over stdio.

Usage:
    from zerg.mcp_server import create_server

    server = create_server(api_url="http://localhost:8080", api_token="xxx")
    server.run()  # stdio transport
"""

from zerg.mcp_server.server import create_server

__all__ = ["create_server"]
