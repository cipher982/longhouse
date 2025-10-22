"""Tests for ImmutableToolRegistry and tool aggregation."""

import pytest
from langchain_core.tools import StructuredTool

from zerg.tools.builtin import BUILTIN_TOOLS
from zerg.tools.registry import ImmutableToolRegistry


def dummy_tool_a():
    return "A"


def dummy_tool_b():
    return "B"


def test_registry_build_and_lookup():
    tool_a = StructuredTool.from_function(func=dummy_tool_a, name="a", description="A")
    tool_b = StructuredTool.from_function(func=dummy_tool_b, name="b", description="B")
    reg = ImmutableToolRegistry.build([[tool_a, tool_b]])
    assert reg.get("a") is tool_a
    assert reg.get("b") is tool_b
    assert reg.get("c") is None
    assert set(reg.list_names()) == {"a", "b"}
    # Compare by tool names since StructuredTool is not hashable
    assert {t.name for t in reg.all_tools()} == {"a", "b"}


def test_registry_duplicate_detection():
    tool_a1 = StructuredTool.from_function(func=dummy_tool_a, name="a", description="A1")
    tool_a2 = StructuredTool.from_function(func=dummy_tool_b, name="a", description="A2")
    with pytest.raises(ValueError):
        ImmutableToolRegistry.build([[tool_a1, tool_a2]])


def test_registry_filter_by_allowlist():
    tool_a = StructuredTool.from_function(func=dummy_tool_a, name="a", description="A")
    tool_b = StructuredTool.from_function(func=dummy_tool_b, name="b", description="B")
    reg = ImmutableToolRegistry.build([[tool_a, tool_b]])
    # No allowlist returns all tools by name
    assert {t.name for t in reg.filter_by_allowlist(None)} == {"a", "b"}
    # Exact match
    assert reg.filter_by_allowlist(["a"]) == [tool_a]
    # Wildcard by name
    assert {t.name for t in reg.filter_by_allowlist(["a", "b*"])} == {"a", "b"}


def test_registry_is_immutable():
    tool_a = StructuredTool.from_function(func=dummy_tool_a, name="a", description="A")
    reg = ImmutableToolRegistry.build([[tool_a]])
    with pytest.raises(Exception):
        reg._tools["b"] = StructuredTool.from_function(func=dummy_tool_b, name="b", description="B")
    with pytest.raises(Exception):
        reg._names.add("b")


def test_builtin_tools_aggregation():
    # All built-in tools should be present and unique
    names = [tool.name for tool in BUILTIN_TOOLS]
    assert len(names) == len(set(names)), "Duplicate tool names in BUILTIN_TOOLS"
    # Spot check for expected tools
    expected = {"get_current_time", "datetime_diff", "http_request", "math_eval", "generate_uuid", "container_exec"}
    assert expected.issubset(set(names))
