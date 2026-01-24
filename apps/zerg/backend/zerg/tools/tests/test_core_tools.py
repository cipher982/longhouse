"""Tests to prevent CORE_TOOLS / BUILTIN_TOOLS drift.

These tests catch the bug where a tool is added to BUILTIN_TOOLS but
not to CORE_TOOLS, causing runtime failures when the LLM tries to call
the tool but it wasn't pre-loaded.

Architecture note (2026-01):
- SUPERVISOR_TOOL_NAMES is the single source of truth (defined in supervisor_tools.py)
- CORE_TOOLS imports from SUPERVISOR_TOOL_NAMES (no manual sync needed)
- supervisor_service.py uses get_supervisor_allowed_tools() (no hardcoded lists)
- This eliminates the 4-place sync problem that caused previous drift bugs
"""

from zerg.tools.builtin import BUILTIN_TOOLS
from zerg.tools.builtin.supervisor_tools import SUPERVISOR_TOOL_NAMES
from zerg.tools.builtin.supervisor_tools import SUPERVISOR_UTILITY_TOOLS
from zerg.tools.builtin.supervisor_tools import TOOLS as SUPERVISOR_TOOLS
from zerg.tools.builtin.supervisor_tools import get_supervisor_allowed_tools
from zerg.tools.catalog import CORE_TOOLS


def test_core_tools_are_builtin():
    """Every CORE_TOOL must exist in BUILTIN_TOOLS.

    Catches typos in CORE_TOOLS that reference non-existent tools.
    """
    builtin_names = {t.name for t in BUILTIN_TOOLS}
    missing = sorted(CORE_TOOLS - builtin_names)
    assert not missing, f"CORE_TOOLS references tools not in BUILTIN_TOOLS: {missing}"


def test_supervisor_tool_names_derived_from_tools():
    """SUPERVISOR_TOOL_NAMES must be derived from TOOLS list.

    This ensures the frozenset stays in sync with the actual tool definitions.
    """
    tools_names = {t.name for t in SUPERVISOR_TOOLS}
    assert SUPERVISOR_TOOL_NAMES == tools_names, (
        f"SUPERVISOR_TOOL_NAMES doesn't match TOOLS list. "
        f"In SUPERVISOR_TOOL_NAMES but not TOOLS: {SUPERVISOR_TOOL_NAMES - tools_names}. "
        f"In TOOLS but not SUPERVISOR_TOOL_NAMES: {tools_names - SUPERVISOR_TOOL_NAMES}"
    )


def test_supervisor_tools_in_core_tools():
    """All supervisor tools must be in CORE_TOOLS (via import).

    Since CORE_TOOLS now imports SUPERVISOR_TOOL_NAMES, this should always pass.
    This test guards against someone breaking the import relationship.
    """
    missing = sorted(SUPERVISOR_TOOL_NAMES - CORE_TOOLS)
    assert not missing, (
        f"Supervisor tools missing from CORE_TOOLS: {missing}. " "CORE_TOOLS should import SUPERVISOR_TOOL_NAMES from supervisor_tools.py"
    )


def test_utility_tools_exist_in_builtin():
    """SUPERVISOR_UTILITY_TOOLS must reference actual builtin tools.

    Catches typos or stale tool names in the utility tools list.
    """
    builtin_names = {t.name for t in BUILTIN_TOOLS}
    missing = sorted(SUPERVISOR_UTILITY_TOOLS - builtin_names)
    assert not missing, (
        f"SUPERVISOR_UTILITY_TOOLS references non-existent tools: {missing}. "
        "Remove them from SUPERVISOR_UTILITY_TOOLS in supervisor_tools.py"
    )


def test_get_supervisor_allowed_tools_complete():
    """get_supervisor_allowed_tools() must return all expected tools.

    This is the function used by supervisor_service.py to set allowed_tools.
    """
    allowed = set(get_supervisor_allowed_tools())

    # Must include all supervisor tools
    missing_supervisor = sorted(SUPERVISOR_TOOL_NAMES - allowed)
    assert not missing_supervisor, f"get_supervisor_allowed_tools() missing supervisor tools: {missing_supervisor}"

    # Must include all utility tools
    missing_utility = sorted(SUPERVISOR_UTILITY_TOOLS - allowed)
    assert not missing_utility, f"get_supervisor_allowed_tools() missing utility tools: {missing_utility}"

    # Should be exactly the union (no extras)
    expected = SUPERVISOR_TOOL_NAMES | SUPERVISOR_UTILITY_TOOLS
    extras = sorted(allowed - expected)
    assert not extras, f"get_supervisor_allowed_tools() has unexpected tools: {extras}"


def test_supervisor_service_uses_centralized_function():
    """supervisor_service.py must use get_supervisor_allowed_tools() not hardcoded lists.

    This guards against someone re-introducing hardcoded tool lists.
    """
    import ast
    from pathlib import Path

    # Parse supervisor_service.py to check for hardcoded lists
    service_path = Path(__file__).parent.parent.parent / "services" / "supervisor_service.py"
    source = service_path.read_text()
    tree = ast.parse(source)

    # Find all list assignments named "supervisor_tools"
    hardcoded_lists = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "supervisor_tools":
                    # Check if it's a hardcoded list (ast.List) vs function call
                    if isinstance(node.value, ast.List):
                        hardcoded_lists.append(node.lineno)

    assert not hardcoded_lists, (
        f"supervisor_service.py has hardcoded supervisor_tools lists at lines {hardcoded_lists}. "
        "Use get_supervisor_allowed_tools() from supervisor_tools.py instead."
    )

    # Verify the function is imported and used
    assert (
        "get_supervisor_allowed_tools" in source
    ), "supervisor_service.py should import get_supervisor_allowed_tools from supervisor_tools.py"
