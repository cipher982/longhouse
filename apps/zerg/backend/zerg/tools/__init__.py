"""Tool registry initialization and access.

This module builds the immutable, production-facing registry from multiple
sources and exposes a small API so dynamic tool providers (e.g. MCP servers)
can refresh the registry after registering new tools.

Classes:
    ImmutableToolRegistry - Thread-safe, immutable tool registry (built once at startup).
    ToolRegistry - Mutable singleton for runtime tool registration (MCP, tests).
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
# ToolRegistry (mutable singleton for runtime registration)
# ---------------------------------------------------------------------------


class ToolRegistry:
    """Mutable tool registry keeping backwards compatibility with v0 API."""

    _instance: "ToolRegistry | None" = None

    def __new__(cls):  # noqa: D401 – singleton pattern
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._tools: Dict[str, StructuredTool] = {}
        return cls._instance

    def register(self, tool: StructuredTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' already registered")
        self._tools[tool.name] = tool

    def get_tool(self, name: str):  # noqa: D401
        return self._tools.get(name)

    def get_all_tools(self):  # noqa: D401
        return list(self._tools.values())

    def filter_tools_by_allowlist(self, allowed):  # noqa: D401
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

    def list_tool_names(self):  # noqa: D401
        from zerg.tools.builtin import BUILTIN_TOOLS

        names = {t.name for t in BUILTIN_TOOLS}
        names.update(self._tools.keys())
        return list(names)

    def clear_runtime_tools(self):  # noqa: D401
        """Clear all runtime-registered tools (for test cleanup)."""
        self._tools.clear()

    def all_tools(self):  # noqa: D401
        """Return built-in + runtime-registered tools."""
        from zerg.tools.builtin import BUILTIN_TOOLS

        combined = {t.name: t for t in BUILTIN_TOOLS}
        combined.update(self._tools)
        return list(combined.values())


# ---------------------------------------------------------------------------
# Decorator helper
# ---------------------------------------------------------------------------


def register_tool(*, name: str, description: str):  # noqa: D401
    """Decorator that registers a function as a StructuredTool instance."""

    def _wrapper(fn):
        tool = StructuredTool.from_function(fn, name=name, description=description)
        ToolRegistry().register(tool)
        return fn

    return _wrapper


# ---------------------------------------------------------------------------
# Production registry singleton
# ---------------------------------------------------------------------------

_PRODUCTION_REGISTRY: Optional[ImmutableToolRegistry] = None


def _get_runtime_tools_unique() -> List:
    """Return runtime-registered tools excluding duplicates with builtins."""
    from .builtin import BUILTIN_TOOLS

    runtime_registry = ToolRegistry()
    runtime_tools = runtime_registry.get_all_tools()

    builtin_names: Set[str] = {t.name for t in BUILTIN_TOOLS}
    unique_runtime = [t for t in runtime_tools if t.name not in builtin_names]
    return unique_runtime


def create_production_registry() -> ImmutableToolRegistry:
    """Create the production tool registry with all available tools."""
    from .builtin import BUILTIN_TOOLS

    tool_sources = [
        BUILTIN_TOOLS,
        _get_runtime_tools_unique(),
    ]
    return ImmutableToolRegistry.build(tool_sources)


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


# Backwards-compat alias used by existing callers
reset_tool_resolver = reset_registry


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
