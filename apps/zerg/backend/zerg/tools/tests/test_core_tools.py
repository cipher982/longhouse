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


def test_supervisor_service_allowlist_includes_supervisor_tools():
    """supervisor_service.py allowlist must include all SUPERVISOR_TOOLS.

    This catches the bug where a tool is added to supervisor_tools.py but not
    to supervisor_service.py's hardcoded allowlist, causing "tool not in request.tools"
    errors at runtime.

    The supervisor agent's allowed_tools are stored in the database and loaded from
    supervisor_service.py. If a tool isn't in this list, the LLM can't use it.
    """
    import ast
    from pathlib import Path

    # Parse supervisor_service.py to extract tool lists
    service_path = Path(__file__).parent.parent.parent / "services" / "supervisor_service.py"
    source = service_path.read_text()
    tree = ast.parse(source)

    # Find all list assignments named "supervisor_tools"
    allowlists: list[set[str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "supervisor_tools":
                    if isinstance(node.value, ast.List):
                        tools = set()
                        for elt in node.value.elts:
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                tools.add(elt.value)
                        allowlists.append(tools)

    assert len(allowlists) >= 2, (
        f"Expected at least 2 supervisor_tools lists in supervisor_service.py, found {len(allowlists)}. "
        "There should be one for existing supervisors and one for new supervisors."
    )

    # All SUPERVISOR_TOOLS should be in every allowlist (except exempted ones)
    supervisor_names = {t.name for t in SUPERVISOR_TOOLS}
    required_tools = supervisor_names - NON_CORE_SUPERVISOR_TOOLS

    for i, allowlist in enumerate(allowlists):
        missing = sorted(required_tools - allowlist)
        assert not missing, (
            f"supervisor_service.py allowlist #{i+1} missing supervisor tools: {missing}. "
            "Add them to the supervisor_tools list in supervisor_service.py"
        )
