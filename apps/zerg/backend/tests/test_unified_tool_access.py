"""Tests for unified tool access system."""

import pytest
from langchain_core.tools import StructuredTool

from zerg.tools.registry import ImmutableToolRegistry
from zerg.tools.unified_access import ToolResolver
from zerg.tools.unified_access import create_tool_resolver
from zerg.tools.unified_access import get_tool_resolver
from zerg.tools.unified_access import reset_tool_resolver
from zerg.tools.unified_access import resolve_tool
from zerg.tools.unified_access import resolve_tools


class TestToolResolver:
    """Test the core ToolResolver functionality."""

    @pytest.fixture
    def sample_tools(self):
        """Create sample tools for testing."""

        def dummy_func1():
            return "tool1"

        def dummy_func2():
            return "tool2"

        def dummy_func3():
            return "tool3"

        return [
            StructuredTool.from_function(dummy_func1, name="tool1", description="Test tool 1"),
            StructuredTool.from_function(dummy_func2, name="tool2", description="Test tool 2"),
            StructuredTool.from_function(dummy_func3, name="prefix_tool", description="Prefixed tool"),
        ]

    @pytest.fixture
    def sample_registry(self, sample_tools):
        """Create sample registry with test tools."""
        return ImmutableToolRegistry.build([sample_tools])

    @pytest.fixture
    def resolver(self, sample_registry):
        """Create resolver from sample registry."""
        return ToolResolver.from_registry(sample_registry)

    def test_resolver_creation(self, sample_registry, sample_tools):
        """Test resolver is created correctly from registry."""
        resolver = ToolResolver.from_registry(sample_registry)

        # Check all tools are available
        assert len(resolver.get_all_tools()) == 3
        assert set(resolver.get_tool_names()) == {"tool1", "tool2", "prefix_tool"}

    def test_get_tool_single(self, resolver):
        """Test getting single tool by name."""
        tool = resolver.get_tool("tool1")
        assert tool is not None
        assert tool.name == "tool1"

        # Test non-existent tool
        assert resolver.get_tool("nonexistent") is None

    def test_resolve_tools_success(self, resolver):
        """Test resolving multiple tools successfully."""
        tools = resolver.resolve_tools(["tool1", "tool2"])
        assert len(tools) == 2
        assert {t.name for t in tools} == {"tool1", "tool2"}

    def test_resolve_tools_fail_fast(self, resolver):
        """Test resolve_tools with fail_fast=True on missing tools."""
        with pytest.raises(ValueError) as exc_info:
            resolver.resolve_tools(["tool1", "missing_tool"])

        error_msg = str(exc_info.value)
        assert "Unknown tools: ['missing_tool']" in error_msg
        assert "Available tools:" in error_msg

    def test_resolve_tools_no_fail_fast(self, resolver):
        """Test resolve_tools with fail_fast=False skips missing tools."""
        tools = resolver.resolve_tools(["tool1", "missing_tool"], fail_fast=False)
        assert len(tools) == 1
        assert tools[0].name == "tool1"

    def test_filter_by_allowlist_all(self, resolver):
        """Test allowlist filtering returns all tools when None."""
        tools = resolver.filter_by_allowlist(None)
        assert len(tools) == 3

    def test_filter_by_allowlist_exact(self, resolver):
        """Test allowlist filtering with exact matches."""
        tools = resolver.filter_by_allowlist(["tool1", "tool2"])
        assert len(tools) == 2
        assert {t.name for t in tools} == {"tool1", "tool2"}

    def test_filter_by_allowlist_wildcard(self, resolver):
        """Test allowlist filtering with wildcard patterns."""
        tools = resolver.filter_by_allowlist(["prefix_*"])
        assert len(tools) == 1
        assert tools[0].name == "prefix_tool"

    def test_filter_by_allowlist_mixed(self, resolver):
        """Test allowlist filtering with mixed exact and wildcard."""
        tools = resolver.filter_by_allowlist(["tool1", "prefix_*"])
        assert len(tools) == 2
        assert {t.name for t in tools} == {"tool1", "prefix_tool"}

    def test_has_tool(self, resolver):
        """Test tool existence checking."""
        assert resolver.has_tool("tool1") is True
        assert resolver.has_tool("nonexistent") is False

    def test_validate_tools(self, resolver):
        """Test tool validation."""
        # All valid
        missing = resolver.validate_tools(["tool1", "tool2"])
        assert missing == []

        # Some missing
        missing = resolver.validate_tools(["tool1", "missing1", "missing2"])
        assert missing == ["missing1", "missing2"]

    def test_get_tool_names_sorted(self, resolver):
        """Test tool names are returned sorted."""
        names = resolver.get_tool_names()
        assert names == ["prefix_tool", "tool1", "tool2"]  # alphabetically sorted


