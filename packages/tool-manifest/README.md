# @swarm/tool-manifest

Shared MCP tool definitions used by both Jarvis (UI/voice) and Zerg (backend).

## Purpose

Provides a single source of truth for:
- Tool names and descriptions
- Tool schemas and parameters
- Tool availability by context (personal/work)
- Tool routing to MCP servers

## Usage

### TypeScript (Jarvis)
```typescript
import { tools } from '@swarm/tool-manifest';
```

### Python (Zerg)
```python
from swarm.tool_manifest import get_tool_config
```

## Generation

The manifest is generated from Python tool definitions in `apps/zerg/backend/zerg/tools/`.

Run `npm run generate` to regenerate TypeScript exports.
