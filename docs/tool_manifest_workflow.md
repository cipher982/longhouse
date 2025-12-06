# Tool Manifest Workflow

**Purpose**: Maintain a single source of truth for MCP tool definitions shared between Jarvis (UI) and Zerg (backend).

## Overview

The tool manifest ensures that:

1. Jarvis knows which tools are available for voice/text commands
2. Zerg knows how to route tool calls to MCP servers
3. Context-specific tools (personal vs work) are properly filtered
4. Tool definitions stay in sync across the platform

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│          Source of Truth: MCP Tool Definitions           │
│        apps/zerg/backend/zerg/tools/mcp_presets.py      │
└────────────────────┬────────────────────────────────────┘
                     │
         ┌───────────▼───────────┐
         │  generate-tool-       │
         │  manifest.py          │
         └───────────┬───────────┘
                     │
      ┌──────────────┴──────────────┐
      │                             │
┌─────▼──────┐              ┌──────▼─────┐
│ TypeScript │              │   Python   │
│ index.ts   │              │  tools.py  │
└─────┬──────┘              └──────┬─────┘
      │                            │
┌─────▼──────────┐        ┌────────▼────────┐
│  Jarvis PWA    │        │  Zerg Backend   │
│  (contexts)    │        │  (tool routing) │
└────────────────┘        └─────────────────┘
```

## Tool Definition Structure

Each tool contains:

```typescript
{
  name: string;           // Tool identifier (e.g., "whoop")
  description: string;    // Human-readable description
  command: string;        // Executable command ("uvx", "npx")
  args: string[];         // Command arguments
  env: Record<string, string>;  // Environment variables
  contexts: string[];     // Available contexts ("personal", "work")
}
```

## Workflow

### 1. Adding a New Tool

**Step 1**: Define in MCP presets (if not already present)

```python
# apps/zerg/backend/zerg/tools/mcp_presets.py
MCP_PRESETS = {
    "new_tool": {
        "command": "uvx",
        "args": ["mcp-server-new-tool"],
        "env": {},
        "description": "New tool description",
    },
    # ... existing tools
}
```

**Step 2**: Update baseline tools in generator

```python
# scripts/generate-tool-manifest.py
BASELINE_TOOLS = [
    {
        "name": "new_tool",
        "description": "New tool description",
        "command": "uvx",
        "args": ["mcp-server-new-tool"],
        "env": {},
        "contexts": ["personal"],  # or ["personal", "work"]
    },
    # ... existing tools
]
```

**Step 3**: Regenerate manifests

```bash
make generate-sdk
# Or directly:
python3 scripts/generate-tool-manifest.py
```

**Step 4**: Verify outputs

```bash
# Check TypeScript export
cat packages/tool-manifest/index.ts

# Check Python export
cat packages/tool-manifest/tools.py
```

**Step 5**: Use in Jarvis context

```typescript
// apps/jarvis/apps/web/contexts/personal/config.ts
import { getToolsForContext } from "@swarm/tool-manifest";

const tools = getToolsForContext("personal");
// Filter and configure for OpenAI Realtime API
```

**Step 6**: Test end-to-end

```bash
# Start Jarvis and verify tool is available
make jarvis-dev

# Dispatch an agent that uses the tool
curl -X POST http://localhost:47300/api/jarvis/dispatch \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"agent_id":1}'
```

### 2. Modifying Tool Contexts

To make a tool available in both personal and work contexts:

```python
# scripts/generate-tool-manifest.py
{
    "name": "gmail",
    "contexts": ["personal", "work"],  # Previously just ["personal"]
}
```

Then regenerate: `python3 scripts/generate-tool-manifest.py`

### 3. Updating Tool Commands

If an MCP server changes its invocation:

```python
# scripts/generate-tool-manifest.py
{
    "name": "whoop",
    "command": "uvx",
    "args": ["mcp-server-whoop", "--new-flag"],  # Added flag
}
```

Regenerate and test both Jarvis and Zerg integrations.

## Generated Files

### packages/tool-manifest/index.ts

TypeScript definitions consumed by Jarvis:

```typescript
export interface ToolDefinition {
  name: string;
  description: string;
  command: string;
  args: string[];
  env: Record<string, string>;
  contexts: string[];
}

export const TOOL_MANIFEST: ToolDefinition[];

export function getToolsForContext(context: string): ToolDefinition[];
export function getToolByName(name: string): ToolDefinition | undefined;
```

### packages/tool-manifest/tools.py

Python definitions consumed by Zerg:

```python
TOOL_MANIFEST: list[dict[str, Any]]