class TestGlobalResolverFunctions:
    """Test global resolver functions and convenience methods."""

    def setUp(self):
        """Reset global state before each test."""
        reset_tool_resolver()

    def tearDown(self):
        """Reset global state after each test."""
        reset_tool_resolver()

    def test_global_resolver_lazy_init(self):
        """Test global resolver is lazily initialized."""
        reset_tool_resolver()

        # Should initialize from production registry
        resolver = get_tool_resolver()
        assert resolver is not None

        # Should return same instance
        resolver2 = get_tool_resolver()
        assert resolver is resolver2

    def test_create_custom_resolver(self):
        """Test creating resolver from custom registry."""

        def dummy_func():
            return "test"

        tool = StructuredTool.from_function(dummy_func, name="custom_tool", description="Custom")
        registry = ImmutableToolRegistry.build([[tool]])

        resolver = create_tool_resolver(registry)
        assert resolver.has_tool("custom_tool")

    def test_resolve_tool_convenience(self):
        """Test convenience resolve_tool function."""
        # This will use the global resolver with builtin tools
        tool = resolve_tool("get_current_time", fail_fast=False)
        # Tool may or may not exist depending on test setup, just test it doesn't crash
        assert tool is None or tool.name == "get_current_time"

    def test_resolve_tool_fail_fast(self):
        """Test resolve_tool with fail_fast on missing tool."""
        with pytest.raises(ValueError):
            resolve_tool("definitely_nonexistent_tool", fail_fast=True)

    def test_resolve_tools_convenience(self):
        """Test convenience resolve_tools function."""
        # Test with empty list (should work)
        tools = resolve_tools([])
        assert tools == []


class TestToolResolverPerformance:
    """Test performance characteristics of tool resolver."""

    @pytest.fixture
    def sample_tools(self):
        """Create sample tools for testing."""

        def dummy_func1():
            return "tool1"

        def dummy_func2():
            return "tool2"

        def dummy_func3():
            return "tool3"

        return [
            StructuredTool.from_function(dummy_func1, name="tool1", description="Test tool 1"),
            StructuredTool.from_function(dummy_func2, name="tool2", description="Test tool 2"),
            StructuredTool.from_function(dummy_func3, name="prefix_tool", description="Prefixed tool"),
        ]

    @pytest.fixture
    def sample_registry(self, sample_tools):
        """Create sample registry with test tools."""
        return ImmutableToolRegistry.build([sample_tools])

    @pytest.fixture
    def resolver(self, sample_registry):
        """Create resolver from sample registry."""
        return ToolResolver.from_registry(sample_registry)

    def test_resolver_immutable(self, resolver):
        """Test resolver is immutable."""
        original_tools = resolver.get_all_tools()

        # Modify returned list shouldn't affect resolver
        original_tools.clear()

        assert len(resolver.get_all_tools()) == 3  # Original count preserved

    def test_no_runtime_dict_creation(self, resolver):
        """Test resolver doesn't create new dicts on each call."""
        # Multiple calls should return equivalent but separate lists
        tools1 = resolver.get_all_tools()
        tools2 = resolver.get_all_tools()

        # Same content
        assert {t.name for t in tools1} == {t.name for t in tools2}

        # Different list instances (defensive copying)
        assert tools1 is not tools2


class TestErrorConditions:
    """Test error handling and edge cases."""

    @pytest.fixture
    def sample_tools(self):
        """Create sample tools for testing."""

        def dummy_func1():
            return "tool1"

        def dummy_func2():
            return "tool2"

        def dummy_func3():
            return "tool3"

        return [
            StructuredTool.from_function(dummy_func1, name="tool1", description="Test tool 1"),
            StructuredTool.from_function(dummy_func2, name="tool2", description="Test tool 2"),
            StructuredTool.from_function(dummy_func3, name="prefix_tool", description="Prefixed tool"),
        ]

    @pytest.fixture
    def sample_registry(self, sample_tools):
        """Create sample registry with test tools."""
        return ImmutableToolRegistry.build([sample_tools])

    def test_empty_registry(self):
        """Test resolver with empty registry."""
        empty_registry = ImmutableToolRegistry.build([[]])
        resolver = ToolResolver.from_registry(empty_registry)

        assert resolver.get_all_tools() == []
        assert resolver.get_tool_names() == []
        assert resolver.get_tool("anything") is None

    def test_resolve_empty_list(self, sample_registry):
        """Test resolving empty tool list."""
        resolver = ToolResolver.from_registry(sample_registry)
        tools = resolver.resolve_tools([])
        assert tools == []

    def test_allowlist_empty_patterns(self, sample_registry):
        """Test allowlist with empty patterns."""
        resolver = ToolResolver.from_registry(sample_registry)
        tools = resolver.filter_by_allowlist([])
        assert len(tools) == 3  # Returns all tools when allowlist is empty
