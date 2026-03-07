"""Oikos tool registry and coordinator/worker allowlists.

Implementation functions live in focused modules:
- oikos_commis_job_tools.py: spawn/list/search/metadata/session-picker flows
- oikos_commis_artifact_tools.py: result/artifact/status/wait/cancel helpers
"""

from __future__ import annotations

import os
from typing import List

from zerg.tools.builtin.oikos_commis_artifact_tools import cancel_commis
from zerg.tools.builtin.oikos_commis_artifact_tools import cancel_commis_async
from zerg.tools.builtin.oikos_commis_artifact_tools import check_commis_status
from zerg.tools.builtin.oikos_commis_artifact_tools import check_commis_status_async
from zerg.tools.builtin.oikos_commis_artifact_tools import get_commis_evidence
from zerg.tools.builtin.oikos_commis_artifact_tools import get_commis_evidence_async
from zerg.tools.builtin.oikos_commis_artifact_tools import get_tool_output
from zerg.tools.builtin.oikos_commis_artifact_tools import get_tool_output_async
from zerg.tools.builtin.oikos_commis_artifact_tools import peek_commis_output
from zerg.tools.builtin.oikos_commis_artifact_tools import peek_commis_output_async
from zerg.tools.builtin.oikos_commis_artifact_tools import read_commis_file
from zerg.tools.builtin.oikos_commis_artifact_tools import read_commis_file_async
from zerg.tools.builtin.oikos_commis_artifact_tools import read_commis_result
from zerg.tools.builtin.oikos_commis_artifact_tools import read_commis_result_async
from zerg.tools.builtin.oikos_commis_artifact_tools import wait_for_commis
from zerg.tools.builtin.oikos_commis_artifact_tools import wait_for_commis_async
from zerg.tools.builtin.oikos_commis_job_tools import get_commis_metadata
from zerg.tools.builtin.oikos_commis_job_tools import get_commis_metadata_async
from zerg.tools.builtin.oikos_commis_job_tools import grep_commiss
from zerg.tools.builtin.oikos_commis_job_tools import grep_commiss_async
from zerg.tools.builtin.oikos_commis_job_tools import list_commiss
from zerg.tools.builtin.oikos_commis_job_tools import list_commiss_async
from zerg.tools.builtin.oikos_commis_job_tools import request_session_selection
from zerg.tools.builtin.oikos_commis_job_tools import request_session_selection_async
from zerg.tools.builtin.oikos_commis_job_tools import spawn_workspace_commis
from zerg.tools.builtin.oikos_commis_job_tools import spawn_workspace_commis_async
from zerg.types.tools import Tool as StructuredTool

