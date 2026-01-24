"""Tests for LazyToolBinder to ensure CORE_TOOLS are properly pre-loaded.

These tests verify that the lazy loading infrastructure actually works
and that CORE_TOOLS are available to the LLM at startup.
"""

from zerg.tools.builtin import BUILTIN_TOOLS
from zerg.tools.catalog import CORE_TOOLS
from zerg.tools.lazy_binder import LazyToolBinder
from zerg.tools.registry import ImmutableToolRegistry
from zerg.tools.unified_access import ToolResolver


def test_lazy_binder_preloads_core_tools():
    """LazyToolBinder must pre-load all CORE_TOOLS at init.

    This catches the scenario where a tool is in CORE_TOOLS but
    the LazyToolBinder fails to load it (e.g., resolver issue).
    """
    registry = ImmutableToolRegistry.build([list(BUILTIN_TOOLS)])
    resolver = ToolResolver.from_registry(registry)
    binder = LazyToolBinder(resolver)

    missing = sorted(CORE_TOOLS - binder.loaded_tool_names)
    assert not missing, f"LazyToolBinder failed to preload core tools: {missing}"


def test_lazy_binder_get_bound_tools_includes_core():
    """get_bound_tools() must return all CORE_TOOLS for LLM binding."""
    registry = ImmutableToolRegistry.build([list(BUILTIN_TOOLS)])
    resolver = ToolResolver.from_registry(registry)
    binder = LazyToolBinder(resolver)

    bound_tools = binder.get_bound_tools()
    bound_names = {t.name for t in bound_tools}

    missing = sorted(CORE_TOOLS - bound_names)
    assert not missing, f"get_bound_tools() missing core tools: {missing}"


def test_lazy_binder_core_tools_count():
    """Sanity check: CORE_TOOLS should have reasonable count."""
    # If this fails, someone probably accidentally cleared CORE_TOOLS
    assert len(CORE_TOOLS) >= 10, f"CORE_TOOLS seems too small: {len(CORE_TOOLS)}"


def test_lazy_binder_loads_non_core_on_demand():
    """Non-core tools should not be loaded at init."""
    registry = ImmutableToolRegistry.build([list(BUILTIN_TOOLS)])
    resolver = ToolResolver.from_registry(registry)
    binder = LazyToolBinder(resolver)

    # Find a non-core tool
    all_tool_names = {t.name for t in BUILTIN_TOOLS}
    non_core = all_tool_names - CORE_TOOLS

    if non_core:
        sample_non_core = next(iter(non_core))
        # Should NOT be loaded yet
        assert not binder.is_loaded(sample_non_core), f"Non-core tool {sample_non_core} was pre-loaded"
        # Should load on demand
        tool = binder.get_tool(sample_non_core)
        assert tool is not None, f"Failed to lazy-load {sample_non_core}"
        assert binder.is_loaded(sample_non_core), "Tool not marked as loaded after get_tool"
