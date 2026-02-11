"""Tool registry initialization and access.

This module builds the immutable, production-facing registry from multiple
sources and exposes a small API so dynamic tool providers (e.g. MCP servers)
can refresh the registry after registering new tools.

Classes:
    ImmutableToolRegistry - Thread-safe, immutable tool registry (built once at startup).

Runtime tools (e.g. MCP-discovered) are stored in a plain module-level list
(_RUNTIME_TOOLS) and folded into the immutable registry on rebuild. No mutable
singleton needed.
"""

import logging
import os
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any
from typing import Callable
from typing import Dict
from typing import FrozenSet
from typing import List
from typing import Optional
from typing import Set

from zerg.types.tools import Tool as StructuredTool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ImmutableToolRegistry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImmutableToolRegistry:
    """Thread-safe, immutable tool registry.

    Built once at startup, passed to fiches via dependency injection.
    No global state, no mutations, no surprises.
    """

    _tools: MappingProxyType
    _names: FrozenSet[str]

    @classmethod
    def build(cls, tool_sources: List[List[StructuredTool]]) -> "ImmutableToolRegistry":
        """Build registry from multiple tool sources.

        Raises:
            ValueError: If duplicate tool names are found
        """
        tools: Dict[str, StructuredTool] = {}
        for source in tool_sources:
            for tool in source:
                if tool.name in tools:
                    raise ValueError(
                        f"Duplicate tool name '{tool.name}' found. "
                        f"Existing: {tools[tool.name].description}, "
                        f"New: {tool.description}"
                    )
                tools[tool.name] = tool

        return cls(_tools=MappingProxyType(tools), _names=frozenset(tools.keys()))

    def get(self, name: str) -> Optional[StructuredTool]:
        return self._tools.get(name)

    def filter_by_allowlist(self, allowed: Optional[List[str]]) -> List[StructuredTool]:
        if not allowed:
            return list(self._tools.values())

        result = []
        for pattern in allowed:
            if pattern.endswith("*"):
                prefix = pattern[:-1]
                result.extend(t for n, t in self._tools.items() if n.startswith(prefix))
            elif pattern in self._tools:
                result.append(self._tools[pattern])
        return result

    def list_names(self) -> List[str]:
        return list(self._names)

    def all_tools(self) -> List[StructuredTool]:
        return list(self._tools.values())

    def get_all_tools(self) -> List[StructuredTool]:
        """Alias for all_tools() used by callers migrated from ToolResolver."""
        return list(self._tools.values())

    def get_tool(self, name: str) -> Optional[StructuredTool]:
        """Alias for get() used by callers migrated from ToolResolver."""
        return self._tools.get(name)

    def get_tool_names(self) -> List[str]:
        """Get sorted list of all tool names."""
        return sorted(self._names)

    def has_tool(self, name: str) -> bool:
        """Check if tool exists."""
        return name in self._names


# ---------------------------------------------------------------------------
# Runtime tools (MCP-discovered, etc.) — plain list, no mutable singleton
# ---------------------------------------------------------------------------

_RUNTIME_TOOLS: List[StructuredTool] = []


def add_runtime_tool(tool: StructuredTool) -> None:
    """Register a dynamically discovered tool (e.g. from an MCP server).

    After adding one or more tools call ``refresh_registry()`` so the
    immutable production registry is rebuilt with the new entries.
    """
    if any(t.name == tool.name for t in _RUNTIME_TOOLS):
        raise ValueError(f"Runtime tool '{tool.name}' already registered")
    _RUNTIME_TOOLS.append(tool)


def clear_runtime_tools() -> None:
    """Remove all runtime-registered tools (for test cleanup)."""
    _RUNTIME_TOOLS.clear()


# ---------------------------------------------------------------------------
# Production registry singleton
# ---------------------------------------------------------------------------

_PRODUCTION_REGISTRY: Optional[ImmutableToolRegistry] = None


