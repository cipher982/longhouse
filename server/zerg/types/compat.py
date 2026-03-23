"""Compatibility layer for LangChain message types.

This module provides a bridge between the old LangChain imports and
the new native types. Import from here during migration, then switch
to direct imports from zerg.types when ready.

Usage:
    # Old code:
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.tools import StructuredTool

    # Migration step 1 (this module):
    from zerg.types.compat import AIMessage, HumanMessage
    from zerg.types.compat import StructuredTool

    # Final (direct import):
    from zerg.types import AIMessage, HumanMessage, Tool
"""

# Re-export native types with LangChain-compatible names
from zerg.types.messages import AIMessage
from zerg.types.messages import BaseMessage
from zerg.types.messages import HumanMessage
from zerg.types.messages import SystemMessage
from zerg.types.messages import ToolMessage
from zerg.types.tools import Tool

# Tool compatibility - StructuredTool is aliased to Tool
from zerg.types.tools import Tool as StructuredTool  # noqa: F401

# Also expose as BaseTool for TYPE_CHECKING imports
BaseTool = Tool

__all__ = [
    "BaseMessage",
    "SystemMessage",
    "HumanMessage",
    "AIMessage",
    "ToolMessage",
    "Tool",
    "StructuredTool",
    "BaseTool",
]