# Note: We provide both func (sync) and coroutine (async) so LangChain
# can use whichever invocation method is appropriate for the runtime.
TOOLS: List[StructuredTool] = [
    StructuredTool.from_function(
        func=spawn_workspace_commis,
        coroutine=spawn_workspace_commis_async,
        name="spawn_workspace_commis",
        description="Spawn a commis to work in a workspace (PRIMARY tool for all commis work). "
        "Optionally clone a git repo by passing git_repo; otherwise uses an ephemeral scratch workspace. "
        "Runs a CLI agent (Claude Code) in an isolated workspace and captures changes. "
        "Use this for: reading code, analyzing dependencies, making changes, running tests, research. "
        "Pass skills=['skill-name'] to activate user skills in the commis prompt.",
    ),
    StructuredTool.from_function(
        func=list_commiss,
        coroutine=list_commiss_async,
        name="list_commiss",
        description="List recent commis jobs with SUMMARIES ONLY. "
        "Returns compressed summaries for quick scanning. "
        "Use read_commis_result(job_id) to get full details. "
        "This prevents context overflow when scanning 50+ commiss.",
    ),
    StructuredTool.from_function(
        func=read_commis_result,
        coroutine=read_commis_result_async,
        name="read_commis_result",
        description="Read the final result from a completed commis job. "
        "Provide the job ID (integer) to get the natural language result text.",
    ),
    StructuredTool.from_function(
        func=get_commis_evidence,
        coroutine=get_commis_evidence_async,
        name="get_commis_evidence",
        description="Compile raw tool evidence for a commis job within a byte budget. "
        "Use this to dereference [EVIDENCE:...] markers when you need full artifact details.",
    ),
    StructuredTool.from_function(
        func=get_tool_output,
        coroutine=get_tool_output_async,
        name="get_tool_output",
        description="Fetch a stored tool output by artifact_id. "
        "Use this to dereference [TOOL_OUTPUT:...] markers. "
        "Returns truncated output by default (max_bytes=32KB). Pass max_bytes=0 for full content.",
    ),
    StructuredTool.from_function(
        func=read_commis_file,
        coroutine=read_commis_file_async,
        name="read_commis_file",
        description="Read a specific file from a commis job's artifacts. "
        "Provide the job ID (integer) and file path to drill into commis details like "
        "tool outputs (tool_calls/*.txt), conversation history (thread.jsonl), or metadata (metadata.json).",
    ),
    StructuredTool.from_function(
        func=peek_commis_output,
        coroutine=peek_commis_output_async,
        name="peek_commis_output",
        description="Peek live output for a running commis (tail buffer). "
        "Provide the commis job ID and optional max_bytes. "
        "Best for seeing live runner_exec output without waiting for completion.",
    ),
    StructuredTool.from_function(
        func=grep_commiss,
        coroutine=grep_commiss_async,
        name="grep_commiss",
        description="Search across completed commis job artifacts for a text pattern. "
        "Performs case-insensitive search and returns matches with job IDs and context. "
        "Useful for finding jobs that encountered specific errors or outputs.",
    ),
    StructuredTool.from_function(
        func=get_commis_metadata,
        coroutine=get_commis_metadata_async,
        name="get_commis_metadata",
        description="Get detailed metadata about a commis job execution including "
        "task, status, timestamps, duration, and configuration. "
        "Provide the job ID (integer) to inspect job details.",
    ),
    StructuredTool.from_function(
        func=check_commis_status,
        coroutine=check_commis_status_async,
        name="check_commis_status",
        description="Check the status of a specific commis or list all active commiss. "
        "Pass job_id for a specific commis, or call without arguments to see all active commiss. "
        "Use this to monitor background commiss without blocking.",
    ),
    StructuredTool.from_function(
        func=cancel_commis,
        coroutine=cancel_commis_async,
        name="cancel_commis",
        description="Cancel a running or queued commis job. "
        "The commis will abort at its next checkpoint. "
        "Use when a task is no longer needed or taking too long.",
    ),
    StructuredTool.from_function(
        func=wait_for_commis,
        coroutine=wait_for_commis_async,
        name="wait_for_commis",
        description="Wait for a specific commis to complete (blocking). "
        "Use sparingly - the async model is preferred. "
        "Only use when you need the result before proceeding.",
    ),
    StructuredTool.from_function(
        func=request_session_selection,
        coroutine=request_session_selection_async,
        name="request_session_selection",
        description="Open a session picker modal for the user to select a past AI session. "
        "Use this when the user wants to resume a session but hasn't provided a specific ID. "
        "Optionally pre-filter by query text or project name.",
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
    "runner_create_enroll_token",
    "runner_exec",
    # Communication
    "send_email",
    "send_telegram",
    # Knowledge
    "knowledge_search",
    # Memory (persistent across sessions)
    "save_memory",
    "search_memory",
    "list_memories",
    "forget_memory",
    # Session discovery
    "search_sessions",
    "grep_sessions",
    "filter_sessions",
    "get_session_detail",
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
    return sorted(OIKOS_TOOL_NAMES | OIKOS_UTILITY_TOOLS)


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
        # Memory files (workspace-scoped persistent context)
        "memory_write",
        "memory_read",
        "memory_ls",
        "memory_search",
        "memory_delete",
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
    return sorted(COMMIS_TOOL_NAMES)


__all__ = [
    "TOOLS",
    "OIKOS_TOOL_NAMES",
    "OIKOS_UTILITY_TOOLS",
    "COMMIS_TOOL_NAMES",
    "get_oikos_allowed_tools",
    "get_commis_allowed_tools",
    "spawn_workspace_commis",
    "spawn_workspace_commis_async",
    "list_commiss",
    "list_commiss_async",
    "grep_commiss",
    "grep_commiss_async",
    "get_commis_metadata",
    "get_commis_metadata_async",
    "request_session_selection",
    "request_session_selection_async",
    "read_commis_result",
    "read_commis_result_async",
    "read_commis_file",
    "read_commis_file_async",
    "peek_commis_output",
    "peek_commis_output_async",
    "get_commis_evidence",
    "get_commis_evidence_async",
    "get_tool_output",
    "get_tool_output_async",
    "check_commis_status",
    "check_commis_status_async",
    "cancel_commis",
    "cancel_commis_async",
    "wait_for_commis",
    "wait_for_commis_async",
]
