"""Unified tool access interface - Single way to resolve tools across the entire codebase.

This module provides Carmack-style tool resolution following the same principles
used in the canonical workflow system:

1. Single source of truth for tool resolution
2. Direct dependency injection instead of global lookups
3. Fail-fast validation at boundaries
4. Immutable, thread-safe design

Key improvements over the old scattered approach:
- Eliminates 5 different tool access patterns
- No runtime dictionary creation
- Clear dependency injection
- Performance optimized with pre-resolved mappings
"""

from dataclasses import dataclass
from typing import Dict
from typing import List
from typing import Optional
from typing import Set

from langchain_core.tools import StructuredTool

from .registry import ImmutableToolRegistry


@dataclass(frozen=True)
class ToolResolver:
    """
    High-performance tool resolver with pre-computed lookups.

    Single interface for all tool resolution across the codebase.
    Built once at startup, injected where needed.
    """

    # Pre-computed mappings for O(1) lookups
    _tool_by_name: Dict[str, StructuredTool]
    _all_tools: List[StructuredTool]
    _tool_names: Set[str]

    @classmethod
    def from_registry(cls, registry: ImmutableToolRegistry) -> "ToolResolver":
        """
        Create resolver from immutable registry.

        Pre-computes all lookup structures for maximum performance.
        """
        all_tools = registry.all_tools()
        tool_by_name = {t.name: t for t in all_tools}
        tool_names = {t.name for t in all_tools}

        return cls(_tool_by_name=tool_by_name, _all_tools=all_tools, _tool_names=tool_names)

    # ===================================================================
    # Primary Interface - Replace all scattered tool access patterns
    # ===================================================================

    def get_tool(self, name: str) -> Optional[StructuredTool]:
        """
        Get single tool by name.

        Replaces:
         - registry.get(name)
         - all_tools.get(name) after dict creation
         - registry._tools.get(name)
        """
        return self._tool_by_name.get(name)

    def resolve_tools(self, tool_names: List[str], fail_fast: bool = True) -> List[StructuredTool]:
        """
        Resolve multiple tools by name with fail-fast validation.

        Replaces:
         - registry.all_tools() + filtering logic
         - Manual loops over tool lists
         - Complex allowlist filtering

        Args:
            tool_names: List of tool names to resolve
            fail_fast: If True, raises ValueError on unknown tools

        Returns:
            List of resolved tools (empty slots omitted if fail_fast=False)

        Raises:
            ValueError: If fail_fast=True and any tool name is unknown
        """
        resolved = []
        missing = []

        for name in tool_names:
            tool = self._tool_by_name.get(name)
            if tool:
                resolved.append(tool)
            elif fail_fast:
                missing.append(name)

        if missing:
            available = sorted(self._tool_names)
            raise ValueError(f"Unknown tools: {missing}. Available tools: {available}")

        return resolved

    def filter_by_allowlist(self, allowed: Optional[List[str]]) -> List[StructuredTool]:
        """
        Filter tools by allowlist with wildcard support.

        Replaces:
         - registry.filter_by_allowlist()
         - Manual allowlist filtering logic

        Args:
            allowed: List of tool names/patterns, None means all tools

        Returns:
            Filtered list of tools
        """
        if not allowed:
            return self._all_tools.copy()

        result = []
        for pattern in allowed:
            if pattern.endswith("*"):
                # Wildcard pattern
                prefix = pattern[:-1]
                result.extend(tool for name, tool in self._tool_by_name.items() if name.startswith(prefix))
            else:
                # Exact match
                tool = self._tool_by_name.get(pattern)
                if tool:
                    result.append(tool)

        return result

    def get_all_tools(self) -> List[StructuredTool]:
        """
        Get all available tools.

        Replaces:
         - registry.all_tools()
         - list(registry._tools.values())
        """
        return self._all_tools.copy()

    def get_tool_names(self) -> List[str]:
        """
        Get all tool names.

        Replaces:
         - registry.list_names()
         - list(registry._tools.keys())
         - [t.name for t in registry.all_tools()]
        """
        return sorted(self._tool_names)

    def has_tool(self, name: str) -> bool:
        """
        Check if tool exists.

        More efficient than get_tool() when you only need existence check.
        """
        return name in self._tool_names

    def validate_tools(self, tool_names: List[str]) -> List[str]:
        """
        Validate tool names and return missing ones.

        Replaces validation logic scattered across workflow validators.

        Args:
            tool_names: Tool names to validate

        Returns:
            List of missing tool names (empty if all valid)
        """
        return [name for name in tool_names if name not in self._tool_names]


# ===================================================================
# Global resolver instance - Initialized once at startup
# ===================================================================

_GLOBAL_RESOLVER: Optional[ToolResolver] = None


def get_tool_resolver() -> ToolResolver:
    """
    Get the global tool resolver instance.

    Lazy initialization from the production registry.
    For testing, use create_tool_resolver() with custom registry.
    """
    global _GLOBAL_RESOLVER
    if _GLOBAL_RESOLVER is None:
        from . import get_registry

        registry = get_registry()
        _GLOBAL_RESOLVER = ToolResolver.from_registry(registry)
    return _GLOBAL_RESOLVER


def create_tool_resolver(registry: ImmutableToolRegistry) -> ToolResolver:
    """
    Create tool resolver from custom registry.

    Use this in tests or when you need a resolver with specific tools.
    """
    return ToolResolver.from_registry(registry)


def reset_tool_resolver() -> None:
    """
    Reset global resolver (for testing).

    Forces lazy re-initialization on next get_tool_resolver() call.
    """
    global _GLOBAL_RESOLVER
    _GLOBAL_RESOLVER = None


# ===================================================================
# Convenience Functions - Direct tool operations
# ===================================================================


def resolve_tool(name: str, fail_fast: bool = True) -> Optional[StructuredTool]:
    """
    Resolve single tool by name using global resolver.

    Convenience function for simple tool lookups.
    For multiple tools or complex operations, use get_tool_resolver() directly.
    """
    resolver = get_tool_resolver()
    tool = resolver.get_tool(name)

    if tool is None and fail_fast:
        available = resolver.get_tool_names()
        raise ValueError(f"Unknown tool: {name}. Available: {available}")

    return tool


def resolve_tools(tool_names: List[str], fail_fast: bool = True) -> List[StructuredTool]:
    """
    Resolve multiple tools by name using global resolver.

    Convenience function for bulk tool resolution.
    """
    resolver = get_tool_resolver()
    return resolver.resolve_tools(tool_names, fail_fast=fail_fast)


__all__ = [
    "ToolResolver",
    "get_tool_resolver",
    "create_tool_resolver",
    "reset_tool_resolver",
    "resolve_tool",
    "resolve_tools",
]
