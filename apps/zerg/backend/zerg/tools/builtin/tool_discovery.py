"""Tool discovery tools for semantic search over available tools.

These meta-tools allow fiches to discover and search for available tools
using natural language queries. They complement the tool catalog by
providing runtime search capabilities.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel
from pydantic import Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Input schemas
# ---------------------------------------------------------------------------


class SearchToolsInput(BaseModel):
    """Input schema for search_tools."""

    query: str = Field(
        description="Natural language description of what you want to do. "
        "Examples: 'send a message to slack', 'get github issues', 'check my health data'"
    )
    max_results: int = Field(
        default=5,
        description="Maximum number of tool results to return",
        ge=1,
        le=20,
    )


class ListToolsInput(BaseModel):
    """Input schema for list_tools."""

    category: str | None = Field(
        default=None,
        description="Optional category filter: 'github', 'messaging', 'personal', 'infrastructure', etc.",
    )
    include_params: bool = Field(
        default=True,
        description="Include parameter hints in output",
    )


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


async def search_tools(
    query: str,
    max_results: int = 5,
) -> dict[str, Any]:
    """Search available tools by natural language description.

    Use this when you need to find the right tool for a task. Describe what
    you want to accomplish and this will return the most relevant tools.

    Args:
        query: What you want to do (e.g., "send a notification", "get calendar events")
        max_results: Maximum number of results to return (default: 5)

    Returns:
        Dictionary with:
        - tools: List of matching tools with name, summary, category, and relevance score
        - total_available: Total number of tools in the registry
        - query: The search query
        - hint: Guidance on using the results

    Examples:
        >>> search_tools("send a message to someone")
        # Returns: send_email, send_sms, send_slack_webhook, etc.

        >>> search_tools("get information from github")
        # Returns: github_list_issues, github_get_pull_request, etc.

        >>> search_tools("track my health")
        # Returns: get_whoop_data, get_current_location, etc.
    """
    from zerg.tools.tool_search import search_tools_for_fiche

    try:
        result = await search_tools_for_fiche(query, max_results=max_results)

        # Add helpful hint
        result["hint"] = (
            "These tools will be available on your next turn. "
            "Call the tool by name after this response completes. "
            "The 'params' field shows required and optional parameters (? = optional)."
        )

        return result

    except Exception as e:
        logger.exception("Error searching tools")
        return {
            "ok": False,
            "error": f"Failed to search tools: {str(e)}",
            "tools": [],
        }


async def list_tools(
    category: str | None = None,
    include_params: bool = True,
) -> dict[str, Any]:
    """List available tools, optionally filtered by category.

    Use this to see all available tools or browse by category.

    Args:
        category: Optional category filter. Categories include:
            - concierge: spawn_commis, list_commis, contact_user
            - messaging: send_email, send_sms, send_slack_webhook, etc.
            - github: github_list_issues, github_create_issue, etc.
            - jira: jira_list_issues, jira_create_issue, etc.
            - linear: linear_list_issues, linear_create_issue, etc.
            - notion: notion_search, notion_create_page, etc.
            - personal: get_whoop_data, get_current_location, search_notes
            - infrastructure: ssh_exec, runner_exec, container_exec
            - tasks: task_create, task_list, task_update, task_delete
            - memory: fiche_memory_get, fiche_memory_set, etc.
            - web: web_search, http_request, web_fetch
            - utility: get_current_time, generate_uuid, math_eval
        include_params: Whether to include parameter hints

    Returns:
        Dictionary with:
        - tools: List of tools (filtered by category if specified)
        - categories: Available categories and their tool counts
        - total: Total tool count
    """
    from zerg.tools.catalog import build_catalog
    from zerg.tools.catalog import get_catalog_by_category
    from zerg.tools.tool_search import _is_tool_allowed

    try:
        catalog = build_catalog()
        by_category = get_catalog_by_category()
        allowed_catalog = [entry for entry in catalog if _is_tool_allowed(entry.name)]

        # Filter by category if specified
        if category:
            category_lower = category.lower()
            entries = [entry for entry in by_category.get(category_lower, []) if _is_tool_allowed(entry.name)]
        else:
            entries = list(allowed_catalog)

        # Format tools
        tools = []
        for entry in sorted(entries, key=lambda e: e.name):
            tool_info: dict[str, Any] = {
                "name": entry.name,
                "summary": entry.summary,
                "category": entry.category,
            }
            if include_params:
                tool_info["params"] = entry.param_hints
            tools.append(tool_info)

        # Build category summary
        category_counts: dict[str, int] = {}
        for entry in allowed_catalog:
            category_counts[entry.category] = category_counts.get(entry.category, 0) + 1
        categories = dict(sorted(category_counts.items()))

        return {
            "tools": tools,
            "categories": categories,
            "total": len(allowed_catalog),
            "filtered_by": category,
        }

    except Exception as e:
        logger.exception("Error listing tools")
        return {
            "ok": False,
            "error": f"Failed to list tools: {str(e)}",
            "tools": [],
        }


# ---------------------------------------------------------------------------
# LangChain tool definitions
# ---------------------------------------------------------------------------


search_tools_tool = StructuredTool.from_function(
    coroutine=search_tools,
    name="search_tools",
    description=(
        "Search available tools by natural language description. "
        "Use this to find the right tool for a task - describe what you want to do "
        "and get relevant tool suggestions. Returns tool names, descriptions, and "
        "parameter hints. Example: search_tools('send a notification to slack')"
    ),
    args_schema=SearchToolsInput,
)

list_tools_tool = StructuredTool.from_function(
    coroutine=list_tools,
    name="list_tools",
    description=(
        "List all available tools, optionally filtered by category. "
        "Categories: concierge, messaging, github, jira, linear, notion, "
        "personal, infrastructure, tasks, memory, web, utility. "
        "Use this to browse available capabilities."
    ),
    args_schema=ListToolsInput,
)

# Export tools list for registry
TOOLS = [search_tools_tool, list_tools_tool]
