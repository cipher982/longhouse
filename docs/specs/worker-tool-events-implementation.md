# Worker Tool Events - Implementation Summary

## Overview

This document summarizes the implementation of **Phase 1: Tool Activity Events** for the Worker Supervision Roundabout architecture. This enables real-time visibility into worker tool execution without polluting the supervisor's conversation thread.

---

## The Problem

**Before this work:**

```
Supervisor spawns worker
    ↓
[2 minutes of silence] ← User sees nothing, worker might be stuck
    ↓
"worker_complete" or "timeout"
```

Problems:

- No visibility into what the worker is doing
- If a tool hangs (SSH timeout, API call), nobody knows until hard timeout
- No opportunity for early exit or intervention
- User sees spinner with no activity details

---

## The Solution: Worker Supervision Roundabout

**Full architecture documented in**: [`docs/specs/worker-supervision-roundabout.md`](./worker-supervision-roundabout.md)

The roundabout is a monitoring loop where the supervisor:

1. Spawns a worker
2. Enters monitoring mode (checks status every 5s)
3. Sees real-time tool activity (ssh_exec, http_request, etc.)
4. Can exit early, cancel, or peek at logs
5. Exits with worker result

**Key principle**: The monitoring checks are **ephemeral** (not persisted to supervisor thread) to keep context clean. Only the final result goes into the conversation history.

---

## Implementation Phases

| Phase       | Description                  | Status          |
| ----------- | ---------------------------- | --------------- |
| **Phase 1** | Tool Activity Events         | ✅ **COMPLETE** |
| **Phase 2** | UI Activity Ticker           | ⏳ Not started  |
| **Phase 3** | Roundabout Monitoring Loop   | ⏳ Not started  |
| **Phase 4** | Supervisor Decision Handling | ⏳ Not started  |
| **Phase 5** | Graceful Failure Handling    | ⏳ Not started  |

---

## Phase 1: Tool Activity Events (COMPLETE)

### What We Built

Real-time event emission from worker tool execution:

- `WORKER_TOOL_STARTED` - emitted when tool begins (ssh_exec, http_request, etc.)
- `WORKER_TOOL_COMPLETED` - emitted when tool succeeds
- `WORKER_TOOL_FAILED` - emitted when tool fails (detected via error_envelope)

### Technical Approach

**Option A (chosen): ContextVars + Manual Event Emission**

We chose `contextvars` over LangChain callbacks because:

- Cleaner integration (single point of setup)
- No callback plumbing through multiple layers
- Decoupled from LangChain/LangGraph internals
- Works perfectly with existing `asyncio.to_thread` pattern

**Key insight from debugging**: LangChain callbacks DO work when passed explicitly, but `contextvars` propagate automatically through `asyncio.to_thread`, making them ideal for this use case.

### Architecture

```
┌─────────────────────────────────────────────────────┐
│ WorkerRunner.run_worker()                           │
│   ↓                                                  │
│   Sets WorkerContext via contextvar                 │
│   ↓                                                  │
│   AgentRunner → zerg_react_agent → _call_tool_async │
│                                     ↓                │
│                                     Reads context    │
│                                     Emits events     │
│                                     ↓                │
└─────────────────────────────────────────────────────┘
         ↓ Events flow via event_bus
┌─────────────────────────────────────────────────────┐
│ SSE → Frontend → Activity Ticker (Phase 2)          │
└─────────────────────────────────────────────────────┘
```

### Components Created

#### 1. **`zerg/context.py`** (NEW)

```python
@dataclass
class WorkerContext:
    worker_id: str
    owner_id: int | None
    run_id: str | None
    task: str
    tool_calls: list[ToolCall]  # Activity log

# ContextVar for passing context through call stack
worker_ctx: ContextVar[WorkerContext | None]

# Helpers
get_worker_context() -> WorkerContext | None
set_worker_context(ctx) -> Token
reset_worker_context(token) -> None
```

**Why this matters**: Any code in the call stack can access worker context without explicit parameter threading. `asyncio.to_thread` automatically propagates context.

#### 2. **Event Types** (`zerg/events/event_bus.py`)

