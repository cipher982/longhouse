"""Immutable tool registry for clean dependency injection."""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Dict
from typing import FrozenSet
from typing import List
from typing import Optional

from langchain_core.tools import StructuredTool

# ---------------------------------------------------------------------------
# Backwards-compatibility shim ------------------------------------------------
# ---------------------------------------------------------------------------
# New code should use the *immutable* registry built at startup.  For existing
# tests (and possibly legacy plugins) we retain a **mutable singleton**
# ``ToolRegistry`` with API parity.  Internally it delegates to the
# production registry instance but still allows *register* and
# *filter_tools_by_allowlist* so the behavioural contract remains intact.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImmutableToolRegistry:
    """
    Thread-safe, immutable tool registry.

    Built once at startup, passed to fiches via dependency injection.
    No global state, no mutations, no surprises.
    """

    _tools: MappingProxyType
    _names: FrozenSet[str]

    @classmethod
    def build(cls, tool_sources: List[List[StructuredTool]]) -> "ImmutableToolRegistry":
        """
        Build registry from multiple tool sources.

        Args:
            tool_sources: List of tool lists (builtin, MCP, custom, etc.)

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


# ---------------------------------------------------------------------------
# Mutable singleton wrapper expected by the older test-suite
# ---------------------------------------------------------------------------


class ToolRegistry:
    """Mutable tool registry keeping backwards compatibility with v0 API."""

    _instance: "ToolRegistry | None" = None

    def __new__(cls):  # noqa: D401 – singleton pattern
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._tools: Dict[str, StructuredTool] = {}
        return cls._instance

    # ------------------------------------------------------------------
    # Basic CRUD --------------------------------------------------------
    # ------------------------------------------------------------------

    def register(self, tool: StructuredTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' already registered")
        self._tools[tool.name] = tool

    # Exposed for @register_tool decorator -----------------------------
    def get_tool(self, name: str):  # noqa: D401 – legacy helper
        return self._tools.get(name)

    def get_all_tools(self):  # noqa: D401 – legacy helper
        return list(self._tools.values())  # legacy behaviour

    # Allowlist filtering copied from immutable impl -------------------
    def filter_tools_by_allowlist(self, allowed):  # noqa: D401 – legacy helper
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

    # Convenience wrappers --------------------------------------------
    def list_tool_names(self):  # noqa: D401 – legacy helper
        from zerg.tools.builtin import BUILTIN_TOOLS  # local

        names = {t.name for t in BUILTIN_TOOLS}
        names.update(self._tools.keys())
        return list(names)

    # Test cleanup helper ------------------------------------------
    def clear_runtime_tools(self):  # noqa: D401 – test helper
        """Clear all runtime-registered tools (for test cleanup)."""
        self._tools.clear()

    # Keep original helper name used across codebase
    def all_tools(self):  # noqa: D401 – legacy helper
        """Return **built-in + runtime-registered** tools."""

        from zerg.tools.builtin import BUILTIN_TOOLS  # local import

        combined = {t.name: t for t in BUILTIN_TOOLS}
        combined.update(self._tools)
        return list(combined.values())


# ---------------------------------------------------------------------------
# Decorator helper – kept for existing tests ---------------------------------
# ---------------------------------------------------------------------------


def register_tool(*, name: str, description: str):  # noqa: D401 – decorator
    """Decorator that registers a function as a ``StructuredTool`` instance."""

    from langchain_core.tools import StructuredTool

    def _wrapper(fn):
        tool = StructuredTool.from_function(fn, name=name, description=description)
        ToolRegistry().register(tool)
        return fn

    return _wrapper


# Convenience expose for tests
def get_registry():  # noqa: D401 – alias for older imports
    return ToolRegistry()


__all__ = [
    "ImmutableToolRegistry",
    "ToolRegistry",
    "register_tool",
    "get_registry",
]
