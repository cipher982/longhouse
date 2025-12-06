# Variable Resolution Engine Design Specification

## Overview

This document specifies the enhanced variable resolution system that will support the new output envelope structure while maintaining backward compatibility and providing intuitive access patterns.

## Current System Analysis

### Existing Implementation

```python
def resolve_variables(data: Any, node_outputs: Dict[str, Any]) -> Any:
    """Current variable resolution implementation."""
    if isinstance(data, str):
        pattern = r'\${([^}]+)}'
        def replace_var(match):
            var_path = match.group(1)
            if '.' in var_path:
                node_id, output_key = var_path.split('.', 1)
                if node_id in node_outputs:
                    node_output = node_outputs[node_id]
                    if isinstance(node_output, dict) and output_key in node_output:
                        return str(node_output[output_key])  # ISSUE: Always returns string
            # ... rest of implementation
```

### Current Problems

1. **String Conversion:** All values converted to strings via `str()`
2. **Limited Nesting:** Only supports one level of nesting (`node.key`)
3. **No Type Preservation:** Loses original data types (int, bool, dict)
4. **Inconsistent Behavior:** Different handling for different data types
5. **No Alias Support:** Can't support `${node.result}` → `${node.value}` mapping

## New Design: Enhanced Variable Resolution

### Core Principles

1. **Type Preservation:** Maintain original Python types throughout resolution
2. **Deep Nesting:** Support arbitrary nesting depth (`node.result.field.subfield`)
3. **Alias Support:** Flexible mapping of access patterns
4. **Backward Compatibility:** Graceful handling of legacy formats
5. **Performance:** Efficient resolution with caching support

### Resolution Algorithm

```python
class VariableResolver:
    """Enhanced variable resolution with type preservation and aliases."""

    def resolve_variables(self, data: Any, node_outputs: Dict[str, Any]) -> Any:
        """
        Resolve variables maintaining original types.

        Process:
        1. Find all ${...} patterns in strings
        2. Parse variable paths (node.field.subfield)
        3. Resolve each path to actual value
        4. Substitute with resolved value (preserving type)
        5. Handle special cases (aliases, compatibility)
        """

    def resolve_variable_path(self, path: str, node_outputs: Dict[str, Any]) -> Any:
        """
        Resolve a single variable path to its value.

        Examples:
        - "tool-1" → node_outputs["tool-1"]["value"]
        - "tool-1.result" → node_outputs["tool-1"]["value"] (alias)
        - "tool-1.value" → node_outputs["tool-1"]["value"]
        - "tool-1.meta.status" → node_outputs["tool-1"]["meta"]["status"]
        - "tool-1.result.score" → node_outputs["tool-1"]["value"]["score"]
        """
```

## Variable Path Resolution Rules

### Path Parsing

```python
def parse_variable_path(path: str) -> Tuple[str, List[str]]:
    """
    Parse variable path into node_id and field path.

    Examples:
    - "tool-1" → ("tool-1", [])
    - "tool-1.result" → ("tool-1", ["result"])
    - "tool-1.meta.status" → ("tool-1", ["meta", "status"])
    - "tool-1.result.score" → ("tool-1", ["result", "score"])
    """
    parts = path.split('.')
    node_id = parts[0]
    field_path = parts[1:] if len(parts) > 1 else []
    return node_id, field_path
```

### Resolution Rules Table

| Path Pattern           | Resolution Logic                | Example                               |
| ---------------------- | ------------------------------- | ------------------------------------- |
| `${node}`              | `node_output["value"]`          | `${tool-1}` → tool result             |
| `${node.value}`        | `node_output["value"]`          | `${tool-1.value}` → tool result       |
| `${node.result}`       | `node_output["value"]` (alias)  | `${tool-1.result}` → tool result      |
| `${node.meta.field}`   | `node_output["meta"]["field"]`  | `${tool-1.meta.status}` → "completed" |
| `${node.result.field}` | `node_output["value"]["field"]` | `${tool-1.result.score}` → 85         |
| `${node.value.field}`  | `node_output["value"]["field"]` | `${tool-1.value.score}` → 85          |

