"""Tests for the ImmutableToolRegistry and runtime tool helpers."""

import pytest

from zerg.tools import ImmutableToolRegistry, add_runtime_tool, clear_runtime_tools, get_registry, reset_registry
from zerg.types.tools import Tool as ZergTool


class TestImmutableToolRegistry:
    """Test ImmutableToolRegistry functionality (replaces legacy mutable ToolRegistry tests)."""

    def test_build_and_lookup(self):
        """Build a registry from a tool list and look up by name."""
        tool = ZergTool.from_function(func=lambda: "ok", name="my_tool", description="desc")
        reg = ImmutableToolRegistry.build([[tool]])

        assert reg.get("my_tool") is tool
        assert reg.get("missing") is None

    def test_duplicate_names_raise(self):
        """Duplicate tool names across sources must raise ValueError."""
        tool_a = ZergTool.from_function(func=lambda: "a", name="dup", description="A")
        tool_b = ZergTool.from_function(func=lambda: "b", name="dup", description="B")

        with pytest.raises(ValueError, match="Duplicate tool name"):
            ImmutableToolRegistry.build([[tool_a, tool_b]])

    def test_filter_by_allowlist_exact(self):
        """Exact names in the allowlist are returned."""
        tool_a = ZergTool.from_function(func=lambda: "a", name="http_request", description="A")
        tool_b = ZergTool.from_function(func=lambda: "b", name="http_post", description="B")
        tool_c = ZergTool.from_function(func=lambda: "c", name="math_eval", description="C")

        reg = ImmutableToolRegistry.build([[tool_a, tool_b, tool_c]])

        filtered = reg.filter_by_allowlist(["http_request", "math_eval"])
        names = [t.name for t in filtered]
        assert sorted(names) == ["http_request", "math_eval"]

    def test_filter_by_allowlist_wildcard(self):
        """Wildcard patterns match tool name prefixes."""
        tool_a = ZergTool.from_function(func=lambda: "a", name="http_request", description="A")
        tool_b = ZergTool.from_function(func=lambda: "b", name="http_post", description="B")
        tool_c = ZergTool.from_function(func=lambda: "c", name="math_eval", description="C")

        reg = ImmutableToolRegistry.build([[tool_a, tool_b, tool_c]])

        filtered = reg.filter_by_allowlist(["http_*"])
        names = [t.name for t in filtered]
        assert sorted(names) == ["http_post", "http_request"]

    def test_filter_by_allowlist_empty_returns_all(self):
        """Empty or None allowlist returns all tools."""
        tool = ZergTool.from_function(func=lambda: "a", name="tool_a", description="A")
        reg = ImmutableToolRegistry.build([[tool]])

        assert len(reg.filter_by_allowlist([])) == 1
        assert len(reg.filter_by_allowlist(None)) == 1

    def test_list_names_and_all_tools(self):
        """list_names / all_tools / get_all_tools return correct counts."""
        tools = [
            ZergTool.from_function(func=lambda: "1", name="tool1", description="T1"),
            ZergTool.from_function(func=lambda: "2", name="tool2", description="T2"),
        ]
        reg = ImmutableToolRegistry.build([tools])

        assert sorted(reg.list_names()) == ["tool1", "tool2"]
        assert len(reg.all_tools()) == 2
        assert len(reg.get_all_tools()) == 2

    def test_has_tool(self):
        tool = ZergTool.from_function(func=lambda: "x", name="exists", description="E")
        reg = ImmutableToolRegistry.build([[tool]])

        assert reg.has_tool("exists")
        assert not reg.has_tool("missing")


class TestRuntimeTools:
    """Test the module-level runtime tool helpers."""

    def test_add_and_clear_runtime_tools(self):
        """add_runtime_tool stores tools; clear_runtime_tools empties the list."""
        tool = ZergTool.from_function(func=lambda: "rt", name="runtime_tool", description="RT")
        add_runtime_tool(tool)

        # The runtime tool should appear in a freshly built production registry.
        reset_registry()
        reg = get_registry()
        assert reg.has_tool("runtime_tool")

        # After clearing, a rebuild should not include it.
        clear_runtime_tools()
        reset_registry()
        reg = get_registry()
        assert not reg.has_tool("runtime_tool")

    def test_duplicate_runtime_tool_raises(self):
        tool = ZergTool.from_function(func=lambda: "rt", name="dup_rt", description="RT")
        add_runtime_tool(tool)
        with pytest.raises(ValueError, match="already registered"):
            add_runtime_tool(tool)


class TestBuiltinTools:
    """Test the built-in tools."""

    def test_builtin_tools_in_registry(self):
        """Built-in tools are present in the production registry."""
        reset_registry()
        reg = get_registry()

        expected_tools = [
            "get_current_time",
            "datetime_diff",
            "http_request",
        ]
        names = reg.get_tool_names()
        for tool_name in expected_tools:
            assert tool_name in names

    def test_get_current_time(self):
        """Test the get_current_time tool."""
        from zerg.tools.builtin.datetime_tools import get_current_time

        result = get_current_time()
        assert isinstance(result, str)
        assert "T" in result

    def test_datetime_diff(self):
        """Test the datetime_diff tool."""
        from zerg.tools.builtin.datetime_tools import datetime_diff

        start = "2025-01-01T00:00:00"
        end = "2025-01-01T01:00:00"

        assert datetime_diff(start, end) == 3600.0
        assert datetime_diff(start, end, "minutes") == 60.0
        assert datetime_diff(start, end, "hours") == 1.0

        start = "2025-01-01T00:00:00"
        end = "2025-01-02T00:00:00"
        assert datetime_diff(start, end, "days") == 1.0

        with pytest.raises(ValueError, match="Invalid unit"):
            datetime_diff(start, end, "invalid")