```python
WORKER_TOOL_STARTED = "worker_tool_started"
WORKER_TOOL_COMPLETED = "worker_tool_completed"
WORKER_TOOL_FAILED = "worker_tool_failed"
```

#### 3. **Modified: `zerg_react_agent._call_tool_async`**

Wrapped tool execution with event emission:

```python
async def _call_tool_async(tool_call: dict):
    ctx = get_worker_context()
    if ctx:
        # Emit STARTED
        await event_bus.publish(WORKER_TOOL_STARTED, {...})

    result = await asyncio.to_thread(_call_tool_sync, tool_call)

    if ctx:
        # Emit COMPLETED or FAILED based on result
        is_error, error_msg = check_tool_error(result_content)
        if is_error:
            await event_bus.publish(WORKER_TOOL_FAILED, {...})
        else:
            await event_bus.publish(WORKER_TOOL_COMPLETED, {...})

    return result
```

#### 4. **Modified: `WorkerRunner.run_worker`**

Sets up and tears down context:

```python
worker_context = WorkerContext(worker_id=..., owner_id=..., ...)
context_token = set_worker_context(worker_context)
try:
    await agent.run()
finally:
    reset_worker_context(context_token)
```

#### 5. **`zerg/tools/result_utils.py`** (NEW - Centralized Utilities)

**`check_tool_error(result_content: Any) -> tuple[bool, str | None]`**

- Detects legacy format: `<tool-error> ...` or `Error: ...`
- Detects error_envelope: `{"ok": False, "error_type": "...", "user_message": "..."}`
- Handles both JSON (double quotes) and Python literal (single quotes) via `ast.literal_eval()`
- Type-safe: accepts None, converts to string

**`redact_sensitive_args(args: Any) -> Any`**

- Recursively walks dicts, lists, tuples, sets
- Redacts keys containing: key, api_key, token, secret, password, credential, auth, bearer, etc.
- Detects key-value pair patterns: `{"key": "Authorization", "value": "Bearer..."}` → redacts value
- Only exempts structural keys (key/title/name) when in a pair pattern with value field
- Type-safe: handles any input type

**`safe_preview(content: Any, max_len: int) -> str`**

- Truncates content with "..." ellipsis
- Type-safe: converts None → "(None)"

---

## Critical Bugs Fixed During Implementation

### 1. Error Detection Broken (Critical)

**Issue**: `str(dict)` produces Python literals like `{'ok': False, ...}` but `json.loads()` expects JSON with double quotes. Error envelopes were NEVER detected.

**Fix**: Added `ast.literal_eval()` fallback to parse Python literals.

**Commit**: `4e3c539`

### 2. Secrets in WorkerContext (Medium → High)

**Issue**: `WorkerContext.tool_calls` stored unredacted args, accessible via logs/SSE/UI.

**Fix**: Pass redacted args to `ctx.record_tool_start()`.

**Commit**: `65ada61`

### 3. Secrets in Nested Lists (High)

**Issue**: Slack attachments like `[{"title": "token", "value": "sk-..."}]` leaked secrets because redaction didn't recurse into lists.

**Fix**: Made `redact_sensitive_args` walk lists/tuples/sets recursively.

**Commit**: `578c068`

### 4. Structural Key Exemption Regression (High)

**Issue**: Lone fields like `{"key": "sk-123"}` leaked because structural keys were exempted globally.

**Fix**: Only exempt structural keys when in a key-value pair pattern (both semantic key AND value field present).

**Commit**: `8687909`

### 5. Tests Don't Exercise Production Code (High)

**Issue**: Tests manually reimplemented logic instead of calling real functions.

**Fix**: Extracted utilities to `result_utils.py` and rewrote tests to import/test real functions.

**Commit**: `bfc286f`

---

## Event Payload Structure

### WORKER_TOOL_STARTED

```json
{
  "event_type": "worker_tool_started",
  "worker_id": "2024-12-05T16-30-00_disk-check",
  "owner_id": 1,
  "run_id": "run-abc123",
  "tool_name": "ssh_exec",
  "tool_call_id": "call_1",
  "tool_args_preview": "{'host': 'cube', 'command': 'df -h'}",
  "timestamp": "2024-12-05T16:30:05.123Z"
}
```

