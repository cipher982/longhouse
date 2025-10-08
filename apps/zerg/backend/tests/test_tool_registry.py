"""Tests for the tool registry and built-in tools."""

import pytest
from langchain_core.tools import StructuredTool

from zerg.tools.registry import ToolRegistry
from zerg.tools.registry import get_registry
from zerg.tools.registry import register_tool


class TestToolRegistry:
    """Test the ToolRegistry functionality."""

    def test_singleton_pattern(self):
        """Test that ToolRegistry follows singleton pattern."""
        registry1 = ToolRegistry()
        registry2 = ToolRegistry()
        assert registry1 is registry2

    def test_register_tool_decorator(self):
        """Test the @register_tool decorator."""
        # Get registry but don't clear it (to avoid affecting other tests)
        registry = get_registry()

        @register_tool(name="test_tool", description="A test tool")
        def my_test_tool(x: int) -> int:
            """Double the input."""
            return x * 2

        # Check tool was registered
        tool = registry.get_tool("test_tool")
        assert tool is not None
        assert isinstance(tool, StructuredTool)
        assert tool.name == "test_tool"
        assert tool.description == "A test tool"

        # Test tool execution
        result = tool.invoke({"x": 5})
        assert result == 10

    def test_get_all_tools(self):
        """Test getting all registered tools."""
        # Create a new registry instance for this test
        test_registry = ToolRegistry()
        test_registry._tools = {}  # Clear it

        # Create tools directly as StructuredTool instances
        from langchain_core.tools import StructuredTool

        tool1 = StructuredTool.from_function(func=lambda: "tool1", name="tool1", description="Tool 1")
        tool2 = StructuredTool.from_function(func=lambda: "tool2", name="tool2", description="Tool 2")

        test_registry.register(tool1)
        test_registry.register(tool2)

        tools = test_registry.get_all_tools()
        assert len(tools) == 2
        tool_names = [t.name for t in tools]
        assert "tool1" in tool_names
        assert "tool2" in tool_names

    def test_filter_tools_by_allowlist(self):
        """Test filtering tools by allowlist."""
        # Create a new registry instance for this test
        test_registry = ToolRegistry()
        test_registry._tools = {}  # Clear it

        # Create tools directly as StructuredTool instances
        from langchain_core.tools import StructuredTool

        http_request_tool = StructuredTool.from_function(
            func=lambda: "http_request", name="http_request", description="HTTP Request"
        )
        http_post_tool = StructuredTool.from_function(
            func=lambda: "http_post", name="http_post", description="HTTP POST"
        )
        math_eval_tool = StructuredTool.from_function(
            func=lambda: "math_eval", name="math_eval", description="Math eval"
        )

        test_registry.register(http_request_tool)
        test_registry.register(http_post_tool)
        test_registry.register(math_eval_tool)

        # Test with specific allowlist
        tools = test_registry.filter_tools_by_allowlist(["http_request", "math_eval"])
        tool_names = [t.name for t in tools]
        assert len(tools) == 2
        assert "http_request" in tool_names
        assert "math_eval" in tool_names
        assert "http_post" not in tool_names

        # Test with wildcard
        tools = test_registry.filter_tools_by_allowlist(["http_*"])
        tool_names = [t.name for t in tools]
        assert len(tools) == 2
        assert "http_request" in tool_names
        assert "http_post" in tool_names
        assert "math_eval" not in tool_names

        # Test with empty allowlist (all tools allowed)
        tools = test_registry.filter_tools_by_allowlist([])
        assert len(tools) == 3

        # Test with None allowlist (all tools allowed)
        tools = test_registry.filter_tools_by_allowlist(None)
        assert len(tools) == 3


class TestBuiltinTools:
    """Test the built-in tools."""

    def test_builtin_tools_registered(self):
        """Test that built-in tools are registered when imported."""
        # Import builtin tools to trigger registration
        import zerg.tools.builtin  # noqa: F401

        registry = get_registry()

        # Check that expected tools are registered
        expected_tools = [
            "get_current_time",
            "datetime_diff",
            "http_request",
            "math_eval",
            "generate_uuid",
        ]

        registered_names = registry.list_tool_names()
        for tool_name in expected_tools:
            assert tool_name in registered_names

    def test_get_current_time(self):
        """Test the get_current_time tool."""
        from zerg.tools.builtin.datetime_tools import get_current_time

        result = get_current_time()
        assert isinstance(result, str)
        # Should be ISO format
        assert "T" in result

    def test_datetime_diff(self):
        """Test the datetime_diff tool."""
        from zerg.tools.builtin.datetime_tools import datetime_diff

        # Test basic difference
        start = "2025-01-01T00:00:00"
        end = "2025-01-01T01:00:00"

        # Test seconds (default)
        diff = datetime_diff(start, end)
        assert diff == 3600.0

        # Test minutes
        diff = datetime_diff(start, end, "minutes")
        assert diff == 60.0

        # Test hours
        diff = datetime_diff(start, end, "hours")
        assert diff == 1.0

        # Test days
        start = "2025-01-01T00:00:00"
        end = "2025-01-02T00:00:00"
        diff = datetime_diff(start, end, "days")
        assert diff == 1.0

        # Test invalid unit
        with pytest.raises(ValueError, match="Invalid unit"):
            datetime_diff(start, end, "invalid")

    def test_math_eval(self):
        """Test the math_eval tool."""
        from zerg.tools.builtin.math_tools import math_eval

        # Basic arithmetic
        assert math_eval("2 + 2") == 4
        assert math_eval("10 - 5") == 5
        assert math_eval("3 * 4") == 12
        assert math_eval("10 / 2") == 5.0
        assert math_eval("10 // 3") == 3
        assert math_eval("10 % 3") == 1
        assert math_eval("2 ** 3") == 8

        # Parentheses
        assert math_eval("(2 + 3) * 4") == 20

        # Functions
        assert math_eval("abs(-5)") == 5
        assert math_eval("round(3.7)") == 4
        assert math_eval("min([1, 5, 3])") == 1
        assert math_eval("max([1, 5, 3])") == 5
        assert math_eval("sum([1, 2, 3])") == 6

        # Invalid expressions
        with pytest.raises(ValueError):
            math_eval("import os")
        with pytest.raises(ValueError):
            math_eval("x = 5")
        with pytest.raises(ValueError):
            math_eval("1 / 0")

    def test_generate_uuid(self):
        """Test the generate_uuid tool."""
        from zerg.tools.builtin.uuid_tools import generate_uuid

        # Test default (v4)
        uuid1 = generate_uuid()
        assert isinstance(uuid1, str)
        assert len(uuid1) == 36  # Standard UUID format
        assert uuid1.count("-") == 4

        # Test uniqueness
        uuid2 = generate_uuid()
        assert uuid1 != uuid2

        # Test version 1
        uuid_v1 = generate_uuid(version=1)
        assert isinstance(uuid_v1, str)
        assert len(uuid_v1) == 36

        # Test version 5 with namespace
        uuid_v5 = generate_uuid(version=5, namespace="dns", name="example.com")
        assert isinstance(uuid_v5, str)
        assert len(uuid_v5) == 36

        # Test invalid version
        with pytest.raises(ValueError, match="Invalid UUID version"):
            generate_uuid(version=99)

        # Test v3/v5 without required params
        with pytest.raises(ValueError, match="require both namespace and name"):
            generate_uuid(version=3)
