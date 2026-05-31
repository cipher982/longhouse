"""Built-in tools for Zerg fiches.

This module contains the standard tools that come with the platform.
All tools in this module are aggregated into a single list for registry construction.
"""

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
from zerg.tools.builtin.runner_setup_tools import TOOLS as RUNNER_SETUP_TOOLS
from zerg.tools.builtin.runner_tools import TOOLS as RUNNER_TOOLS
from zerg.tools.builtin.session_coordination_tools import TOOLS as SESSION_COORDINATION_TOOLS
from zerg.tools.builtin.session_tools import TOOLS as SESSION_TOOLS
from zerg.tools.builtin.slack_tools import TOOLS as SLACK_TOOLS
from zerg.tools.builtin.sms_tools import TOOLS as SMS_TOOLS
from zerg.tools.builtin.task_tools import TOOLS as TASK_TOOLS
from zerg.tools.builtin.telegram_tools import send_telegram_tool as _TELEGRAM_TOOL
from zerg.tools.builtin.web_fetch import TOOLS as WEB_FETCH_TOOLS
from zerg.tools.builtin.web_search import TOOLS as WEB_SEARCH_TOOLS

TELEGRAM_TOOLS = [_TELEGRAM_TOOL]

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
    + RUNNER_TOOLS
    + RUNNER_SETUP_TOOLS
    + SLACK_TOOLS
    + SESSION_COORDINATION_TOOLS
    + SESSION_TOOLS
    + SMS_TOOLS
    + TASK_TOOLS
    + TELEGRAM_TOOLS
    + WEB_FETCH_TOOLS
    + WEB_SEARCH_TOOLS
)

__all__ = [
    "BUILTIN_TOOLS",
]