### Alias Resolution

```python
class AliasResolver:
    """Handle variable path aliases for backward compatibility."""

    ALIASES = {
        "result": "value",  # ${node.result} → ${node.value}
    }

    def resolve_alias(self, field_path: List[str]) -> List[str]:
        """
        Resolve field path aliases.

        Examples:
        - ["result"] → ["value"]
        - ["result", "score"] → ["value", "score"]
        - ["meta", "status"] → ["meta", "status"] (no change)
        """
        if not field_path:
            return field_path

        first_field = field_path[0]
        if first_field in self.ALIASES:
            resolved_path = [self.ALIASES[first_field]] + field_path[1:]
            return resolved_path

        return field_path
```

## Type-Preserving Resolution

### Current Problem

```python
# Current implementation always returns strings
return str(node_output[output_key])  # 85 becomes "85", True becomes "True"
```

### New Implementation

```python
def resolve_with_type_preservation(self, node_output: Dict, field_path: List[str]) -> Any:
    """
    Resolve field path while preserving original types.

    Returns:
        Original Python object (int, bool, dict, list, str, etc.)
    """
    current = node_output

    for field in field_path:
        if isinstance(current, dict) and field in current:
            current = current[field]
        else:
            raise VariableResolutionError(
                f"Field '{field}' not found in path {'.'.join(field_path)}"
            )

    return current  # Return actual object, not string representation
```

### Type Preservation Examples

```python
# Tool returns: {"value": {"score": 85, "passed": True}, "meta": {...}}

# Variable resolution results:
"${tool-1.result.score}" → 85 (int, not "85")
"${tool-1.result.passed}" → True (bool, not "True")
"${tool-1.result}" → {"score": 85, "passed": True} (dict, not str repr)
"${tool-1.meta.status}" → "completed" (str)
```

## String Interpolation Handling

### Mixed Type Resolution

When variables are used in string contexts, handle appropriately:

```python
def resolve_in_string_context(self, template: str, node_outputs: Dict[str, Any]) -> str:
    """
    Resolve variables within string templates.

    Example:
    "Score: ${tool-1.result.score} (${tool-1.meta.status})"
    → "Score: 85 (completed)"
    """
    pattern = r'\${([^}]+)}'

    def replace_var(match):
        var_path = match.group(1)
        try:
            resolved_value = self.resolve_variable_path(var_path, node_outputs)
            # Convert to string for interpolation
            return str(resolved_value)
        except VariableResolutionError:
            # Keep original if resolution fails
            return match.group(0)

    return re.sub(pattern, replace_var, template)
```

### Pure Variable Resolution

When the entire value is a variable, return the actual object:

```python
def resolve_data_structure(self, data: Any, node_outputs: Dict[str, Any]) -> Any:
    """
    Resolve variables in data structures while preserving types.

    Examples:
    - "${tool-1.result.score}" → 85 (int)
    - "Score: ${tool-1.result.score}" → "Score: 85" (str)
    - {"threshold": "${tool-1.result.score}"} → {"threshold": 85}
    """
    if isinstance(data, str):
        # Check if entire string is a single variable
        single_var_pattern = r'^\${([^}]+)}$'
        match = re.match(single_var_pattern, data)
        if match:
            # Return actual resolved value (preserve type)
            var_path = match.group(1)
            return self.resolve_variable_path(var_path, node_outputs)
        else:
            # String interpolation (convert to string)
            return self.resolve_in_string_context(data, node_outputs)

    elif isinstance(data, dict):
        return {key: self.resolve_data_structure(value, node_outputs)
                for key, value in data.items()}

    elif isinstance(data, list):
        return [self.resolve_data_structure(item, node_outputs)
                for item in data]

    else:
        return data  # Return non-string data as-is
```

## Error Handling

### Exception Hierarchy