def get_tools_for_context(context: str) -> list[dict[str, Any]]:
    """Get tools available for a specific context."""

def get_tool_by_name(name: str) -> dict[str, Any] | None:
    """Get tool by name."""
```

## Integration Points

### Jarvis Contexts

```typescript
// apps/jarvis/apps/web/contexts/personal/config.ts
import { getToolsForContext } from "@swarm/tool-manifest";

// Get tools for personal context
const availableTools = getToolsForContext("personal");

// Configure for OpenAI Realtime API
export const personalContext = {
  tools: availableTools.map(convertToOpenAITool),
  // ... other config
};
```

### Zerg Tool Registry

```python
# apps/zerg/backend/zerg/tools/registry.py
from swarm.tool_manifest import get_tool_by_name

def get_mcp_server_config(tool_name: str):
    """Get MCP server configuration from manifest."""
    tool = get_tool_by_name(tool_name)
    if tool:
        return {
            "command": tool["command"],
            "args": tool["args"],
            "env": tool["env"],
        }
    return None
```

## Validation

### Contract Testing

Ensure Jarvis and Zerg tool definitions match:

```bash
# Run contract validation
make validate-contracts

# This should verify:
# 1. All tools in Jarvis contexts exist in manifest
# 2. All tools in agent configs exist in manifest
# 3. Tool commands are valid executables
```

### Testing Tool Availability

```typescript
// Test in Jarvis
import { TOOL_MANIFEST } from "@swarm/tool-manifest";

console.log(
  "Available tools:",
  TOOL_MANIFEST.map((t) => t.name),
);
// Expected: ['whoop', 'obsidian', 'traccar', 'gmail', 'slack']
```

```python
# Test in Zerg
from swarm.tool_manifest import TOOL_MANIFEST

print("Available tools:", [t["name"] for t in TOOL_MANIFEST])
# Expected: ['whoop', 'obsidian', 'traccar', 'gmail', 'slack']
```

## Best Practices

### 1. Single Source of Truth

- Tool definitions should ultimately come from `apps/zerg/backend/zerg/tools/mcp_presets.py`
- For now, `BASELINE_TOOLS` in the generator acts as the source
- In production, script should import and extract from actual presets

### 2. Context Separation

- **Personal**: Personal health, location, notes
- **Work**: Company emails, Slack, knowledge base
- Tools can be available in multiple contexts

### 3. Environment Variables

- Store API keys in `.env`, not in tool definitions
- Tool definitions include env variable _names_, not values
- Example: `"env": {"WHOOP_API_KEY": "$WHOOP_API_KEY"}`

### 4. Version Control

- Commit generated files to ensure reproducibility
- Tag manifest changes in PR descriptions
- Run generation in CI to catch drift

### 5. Documentation

- Update tool descriptions when capabilities change
- Document required environment variables
- Provide examples of tool usage

## Troubleshooting

### "Tool not found" errors in Jarvis

1. Check tool exists in manifest: `cat packages/tool-manifest/index.ts`
2. Verify tool is in correct context: `getToolsForContext('personal')`
3. Regenerate manifest: `python3 scripts/generate-tool-manifest.py`
4. Restart Jarvis dev server

### Tool execution fails in Zerg

1. Verify MCP server command is valid: `which uvx`
2. Check environment variables are set: `echo $WHOOP_API_KEY`
3. Test MCP server directly: `uvx mcp-server-whoop`
4. Check Zerg logs for detailed error

### Manifest out of sync

```bash
# Regenerate from source
python3 scripts/generate-tool-manifest.py

# Verify changes
git diff packages/tool-manifest/

# Commit if intentional
git add packages/tool-manifest/
git commit -m "chore: regenerate tool manifest"
```

## Future Improvements

### Auto-extraction from Backend

```python
# Instead of BASELINE_TOOLS, extract directly:
from zerg.tools.mcp_presets import MCP_PRESETS

def extract_tool_definitions():
    return [
        {
            "name": name,
            "description": config.get("description", ""),
            # ... extract from actual config
        }
        for name, config in MCP_PRESETS.items()
    ]
```

### Schema Validation

- Add JSON schema for tool definitions
- Validate on generation
- Fail CI if invalid

### Dynamic Tool Registration

- Allow Jarvis to discover tools at runtime
- Support plugin architecture
- Hot-reload tool changes without restart

## Related Documentation

- [Jarvis Integration Architecture](./jarvis_integration.md)
- [MCP Server Configuration](../apps/zerg/backend/zerg/tools/README.md)
- [Jarvis Context System](../apps/jarvis/docs/ARCHITECTURE.md)
