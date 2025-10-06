"""
Clean Variable Resolution System

Single format: NodeOutputEnvelope {"value": ..., "meta": {...}}
Direct, flat access patterns. No compatibility layers.
"""

import logging
import re
from typing import Any
from typing import Dict

logger = logging.getLogger(__name__)


class VariableResolutionError(Exception):
    """Variable resolution failed."""

    pass


def resolve_variables(data: Any, node_outputs: Dict[str, Any]) -> Any:
    """
    Resolve variables in data structures.

    Single format: ${node.value}, ${node.meta.field}
    No aliases, no compatibility, no nesting.
    """
    if isinstance(data, str):
        return _resolve_string_variables(data, node_outputs)
    elif isinstance(data, dict):
        return {key: resolve_variables(value, node_outputs) for key, value in data.items()}
    elif isinstance(data, list):
        return [resolve_variables(item, node_outputs) for item in data]
    else:
        return data


def _resolve_string_variables(text: str, node_outputs: Dict[str, Any]) -> Any:
    """Resolve variables in string context."""
    if "${" not in text:
        return text

    # Check if entire string is a single variable (pure variable case)
    single_var_pattern = r"^\$\{([^}]+)\}$"
    single_match = re.match(single_var_pattern, text)

    if single_match:
        # Pure variable case - return actual resolved value
        var_path = single_match.group(1)
        try:
            return resolve_variable_path(var_path, node_outputs)
        except VariableResolutionError:
            logger.warning(f"Failed to resolve variable ${{{var_path}}}")
            return text
    else:
        # String interpolation case
        return _resolve_string_interpolation(text, node_outputs)


def _resolve_string_interpolation(text: str, node_outputs: Dict[str, Any]) -> str:
    """Resolve variables within string templates."""
    pattern = r"\$\{([^}]+)\}"

    def replace_var(match):
        var_path = match.group(1)
        try:
            resolved_value = resolve_variable_path(var_path, node_outputs)
            return str(resolved_value)
        except VariableResolutionError:
            logger.warning(f"Variable resolution failed for ${{{var_path}}}")
            return match.group(0)

    return re.sub(pattern, replace_var, text)


def resolve_variable_path(path: str, node_outputs: Dict[str, Any]) -> Any:
    """
    Resolve single variable path.

    Supported patterns:
    - ${node} → node_outputs[node]["value"]
    - ${node.value} → node_outputs[node]["value"]
    - ${node.value.field} → node_outputs[node]["value"]["field"]
    - ${node.meta.field} → node_outputs[node]["meta"]["field"]

    No aliases. No legacy format. Direct access only.
    """
    if not path:
        raise VariableResolutionError("Empty variable path")

    parts = path.split(".")
    node_id = parts[0]

    if node_id not in node_outputs:
        available = list(node_outputs.keys())
        raise VariableResolutionError(f"Node '{node_id}' not found. Available: {available}")

    node_output = node_outputs[node_id]

    # Envelope format only: {"value": ..., "meta": {...}}
    if not isinstance(node_output, dict) or "value" not in node_output or "meta" not in node_output:
        raise VariableResolutionError(f"Node '{node_id}' output is not envelope format")

    if len(parts) == 1:
        # ${node} → return value
        return node_output["value"]

    section = parts[1]
    if section == "value":
        # ${node.value} or ${node.value.field}
        current = node_output["value"]
        remaining = parts[2:]
    elif section == "meta":
        # ${node.meta.field}
        if len(parts) < 3:
            raise VariableResolutionError("Meta access requires field: ${node.meta.field}")
        current = node_output["meta"]
        remaining = parts[2:]
    else:
        # ${node.field} → shorthand for ${node.value.field}
        current = node_output["value"]
        remaining = parts[1:]

    # Traverse remaining path
    for field in remaining:
        if isinstance(current, dict) and field in current:
            current = current[field]
        elif isinstance(current, list):
            try:
                index = int(field)
                current = current[index]
            except (ValueError, IndexError):
                raise VariableResolutionError(f"Invalid list access: {field}")
        else:
            available = list(current.keys()) if isinstance(current, dict) else "not a dict"
            raise VariableResolutionError(f"Field '{field}' not found. Available: {available}")

    return current


# Note: No global instance needed - use functions directly
