# Output Envelope Structure Design Specification

## Overview

This document defines the new standardized output structure for all workflow node executors, replacing the current inconsistent metadata wrapping with a clean `value`/`meta` separation.

## Current Problems

### Inconsistent Output Structures

**ToolNodeExecutor:**

```json
{
  "tool_name": "data_processor",
  "parameters": { "operation": "calculate" },
  "result": { "actual": "result", "data": "here" },
  "status": "completed"
}
```

**AgentNodeExecutor:**

```json
{
  "agent_id": 123,
  "thread_id": 456,
  "messages_created": 3,
  "status": "completed"
}
```

### User Experience Issues

1. **Confusing Access Patterns:** `${tool.result.actual}` vs `${agent.messages_created}`
2. **Implementation Leakage:** Users must understand internal wrapping structure
3. **Inconsistent Metadata:** Different nodes expose different metadata fields
4. **Poor Discoverability:** No standard way to access "the main result"

## New Design: Standardized Envelope

### Core Structure

```json
{
  "value": <primary_result_data>,
  "meta": {
    "node_type": "tool|agent|trigger|conditional",
    "status": "completed|failed|running",
    "execution_time_ms": 1250,
    "node_specific_field": "...",
    "..."
  }
}
```

### Design Principles

1. **Primary Value First:** Main result always accessible via `.value`
2. **Consistent Metadata:** All nodes expose standard metadata fields
3. **Extensible:** Node-specific metadata can be added under `meta`
4. **Intuitive Access:** `${node.result}` and `${node.value}` both work
5. **Rich Context:** Execution metadata preserved for debugging/monitoring

## Node-Specific Implementations

### ToolNodeExecutor

**Before:**

```json
{
  "tool_name": "data_processor",
  "parameters": { "operation": "calculate" },
  "result": { "score": 85, "status": "completed" },
  "status": "completed"
}
```

**After:**

```json
{
  "value": { "score": 85, "status": "completed" },
  "meta": {
    "node_type": "tool",
    "status": "completed",
    "execution_time_ms": 1250,
    "tool_name": "data_processor",
    "parameters": { "operation": "calculate" },
    "tool_version": "1.2.0"
  }
}
```

**Variable Access:**

- `${tool-1.result}` → `{"score": 85, "status": "completed"}`
- `${tool-1.value}` → `{"score": 85, "status": "completed"}`
- `${tool-1.result.score}` → `85` (nested access)
- `${tool-1.meta.tool_name}` → `"data_processor"`
- `${tool-1.meta.status}` → `"completed"`

### AgentNodeExecutor

**Before:**

```json
{
  "agent_id": 123,
  "thread_id": 456,
  "messages_created": 3,
  "status": "completed"
}
```

**After:**

```json
{
  "value": {
    "messages_created": 3,
    "final_response": "Task completed successfully",
    "thread_id": 456
  },
  "meta": {
    "node_type": "agent",
    "status": "completed",
    "execution_time_ms": 5670,
    "agent_id": 123,
    "agent_name": "Data Analyst",
    "model_used": "gpt-4",
    "total_tokens": 1250,
    "total_cost_usd": 0.025
  }
}
```

**Variable Access:**

- `${agent-1.result}` → `{"messages_created": 3, "final_response": "...", "thread_id": 456}`
- `${agent-1.result.messages_created}` → `3`
- `${agent-1.meta.agent_name}` → `"Data Analyst"`
- `${agent-1.meta.total_tokens}` → `1250`

### ConditionalNodeExecutor

**Before:**

```json
{
  "condition": "${tool-1.result} > 50",
  "condition_result": true,
  "status": "completed",
  "branch": "true"
}
```

**After:**

```json
{
  "value": {
    "condition_result": true,
    "branch": "true"
  },
  "meta": {
    "node_type": "conditional",
    "status": "completed",
    "execution_time_ms": 15,
    "condition": "${tool-1.result} > 50",
    "resolved_condition": "85 > 50",
    "evaluation_method": "ast_safe"
  }
}
```

**Variable Access:**

- `${conditional-1.result}` → `{"condition_result": true, "branch": "true"}`
- `${conditional-1.result.branch}` → `"true"`
- `${conditional-1.meta.condition}` → `"${tool-1.result} > 50"`

### TriggerNodeExecutor

**Before:**

```json
{
  "status": "triggered",
  "config": {
    "trigger": {
      "type": "manual",
      "config": { "enabled": true, "params": {}, "filters": [] }
    }
  }
}
```

**After:**

```json
{
  "value": {
    "triggered": true,
    "trigger_data": null
  },
  "meta": {
    "node_type": "trigger",
    "status": "completed",
    "execution_time_ms": 5,
    "trigger_type": "manual",
    "trigger_config": { "enabled": true, "params": {}, "filters": [] }
  }
}
```

## Standard Metadata Fields

### Required Fields (All Nodes)