```python
class VariableResolutionError(Exception):
    """Base exception for variable resolution errors."""
    pass

class NodeNotFoundError(VariableResolutionError):
    """Raised when referenced node doesn't exist."""
    pass

class FieldNotFoundError(VariableResolutionError):
    """Raised when referenced field doesn't exist in node output."""
    pass

class InvalidPathError(VariableResolutionError):
    """Raised when variable path is malformed."""
    pass
```

### Error Handling Strategy

```python
def resolve_variable_path_safe(self, path: str, node_outputs: Dict[str, Any]) -> Any:
    """
    Safely resolve variable path with helpful error messages.

    Returns:
        Resolved value or raises descriptive error
    """
    try:
        node_id, field_path = self.parse_variable_path(path)

        if node_id not in node_outputs:
            available_nodes = list(node_outputs.keys())
            raise NodeNotFoundError(
                f"Node '{node_id}' not found. Available nodes: {available_nodes}"
            )

        node_output = node_outputs[node_id]
        resolved_path = self.resolve_alias(field_path)

        return self.resolve_with_type_preservation(node_output, resolved_path)

    except Exception as e:
        logger.warning(f"Variable resolution failed for path '{path}': {e}")
        raise VariableResolutionError(f"Failed to resolve ${{{path}}}: {e}")
```

## Backward Compatibility

### Legacy Format Detection

```python
def detect_output_format(self, node_output: Any) -> str:
    """
    Detect if node output uses legacy or new envelope format.

    Returns:
        "envelope" for new {"value": ..., "meta": ...} format
        "legacy" for old format
    """
    if (isinstance(node_output, dict) and
        "value" in node_output and
        "meta" in node_output):
        return "envelope"
    else:
        return "legacy"
```

### Legacy Compatibility Layer

```python
def resolve_legacy_format(self, node_output: Any, field_path: List[str]) -> Any:
    """
    Handle legacy node output formats.

    For ToolNodeExecutor legacy format:
    {"tool_name": "...", "result": {...}, "status": "..."}

    Mapping:
    - ${node.result} → legacy_output["result"]
    - ${node.meta.status} → legacy_output["status"]
    - ${node.meta.tool_name} → legacy_output["tool_name"]
    """
    if not field_path:
        # ${node} → return the "result" field for tools, or entire output
        return node_output.get("result", node_output)

    first_field = field_path[0]

    if first_field in ["result", "value"]:
        # ${node.result} or ${node.value} → legacy result field
        result_value = node_output.get("result", node_output)
        if len(field_path) > 1:
            # ${node.result.field} → nested access
            return self.resolve_with_type_preservation({"result": result_value}, field_path)
        else:
            return result_value

    elif first_field == "meta":
        # ${node.meta.field} → map to legacy top-level fields
        if len(field_path) < 2:
            raise FieldNotFoundError("meta access requires field specification")

        meta_field = field_path[1]
        if meta_field in node_output:
            return node_output[meta_field]
        else:
            raise FieldNotFoundError(f"Legacy format doesn't have meta field '{meta_field}'")

    else:
        # Direct field access: ${node.field}
        if first_field in node_output:
            value = node_output[first_field]
            if len(field_path) > 1:
                # Nested access
                return self.resolve_with_type_preservation({first_field: value}, field_path)
            else:
                return value
        else:
            raise FieldNotFoundError(f"Field '{first_field}' not found in legacy output")
```

### Deprecation Warnings

```python
def check_deprecated_patterns(self, path: str):
    """Check for deprecated variable access patterns."""

    # Check for nested .result.result pattern
    if ".result.result" in path:
        logger.warning(
            f"Deprecated pattern detected: ${{{path}}}. "
            f"Use ${{node.result}} instead of ${{node.result.result}}. "
            "The nested .result.result pattern will be removed in a future version."
        )

    # Check for legacy direct field access that should use meta
    deprecated_direct_fields = ["status", "tool_name", "agent_id"]
    parts = path.split('.')
    if len(parts) == 2 and parts[1] in deprecated_direct_fields:
        logger.warning(
            f"Direct field access ${{{path}}} is deprecated. "
            f"Use ${{node.meta.{parts[1]}}} instead for metadata access."
        )
```

