# Oikos Tools

## Overview

The oikos tools layer enables Zerg's oikos/commis architecture by providing tools that allow oikos agents to spawn, manage, and query commis agents. This implements Milestone 2 of the commis system architecture.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Oikos Agent                         │
│  (can delegate tasks, query results, drill into artifacts)  │
└──────────────────────┬──────────────────────────────────────┘
                       │ uses oikos tools
                       ↓
┌─────────────────────────────────────────────────────────────┐
│                   Oikos Tools                           │
│  - spawn_commis()        - list_commiss()                    │
│  - read_commis_result()  - read_commis_file()                │
│  - peek_commis_output()                                      │
│  - grep_commiss()        - get_commis_metadata()             │
└──────────────────────┬──────────────────────────────────────┘
                       │ wraps
                       ↓
┌─────────────────────────────────────────────────────────────┐
│                Commis Services (Milestone 1)                 │
│  - CommisRunner       - CommisArtifactStore                  │
└─────────────────────────────────────────────────────────────┘
```

## Bootstrap vs Registry Tools

Oikos tools come from two paths so we keep the agent boot sequence fast while still
supporting a large tool catalog.

- **Bootstrap/core tools** are pre-loaded for every oikos run (commis orchestration +
  discovery utilities). Source of truth is `zerg/tools/builtin/oikos_tools.py`
  (`OIKOS_TOOL_NAMES`), which feeds `CORE_TOOLS` in `zerg/tools/catalog.py`.
- **Registry tools** are everything else in `BUILTIN_TOOLS` plus runtime/MCP tools.
  They live in the registry and are lazy-loaded via `ToolResolver` + `LazyToolBinder`
  on first use.

Rule of thumb: if a tool is required for the very first turn, it must be in
`OIKOS_TOOL_NAMES`; otherwise it can stay registry-only.

## Tools

### spawn_commis(task: str, model: str | None = None) -> str

Spawns a disposable commis agent to execute a task independently.

**Use cases:**

- Delegating sub-tasks from a oikos
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

**Note:** The oikos-facing `spawn_commis` tool is intentionally fire-and-forget (durable-runs model).
Roundabout-style waiting exists in the underlying implementation but is not exposed to the LLM tool schema.

---

### list_commiss(limit: int = 20, status: str = None, since_hours: int = None) -> str

Lists recent commis executions with optional filters.

**Parameters:**

- `limit`: Maximum commiss to return (default: 20)
- `status`: Filter by "queued", "running", "success", "failed", or None for all
- `since_hours`: Only show commiss from last N hours

**Example:**

```python
# List all recent commiss
list_commiss(limit=10)

# List only failed commiss from last 24 hours
list_commiss(status="failed", since_hours=24)
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

### peek_commis_output(job_id: str, max_bytes: int = 4000) -> str

Peeks at live output for a running commis (tail buffer).

**Use cases:**

- Streaming runner_exec output while a commis is still running
- Quick progress checks without waiting for completion

**Example:**

```python
output = peek_commis_output("123", max_bytes=2000)
```

**Notes:**

- Returns the most recent output for active commiss (best-effort).
- Full artifacts (`thread.jsonl`, `tool_calls/*.txt`, `result.txt`) are written after completion; live tail uses `peek_commis_output` / `commis_output_chunk` SSE.
- Workspace commiss do not stream live output.

---

### grep_commiss(pattern: str, since_hours: int = 24) -> str

Searches across commis artifacts for a text pattern.

**Features:**

- Case-insensitive search
- Searches all .txt files in commis directories
- Returns matches with context

**Example:**

```python
# Find all commiss that encountered "timeout" errors
matches = grep_commiss("timeout", since_hours=48)

# Search for specific output patterns
matches = grep_commiss("disk usage", since_hours=24)
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

- `oikos_tools.py` → `CommisRunner`
- `CommisRunner` → `Runner`
- `Runner` → `tools.builtin`

We use **lazy imports** - `CommisRunner` is imported inside the `spawn_commis` function rather than at module level.

## Testing

### Error Handling Tests

**Location:** `tests/tools/test_oikos_tools_errors.py`

**Coverage:**

- Tool error cases and edge conditions
- Path traversal security
- Graceful failure modes

## Usage Example

```python
from zerg.tools.builtin.oikos_tools import (
    spawn_commis,
    list_commiss,
    read_commis_result,
)

# Spawn a commis
result = spawn_commis(
    task="Analyze the logs from the last deployment",
    model="gpt-4o"
)

# List recent commiss
commiss = list_commiss(limit=5, status="success")

# Read a specific result
commis_result = read_commis_result("2024-12-03T14-32-00_analyze-logs")
```

## Next Steps

### Milestone 3: Agent API Integration

To expose oikos tools to agents via the API:

1. **Update agent configuration** to include oikos tools in allowlist
2. **Frontend integration** - UI to enable oikos mode for agents
3. **Tool group creation** - Add "oikos" tool group to `constants/toolGroups.ts`
4. **Documentation** - User-facing docs on oikos/commis patterns

### Potential Enhancements

1. **Commis cancellation** - Add `cancel_commis(job_id)` tool
2. **Commis streaming** - Stream commis output in real-time
3. **Commis pools** - Spawn multiple commiss in parallel with `spawn_commis_pool()`
4. **Result aggregation** - Tool to aggregate results from multiple commiss
5. **Commis retry** - Automatically retry failed commiss