```json
{
  "meta": {
    "node_type": "tool|agent|trigger|conditional",
    "status": "completed|failed|running",
    "execution_time_ms": 1250,
    "started_at": "2024-01-15T10:30:00Z",
    "finished_at": "2024-01-15T10:30:01.250Z"
  }
}
```

### Optional Standard Fields

```json
{
  "meta": {
    "error": "Error message if status=failed",
    "error_type": "ValidationError|TimeoutError|...",
    "retry_count": 0,
    "memory_usage_mb": 45.2,
    "cpu_time_ms": 1100
  }
}
```

### Node-Specific Metadata

**Tool Nodes:**

```json
{
  "meta": {
    "tool_name": "data_processor",
    "tool_version": "1.2.0",
    "parameters": { "operation": "calculate" },
    "tool_execution_time_ms": 1200
  }
}
```

**Agent Nodes:**

```json
{
  "meta": {
    "agent_id": 123,
    "agent_name": "Data Analyst",
    "model_used": "gpt-4",
    "total_tokens": 1250,
    "total_cost_usd": 0.025,
    "thread_id": 456
  }
}
```

## Variable Resolution Rules

### Primary Access Patterns

| Pattern          | Resolves To       | Description                      |
| ---------------- | ----------------- | -------------------------------- |
| `${node}`        | `output["value"]` | Primary result (shorthand)       |
| `${node.value}`  | `output["value"]` | Primary result (explicit)        |
| `${node.result}` | `output["value"]` | Primary result (intuitive alias) |

### Nested Access Patterns

| Pattern                | Resolves To                | Description            |
| ---------------------- | -------------------------- | ---------------------- |
| `${node.result.field}` | `output["value"]["field"]` | Nested field in result |
| `${node.value.field}`  | `output["value"]["field"]` | Nested field in result |

### Metadata Access Patterns

| Pattern                          | Resolves To                           | Description            |
| -------------------------------- | ------------------------------------- | ---------------------- |
| `${node.meta.status}`            | `output["meta"]["status"]`            | Execution status       |
| `${node.meta.tool_name}`         | `output["meta"]["tool_name"]`         | Tool-specific metadata |
| `${node.meta.execution_time_ms}` | `output["meta"]["execution_time_ms"]` | Performance data       |

## Backward Compatibility Strategy

### Migration Phase 1: Dual Support

Support both old and new formats during transition:

```python
def resolve_variable_with_compatibility(node_output, path_parts):
    """Resolve variable with backward compatibility."""

    # Check if this is new envelope format
    if "value" in node_output and "meta" in node_output:
        return resolve_new_format(node_output, path_parts)
    else:
        # Legacy format - apply compatibility mapping
        return resolve_legacy_format(node_output, path_parts)
```

### Legacy Format Mapping

**For ToolNodeExecutor legacy outputs:**

```python
def map_legacy_tool_output(legacy_output):
    """Map legacy tool output to new envelope format."""
    return {
        "value": legacy_output.get("result", legacy_output),
        "meta": {
            "node_type": "tool",
            "status": legacy_output.get("status", "completed"),
            "tool_name": legacy_output.get("tool_name"),
            "parameters": legacy_output.get("parameters", {}),
            # execution_time_ms will be missing in legacy data
        }
    }
```

### Deprecation Warnings

```python
def resolve_legacy_format(node_output, path_parts):
    """Resolve legacy format with deprecation warning."""

    # Detect problematic patterns
    if len(path_parts) >= 2 and path_parts[0] == "result" and path_parts[1] == "result":
        logger.warning(
            f"Deprecated variable access pattern detected: ${{{'.'.join(path_parts)}}}. "
            f"Use ${{node.result}} instead of ${{node.result.result}}. "
            "This pattern will be removed in the next major version."
        )

    return legacy_resolve_logic(node_output, path_parts)
```

## Implementation Plan

### Phase 1: Base Infrastructure

1. Create `NodeOutputEnvelope` schema class
2. Update `BaseNodeExecutor` to use new format
3. Implement compatibility layer for variable resolution

### Phase 2: Node Executor Updates

1. Update each node executor to emit new format
2. Preserve all existing metadata in appropriate locations
3. Add standard metadata fields (execution time, status, etc.)

### Phase 3: Testing & Validation

1. Test all variable access patterns work correctly
2. Validate backward compatibility with existing workflows
3. Performance testing for metadata overhead

### Phase 4: Migration Support

1. Add deprecation warnings for old patterns
2. Create migration tools for existing workflows
3. Update documentation and examples

## Benefits

### For Users

- **Intuitive Access:** `${tool.result}` just works
- **Rich Metadata:** Performance data, execution context available
- **Consistent Experience:** Same patterns across all node types
- **Better Debugging:** Clear execution metadata for troubleshooting

### For Developers

- **Standardized Interface:** All nodes follow same pattern
- **Extensible Design:** Easy to add new metadata fields
- **Clean Separation:** Business logic (value) vs execution metadata (meta)
- **Better Testing:** Standard structure makes testing easier

---

**Status:** Design Complete ✅
**Next Task:** Design Variable Resolution Engine
