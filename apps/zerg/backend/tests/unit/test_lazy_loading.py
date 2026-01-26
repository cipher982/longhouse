"""Unit tests for lazy tool loading behavior.

These tests lock in the lazy loading contract to prevent silent failures:
1. LazyToolBinder respects allowlist for core tools
2. search_tools respects allowlist filtering
3. search_tools respects max_results cap
4. Rebind mechanism works correctly after search_tools
"""

import json
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from zerg.tools.catalog import CORE_TOOLS


# ---------------------------------------------------------------------------
# LazyToolBinder Tests
# ---------------------------------------------------------------------------


class TestLazyToolBinderAllowlist:
    """Test that LazyToolBinder respects allowlist even for core tools."""

    def test_core_tools_filtered_by_allowlist(self):
        """Core tools not in allowlist should NOT be loaded."""
        from zerg.tools.lazy_binder import LazyToolBinder

        # Create a mock resolver that returns mock tools
        mock_resolver = MagicMock()
        mock_tool = MagicMock()
        mock_tool.name = "spawn_commis"
        mock_resolver.get_tool.return_value = mock_tool

        # Only allow spawn_commis, not other core tools
        allowed = ["spawn_commis"]
        binder = LazyToolBinder(mock_resolver, allowed_tools=allowed)

        # Should only have spawn_commis loaded
        loaded_names = binder.loaded_tool_names
        assert "spawn_commis" in loaded_names
        # Other core tools should NOT be loaded
        for name in CORE_TOOLS:
            if name != "spawn_commis":
                assert name not in loaded_names, f"{name} should not be loaded"

    def test_wildcard_allowlist(self):
        """Wildcard patterns in allowlist should work."""
        from zerg.tools.lazy_binder import LazyToolBinder

        mock_resolver = MagicMock()

        def mock_get_tool(name):
            tool = MagicMock()
            tool.name = name
            return tool

        mock_resolver.get_tool.side_effect = mock_get_tool

        # Allow all github_* tools and spawn_commis
        allowed = ["spawn_commis", "github_*"]
        binder = LazyToolBinder(mock_resolver, allowed_tools=allowed)

        # spawn_commis should be loaded (it's a core tool)
        assert binder.is_loaded("spawn_commis")

        # Try to load a github tool - should succeed
        tool = binder.get_tool("github_list_issues")
        assert tool is not None
        assert binder.is_loaded("github_list_issues")

        # Try to load a non-matching tool - should fail
        tool = binder.get_tool("send_email")
        assert tool is None
        assert not binder.is_loaded("send_email")

    def test_no_allowlist_allows_all(self):
        """When allowlist is None, all tools should be allowed."""
        from zerg.tools.lazy_binder import LazyToolBinder

        mock_resolver = MagicMock()

        def mock_get_tool(name):
            tool = MagicMock()
            tool.name = name
            return tool

        mock_resolver.get_tool.side_effect = mock_get_tool

        binder = LazyToolBinder(mock_resolver, allowed_tools=None)

        # All core tools should be loaded
        for name in CORE_TOOLS:
            assert binder.is_loaded(name), f"{name} should be loaded with no allowlist"

    def test_loaded_tool_names_property(self):
        """loaded_tool_names should return frozenset of loaded tool names."""
        from zerg.tools.lazy_binder import LazyToolBinder

        mock_resolver = MagicMock()

        def mock_get_tool(name):
            tool = MagicMock()
            tool.name = name
            return tool

        mock_resolver.get_tool.side_effect = mock_get_tool

        allowed = ["spawn_commis", "contact_user"]
        binder = LazyToolBinder(mock_resolver, allowed_tools=allowed)

        names = binder.loaded_tool_names
        assert isinstance(names, frozenset)
        assert "spawn_commis" in names
        assert "contact_user" in names


# ---------------------------------------------------------------------------
# Search Context Tests
# ---------------------------------------------------------------------------


