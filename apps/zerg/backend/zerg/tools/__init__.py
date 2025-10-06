"""Tool registry initialization and access."""

from typing import Optional

from .builtin import BUILTIN_TOOLS
from .registry import ImmutableToolRegistry

_PRODUCTION_REGISTRY: Optional[ImmutableToolRegistry] = None


def create_production_registry() -> ImmutableToolRegistry:
    """
    Create the production tool registry with all available tools.

    This includes:
    - Builtin tools (datetime, http, math, uuid)
    - MCP tools (if configured)
    - Any custom tools
    """
    tool_sources = [
        BUILTIN_TOOLS,
        # Future: MCP_TOOLS, CUSTOM_TOOLS
    ]
    return ImmutableToolRegistry.build(tool_sources)


def get_registry() -> ImmutableToolRegistry:
    """
    Get the production registry (lazy initialization).

    For testing, use a custom registry instead.
    """
    global _PRODUCTION_REGISTRY
    if _PRODUCTION_REGISTRY is None:
        _PRODUCTION_REGISTRY = create_production_registry()
    return _PRODUCTION_REGISTRY
