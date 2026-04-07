"""Oikos tool registry and coordinator/worker allowlists.

Commis tools (spawn, check, cancel) live in oikos_commis_tools.py.
"""

from __future__ import annotations

import os
from typing import List

from zerg.config import get_settings
from zerg.tools.builtin.memory_tools import MEMORY_FILE_TOOL_NAMES
from zerg.tools.builtin.oikos_commis_tools import cancel_commis
from zerg.tools.builtin.oikos_commis_tools import cancel_commis_async
from zerg.tools.builtin.oikos_commis_tools import check_commis_status
from zerg.tools.builtin.oikos_commis_tools import check_commis_status_async
from zerg.tools.builtin.oikos_commis_tools import spawn_commis
from zerg.tools.builtin.oikos_commis_tools import spawn_commis_async
from zerg.types.tools import Tool as StructuredTool

TOOLS: List[StructuredTool] = [
    StructuredTool.from_function(
        func=spawn_commis,
        coroutine=spawn_commis_async,
        name="spawn_commis",
        description="Spawn a background agent to execute a task. "
        "Optionally pass git_repo to clone a repo; otherwise uses scratch workspace. "
        "Pass resume_session_id to continue a prior session. "
        "Use for: code changes, research, tests, analysis — anything that takes time.",
    ),
    StructuredTool.from_function(
        func=check_commis_status,
        coroutine=check_commis_status_async,
        name="check_commis_status",
        description="Check status of a specific commis job, or list all active jobs.",
    ),
    StructuredTool.from_function(
        func=cancel_commis,
        coroutine=cancel_commis_async,
        name="cancel_commis",
        description="Cancel a running or queued commis job.",
    ),
]

# ---------------------------------------------------------------------------
# Single source of truth for oikos tool names
# ---------------------------------------------------------------------------

OIKOS_TOOL_NAMES: frozenset[str] = frozenset(t.name for t in TOOLS)

# Additional utility tools that oikoss need access to.
_OIKOS_UTILITY_TOOL_LIST = [
    # Time/scheduling
    "get_current_time",
    # Web/HTTP
    "http_request",
    "web_search",
    "web_fetch",
    # Infrastructure
    "runner_list",
    "runner_doctor",
    "runner_create_enroll_token",
    "runner_exec",
    # Communication
    "send_email",
    "send_telegram",
    # Knowledge
    "knowledge_search",
    # Session discovery and coordination
    "search_sessions",
    "grep_sessions",
    "filter_sessions",
    "get_session_detail",
    "get_session_events",
    "session_tail",
    "peers",
    "message_session",
    "check_messages",
    "ack_message",
    # Canonical conversation discovery
    "search_conversations",
    "read_conversation",
]

# Personal context tools are gated behind PERSONAL_TOOLS_ENABLED env var
if os.getenv("PERSONAL_TOOLS_ENABLED", "").lower() in ("1", "true", "yes"):
    _OIKOS_UTILITY_TOOL_LIST.extend(
        [
            "get_current_location",
            "get_whoop_data",
            "search_notes",
        ]
    )

OIKOS_UTILITY_TOOLS: frozenset[str] = frozenset(_OIKOS_UTILITY_TOOL_LIST)


def get_oikos_allowed_tools() -> list[str]:
    """Get the complete list of tools a oikos agent should have access to."""
    allowed = OIKOS_TOOL_NAMES | OIKOS_UTILITY_TOOLS
    if get_settings().memory_files_enabled:
        allowed = allowed | MEMORY_FILE_TOOL_NAMES
    return sorted(allowed)


# ---------------------------------------------------------------------------
# Commis tool subset — execution-focused, no coordinator tools
# ---------------------------------------------------------------------------

COMMIS_TOOL_NAMES: frozenset[str] = frozenset(
    [
        # Time
        "get_current_time",
        # Web/HTTP
        "http_request",
        "web_search",
        "web_fetch",
        # Communication
        "contact_user",
        "send_email",
        "send_slack_webhook",
        "send_discord_webhook",
        # Project management — GitHub
        "github_list_repositories",
        "github_create_issue",
        "github_list_issues",
        "github_get_issue",
        "github_add_comment",
        "github_list_pull_requests",
        "github_get_pull_request",
        # Project management — Jira
        "jira_create_issue",
        "jira_list_issues",
        "jira_get_issue",
        "jira_add_comment",
        "jira_transition_issue",
        "jira_update_issue",
        # Project management — Linear
        "linear_create_issue",
        "linear_list_issues",
        "linear_get_issue",
        "linear_update_issue",
        "linear_add_comment",
        "linear_list_teams",
        # Project management — Notion
        "notion_create_page",
        "notion_get_page",
        "notion_update_page",
        "notion_search",
        "notion_query_database",
        "notion_append_blocks",
        # Knowledge
        "knowledge_search",
        # Session discovery (look up past work for context)
        "search_sessions",
        "grep_sessions",
        "filter_sessions",
        "get_session_detail",
        # Tasks
        "task_create",
        "task_list",
        "task_update",
        "task_delete",
        # Runner execution
        "runner_exec",
    ]
)


def get_commis_allowed_tools() -> list[str]:
    """Get the complete list of tools a commis agent should have access to."""
    allowed = COMMIS_TOOL_NAMES
    if get_settings().memory_files_enabled:
        allowed = allowed | MEMORY_FILE_TOOL_NAMES
    return sorted(allowed)


__all__ = [
    "TOOLS",
    "OIKOS_TOOL_NAMES",
    "OIKOS_UTILITY_TOOLS",
    "COMMIS_TOOL_NAMES",
    "get_oikos_allowed_tools",
    "get_commis_allowed_tools",
    "spawn_commis",
    "spawn_commis_async",
    "check_commis_status",
    "check_commis_status_async",
    "cancel_commis",
    "cancel_commis_async",
]