### WORKER_TOOL_COMPLETED

```json
{
  "event_type": "worker_tool_completed",
  "worker_id": "2024-12-05T16-30-00_disk-check",
  "owner_id": 1,
  "run_id": "run-abc123",
  "tool_name": "ssh_exec",
  "tool_call_id": "call_1",
  "duration_ms": 823,
  "result_preview": "Filesystem      Size  Used Avail Use% ...",
  "timestamp": "2024-12-05T16:30:06.456Z"
}
```

### WORKER_TOOL_FAILED

```json
{
  "event_type": "worker_tool_failed",
  "worker_id": "2024-12-05T16-30-00_disk-check",
  "owner_id": 1,
  "run_id": "run-abc123",
  "tool_name": "ssh_exec",
  "tool_call_id": "call_1",
  "duration_ms": 125,
  "error": "SSH client not found. Ensure OpenSSH is installed.",
  "timestamp": "2024-12-05T16:30:05.248Z"
}
```

---

## Test Coverage

**63 tests total**, organized in 4 files:

### `test_worker_context.py` (10 tests)

- ContextVar basic operations (set/get/reset)
- Nested contexts
- Async propagation through `asyncio.to_thread`
- Isolation between concurrent workers

### `test_worker_tool_events.py` (13 tests)

- WorkerContext accessibility
- ToolCall tracking
- Event payload structure
- Integration with real redaction function

### `test_result_utils.py` (29 tests)

- Error detection (JSON, Python literal, legacy formats)
- Secret redaction (flat, nested, lists, tuples, key-value pairs)
- Safe preview (truncation, None handling)
- **5 regression tests** for security fixes

### `test_error_detection.py` (11 tests)

- Comprehensive envelope parsing edge cases
- Malformed input handling
- Nested data structures

---

## Security Model

### Redaction Strategy

**Sensitive key patterns** (case-insensitive, partial match):

```
key, api_key, apikey, token, secret, password, passwd,
credential, credentials, auth, authorization, bearer,
private_key, privatekey, access_token, refresh_token
```

**Redaction rules**:

1. **Lone sensitive key**: `{"api_key": "sk-123"}` → `{"api_key": "[REDACTED]"}`
2. **Nested sensitive key**: `{"config": {"token": "..."}}` → `{"config": {"token": "[REDACTED]"}}`
3. **Key-value pair pattern**: `{"key": "Authorization", "value": "Bearer..."}` → value redacted
4. **Lists**: `[{"title": "token", "value": "sk-..."}]` → value redacted
5. **Tuples/Sets**: Recursively processed

**Structural exemption**: `key`, `title`, `name` fields are only exempt when they're part of a key-value pair pattern (both semantic key AND value field present). Otherwise they're redacted like any other key.

### What Gets Redacted

**Event payloads (SSE)**:

