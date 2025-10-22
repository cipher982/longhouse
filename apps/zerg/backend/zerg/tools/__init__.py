"""Tool registry initialization and access.

This module builds the immutable, production-facing registry from multiple
sources and exposes a small API so dynamic tool providers (e.g. MCP servers)
can refresh the resolver view after registering new tools.
"""

from typing import List
from typing import Optional
from typing import Set

from .builtin import BUILTIN_TOOLS
from .registry import ImmutableToolRegistry
from .registry import ToolRegistry

_PRODUCTION_REGISTRY: Optional[ImmutableToolRegistry] = None


def _get_runtime_tools_unique() -> List:
    """Return runtime-registered tools excluding duplicates with builtins.

    Tools can be registered at runtime via the mutable ``ToolRegistry``
    (used by MCP integration and tests). We merge those tools with the
    built-ins while preventing duplicate names.
    """

    runtime_registry = ToolRegistry()
    runtime_tools = runtime_registry.get_all_tools()

    builtin_names: Set[str] = {t.name for t in BUILTIN_TOOLS}
    unique_runtime = [t for t in runtime_tools if t.name not in builtin_names]
    return unique_runtime


def create_production_registry() -> ImmutableToolRegistry:
    """Create the production tool registry with all available tools.

    Sources:
    - Builtin tools (datetime, http, math, uuid)
    - Runtime tools (e.g. MCP) registered in ``ToolRegistry``
    """

    tool_sources = [
        BUILTIN_TOOLS,
        _get_runtime_tools_unique(),
    ]
    return ImmutableToolRegistry.build(tool_sources)


def refresh_registry() -> None:
    """Rebuild the production registry to include newly registered tools.

    Call this after dynamically registering MCP/custom tools so that the
    immutable view seen by the global resolver reflects the latest set.
    """

    global _PRODUCTION_REGISTRY
    _PRODUCTION_REGISTRY = create_production_registry()


def get_registry() -> ImmutableToolRegistry:
    """Get the production registry (lazy initialization)."""

    global _PRODUCTION_REGISTRY
    if _PRODUCTION_REGISTRY is None:
        _PRODUCTION_REGISTRY = create_production_registry()
    return _PRODUCTION_REGISTRY
