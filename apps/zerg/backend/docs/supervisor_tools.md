# Concierge Tools

## Overview

The concierge tools layer enables Zerg's concierge/commis architecture by providing tools that allow concierge fiches to spawn, manage, and query commis fiches. This implements Milestone 2 of the commis system architecture.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Concierge Fiche                         │
│  (can delegate tasks, query results, drill into artifacts)  │
└──────────────────────┬──────────────────────────────────────┘
                       │ uses concierge tools
                       ↓
┌─────────────────────────────────────────────────────────────┐
│                   Concierge Tools                           │
│  - spawn_commis()        - list_commis()                    │
│  - read_commis_result()  - read_commis_file()                │
│  - grep_commis()        - get_commis_metadata()             │
└──────────────────────┬──────────────────────────────────────┘
                       │ wraps
                       ↓
┌─────────────────────────────────────────────────────────────┐
│                Commis Services (Milestone 1)                 │
│  - CommisRunner       - CommisArtifactStore                  │
└─────────────────────────────────────────────────────────────┘
```

## Tools

### spawn_commis(task: str, model: str | None = None) -> str

Spawns a disposable commis fiche to execute a task independently.

**Use cases:**

- Delegating sub-tasks from a concierge
- Parallel execution of multiple tasks
- Isolating verbose or risky operations

**Example:**

```python
result = spawn_commis(
    task="Check disk usage on cube server via SSH",
)
# Returns: "Commis job <id> queued successfully..."
```

**Returns:**

- A queued summary containing the `job_id`

**Note:** The concierge-facing `spawn_commis` tool is intentionally fire-and-forget (durable-runs model).
Roundabout-style waiting exists in the underlying implementation but is not exposed to the LLM tool schema.

---

### list_commis(limit: int = 20, status: str = None, since_hours: int = None) -> str

Lists recent commis executions with optional filters.

**Parameters:**

- `limit`: Maximum commis to return (default: 20)
- `status`: Filter by "queued", "running", "success", "failed", or None for all
- `since_hours`: Only show commis from last N hours

**Example:**

```python
# List all recent commis
list_commis(limit=10)

# List only failed commis from last 24 hours
list_commis(status="failed", since_hours=24)
```

**Returns:** Formatted list with commis IDs, tasks, status, timestamps

---

### read_commis_result(job_id: str) -> str

Reads the final result from a completed commis.

**Example:**

```python
result = read_commis_result("123")
# Returns the commis's natural language result (includes duration if available)
```

---

### read_commis_file(job_id: str, file_path: str) -> str

Reads a specific file from a commis's artifact directory.

**Common file paths:**

- `result.txt` - Final result
- `metadata.json` - Commis metadata
- `thread.jsonl` - Full conversation history
- `tool_calls/001_ssh_exec.txt` - Individual tool outputs

**Security:** Path traversal is blocked. Only files within the commis directory are accessible.

**Example:**

```python
# Read metadata
metadata = read_commis_file(
    "123",
    "metadata.json"
)

# Read a specific tool output
output = read_commis_file(
    "123",
    "tool_calls/001_ssh_exec.txt"
)
```

---

### grep_commis(pattern: str, since_hours: int = 24) -> str

Searches across commis artifacts for a text pattern.

**Features:**

- Case-insensitive search
- Searches all .txt files in commis directories
- Returns matches with context

**Example:**

```python
# Find all commis that encountered "timeout" errors
matches = grep_commis("timeout", since_hours=48)

# Search for specific output patterns
matches = grep_commis("disk usage", since_hours=24)
```

---

### get_commis_metadata(job_id: str) -> str

Gets detailed metadata about a commis execution.

**Returns:**

- Task description
- Status (success/failed)
- Timestamps (created, started, finished)
- Duration
- Configuration
- Error message (if failed)

**Example:**

```python
metadata = get_commis_metadata("123")
```

## Implementation Details

### Database Access

The tools use Zerg's existing credential resolver pattern to access the database:

```python
from zerg.connectors.context import get_credential_resolver

resolver = get_credential_resolver()
if resolver:
    db = resolver.db  # SQLAlchemy session
    owner_id = resolver.owner_id
```

This follows the same pattern as other Zerg tools (slack_tools, github_tools, etc.).

### Async Handling

`spawn_commis` is internally async. The tool wraps the async call synchronously:

```python
from zerg.utils.async_utils import run_async_safely

result = run_async_safely(spawn_commis_async(...))
```

This is necessary because LangChain tools must be synchronous functions.

### Circular Import Prevention

To avoid circular imports between:

- `concierge_tools.py` → `CommisRunner`
- `CommisRunner` → `AgentRunner`
- `AgentRunner` → `tools.builtin`

We use **lazy imports** - `CommisRunner` is imported inside the `spawn_commis` function rather than at module level.

## Testing

### Unit Tests

**Location:** `tests/test_concierge_tools.py`

**Coverage:**

- All 6 tools with success and error cases
- Path traversal security
- Time filters
- Status filters
- Case-insensitive search
- Multiple commis workflows

**Results:** 20/20 tests passing

### Integration Tests

**Location:** `tests/test_concierge_tools_integration.py`

**Tests:**

- Tool registration in BUILTIN_TOOLS
- End-to-end fiche usage (requires tool allowlist configuration)

## Usage Example

```python
from zerg.tools.builtin.concierge_tools import (
    spawn_commis,
    list_commis,
    read_commis_result,
)

# Spawn a commis
result = spawn_commis(
    task="Analyze the logs from the last deployment",
    model="gpt-4o"
)

# List recent commis
commis = list_commis(limit=5, status="success")

# Read a specific result
commis_result = read_commis_result("2024-12-03T14-32-00_analyze-logs")
```

## Demo

Run the interactive demo:

```bash
cd apps/zerg/backend
uv run python examples/concierge_tools_demo.py
```

## Files Created/Modified

### Created:

- `zerg/tools/builtin/concierge_tools.py` - Tool implementations
- `tests/test_concierge_tools.py` - Unit tests
- `tests/test_concierge_tools_integration.py` - Integration tests
- `examples/concierge_tools_demo.py` - Demo script
- `apps/zerg/backend/docs/concierge_tools.md` - This document

### Modified:

- `zerg/tools/builtin/__init__.py` - Registered concierge tools

## Next Steps

### Milestone 3: Fiche API Integration

To expose concierge tools to fiches via the API:

1. **Update fiche configuration** to include concierge tools in allowlist
2. **Frontend integration** - UI to enable concierge mode for fiches
3. **Tool group creation** - Add "concierge" tool group to `constants/toolGroups.ts`
4. **Documentation** - User-facing docs on concierge/commis patterns

### Potential Enhancements

1. **Commis cancellation** - Add `cancel_commis(job_id)` tool
2. **Commis streaming** - Stream commis output in real-time
3. **Commis pools** - Spawn multiple commis in parallel with `spawn_commis_pool()`
4. **Result aggregation** - Tool to aggregate results from multiple commis
5. **Commis retry** - Automatically retry failed commis