class TestSearchContext:
    """Test search context (allowlist and max_results) for search_tools."""

    def test_set_and_clear_context(self):
        """set_search_context and clear_search_context should work."""
        from zerg.tools.tool_search import _is_tool_allowed
        from zerg.tools.tool_search import _search_allowed_tools
        from zerg.tools.tool_search import _search_max_results
        from zerg.tools.tool_search import clear_search_context
        from zerg.tools.tool_search import set_search_context

        # Default state - all allowed
        assert _is_tool_allowed("anything") is True

        # Set context with allowlist
        set_search_context(allowed_tools=["github_*", "send_email"], max_results=5)

        # Check allowlist is applied
        assert _is_tool_allowed("github_list_issues") is True
        assert _is_tool_allowed("github_create_pr") is True
        assert _is_tool_allowed("send_email") is True
        assert _is_tool_allowed("web_search") is False

        # Check max_results
        assert _search_max_results.get() == 5

        # Clear context
        clear_search_context()

        # Should be back to default
        assert _is_tool_allowed("anything") is True
        assert _search_max_results.get() == 20

    def test_is_tool_allowed_wildcard(self):
        """_is_tool_allowed should handle wildcards correctly."""
        from zerg.tools.tool_search import _is_tool_allowed
        from zerg.tools.tool_search import clear_search_context
        from zerg.tools.tool_search import set_search_context

        set_search_context(allowed_tools=["github_*", "jira_*"])

        # Matching wildcards
        assert _is_tool_allowed("github_list_issues") is True
        assert _is_tool_allowed("github_") is True  # Edge case - matches prefix
        assert _is_tool_allowed("jira_create_issue") is True

        # Non-matching
        assert _is_tool_allowed("linear_list_issues") is False
        assert _is_tool_allowed("git_hub_issues") is False  # Close but not matching

        clear_search_context()


# ---------------------------------------------------------------------------
# Search Tools Filtering Tests
# ---------------------------------------------------------------------------


class TestSearchToolsFiltering:
    """Test that search_tools_for_agent respects context filtering."""

    @pytest.mark.asyncio
    async def test_search_results_filtered_by_allowlist(self):
        """search_tools_for_agent should filter results by allowlist."""
        from zerg.tools.tool_search import clear_search_context
        from zerg.tools.tool_search import search_tools_for_agent
        from zerg.tools.tool_search import set_search_context

        # Mock the search index to return predictable results
        mock_results = [
            (MagicMock(name="github_list_issues", summary="List GitHub issues", category="github", param_hints="repo"), 0.9),
            (MagicMock(name="jira_list_issues", summary="List Jira issues", category="jira", param_hints="project"), 0.85),
            (MagicMock(name="linear_list_issues", summary="List Linear issues", category="linear", param_hints="team"), 0.8),
        ]
        # Set the name attribute properly (MagicMock doesn't set it from constructor)
        for entry, _ in mock_results:
            entry.name = entry._mock_name

        mock_index = AsyncMock()
        mock_index.search = AsyncMock(return_value=mock_results)
        mock_index.catalog = mock_results

        with patch("zerg.tools.tool_search.get_tool_search_index", return_value=mock_index):
            # Set context to only allow github tools
            set_search_context(allowed_tools=["github_*"], max_results=10)

            result = await search_tools_for_agent("list issues", max_results=10)

            # Only github_list_issues should be in results
            tool_names = [t["name"] for t in result["tools"]]
            assert "github_list_issues" in tool_names
            assert "jira_list_issues" not in tool_names
            assert "linear_list_issues" not in tool_names

            clear_search_context()

    @pytest.mark.asyncio
    async def test_search_results_capped_by_context_max(self):
        """search_tools_for_agent should respect context max_results cap."""
        from zerg.tools.tool_search import clear_search_context
        from zerg.tools.tool_search import search_tools_for_agent
        from zerg.tools.tool_search import set_search_context

        # Create many mock results
        mock_results = []
        for i in range(15):
            entry = MagicMock()
            entry.name = f"tool_{i}"
            entry.summary = f"Tool {i}"
            entry.category = "test"
            entry.param_hints = ""
            mock_results.append((entry, 0.9 - i * 0.01))

        mock_index = AsyncMock()
        mock_index.search = AsyncMock(return_value=mock_results)
        mock_index.catalog = mock_results

        with patch("zerg.tools.tool_search.get_tool_search_index", return_value=mock_index):
            # Set context cap to 5 (simulating MAX_TOOLS_FROM_SEARCH)
            set_search_context(allowed_tools=None, max_results=5)

            # Request 10, but context cap is 5
            result = await search_tools_for_agent("test", max_results=10)

            # Should be capped at 5
            assert len(result["tools"]) <= 5

            clear_search_context()


