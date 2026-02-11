"""Tests to prevent CORE_TOOLS / BUILTIN_TOOLS drift.

These tests catch the bug where a tool is added to BUILTIN_TOOLS but
not to CORE_TOOLS, causing runtime failures when the LLM tries to call
the tool but it wasn't pre-loaded.

Architecture note (2026-01):
- OIKOS_TOOL_NAMES is the single source of truth (defined in oikos_tools.py)
- CORE_TOOLS imports from OIKOS_TOOL_NAMES (no manual sync needed)
- COMMIS_TOOL_NAMES defines the commis-focused tool subset (no coordinator tools)
- oikos_service.py uses get_oikos_allowed_tools() (no hardcoded lists)
- This eliminates the 4-place sync problem that caused previous drift bugs
"""

from zerg.tools.builtin import BUILTIN_TOOLS
from zerg.tools.builtin.oikos_tools import COMMIS_TOOL_NAMES
from zerg.tools.builtin.oikos_tools import OIKOS_TOOL_NAMES
from zerg.tools.builtin.oikos_tools import OIKOS_UTILITY_TOOLS
from zerg.tools.builtin.oikos_tools import TOOLS as OIKOS_TOOLS
from zerg.tools.builtin.oikos_tools import get_commis_allowed_tools
from zerg.tools.builtin.oikos_tools import get_oikos_allowed_tools
from zerg.tools.lazy_binder import COMMIS_CORE_TOOLS
from zerg.tools.lazy_binder import CORE_TOOLS


def test_core_tools_are_builtin():
    """Every CORE_TOOL must exist in BUILTIN_TOOLS.

    Catches typos in CORE_TOOLS that reference non-existent tools.
    """
    builtin_names = {t.name for t in BUILTIN_TOOLS}
    missing = sorted(CORE_TOOLS - builtin_names)
    assert not missing, f"CORE_TOOLS references tools not in BUILTIN_TOOLS: {missing}"


def test_oikos_tool_names_derived_from_tools():
    """OIKOS_TOOL_NAMES must be derived from TOOLS list.

    This ensures the frozenset stays in sync with the actual tool definitions.
    """
    tools_names = {t.name for t in OIKOS_TOOLS}
    assert OIKOS_TOOL_NAMES == tools_names, (
        f"OIKOS_TOOL_NAMES doesn't match TOOLS list. "
        f"In OIKOS_TOOL_NAMES but not TOOLS: {OIKOS_TOOL_NAMES - tools_names}. "
        f"In TOOLS but not OIKOS_TOOL_NAMES: {tools_names - OIKOS_TOOL_NAMES}"
    )


def test_oikos_tools_in_core_tools():
    """All oikos tools must be in CORE_TOOLS (via import).

    Since CORE_TOOLS now imports OIKOS_TOOL_NAMES, this should always pass.
    This test guards against someone breaking the import relationship.
    """
    missing = sorted(OIKOS_TOOL_NAMES - CORE_TOOLS)
    assert not missing, f"Oikos tools missing from CORE_TOOLS: {missing}. " "CORE_TOOLS should import OIKOS_TOOL_NAMES from oikos_tools.py"


def test_utility_tools_exist_in_builtin():
    """OIKOS_UTILITY_TOOLS must reference actual builtin tools.

    Catches typos or stale tool names in the utility tools list.
    """
    builtin_names = {t.name for t in BUILTIN_TOOLS}
    missing = sorted(OIKOS_UTILITY_TOOLS - builtin_names)
    assert not missing, (
        f"OIKOS_UTILITY_TOOLS references non-existent tools: {missing}. " "Remove them from OIKOS_UTILITY_TOOLS in oikos_tools.py"
    )


def test_get_oikos_allowed_tools_complete():
    """get_oikos_allowed_tools() must return all expected tools.

    This is the function used by oikos_service.py to set allowed_tools.
    """
    allowed = set(get_oikos_allowed_tools())

    # Must include all oikos tools
    missing_oikos = sorted(OIKOS_TOOL_NAMES - allowed)
    assert not missing_oikos, f"get_oikos_allowed_tools() missing oikos tools: {missing_oikos}"

    # Must include all utility tools
    missing_utility = sorted(OIKOS_UTILITY_TOOLS - allowed)
    assert not missing_utility, f"get_oikos_allowed_tools() missing utility tools: {missing_utility}"

    # Should be exactly the union (no extras)
    expected = OIKOS_TOOL_NAMES | OIKOS_UTILITY_TOOLS
    extras = sorted(allowed - expected)
    assert not extras, f"get_oikos_allowed_tools() has unexpected tools: {extras}"


def test_oikos_service_uses_centralized_function():
    """oikos_service.py must use get_oikos_allowed_tools() not hardcoded lists.

    This guards against someone re-introducing hardcoded tool lists.
    """
    import ast
    from pathlib import Path

    # Parse oikos_service.py to check for hardcoded lists
    service_path = Path(__file__).parent.parent.parent / "services" / "oikos_service.py"
    source = service_path.read_text()
    tree = ast.parse(source)

    # Find all list assignments named "oikos_tools"
    hardcoded_lists = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "oikos_tools":
                    # Check if it's a hardcoded list (ast.List) vs function call
                    if isinstance(node.value, ast.List):
                        hardcoded_lists.append(node.lineno)

    assert not hardcoded_lists, (
        f"oikos_service.py has hardcoded oikos_tools lists at lines {hardcoded_lists}. "
        "Use get_oikos_allowed_tools() from oikos_tools.py instead."
    )

    # Verify the function is imported and used
    assert "get_oikos_allowed_tools" in source, "oikos_service.py should import get_oikos_allowed_tools from oikos_tools.py"


