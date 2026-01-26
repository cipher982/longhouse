"""Lazy tool binder for on-demand tool loading.

This module provides the LazyToolBinder class which manages tool loading
for the concierge ReAct loop. It:

1. Pre-loads core tools (spawn_commis, contact_user, etc.)
2. Lazy-loads other tools on first use
3. Tracks which tools have been loaded for rebinding

Usage in concierge:
    binder = LazyToolBinder(resolver, allowed_tools)

    # Get currently bound tools for LLM
    tools = binder.get_bound_tools()

    # When LLM wants to call a tool
    tool = binder.get_tool("github_list_issues")  # Loads if needed

    # Check if tools were added (need to rebind LLM)
    if binder.needs_rebind():
        tools = binder.get_bound_tools()
        llm = llm.bind_tools(tools)
        binder.clear_rebind_flag()
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from .catalog import CORE_TOOLS

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

    from .unified_access import ToolResolver

logger = logging.getLogger(__name__)


class LazyToolBinder:
    """Manages lazy loading of tools for concierge execution.

    Core tools are loaded upfront. Other tools are loaded on-demand
    when the LLM tries to use them.
    """

    def __init__(
        self,
        resolver: ToolResolver,
        allowed_tools: list[str] | None = None,
        *,
        core_tools: frozenset[str] | None = None,
    ):
        """Initialize the lazy tool binder.

        Args:
            resolver: ToolResolver for fetching tool implementations.
            allowed_tools: Optional allowlist (supports wildcards like "github_*").
                          If None, all tools are allowed.
            core_tools: Set of core tool names to pre-load.
                       Defaults to CORE_TOOLS from catalog.
        """
        self._resolver = resolver
        self._allowed_tools = allowed_tools
        self._core_tools = core_tools or CORE_TOOLS
        self._loaded: dict[str, BaseTool] = {}
        self._needs_rebind = False

        # Metrics tracking
        self._load_times: dict[str, float] = {}  # tool_name -> load_time_ms
        self._rebind_count: int = 0

        # Pre-load core tools
        self._preload_core_tools()

    def _preload_core_tools(self) -> None:
        """Pre-load core tools into the binder.

        Only loads core tools that pass the allowlist check (if an allowlist is set).
        This ensures the allowlist is respected even for core tools.
        """
        loaded_count = 0
        for name in self._core_tools:
            # Respect allowlist even for core tools
            if not self._is_tool_allowed(name):
                logger.debug(f"Core tool '{name}' not in allowlist, skipping")
                continue

            tool = self._resolver.get_tool(name)
            if tool:
                self._loaded[name] = tool
                loaded_count += 1
                logger.debug(f"Pre-loaded core tool: {name}")
            else:
                logger.warning(f"Core tool not found in resolver: {name}")

        logger.info(f"Pre-loaded {loaded_count} core tools")

    def _is_tool_allowed(self, name: str) -> bool:
        """Check if a tool is allowed by the allowlist."""
        if self._allowed_tools is None or len(self._allowed_tools) == 0:
            return True  # No allowlist = all allowed

        for pattern in self._allowed_tools:
            if pattern.endswith("*"):
                if name.startswith(pattern[:-1]):
                    return True
            elif pattern == name:
                return True

        return False

    def get_tool(self, name: str) -> BaseTool | None:
        """Get a tool by name, loading lazily if needed.

        Args:
            name: Tool name to get.

        Returns:
            Tool instance, or None if not found/allowed.
        """
        # Return already-loaded tool
        if name in self._loaded:
            return self._loaded[name]

        # Check allowlist
        if not self._is_tool_allowed(name):
            logger.warning(f"Tool '{name}' not in allowlist")
            return None

        # Lazy load with timing
        start = time.perf_counter()
        tool = self._resolver.get_tool(name)
        load_time_ms = (time.perf_counter() - start) * 1000

        if tool:
            self._loaded[name] = tool
            self._load_times[name] = load_time_ms
            self._needs_rebind = True
            logger.info(f"Lazy-loaded tool: {name} ({load_time_ms:.1f}ms)")
            return tool

        logger.warning(f"Tool '{name}' not found in resolver")
        return None

    def load_tools(self, names: list[str]) -> list[str]:
        """Load multiple tools at once.

        Args:
            names: List of tool names to load.

        Returns:
            List of successfully loaded tool names.
        """
        loaded = []
        for name in names:
            if self.get_tool(name):
                loaded.append(name)
        return loaded

    def get_bound_tools(self) -> list[BaseTool]:
        """Get all currently loaded tools for LLM binding.

        Returns:
            List of loaded tool instances.
        """
        return list(self._loaded.values())

    def needs_rebind(self) -> bool:
        """Check if new tools were loaded since last rebind.

        Returns:
            True if get_bound_tools() would return new tools.
        """
        return self._needs_rebind

    def clear_rebind_flag(self) -> None:
        """Clear the rebind flag after rebinding LLM."""
        if self._needs_rebind:
            self._rebind_count += 1
        self._needs_rebind = False

    @property
    def loaded_tool_names(self) -> frozenset[str]:
        """Get names of all loaded tools."""
        return frozenset(self._loaded.keys())

    @property
    def loaded_count(self) -> int:
        """Get number of loaded tools."""
        return len(self._loaded)

    def is_loaded(self, name: str) -> bool:
        """Check if a tool is already loaded."""
        return name in self._loaded

    def get_load_stats(self) -> dict:
        """Get statistics about tool loading.

        Returns dict with:
            total_loaded: Total number of tools loaded
            core_loaded: Number of core tools loaded at init
            lazy_loaded: Number of tools lazy-loaded on demand
            loaded_names: Sorted list of loaded tool names
            load_times_ms: Dict mapping lazy-loaded tool names to load time in ms
            rebinds: Number of times LLM was rebound with new tools
        """
        core_loaded = sum(1 for n in self._loaded if n in self._core_tools)
        lazy_loaded = len(self._loaded) - core_loaded

        return {
            "total_loaded": len(self._loaded),
            "core_loaded": core_loaded,
            "lazy_loaded": lazy_loaded,
            "loaded_names": sorted(self._loaded.keys()),
            "load_times_ms": dict(self._load_times),
            "rebinds": self._rebind_count,
        }


class EagerToolBinder:
    """Non-lazy tool binder for backwards compatibility.

    This binder loads all allowed tools upfront, matching the original
    behavior before lazy loading was introduced.
    """

    def __init__(
        self,
        resolver: ToolResolver,
        allowed_tools: list[str] | None = None,
    ):
        """Initialize the eager tool binder.

        Args:
            resolver: ToolResolver for fetching tool implementations.
            allowed_tools: Optional allowlist (supports wildcards).
        """
        self._resolver = resolver

        # Load all allowed tools
        if allowed_tools:
            filtered = resolver.filter_by_allowlist(allowed_tools)
            self._tools = {t.name: t for t in filtered}
        else:
            self._tools = {t.name: t for t in resolver.get_all_tools()}

        logger.info(f"Eager-loaded {len(self._tools)} tools")

    def get_tool(self, name: str) -> BaseTool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def get_bound_tools(self) -> list[BaseTool]:
        """Get all tools for LLM binding."""
        return list(self._tools.values())

    def needs_rebind(self) -> bool:
        """Never needs rebind - all tools loaded upfront."""
        return False

    def clear_rebind_flag(self) -> None:
        """No-op for eager binder."""
        pass

    @property
    def loaded_tool_names(self) -> frozenset[str]:
        """Get names of all loaded tools."""
        return frozenset(self._tools.keys())

    @property
    def loaded_count(self) -> int:
        """Get number of loaded tools."""
        return len(self._tools)

    def is_loaded(self, name: str) -> bool:
        """Check if a tool is loaded (always True for allowed tools)."""
        return name in self._tools

    def get_load_stats(self) -> dict:
        """Get statistics about tool loading (eager mode).

        Returns dict matching LazyToolBinder.get_load_stats() format
        for consistency in comparisons.
        """
        return {
            "total_loaded": len(self._tools),
            "core_loaded": len(self._tools),  # All loaded at init
            "lazy_loaded": 0,  # No lazy loading in eager mode
            "loaded_names": sorted(self._tools.keys()),
            "load_times_ms": {},  # No timing in eager mode
            "rebinds": 0,  # No rebinds in eager mode
        }


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------


def create_tool_binder(
    resolver: ToolResolver,
    allowed_tools: list[str] | None = None,
    *,
    lazy: bool = True,
) -> LazyToolBinder | EagerToolBinder:
    """Create a tool binder.

    Args:
        resolver: ToolResolver for fetching tool implementations.
        allowed_tools: Optional allowlist (supports wildcards).
        lazy: If True, use LazyToolBinder. If False, use EagerToolBinder.

    Returns:
        Tool binder instance.
    """
    if lazy:
        return LazyToolBinder(resolver, allowed_tools)
    else:
        return EagerToolBinder(resolver, allowed_tools)
