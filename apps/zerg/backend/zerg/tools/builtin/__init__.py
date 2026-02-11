"""Built-in tools for Zerg fiches.

This module contains the standard tools that come with the platform.
All tools in this module are aggregated into a single list for registry construction.
"""

import os

from zerg.tools.builtin.connector_tools import TOOLS as CONNECTOR_TOOLS
from zerg.tools.builtin.contact_user import TOOLS as CONTACT_USER_TOOLS
from zerg.tools.builtin.datetime_tools import TOOLS as DATETIME_TOOLS
from zerg.tools.builtin.discord_tools import TOOLS as DISCORD_TOOLS
from zerg.tools.builtin.email_tools import TOOLS as EMAIL_TOOLS
from zerg.tools.builtin.github_tools import TOOLS as GITHUB_TOOLS
from zerg.tools.builtin.http_tools import TOOLS as HTTP_TOOLS
from zerg.tools.builtin.imessage_tools import TOOLS as IMESSAGE_TOOLS
from zerg.tools.builtin.jira_tools import TOOLS as JIRA_TOOLS
from zerg.tools.builtin.knowledge_tools import TOOLS as KNOWLEDGE_TOOLS
from zerg.tools.builtin.linear_tools import TOOLS as LINEAR_TOOLS
from zerg.tools.builtin.memory_tools import TOOLS as MEMORY_TOOLS
from zerg.tools.builtin.notion_tools import TOOLS as NOTION_TOOLS
from zerg.tools.builtin.oikos_memory_tools import OIKOS_MEMORY_TOOL_NAMES
from zerg.tools.builtin.oikos_memory_tools import TOOLS as OIKOS_MEMORY_TOOLS
from zerg.tools.builtin.oikos_tools import COMMIS_TOOL_NAMES
from zerg.tools.builtin.oikos_tools import OIKOS_TOOL_NAMES
from zerg.tools.builtin.oikos_tools import OIKOS_UTILITY_TOOLS
from zerg.tools.builtin.oikos_tools import TOOLS as OIKOS_TOOLS
from zerg.tools.builtin.oikos_tools import get_commis_allowed_tools
from zerg.tools.builtin.oikos_tools import get_oikos_allowed_tools
from zerg.tools.builtin.runner_setup_tools import TOOLS as RUNNER_SETUP_TOOLS
from zerg.tools.builtin.runner_tools import TOOLS as RUNNER_TOOLS
from zerg.tools.builtin.session_tools import TOOLS as SESSION_TOOLS
from zerg.tools.builtin.slack_tools import TOOLS as SLACK_TOOLS
from zerg.tools.builtin.sms_tools import TOOLS as SMS_TOOLS
from zerg.tools.builtin.task_tools import TOOLS as TASK_TOOLS
from zerg.tools.builtin.web_fetch import TOOLS as WEB_FETCH_TOOLS
from zerg.tools.builtin.web_search import TOOLS as WEB_SEARCH_TOOLS

# Personal tools (Traccar, WHOOP, Obsidian) are David-specific integrations,
# not part of the OSS core. Gate behind PERSONAL_TOOLS_ENABLED env var.
_PERSONAL_TOOLS_ENABLED = os.getenv("PERSONAL_TOOLS_ENABLED", "").lower() in ("1", "true", "yes")

if _PERSONAL_TOOLS_ENABLED:
    from zerg.tools.builtin.personal_tools import TOOLS as PERSONAL_TOOLS
else:
    PERSONAL_TOOLS = []

BUILTIN_TOOLS = (
    CONNECTOR_TOOLS
    + CONTACT_USER_TOOLS
    + DATETIME_TOOLS
    + DISCORD_TOOLS
    + EMAIL_TOOLS
    + GITHUB_TOOLS
    + HTTP_TOOLS
    + IMESSAGE_TOOLS
    + JIRA_TOOLS
    + KNOWLEDGE_TOOLS
    + LINEAR_TOOLS
    + MEMORY_TOOLS
    + NOTION_TOOLS
    + OIKOS_MEMORY_TOOLS
    + PERSONAL_TOOLS
    + RUNNER_TOOLS
    + RUNNER_SETUP_TOOLS
    + SLACK_TOOLS
    + SESSION_TOOLS
    + SMS_TOOLS
    + OIKOS_TOOLS
    + TASK_TOOLS
    + WEB_FETCH_TOOLS
    + WEB_SEARCH_TOOLS
)

__all__ = [
    "BUILTIN_TOOLS",
    "COMMIS_TOOL_NAMES",
    "OIKOS_MEMORY_TOOL_NAMES",
    "OIKOS_TOOL_NAMES",
    "OIKOS_UTILITY_TOOLS",
    "_PERSONAL_TOOLS_ENABLED",
    "get_commis_allowed_tools",
    "get_oikos_allowed_tools",
]