# ---------------------------------------------------------------------------
# Rebind Mechanism Tests
# ---------------------------------------------------------------------------


class TestRebindMechanism:
    """Test the rebind-after-search_tools mechanism."""

    def test_load_tools_triggers_rebind_flag(self):
        """Loading new tools should set needs_rebind flag."""
        from zerg.tools.lazy_binder import LazyToolBinder

        mock_resolver = MagicMock()

        def mock_get_tool(name):
            tool = MagicMock()
            tool.name = name
            return tool

        mock_resolver.get_tool.side_effect = mock_get_tool

        binder = LazyToolBinder(mock_resolver, allowed_tools=None)

        # After init, rebind flag should be False
        assert binder.needs_rebind() is False

        # Load a new tool (not in core)
        binder.get_tool("custom_tool")

        # Now rebind should be True
        assert binder.needs_rebind() is True

        # Clear the flag
        binder.clear_rebind_flag()
        assert binder.needs_rebind() is False

    def test_load_tools_batch(self):
        """load_tools should load multiple tools and return success list."""
        from zerg.tools.lazy_binder import LazyToolBinder

        mock_resolver = MagicMock()

        def mock_get_tool(name):
            # Simulate some tools existing, some not
            if name in ["tool_a", "tool_b"]:
                tool = MagicMock()
                tool.name = name
                return tool
            return None

        mock_resolver.get_tool.side_effect = mock_get_tool

        binder = LazyToolBinder(mock_resolver, allowed_tools=None)

        # Load a batch of tools
        loaded = binder.load_tools(["tool_a", "tool_b", "tool_c"])

        # Only existing tools should be returned
        assert "tool_a" in loaded
        assert "tool_b" in loaded
        assert "tool_c" not in loaded

    def test_get_bound_tools_returns_loaded(self):
        """get_bound_tools should return all loaded tools."""
        from zerg.tools.lazy_binder import LazyToolBinder

        mock_resolver = MagicMock()
        tool_instances = {}

        def mock_get_tool(name):
            if name not in tool_instances:
                tool = MagicMock()
                tool.name = name
                tool_instances[name] = tool
            return tool_instances[name]

        mock_resolver.get_tool.side_effect = mock_get_tool

        allowed = ["spawn_commis", "contact_user", "custom_tool"]
        binder = LazyToolBinder(mock_resolver, allowed_tools=allowed)

        # Initially only core tools that are allowed
        initial_tools = binder.get_bound_tools()
        initial_names = {t.name for t in initial_tools}
        assert "spawn_commis" in initial_names
        assert "contact_user" in initial_names

        # Load custom_tool
        binder.get_tool("custom_tool")

        # Now should include custom_tool
        updated_tools = binder.get_bound_tools()
        updated_names = {t.name for t in updated_tools}
        assert "custom_tool" in updated_names


# ---------------------------------------------------------------------------
# Integration Test - Catalog Prompt
# ---------------------------------------------------------------------------


class TestCatalogPromptIntegration:
    """Test that catalog prompt uses correct core tools list."""

    def test_catalog_prompt_uses_loaded_tools_not_all_core(self):
        """Catalog prompt should list only loaded core tools, not all CORE_TOOLS."""
        from zerg.tools.lazy_binder import LazyToolBinder

        mock_resolver = MagicMock()

        def mock_get_tool(name):
            tool = MagicMock()
            tool.name = name
            return tool

        mock_resolver.get_tool.side_effect = mock_get_tool

        # Restrict to only 2 core tools
        allowed = ["spawn_commis", "contact_user"]
        binder = LazyToolBinder(mock_resolver, allowed_tools=allowed)

        # The loaded_tool_names should only have the allowed core tools
        loaded_names = sorted(binder.loaded_tool_names)

        # Build the catalog header string (simulating what concierge does)
        catalog_header = f"### Core Tools (always loaded): {', '.join(loaded_names)}"

        # Should NOT contain tools that aren't allowed
        assert "spawn_commis" in catalog_header
        assert "contact_user" in catalog_header
        assert "web_search" not in catalog_header
        assert "http_request" not in catalog_header

        # Verify it's a subset of CORE_TOOLS
        for name in loaded_names:
            assert name in allowed, f"{name} should be in allowed list"