## Performance Optimizations

### Variable Path Caching

```python
class CachedVariableResolver(VariableResolver):
    """Variable resolver with path parsing cache."""

    def __init__(self):
        super().__init__()
        self._path_cache = {}  # Cache parsed paths
        self._alias_cache = {}  # Cache alias resolutions

    def parse_variable_path_cached(self, path: str) -> Tuple[str, List[str]]:
        """Parse variable path with caching."""
        if path not in self._path_cache:
            self._path_cache[path] = self.parse_variable_path(path)
        return self._path_cache[path]
```

### Resolution Optimization

```python
def resolve_variables_optimized(self, data: Any, node_outputs: Dict[str, Any]) -> Any:
    """
    Optimized variable resolution with early exit conditions.

    Optimizations:
    1. Skip processing if no ${} patterns detected
    2. Cache regex compilation
    3. Batch process multiple variables in same string
    4. Early type detection
    """
    if isinstance(data, str):
        # Quick check: does string contain variables?
        if '${' not in data:
            return data  # Early exit

        # Use compiled regex for better performance
        return self._resolve_string_variables(data, node_outputs)

    # ... rest of implementation
```

## Integration with Expression Evaluator

### Seamless Integration

```python
def prepare_variables_for_evaluation(self, expression: str, node_outputs: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """
    Prepare expression and variables for safe evaluation.

    Process:
    1. Find all ${...} patterns in expression
    2. Resolve each to actual value (preserving type)
    3. Replace with temporary variable names
    4. Return modified expression + variable mapping

    Example:
    Input: "${tool-1.result.score} >= 80"
    Output: ("__var_0 >= 80", {"__var_0": 85})
    """
    variables = {}
    var_counter = 0

    def replace_with_temp_var(match):
        nonlocal var_counter
        var_path = match.group(1)
        temp_var = f"__var_{var_counter}"
        var_counter += 1

        # Resolve to actual value
        resolved_value = self.resolve_variable_path(var_path, node_outputs)
        variables[temp_var] = resolved_value

        return temp_var

    pattern = r'\${([^}]+)}'
    modified_expression = re.sub(pattern, replace_with_temp_var, expression)

    return modified_expression, variables
```

## Testing Strategy

### Unit Tests

```python
def test_type_preservation():
    """Test that variable resolution preserves types."""
    node_outputs = {
        "tool-1": {
            "value": {"score": 85, "passed": True, "data": [1, 2, 3]},
            "meta": {"status": "completed"}
        }
    }

    resolver = VariableResolver()

    # Test type preservation
    assert resolver.resolve_variables("${tool-1.result.score}", node_outputs) == 85
    assert resolver.resolve_variables("${tool-1.result.passed}", node_outputs) is True
    assert resolver.resolve_variables("${tool-1.result.data}", node_outputs) == [1, 2, 3]
    assert resolver.resolve_variables("${tool-1.meta.status}", node_outputs) == "completed"

def test_alias_resolution():
    """Test that aliases work correctly."""
    node_outputs = {"tool-1": {"value": 42, "meta": {}}}
    resolver = VariableResolver()

    # All should resolve to same value
    assert resolver.resolve_variables("${tool-1}", node_outputs) == 42
    assert resolver.resolve_variables("${tool-1.value}", node_outputs) == 42
    assert resolver.resolve_variables("${tool-1.result}", node_outputs) == 42

def test_backward_compatibility():
    """Test legacy format compatibility."""
    legacy_output = {
        "tool-1": {"tool_name": "processor", "result": {"score": 85}, "status": "completed"}
    }

    resolver = VariableResolver()

    # Should work with legacy format
    assert resolver.resolve_variables("${tool-1.result.score}", legacy_output) == 85
    assert resolver.resolve_variables("${tool-1.meta.status}", legacy_output) == "completed"
```

---

**Status:** Design Complete ✅
**Next Phase:** Ready to begin implementation (Task 2.1: Implement Safe AST Evaluator)
