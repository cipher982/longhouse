"""Built-in tools for Longhouse runtime agents.

All tools in this module are aggregated into a single list for registry construction.
"""

from zerg.tools.builtin.datetime_tools import TOOLS as DATETIME_TOOLS
from zerg.tools.builtin.http_tools import TOOLS as HTTP_TOOLS
from zerg.tools.builtin.memory_tools import TOOLS as MEMORY_TOOLS
from zerg.tools.builtin.notion_tools import TOOLS as NOTION_TOOLS
from zerg.tools.builtin.runner_setup_tools import TOOLS as RUNNER_SETUP_TOOLS
from zerg.tools.builtin.runner_tools import TOOLS as RUNNER_TOOLS
from zerg.tools.builtin.session_tools import TOOLS as SESSION_TOOLS
from zerg.tools.builtin.slack_tools import TOOLS as SLACK_TOOLS
from zerg.tools.builtin.web_fetch import TOOLS as WEB_FETCH_TOOLS
from zerg.tools.builtin.web_search import TOOLS as WEB_SEARCH_TOOLS

BUILTIN_TOOLS = (
    DATETIME_TOOLS
    + HTTP_TOOLS
    + MEMORY_TOOLS
    + NOTION_TOOLS
    + RUNNER_TOOLS
    + RUNNER_SETUP_TOOLS
    + SLACK_TOOLS
    + SESSION_TOOLS
    + WEB_FETCH_TOOLS
    + WEB_SEARCH_TOOLS
)

__all__ = [
    "BUILTIN_TOOLS",
]
