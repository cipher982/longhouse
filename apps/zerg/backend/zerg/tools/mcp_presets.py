"""Convenience MCP server presets.

Keeping the presets in a *dedicated* module allows product teams to update the
default list (or downstream forks to customise it) **without** touching the
core adapter logic.  All entries use the :pyclass:`zerg.tools.mcp_adapter.MCPServerConfig` dataclass.
"""

from __future__ import annotations

from typing import Dict

from zerg.tools.mcp_adapter import MCPServerConfig

# ---------------------------------------------------------------------------
# Public mapping – **modify** as needed
# ---------------------------------------------------------------------------

# Naming convention: the *dict key* doubles as the prefix inserted between the
# ``mcp_`` namespace and the tool name (see ``MCPToolAdapter.tool_prefix``).

PRESET_MCP_SERVERS: Dict[str, MCPServerConfig] = {
    "github": MCPServerConfig(
        name="github",
        url="https://api.github.com/mcp",  # hypothetical MCP endpoint
        allowed_tools=[
            "search_issues",
            "create_issue",
            "get_repository",
            "list_pull_requests",
            "create_pull_request",
        ],
        timeout=30.0,
        max_retries=3,
    ),
    "linear": MCPServerConfig(
        name="linear",
        url="https://mcp.linear.app",  # hypothetical MCP endpoint
        allowed_tools=[
            "create_issue",
            "update_issue",
            "search_issues",
            "list_projects",
            "assign_issue",
        ],
        timeout=30.0,
        max_retries=3,
    ),
    "slack": MCPServerConfig(
        name="slack",
        url="https://slack.com/api/mcp",  # hypothetical MCP endpoint
        allowed_tools=[
            "send_message",
            "list_channels",
            "get_channel_history",
            "create_channel",
        ],
        timeout=20.0,  # Slack might be faster
        max_retries=2,
    ),
    "notion": MCPServerConfig(
        name="notion",
        url="https://api.notion.com/v1/mcp",  # hypothetical MCP endpoint
        allowed_tools=[
            "create_page",
            "update_page",
            "search_pages",
            "get_database",
            "query_database",
            "create_block",
            "update_block",
        ],
        timeout=30.0,
        max_retries=3,
    ),
    "asana": MCPServerConfig(
        name="asana",
        url="https://app.asana.com/api/1.0/mcp",  # hypothetical MCP endpoint
        allowed_tools=[
            "create_task",
            "update_task",
            "search_tasks",
            "list_projects",
            "assign_task",
            "add_comment",
            "create_project",
        ],
        timeout=30.0,
        max_retries=3,
    ),
}
