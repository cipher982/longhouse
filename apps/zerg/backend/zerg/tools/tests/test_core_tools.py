"""Tests to prevent CORE_TOOLS / BUILTIN_TOOLS drift.

These tests catch the bug where a tool is added to BUILTIN_TOOLS but
not to CORE_TOOLS, causing runtime failures when the LLM tries to call
the tool but it wasn't pre-loaded.
"""

from zerg.tools.builtin import BUILTIN_TOOLS
from zerg.tools.builtin.supervisor_tools import TOOLS as SUPERVISOR_TOOLS
from zerg.tools.catalog import CORE_TOOLS

# Supervisor tools that are intentionally NOT in CORE_TOOLS.
# Add tools here only if they should be lazy-loaded rather than pre-loaded.
# Most supervisor tools should be in CORE_TOOLS since they're essential.
NON_CORE_SUPERVISOR_TOOLS: set[str] = {
    "read_worker_file",  # Rarely used, can be lazy-loaded
}


def test_core_tools_are_builtin():
    """Every CORE_TOOL must exist in BUILTIN_TOOLS.

    Catches typos in CORE_TOOLS that reference non-existent tools.
    """
    builtin_names = {t.name for t in BUILTIN_TOOLS}
    missing = sorted(CORE_TOOLS - builtin_names)
    assert not missing, f"CORE_TOOLS references tools not in BUILTIN_TOOLS: {missing}"


def test_supervisor_tools_are_core_or_exempt():
    """Every supervisor tool must be in CORE_TOOLS or explicitly exempted.

    This prevents the bug where a new supervisor tool is added but not
    registered in CORE_TOOLS, causing runtime "tool not in request.tools" errors.

    If you're adding a new supervisor tool:
    1. Add it to CORE_TOOLS in zerg/tools/catalog.py (preferred)
    2. OR add it to NON_CORE_SUPERVISOR_TOOLS above with justification
    """
    supervisor_names = {t.name for t in SUPERVISOR_TOOLS}
    missing = sorted(supervisor_names - CORE_TOOLS - NON_CORE_SUPERVISOR_TOOLS)
    assert not missing, (
        f"Supervisor tools not in CORE_TOOLS: {missing}. " "Add them to CORE_TOOLS in catalog.py or exempt in test_core_tools.py"
    )


def test_non_core_exemptions_are_valid():
    """NON_CORE_SUPERVISOR_TOOLS must only contain actual supervisor tools.

    Catches stale exemptions after tools are renamed or removed.
    """
    supervisor_names = {t.name for t in SUPERVISOR_TOOLS}
    invalid = sorted(NON_CORE_SUPERVISOR_TOOLS - supervisor_names)
    assert not invalid, f"NON_CORE_SUPERVISOR_TOOLS contains non-existent tools: {invalid}"
