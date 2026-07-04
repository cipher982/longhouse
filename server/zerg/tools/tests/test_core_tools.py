"""Tests to prevent lazy-binder core tool drift."""

from zerg.tools.builtin import BUILTIN_TOOLS
from zerg.tools.builtin.memory_tools import MEMORY_FILE_TOOL_NAMES
from zerg.tools.lazy_binder import COMMIS_CORE_TOOLS
from zerg.tools.lazy_binder import CORE_TOOLS


def test_core_tools_are_builtin():
    """Every core tool must exist in BUILTIN_TOOLS."""
    builtin_names = {t.name for t in BUILTIN_TOOLS}
    missing = sorted(CORE_TOOLS - builtin_names)
    assert not missing, f"CORE_TOOLS references tools not in BUILTIN_TOOLS: {missing}"


def test_commis_core_tools_are_builtin():
    """Every commis core tool must exist in BUILTIN_TOOLS."""
    builtin_names = {t.name for t in BUILTIN_TOOLS}
    missing = sorted(COMMIS_CORE_TOOLS - builtin_names)
    assert not missing, f"COMMIS_CORE_TOOLS references tools not in BUILTIN_TOOLS: {missing}"


def test_commis_core_tools_no_coordinator_only_tools():
    """Commis agents should not receive tools that spawn or steer peer work."""
    coordinator_only = {
        "message_session",
        "peers",
        "runner_create_enroll_token",
    }
    overlap = sorted(COMMIS_CORE_TOOLS & coordinator_only)
    assert not overlap, f"COMMIS_CORE_TOOLS includes coordinator-only tools: {overlap}"


def test_memory_files_not_in_default_core_tool_sets():
    """Memory files are opt-in and should not appear in default core sets."""
    assert not (CORE_TOOLS & MEMORY_FILE_TOOL_NAMES)
    assert not (COMMIS_CORE_TOOLS & MEMORY_FILE_TOOL_NAMES)


def test_commis_has_essential_execution_tools():
    """Execution agents must include essential tools."""
    essential = {
        "web_fetch",
        "http_request",
        "get_current_time",
        "search_sessions",
        "get_session_detail",
    }
    missing = sorted(essential - COMMIS_CORE_TOOLS)
    assert not missing, f"COMMIS_CORE_TOOLS missing essential execution tools: {missing}"
