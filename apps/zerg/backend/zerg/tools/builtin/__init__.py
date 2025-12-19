"""Built-in tools for Zerg agents.

This module contains the standard tools that come with the platform.
All tools in this module are aggregated into a single list for registry construction.
"""

from zerg.tools.builtin.agent_memory_tools import TOOLS as AGENT_MEMORY_TOOLS
from zerg.tools.builtin.connector_tools import TOOLS as CONNECTOR_TOOLS
from zerg.tools.builtin.contact_user import TOOLS as CONTACT_USER_TOOLS
from zerg.tools.builtin.container_tools import TOOLS as CONTAINER_TOOLS
from zerg.tools.builtin.datetime_tools import TOOLS as DATETIME_TOOLS
from zerg.tools.builtin.discord_tools import TOOLS as DISCORD_TOOLS
from zerg.tools.builtin.email_tools import TOOLS as EMAIL_TOOLS
from zerg.tools.builtin.github_tools import TOOLS as GITHUB_TOOLS
from zerg.tools.builtin.http_tools import TOOLS as HTTP_TOOLS
from zerg.tools.builtin.imessage_tools import TOOLS as IMESSAGE_TOOLS
from zerg.tools.builtin.jira_tools import TOOLS as JIRA_TOOLS
from zerg.tools.builtin.knowledge_tools import TOOLS as KNOWLEDGE_TOOLS
from zerg.tools.builtin.linear_tools import TOOLS as LINEAR_TOOLS
from zerg.tools.builtin.math_tools import TOOLS as MATH_TOOLS
from zerg.tools.builtin.notion_tools import TOOLS as NOTION_TOOLS
from zerg.tools.builtin.personal_tools import TOOLS as PERSONAL_TOOLS
from zerg.tools.builtin.runner_tools import TOOLS as RUNNER_TOOLS
from zerg.tools.builtin.runner_setup_tools import TOOLS as RUNNER_SETUP_TOOLS
from zerg.tools.builtin.slack_tools import TOOLS as SLACK_TOOLS
from zerg.tools.builtin.sms_tools import TOOLS as SMS_TOOLS
from zerg.tools.builtin.ssh_tools import TOOLS as SSH_TOOLS
from zerg.tools.builtin.supervisor_tools import TOOLS as SUPERVISOR_TOOLS
from zerg.tools.builtin.task_tools import TOOLS as TASK_TOOLS
from zerg.tools.builtin.uuid_tools import TOOLS as UUID_TOOLS
from zerg.tools.builtin.web_fetch import TOOLS as WEB_FETCH_TOOLS
from zerg.tools.builtin.web_search import TOOLS as WEB_SEARCH_TOOLS
from zerg.tools.registry import ToolRegistry

BUILTIN_TOOLS = (
    AGENT_MEMORY_TOOLS
    + CONNECTOR_TOOLS
    + CONTACT_USER_TOOLS
    + CONTAINER_TOOLS
    + DATETIME_TOOLS
    + DISCORD_TOOLS
    + EMAIL_TOOLS
    + GITHUB_TOOLS
    + HTTP_TOOLS
    + IMESSAGE_TOOLS
    + JIRA_TOOLS
    + KNOWLEDGE_TOOLS
    + LINEAR_TOOLS
    + MATH_TOOLS
    + NOTION_TOOLS
    + PERSONAL_TOOLS
    + RUNNER_TOOLS
    + RUNNER_SETUP_TOOLS
    + SLACK_TOOLS
    + SMS_TOOLS
    + SSH_TOOLS
    + SUPERVISOR_TOOLS
    + TASK_TOOLS
    + UUID_TOOLS
    + WEB_FETCH_TOOLS
    + WEB_SEARCH_TOOLS
)

__all__ = [
    "BUILTIN_TOOLS",
]

# ---------------------------------------------------------------------------
# Automatically register built-in tools with the *mutable* singleton registry
# so legacy tests (and any runtime code relying on ToolRegistry) see them.
# ---------------------------------------------------------------------------

_registry = ToolRegistry()
for _tool in BUILTIN_TOOLS:
    if _tool.name not in _registry.list_tool_names():  # idempotent
        _registry.register(_tool)
