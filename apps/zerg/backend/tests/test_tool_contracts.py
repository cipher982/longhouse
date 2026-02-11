"""Test tool contract validation system."""

import pytest

from zerg.tools.builtin import _PERSONAL_TOOLS_ENABLED
from zerg.tools.builtin import BUILTIN_TOOLS
from zerg.tools.generated.tool_definitions import TOOL_SERVER_MAPPING
from zerg.tools.generated.tool_definitions import ServerName
from zerg.tools.generated.tool_definitions import ToolName
from zerg.tools.generated.tool_definitions import get_expected_server
from zerg.tools.generated.tool_definitions import list_all_tools
from zerg.tools.generated.tool_definitions import validate_tool_registration

# Personal tools remain in the schema but are gated behind PERSONAL_TOOLS_ENABLED.
# When disabled, exclude them from contract comparisons.
_PERSONAL_TOOL_NAMES = {"get_current_location", "get_whoop_data", "search_notes"}


def test_all_builtin_tools_in_contract():
    """Test that all builtin tools are covered by contracts."""
    builtin_tool_names = {tool.name for tool in BUILTIN_TOOLS}
    contract_tool_names = set(list_all_tools())

    # All builtin tools should be in contract
    missing_from_contract = builtin_tool_names - contract_tool_names
    assert not missing_from_contract, f"Tools missing from contract: {missing_from_contract}"

    # Contract should not have extra tools (would indicate stale schema)
    # When personal tools are disabled, they'll be "extra" in the schema -- that's expected.
    extra_in_contract = contract_tool_names - builtin_tool_names
    if not _PERSONAL_TOOLS_ENABLED:
        extra_in_contract -= _PERSONAL_TOOL_NAMES
    if extra_in_contract:
        pytest.warn(f"Extra tools in contract (update schema): {extra_in_contract}")


def test_tool_server_mapping_validation():
    """Test tool-server mapping validation."""
    # Test valid mappings
    assert validate_tool_registration("http_request", "http")

    # Test invalid mappings
    assert not validate_tool_registration("http_request", "math")
    assert not validate_tool_registration("invalid_tool", "http")
    assert not validate_tool_registration("http_request", "invalid_server")


def test_expected_server_lookup():
    """Test expected server lookup functionality."""
    assert get_expected_server("http_request") == "http"
    assert get_expected_server("get_current_time") == "datetime"
    assert get_expected_server("invalid_tool") is None


def test_enum_roundtrip():
    """Test that enum values can roundtrip through string conversion."""
    for tool_name in ToolName:
        assert ToolName(tool_name.value) == tool_name

    for server_name in ServerName:
        assert ServerName(server_name.value) == server_name


def test_tool_mapping_completeness():
    """Test that all defined tools have server mappings."""
    all_tools = set(ToolName)
    mapped_tools = set(TOOL_SERVER_MAPPING.keys())

    unmapped_tools = all_tools - mapped_tools
    assert not unmapped_tools, f"Tools without server mapping: {unmapped_tools}"


class TestContractBreakageDetection:
    """Test that contract breakage is properly detected."""

    def test_missing_tool_detection(self):
        """Test detection of tools missing from registry."""
        # This would fail if a tool in the schema wasn't in BUILTIN_TOOLS
        all_schema_tools = set(list_all_tools())
        builtin_tools = {tool.name for tool in BUILTIN_TOOLS}

        missing = all_schema_tools - builtin_tools
        # Personal tools are gated behind PERSONAL_TOOLS_ENABLED -- not a contract violation
        if not _PERSONAL_TOOLS_ENABLED:
            missing -= _PERSONAL_TOOL_NAMES
        assert not missing, f"Schema defines tools not in registry: {missing}"

    def test_tool_server_consistency(self):
        """Test that tools are in expected server modules."""
        from zerg.tools.builtin.datetime_tools import TOOLS as DATETIME_TOOLS
        from zerg.tools.builtin.http_tools import TOOLS as HTTP_TOOLS
        from zerg.tools.builtin.oikos_tools import TOOLS as OIKOS_TOOLS
        from zerg.tools.builtin.personal_tools import TOOLS as PERSONAL_TOOLS

        # Build actual tool-to-server mapping from module structure
        actual_mapping = {}
        for tool in HTTP_TOOLS:
            actual_mapping[tool.name] = "http"
        for tool in DATETIME_TOOLS:
            actual_mapping[tool.name] = "datetime"
        for tool in OIKOS_TOOLS:
            actual_mapping[tool.name] = "oikos"
        for tool in PERSONAL_TOOLS:
            actual_mapping[tool.name] = "personal"

        # Verify against contract
        for tool_name, expected_server in actual_mapping.items():
            contract_server = get_expected_server(tool_name)
            assert contract_server == expected_server, (
                f"Tool {tool_name}: contract expects {contract_server}, actually in {expected_server}"
            )
