"""Native Tool type for Longhouse.

Replaces langchain_core.tools.StructuredTool with a simple dataclass
that can be converted to OpenAI function calling format.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import Callable
from typing import get_type_hints

logger = logging.getLogger(__name__)


def _python_type_to_json_schema(py_type: type) -> dict[str, Any]:
    """Convert a Python type to JSON Schema type.

    Args:
        py_type: Python type annotation.

    Returns:
        JSON Schema type definition.
    """
    # Handle None/NoneType
    if py_type is type(None):
        return {"type": "null"}

    # Handle basic types
    type_map = {
        str: {"type": "string"},
        int: {"type": "integer"},
        float: {"type": "number"},
        bool: {"type": "boolean"},
        list: {"type": "array"},
        dict: {"type": "object"},
    }

    if py_type in type_map:
        return type_map[py_type]

    # Handle Optional/Union types
    origin = getattr(py_type, "__origin__", None)

    if origin is list:
        args = getattr(py_type, "__args__", ())
        items_schema = _python_type_to_json_schema(args[0]) if args else {}
        return {"type": "array", "items": items_schema}

    if origin is dict:
        return {"type": "object"}

    # Handle Union (including Optional which is Union[X, None])
    if origin is not None:
        # Check for typing.Union
        import typing

        if origin is typing.Union:
            args = getattr(py_type, "__args__", ())
            # Filter out NoneType for Optional handling
            non_none_args = [a for a in args if a is not type(None)]
            if len(non_none_args) == 1:
                return _python_type_to_json_schema(non_none_args[0])
            # For complex unions, just use object
            return {"type": "object"}

    # Handle PEP604 union types (X | Y syntax) - Python 3.10+
    import types

    if isinstance(py_type, types.UnionType):
        args = py_type.__args__
        # Filter out NoneType for Optional handling
        non_none_args = [a for a in args if a is not type(None)]
        if len(non_none_args) == 1:
            return _python_type_to_json_schema(non_none_args[0])
        # For complex unions, just use object
        return {"type": "object"}

    # Default to string for unknown types
    return {"type": "string"}


def _generate_schema_from_function(func: Callable) -> dict[str, Any]:
    """Generate JSON Schema from function signature.

    Args:
        func: Function to generate schema for.

    Returns:
        JSON Schema for the function parameters.
    """
    sig = inspect.signature(func)
    hints = {}
    try:
        hints = get_type_hints(func)
    except Exception:
        pass

    properties = {}
    required = []

    for param_name, param in sig.parameters.items():
        # Skip self, cls, and private parameters
        if param_name in ("self", "cls") or param_name.startswith("_"):
            continue

        # Get type hint or default to string
        param_type = hints.get(param_name, str)
        param_schema = _python_type_to_json_schema(param_type)

        # Add description from docstring if available
        properties[param_name] = param_schema

        # Check if parameter is required (no default value)
        if param.default is inspect.Parameter.empty:
            required.append(param_name)

    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }


@dataclass
class Tool:
    """A tool that can be called by an AI agent.

    Replaces LangChain's StructuredTool with a simpler dataclass.
    """

    name: str
    """Unique name for the tool."""

    description: str
    """Human-readable description of what the tool does."""

    func: Callable[..., Any]
    """The function to call when the tool is invoked."""

    parameters: dict[str, Any] = field(default_factory=dict)
    """JSON Schema for the tool's parameters. Auto-generated if not provided."""

    coroutine: Callable[..., Any] | None = None
    """Optional async version of the function."""

    args_schema: type | None = None
    """Optional Pydantic model for argument validation."""

    def __post_init__(self):
        """Auto-generate parameters schema if not provided."""
        if not self.parameters:
            if self.args_schema is not None:
                # Use Pydantic model schema if provided
                self.parameters = self._schema_from_pydantic(self.args_schema)
            else:
                self.parameters = _generate_schema_from_function(self.func)

    def _schema_from_pydantic(self, model: type) -> dict[str, Any]:
        """Generate JSON Schema from a Pydantic model.

        Args:
            model: Pydantic BaseModel class.

        Returns:
            JSON Schema dict.
        """
        try:
            # Pydantic v2
            if hasattr(model, "model_json_schema"):
                schema = model.model_json_schema()
            # Pydantic v1
            elif hasattr(model, "schema"):
                schema = model.schema()
            else:
                return _generate_schema_from_function(self.func)

            # Remove extra fields that OpenAI doesn't need
            schema.pop("title", None)
            schema.pop("$defs", None)
            schema.pop("definitions", None)

            return schema
        except Exception:
            return _generate_schema_from_function(self.func)

    def invoke(self, input: dict[str, Any] | str) -> Any:
        """Invoke the tool synchronously.

        Args:
            input: Tool arguments as dict or JSON string.

        Returns:
            Tool result.
        """
        if isinstance(input, str):
            try:
                input = json.loads(input)
            except json.JSONDecodeError:
                input = {"input": input}

        # Filter out private parameters (starting with _)
        filtered_input = {k: v for k, v in input.items() if not k.startswith("_")}
        return self.func(**filtered_input)

    async def ainvoke(self, input: dict[str, Any] | str) -> Any:
        """Invoke the tool asynchronously.

        Args:
            input: Tool arguments as dict or JSON string.

        Returns:
            Tool result.
        """
        if isinstance(input, str):
            try:
                input = json.loads(input)
            except json.JSONDecodeError:
                input = {"input": input}

        # Filter out private parameters (starting with _)
        filtered_input = {k: v for k, v in input.items() if not k.startswith("_")}

        # Use coroutine if available, otherwise run sync func in thread
        if self.coroutine:
            return await self.coroutine(**filtered_input)
        else:
            return await asyncio.to_thread(self.func, **filtered_input)

    def to_openai_tool(self) -> dict[str, Any]:
        """Convert to OpenAI function calling format.

        Returns:
            Dict in OpenAI tools format.
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    @classmethod
    def from_function(
        cls,
        func: Callable[..., Any] | None = None,
        name: str | None = None,
        description: str | None = None,
        coroutine: Callable[..., Any] | None = None,
        args_schema: type | None = None,
    ) -> "Tool":
        """Create a Tool from a function.

        This is the primary way to create tools, matching the LangChain
        StructuredTool.from_function() pattern.

        Args:
            func: The function to wrap. If not provided, coroutine is used.
            name: Tool name (defaults to function name).
            description: Tool description (defaults to docstring).
            coroutine: Optional async version of the function. Used as func if func is None.
            args_schema: Optional Pydantic model for argument validation.

        Returns:
            A Tool instance.
        """
        # If only coroutine is provided, use it as the main func
        if func is None and coroutine is not None:
            func = coroutine

        if func is None:
            raise ValueError("Either func or coroutine must be provided")

        tool_name = name or func.__name__
        tool_description = description or func.__doc__ or f"Tool: {tool_name}"

        return cls(
            name=tool_name,
            description=tool_description.strip(),
            func=func,
            coroutine=coroutine,
            args_schema=args_schema,
        )


def tools_to_openai_format(tools: list[Tool]) -> list[dict[str, Any]]:
    """Convert a list of tools to OpenAI format.

    Args:
        tools: List of Tool instances.

    Returns:
        List of tool definitions in OpenAI format.
    """
    return [tool.to_openai_tool() for tool in tools]