def create_production_registry() -> ImmutableToolRegistry:
    """Create the production tool registry with all available tools."""
    from .builtin import BUILTIN_TOOLS

    # De-duplicate: runtime tools that share a name with builtins are dropped.
    builtin_names: Set[str] = {t.name for t in BUILTIN_TOOLS}
    unique_runtime = [t for t in _RUNTIME_TOOLS if t.name not in builtin_names]

    return ImmutableToolRegistry.build([BUILTIN_TOOLS, unique_runtime])


def refresh_registry() -> None:
    """Rebuild the production registry to include newly registered tools."""
    global _PRODUCTION_REGISTRY
    _PRODUCTION_REGISTRY = create_production_registry()


def get_registry() -> ImmutableToolRegistry:
    """Get the production registry (lazy initialization).

    If LONGHOUSE_TOOL_STUBS_PATH is set (and TESTING=1), tools will be
    wrapped with stubbing logic.
    """
    global _PRODUCTION_REGISTRY
    if _PRODUCTION_REGISTRY is None:
        _PRODUCTION_REGISTRY = create_production_registry()

        # Apply stubbing if enabled (only possible when TESTING=1)
        stub_config = _load_stub_config()
        if stub_config is not None:
            stub_matcher, stubbed_tool_names = stub_config
            _PRODUCTION_REGISTRY = _apply_stubs(_PRODUCTION_REGISTRY, stub_matcher, stubbed_tool_names)

    return _PRODUCTION_REGISTRY


def reset_registry() -> None:
    """Reset production registry (for testing). Forces lazy re-init."""
    global _PRODUCTION_REGISTRY
    _PRODUCTION_REGISTRY = None


# ---------------------------------------------------------------------------
# Tool stubbing (test infrastructure)
# ---------------------------------------------------------------------------


def _create_stubbed_tool(
    original_tool: StructuredTool,
    stub_matcher: Callable[[str, Dict], Any],
) -> StructuredTool:
    """Wrap a tool with stubbing logic, preserving all original attributes."""
    original_func = original_tool.func

    def stubbed_func(**kwargs):
        stub_result = stub_matcher(original_tool.name, kwargs)
        if stub_result is not None:
            logger.info(f"Using stubbed result for tool '{original_tool.name}'")
            return stub_result
        return original_func(**kwargs)

    return StructuredTool(
        name=original_tool.name,
        description=original_tool.description,
        func=stubbed_func,
        args_schema=getattr(original_tool, "args_schema", None),
        coroutine=getattr(original_tool, "coroutine", None),
    )


def _apply_stubs(
    registry: ImmutableToolRegistry,
    stub_matcher: Callable[[str, Dict], Any],
    stubbed_tool_names: Set[str],
) -> ImmutableToolRegistry:
    """Return a new registry with stubbed tools."""
    new_tools = []
    for tool in registry.all_tools():
        if tool.name in stubbed_tool_names:
            new_tools.append(_create_stubbed_tool(tool, stub_matcher))
        else:
            new_tools.append(tool)
    return ImmutableToolRegistry.build([new_tools])


def _load_stub_config() -> Optional[tuple[Callable[[str, Dict], Any], Set[str]]]:
    """Load stub matcher from LONGHOUSE_TOOL_STUBS_PATH if configured."""
    stubs_path = os.getenv("LONGHOUSE_TOOL_STUBS_PATH")
    if not stubs_path:
        return None

    from zerg.config import get_settings

    settings = get_settings()
    if not settings.testing:
        logger.error("LONGHOUSE_TOOL_STUBS_PATH is set but TESTING=1 is not. " "Refusing to load stubs.")
        return None

    from zerg.testing.tool_stubs import get_tool_stubs
    from zerg.testing.tool_stubs import match_stub

    stubs = get_tool_stubs()
    if stubs is None:
        logger.warning(f"LONGHOUSE_TOOL_STUBS_PATH set to '{stubs_path}' but stubs could not be loaded")
        return None

    stubbed_tool_names = {k for k in stubs.keys() if not k.startswith("$")}
    logger.info(f"Tool stubbing enabled from {stubs_path}: {stubbed_tool_names}")
    return match_stub, stubbed_tool_names