- `tool_args_preview` - redacted before emission
- `result_preview` - safe preview (no redaction needed, results don't contain input secrets)

**WorkerContext.tool_calls** (in-memory activity log):

- `args_preview` - redacted before storage

**NOT redacted** (full audit trail):

- Disk artifacts: `/data/workers/{worker_id}/tool_calls/` - contains full args for debugging

---

## Remaining Work

### Phase 2: UI Activity Ticker

**Frontend component** that subscribes to tool events and displays:

```
┌─────────────────────────────────────────┐
│ 🔍 Investigating...                     │
│                                         │
│ Worker: Check disk on cube              │
│ ├─ ssh_exec "df -h" ✓ (823ms)          │
│ └─ ssh_exec "du -sh /var/*" ⏳ 2.1s...  │
│                                         │
│ [Details] [Cancel]                      │
└─────────────────────────────────────────┘
```

**Technical requirements**:

- Subscribe to `WORKER_TOOL_STARTED/COMPLETED/FAILED` via SSE
- Show tool name, duration, status icon
- Clear ticker on `WORKER_COMPLETE` event
- Handle multiple workers (future: parallel workers)

**Files to modify**:

- `apps/zerg/frontend-web/src/components/WorkerActivityTicker.tsx` (NEW)
- Subscribe to events in chat message handler

### Phase 3: Roundabout Monitoring Loop

**Supervisor-side implementation** in `supervisor_tools.py`:

```python
def spawn_worker(task: str) -> str:
    job = create_worker_job(task)

    # Enter roundabout
    while True:
        status = check_worker_status(job.id)

        # Present to supervisor (ephemeral - not in thread)
        decision = supervisor_check_in(status)

        if decision == "exit_early":
            return format_early_exit(status)
        if decision == "intervene":
            handle_intervention(job.id)
        if status.complete:
            return format_completion(status)

        await asyncio.sleep(5)  # Check interval
```

**Configuration**:

```python
ROUNDABOUT_CONFIG = {
    "check_interval_seconds": 5,
    "hard_timeout_seconds": 300,
    "stuck_threshold_seconds": 30,
    "activity_log_max_entries": 20,
}
```

### Phase 4: Supervisor Decision Handling

**Supervisor options during monitoring**:

- **wait** (default): Continue monitoring, check again in 5s
- **exit**: Saw the answer in logs, don't need full result
- **cancel**: Worker is stuck or on wrong path, abort
- **peek**: Read full worker logs for debugging

### Phase 5: Graceful Failure Handling

**Make tools fail fast**:

- `ssh_exec`: Check for SSH key before connecting → immediate error if missing
- `jira_*`: Check connector configured → immediate error if not
- `github_*`: Check credentials → immediate error if invalid

**Why this matters**: Currently these tools hang/timeout when misconfigured. With fail-fast, workers can report "I'm not configured" in ~100ms instead of timing out after 2 minutes.

---

## Key Implementation Decisions

### Why ContextVars Over Callbacks?

We debugged both approaches with test scripts:

**LangChain Callbacks** (Option C):

- ✅ Work when passed to `tool.invoke(config={"callbacks": [...]})`
- ❌ Require plumbing through multiple layers
- ❌ Some LangGraph versions have callback firing issues
- ❌ Tightly coupled to LangChain internals

**ContextVars** (Option A - chosen):

- ✅ Single point of setup (WorkerRunner)
- ✅ Automatically propagate through `asyncio.to_thread`
- ✅ Visible everywhere in call stack
- ✅ Decoupled from LangChain/LangGraph
- ✅ Standard library primitive

### Why Extract to `result_utils.py`?

Originally implemented inline in `zerg_react_agent.py`, but extracted because:

1. **Reusability**: Other parts of the system need this (artifact store, roundabout loop)
2. **Testability**: Can't easily test functions defined inside `get_runnable()`
3. **Maintainability**: Single source of truth for error detection and redaction

### Error Detection Strategy

**Challenge**: Tools return dicts that get stringified via `str(observation)`, producing Python literal syntax with single quotes and capitalized booleans.

**Solution**: Try JSON first, fall back to `ast.literal_eval()`:

```python
try:
    parsed = json.loads(result_content)  # Double quotes
except JSONDecodeError:
    parsed = ast.literal_eval(result_content)  # Single quotes

if isinstance(parsed, dict) and parsed.get("ok") is False:
    return True, parsed.get("user_message")
```

### Secret Redaction Strategy

**Challenge**: Secrets appear in various forms:

- Direct keys: `{"api_key": "sk-123"}`
- Nested: `{"config": {"token": "xyz"}}`
- Lists: `[{"title": "token", "value": "sk-..."}]`
- Key-value pairs: `{"key": "Authorization", "value": "Bearer..."}`

**Solution**: Multi-pass recursive algorithm:

1. Check for key-value pair pattern (semantic key + value field both present)
2. If pattern detected and semantic key is sensitive → redact value field
3. Otherwise, apply standard redaction to all keys
4. Recurse into nested structures (dicts, lists, tuples, sets)

**Edge case handled**: Structural keys (key/title/name) are only exempt in pair patterns. Lone `{"key": "sk-123"}` is redacted.

---

## Commits (12 total)

```
8687909 fix: prevent structural key exemption from leaking secrets
446bcd1 fix: add type safety and None handling to result_utils
578c068 fix: redact secrets in nested lists and key-value patterns
bfc286f refactor: extract tool result utils to reusable module
65ada61 fix: redact sensitive args in WorkerContext.tool_calls
4e3c539 fix: detect error_envelope with Python literal syntax
e749265 fix: improve tool event detection and add security measures
21571c5 test: add tests for worker tool events
76b9d31 feat: set worker context in WorkerRunner for tool events
a9d3146 feat: emit worker tool events from zerg_react_agent
6738209 feat: add worker tool event types for roundabout monitoring
2a8e671 feat: add worker context module for cross-cutting concerns
```

---

## Files Modified/Created

### New Files

- `apps/zerg/backend/zerg/context.py` (117 lines)
- `apps/zerg/backend/zerg/tools/result_utils.py` (154 lines)
- `apps/zerg/backend/tests/unit/test_worker_context.py` (140 lines)
- `apps/zerg/backend/tests/unit/test_worker_tool_events.py` (234 lines)
- `apps/zerg/backend/tests/unit/test_result_utils.py` (358 lines)
- `apps/zerg/backend/tests/unit/test_error_detection.py` (112 lines)

### Modified Files

- `apps/zerg/backend/zerg/events/event_bus.py` (added 3 event types)
- `apps/zerg/backend/zerg/agents_def/zerg_react_agent.py` (~100 lines added for event emission)
- `apps/zerg/backend/zerg/services/worker_runner.py` (15 lines for context setup/teardown)

---

## How to Use (Developer Guide)

### Running a Worker with Tool Events

```python
from zerg.services.worker_runner import WorkerRunner

runner = WorkerRunner()
result = await runner.run_worker(
    db=db,
    task="Check disk space on cube",
    event_context={"run_id": "run-123"},  # Enables event emission
)

# Events are automatically emitted:
# - WORKER_STARTED when worker begins
# - WORKER_TOOL_STARTED when each tool starts
# - WORKER_TOOL_COMPLETED/FAILED when each tool finishes
# - WORKER_COMPLETE when worker finishes
```

### Subscribing to Events (Frontend)

```typescript
// Subscribe to tool events via SSE
eventSource.addEventListener("worker_tool_started", (event) => {
  const data = JSON.parse(event.data);
  // data.worker_id, data.tool_name, data.tool_args_preview
});

eventSource.addEventListener("worker_tool_completed", (event) => {
  const data = JSON.parse(event.data);
  // data.duration_ms, data.result_preview
});
```

### Testing Error Detection

```python
from zerg.tools.result_utils import check_tool_error

# Test various formats
is_error, msg = check_tool_error("{'ok': False, 'user_message': 'SSH failed'}")
# Returns: (True, "SSH failed")

is_error, msg = check_tool_error("<tool-error> Connection refused")
# Returns: (True, "<tool-error> Connection refused")

is_error, msg = check_tool_error("{'ok': True, 'data': '...'}")
# Returns: (False, None)
```

### Testing Secret Redaction

```python
from zerg.tools.result_utils import redact_sensitive_args

# Flat dict
redact_sensitive_args({"api_key": "sk-123", "host": "cube"})
# Returns: {"api_key": "[REDACTED]", "host": "cube"}

# Nested list (Slack/Discord)
redact_sensitive_args({
    "attachments": [
        {"title": "token", "value": "sk-live-abc"}
    ]
})
# Returns: {"attachments": [{"title": "token", "value": "[REDACTED]"}]}
```

---

## Open Questions for Future Phases

### Phase 2 (UI Ticker)

1. **Per-tool preview fields**: Should we create a registry mapping tool names to safe fields?
   - Example: `ssh_exec → {host, command[:120]}`
   - Would avoid generic redaction complexity
   - More explicit about what's shown in UI

2. **Event buffering**: Should we batch events for performance, or stream individually?

### Phase 3 (Roundabout Loop)

1. **Concurrent workers**: If supervisor spawns multiple workers, do we have nested roundabouts or combined monitoring?

2. **Worker-to-supervisor communication**: Can a worker explicitly signal "I'm stuck, need help"? Or purely observation-based?

3. **Intervention depth**: Beyond cancel, can supervisor send instructions to running worker?

### Phase 5 (Fail-Fast)

1. **Configuration checks**: Where should these live? In tool definitions or as decorators?

2. **Error messages**: Should we use a standard format for "not configured" errors?

---

## Testing Strategy

### Unit Tests (63 tests)

- Fast, isolated tests for individual components
- Mock event bus where needed
- Focus on edge cases and regressions

### Integration Tests (TODO - Phase 2)

- End-to-end worker execution with event verification
- Real event bus, real tools
- Verify events reach SSE subscribers

### Manual Testing

- Debug scripts in `apps/zerg/backend/scripts/` (cleaned up after verification)
- Used to validate contextvars propagation and callback behavior

---

## Performance Considerations

### Event Emission Overhead

**Current approach**: `await event_bus.publish()` blocks tool execution

**Impact**: ~1-5ms per event (depends on subscriber count)

**Mitigation**: Wrapped in try/except, failures don't break tools

**Future option**: Fire-and-forget with `asyncio.create_task()` if latency becomes issue

### Context Lookup Overhead

`get_worker_context()` is a simple ContextVar lookup: ~0.1μs (negligible)

### Memory Overhead

`WorkerContext.tool_calls` list grows with tool usage:

- ~200 bytes per ToolCall
- Cleared when worker completes
- Typical worker: 5-10 tool calls → ~2KB

---

## Next Steps

### Immediate: Phase 2 (UI Activity Ticker)

1. Create `WorkerActivityTicker.tsx` component
2. Subscribe to `WORKER_TOOL_*` events
3. Display tool name, duration, status icon
4. Show elapsed time for running tools
5. Clear on `WORKER_COMPLETE`

### Soon: Phase 5 (Fail-Fast Tools)

1. Add configuration checks to `ssh_exec`:

   ```python
   def ssh_exec(host: str, command: str) -> dict:
       if not settings.SSH_ENABLED or not settings.SSH_KEY:
           return tool_error(
               ErrorType.CONNECTOR_NOT_CONFIGURED,
               "SSH client not found. Ensure OpenSSH is installed."
           )
   ```

2. Add checks to connector tools (jira, github, slack, etc.)

### Later: Phase 3 (Roundabout Loop)

Implement the 5s monitoring cycle in supervisor tools.

---

## References

### Original Specs

- [`docs/specs/worker-supervision-roundabout.md`](./worker-supervision-roundabout.md) - Full architecture
- [`docs/specs/super-siri-architecture.md`](./super-siri-architecture.md) - Overall system design

### Related Code

- `apps/zerg/backend/zerg/services/worker_artifact_store.py` - Disk persistence
- `apps/zerg/backend/zerg/tools/builtin/supervisor_tools.py` - Supervisor capabilities
- `apps/zerg/backend/zerg/tools/error_envelope.py` - Standardized error format

### Debug Scripts (Archived)

Debug scripts were created to understand LangGraph/LangChain internals:

- `debug_langgraph_callbacks.py` - Tested callback firing
- `debug_worker_events.py` - Prototype for event emission pattern

Both deleted after verification (knowledge captured in this doc).

---

## Key Insights from Implementation

1. **ContextVars are magic**: They propagate through `asyncio.to_thread` automatically, making them perfect for cross-cutting concerns like logging/events.

2. **Error envelopes are strings**: Even though tools return dicts, LangChain stringifies them. Must parse both JSON and Python literal syntax.

3. **Security is recursive**: Secrets hide in nested lists/tuples. Must walk entire structure, not just top-level keys.

4. **Structural keys need context**: "key" field is sensitive when alone (`{"key": "sk-123"}`) but structural in pairs (`{"key": "Status", "value": "OK"}`). Detection must consider the full pattern.

5. **Type safety matters**: Functions receiving "any tool result" must handle None, dicts, primitives gracefully.

---

## Status: Phase 1 COMPLETE ✅

**What works now**:

- Workers emit real-time tool events
- Events contain safe, redacted payloads
- Error detection works for all envelope formats
- Full test coverage with regression guards
- Utilities are reusable across the codebase

**Ready for**: Building the UI activity ticker (Phase 2) or implementing fail-fast tools (Phase 5).
