# Supervisor Tools

## Overview

The supervisor tools layer enables Zerg's supervisor/worker architecture by providing tools that allow supervisor agents to spawn, manage, and query worker agents. This implements Milestone 2 of the worker system architecture.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Supervisor Agent                         │
│  (can delegate tasks, query results, drill into artifacts)  │
└──────────────────────┬──────────────────────────────────────┘
                       │ uses supervisor tools
                       ↓
┌─────────────────────────────────────────────────────────────┐
│                   Supervisor Tools                           │
│  - spawn_worker()        - list_workers()                    │
│  - read_worker_result()  - read_worker_file()                │
│  - grep_workers()        - get_worker_metadata()             │
└──────────────────────┬──────────────────────────────────────┘
                       │ wraps
                       ↓
┌─────────────────────────────────────────────────────────────┐
│                Worker Services (Milestone 1)                 │
│  - WorkerRunner       - WorkerArtifactStore                  │
└─────────────────────────────────────────────────────────────┘
```

## Tools

### spawn_worker(task: str, model: str | None = None, wait: bool = False, timeout_seconds: float = 300.0, decision_mode: str = "heuristic") -> str

Spawns a disposable worker agent to execute a task independently.

**Use cases:**

- Delegating sub-tasks from a supervisor
- Parallel execution of multiple tasks
- Isolating verbose or risky operations

**Example:**

```python
result = spawn_worker(
    task="Check disk usage on cube server via SSH",
    wait=False
)
# Returns: "Worker job <id> queued successfully..."
```

**Returns:**

- If `wait=False` (default): a queued summary containing the `job_id`
- If `wait=True`: waits for completion (roundabout) and returns a formatted result

---

### list_workers(limit: int = 20, status: str = None, since_hours: int = None) -> str

Lists recent worker executions with optional filters.

**Parameters:**

- `limit`: Maximum workers to return (default: 20)
- `status`: Filter by "queued", "running", "success", "failed", or None for all
- `since_hours`: Only show workers from last N hours

**Example:**

```python
# List all recent workers
list_workers(limit=10)

# List only failed workers from last 24 hours
list_workers(status="failed", since_hours=24)
```

**Returns:** Formatted list with worker IDs, tasks, status, timestamps

---

### read_worker_result(job_id: str) -> str

Reads the final result from a completed worker.

**Example:**

```python
result = read_worker_result("123")
# Returns the worker's natural language result (includes duration if available)
```

---

### read_worker_file(job_id: str, file_path: str) -> str

Reads a specific file from a worker's artifact directory.

**Common file paths:**

- `result.txt` - Final result
- `metadata.json` - Worker metadata
- `thread.jsonl` - Full conversation history
- `tool_calls/001_ssh_exec.txt` - Individual tool outputs

**Security:** Path traversal is blocked. Only files within the worker directory are accessible.

**Example:**

```python
# Read metadata
metadata = read_worker_file(
    "123",
    "metadata.json"
)

# Read a specific tool output
output = read_worker_file(
    "123",
    "tool_calls/001_ssh_exec.txt"
)
```

---

### grep_workers(pattern: str, since_hours: int = 24) -> str

Searches across worker artifacts for a text pattern.

**Features:**

- Case-insensitive search
- Searches all .txt files in worker directories
- Returns matches with context

**Example:**

```python
# Find all workers that encountered "timeout" errors
matches = grep_workers("timeout", since_hours=48)

# Search for specific output patterns
matches = grep_workers("disk usage", since_hours=24)
```

---

### get_worker_metadata(job_id: str) -> str

Gets detailed metadata about a worker execution.

**Returns:**

- Task description
- Status (success/failed)
- Timestamps (created, started, finished)
- Duration
- Configuration
- Error message (if failed)

**Example:**

```python
metadata = get_worker_metadata("123")
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

`spawn_worker` is internally async. The tool wraps the async call synchronously:

```python
from zerg.utils.async_utils import run_async_safely

result = run_async_safely(spawn_worker_async(...))
```

This is necessary because LangChain tools must be synchronous functions.

### Circular Import Prevention

To avoid circular imports between:

- `supervisor_tools.py` → `WorkerRunner`
- `WorkerRunner` → `AgentRunner`
- `AgentRunner` → `tools.builtin`

We use **lazy imports** - `WorkerRunner` is imported inside the `spawn_worker` function rather than at module level.

## Testing

### Unit Tests

**Location:** `tests/test_supervisor_tools.py`

**Coverage:**

- All 6 tools with success and error cases
- Path traversal security
- Time filters
- Status filters
- Case-insensitive search
- Multiple worker workflows

**Results:** 20/20 tests passing

### Integration Tests

**Location:** `tests/test_supervisor_tools_integration.py`

**Tests:**

- Tool registration in BUILTIN_TOOLS
- End-to-end agent usage (requires tool allowlist configuration)

## Usage Example

```python
from zerg.tools.builtin.supervisor_tools import (
    spawn_worker,
    list_workers,
    read_worker_result,
)

# Spawn a worker
result = spawn_worker(
    task="Analyze the logs from the last deployment",
    model="gpt-4o"
)

# List recent workers
workers = list_workers(limit=5, status="success")

# Read a specific result
worker_result = read_worker_result("2024-12-03T14-32-00_analyze-logs")
```

## Demo

Run the interactive demo:

```bash
cd apps/zerg/backend
uv run python examples/supervisor_tools_demo.py
```

## Files Created/Modified

### Created:

- `zerg/tools/builtin/supervisor_tools.py` - Tool implementations
- `tests/test_supervisor_tools.py` - Unit tests
- `tests/test_supervisor_tools_integration.py` - Integration tests
- `examples/supervisor_tools_demo.py` - Demo script
- `apps/zerg/backend/docs/supervisor_tools.md` - This document

### Modified:

- `zerg/tools/builtin/__init__.py` - Registered supervisor tools

## Next Steps

### Milestone 3: Agent API Integration

To expose supervisor tools to agents via the API:

1. **Update agent configuration** to include supervisor tools in allowlist
2. **Frontend integration** - UI to enable supervisor mode for agents
3. **Tool group creation** - Add "supervisor" tool group to `constants/toolGroups.ts`
4. **Documentation** - User-facing docs on supervisor/worker patterns

### Potential Enhancements

1. **Worker cancellation** - Add `cancel_worker(job_id)` tool
2. **Worker streaming** - Stream worker output in real-time
3. **Worker pools** - Spawn multiple workers in parallel with `spawn_worker_pool()`
4. **Result aggregation** - Tool to aggregate results from multiple workers
5. **Worker retry** - Automatically retry failed workers