# ---------------------------------------------------------------------------
# Commis tool subset tests
# ---------------------------------------------------------------------------


def test_commis_tools_exist_in_builtin():
    """Every COMMIS_TOOL_NAMES entry must exist in BUILTIN_TOOLS.

    Catches typos or stale tool names in the commis tool set.
    """
    builtin_names = {t.name for t in BUILTIN_TOOLS}
    missing = sorted(COMMIS_TOOL_NAMES - builtin_names)
    assert not missing, (
        f"COMMIS_TOOL_NAMES references non-existent tools: {missing}. " "Remove them from COMMIS_TOOL_NAMES in oikos_tools.py"
    )


def test_commis_tools_no_coordinator_tools():
    """Commis must NOT have coordinator tools (spawn_commis, manage commis jobs).

    Commis agents are execution workers — they should not be able to spawn
    other commis or manage the oikos coordination layer.
    """
    coordinator_tools = {
        "spawn_commis",
        "spawn_workspace_commis",
        "list_commiss",
        "read_commis_result",
        "get_commis_evidence",
        "get_tool_output",
        "read_commis_file",
        "peek_commis_output",
        "grep_commiss",
        "get_commis_metadata",
        "check_commis_status",
        "cancel_commis",
        "wait_for_commis",
        "request_session_selection",
    }
    overlap = sorted(COMMIS_TOOL_NAMES & coordinator_tools)
    assert not overlap, (
        f"COMMIS_TOOL_NAMES includes coordinator tools: {overlap}. " "Commis should not have access to coordinator/oikos tools."
    )


def test_commis_tools_no_oikos_memory():
    """Commis must NOT have oikos-level memory tools.

    Oikos memory (save_memory, search_memory, etc.) is for the coordinator's
    persistent context. Commis agents use memory files (memory_write/read)
    for workspace-scoped context instead.
    """
    from zerg.tools.builtin.oikos_memory_tools import OIKOS_MEMORY_TOOL_NAMES

    overlap = sorted(COMMIS_TOOL_NAMES & OIKOS_MEMORY_TOOL_NAMES)
    assert not overlap, (
        f"COMMIS_TOOL_NAMES includes oikos memory tools: {overlap}. "
        "Commis should use memory files (memory_write/read), not oikos memory."
    )


def test_commis_and_oikos_are_disjoint_where_expected():
    """OIKOS_TOOL_NAMES and COMMIS_TOOL_NAMES should not overlap.

    Oikos-specific tools (commis management) should not appear in commis.
    Shared utility tools appear in OIKOS_UTILITY_TOOLS, not OIKOS_TOOL_NAMES.
    """
    overlap = sorted(OIKOS_TOOL_NAMES & COMMIS_TOOL_NAMES)
    assert not overlap, (
        f"OIKOS_TOOL_NAMES and COMMIS_TOOL_NAMES overlap: {overlap}. " "Oikos-specific tools should not be in commis tool set."
    )


def test_get_commis_allowed_tools_matches_set():
    """get_commis_allowed_tools() must return exactly COMMIS_TOOL_NAMES."""
    allowed = set(get_commis_allowed_tools())
    assert allowed == COMMIS_TOOL_NAMES, (
        f"get_commis_allowed_tools() doesn't match COMMIS_TOOL_NAMES. "
        f"Missing: {sorted(COMMIS_TOOL_NAMES - allowed)}. "
        f"Extras: {sorted(allowed - COMMIS_TOOL_NAMES)}"
    )


def test_commis_core_tools_match_commis_tool_names():
    """COMMIS_CORE_TOOLS in lazy_binder must equal COMMIS_TOOL_NAMES.

    Guards against the import relationship breaking.
    """
    assert COMMIS_CORE_TOOLS == COMMIS_TOOL_NAMES, (
        f"COMMIS_CORE_TOOLS doesn't match COMMIS_TOOL_NAMES. "
        f"Missing: {sorted(COMMIS_TOOL_NAMES - COMMIS_CORE_TOOLS)}. "
        f"Extras: {sorted(COMMIS_CORE_TOOLS - COMMIS_TOOL_NAMES)}"
    )


def test_commis_has_essential_execution_tools():
    """Commis must include essential execution tools.

    These are the minimum tools a commis agent needs to be useful.
    """
    essential = {
        "contact_user",  # Ask the user questions
        "web_fetch",  # Fetch web content
        "http_request",  # Make API calls
        "get_current_time",  # Know when it is
        "knowledge_search",  # Search knowledge base
    }
    missing = sorted(essential - COMMIS_TOOL_NAMES)
    assert not missing, f"COMMIS_TOOL_NAMES missing essential execution tools: {missing}"
