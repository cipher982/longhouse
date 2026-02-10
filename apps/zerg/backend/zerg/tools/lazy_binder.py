"""Lazy tool binder for on-demand tool loading.

This module provides the LazyToolBinder class which manages tool loading
for the oikos ReAct loop. It:

1. Pre-loads core tools (spawn_commis, contact_user, etc.)
2. Lazy-loads other tools on first use
3. Tracks which tools have been loaded for rebinding

Usage in oikos:
    binder = LazyToolBinder(registry, allowed_tools)

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

# Import oikos tool names from the single source of truth
from zerg.tools.builtin.oikos_tools import OIKOS_TOOL_NAMES

if TYPE_CHECKING:
    from zerg.tools import ImmutableToolRegistry
    from zerg.types.tools import Tool as BaseTool

logger = logging.getLogger(__name__)

# Core tools - always loaded with full schemas.
# Includes all oikos tools + common utilities.
# search_tools/list_tools are NOT in BUILTIN_TOOLS; they are registered
# dynamically and loaded on demand via the lazy loading mechanism.
CORE_TOOLS: frozenset[str] = OIKOS_TOOL_NAMES | frozenset(
    [
        "contact_user",
        "web_search",
        "http_request",
    ]
)


class LazyToolBinder:
    """Manages lazy loading of tools for oikos execution.

    Core tools are loaded upfront. Other tools are loaded on-demand
    when the LLM tries to use them.
    """

    def __init__(
        self,
        registry: ImmutableToolRegistry,
        allowed_tools: list[str] | None = None,
        *,
        core_tools: frozenset[str] | None = None,
    ):
        """Initialize the lazy tool binder.

        Args:
            registry: ImmutableToolRegistry for fetching tool implementations.
            allowed_tools: Optional allowlist (supports wildcards like "github_*").
                          If None, all tools are allowed.
            core_tools: Set of core tool names to pre-load.
                       Defaults to CORE_TOOLS.
        """
        self._registry = registry
        self._allowed_tools = allowed_tools
        self._core_tools = core_tools or CORE_TOOLS
        self._loaded: dict[str, BaseTool] = {}
        self._needs_rebind = False

        # Metrics tracking
        self._load_times: dict[str, float] = {}
        self._rebind_count: int = 0

        # Pre-load core tools
        self._preload_core_tools()

    def _preload_core_tools(self) -> None:
        """Pre-load core tools into the binder."""
        loaded_count = 0
        for name in self._core_tools:
            if not self._is_tool_allowed(name):
                logger.debug(f"Core tool '{name}' not in allowlist, skipping")
                continue

            tool = self._registry.get(name)
            if tool:
                self._loaded[name] = tool
                loaded_count += 1
                logger.debug(f"Pre-loaded core tool: {name}")
            else:
                logger.warning(f"Core tool not found in registry: {name}")

        logger.info(f"Pre-loaded {loaded_count} core tools")

    def _is_tool_allowed(self, name: str) -> bool:
        """Check if a tool is allowed by the allowlist."""
        if self._allowed_tools is None or len(self._allowed_tools) == 0:
            return True

        for pattern in self._allowed_tools:
            if pattern.endswith("*"):
                if name.startswith(pattern[:-1]):
                    return True
            elif pattern == name:
                return True

        return False

    def get_tool(self, name: str) -> BaseTool | None:
        """Get a tool by name, loading lazily if needed."""
        if name in self._loaded:
            return self._loaded[name]

        if not self._is_tool_allowed(name):
            logger.warning(f"Tool '{name}' not in allowlist")
            return None

        start = time.perf_counter()
        tool = self._registry.get(name)
        load_time_ms = (time.perf_counter() - start) * 1000

        if tool:
            self._loaded[name] = tool
            self._load_times[name] = load_time_ms
            self._needs_rebind = True
            logger.info(f"Lazy-loaded tool: {name} ({load_time_ms:.1f}ms)")
            return tool

        logger.warning(f"Tool '{name}' not found in registry")
        return None

    def load_tools(self, names: list[str]) -> list[str]:
        """Load multiple tools at once. Returns list of successfully loaded names."""
        loaded = []
        for name in names:
            if self.get_tool(name):
                loaded.append(name)
        return loaded

    def get_bound_tools(self) -> list[BaseTool]:
        """Get all currently loaded tools for LLM binding."""
        return list(self._loaded.values())

    def needs_rebind(self) -> bool:
        """Check if new tools were loaded since last rebind."""
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
        """Get statistics about tool loading."""
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
