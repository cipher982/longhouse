# Worker Supervision Roundabout

## Implementation Status

| Phase       | Description                  | Status          | Notes                                                     |
| ----------- | ---------------------------- | --------------- | --------------------------------------------------------- |
| **Phase 1** | Tool Activity Events         | ✅ **COMPLETE** | See `docs/completed/worker-tool-events-implementation.md` |
| **Phase 2** | UI Activity Ticker           | ✅ **COMPLETE** | Jarvis shows real-time tool calls per worker              |
| **Phase 3** | Roundabout Monitoring Loop   | ✅ **COMPLETE** | Supervisor waits for worker with 5s polling               |
| **Phase 4** | Supervisor Decision Handling | ✅ **COMPLETE** | Heuristic-based wait/exit/cancel/peek (v1)                |
| **Phase 5** | LLM-Gated Decisions          | ✅ **COMPLETE** | Optional LLM decider with budget/timeout safeguards       |
| **Phase 6** | Graceful Failure Handling    | ⏳ Not started  | Fail-fast tools                                           |

**Next recommended**: Phase 6 (Fail-Fast Tools)

---

## Overview

When a supervisor spawns a worker, it enters a "roundabout" - a temporary monitoring loop that provides real-time visibility into worker execution without polluting the supervisor's long-lived thread context.

The roundabout is like a highway interchange: the main thread (linear conversation) temporarily enters a circular monitoring pattern, checking exits (worker complete, early termination, intervention needed) until the right one appears, then continues on the main path.

## The Problem This Solves

### Without Roundabout (Current State)

```
Supervisor spawns worker
    ↓
[2 minutes of silence]
    ↓
"worker_complete" or "timeout"
```

Problems:

- Supervisor has no visibility into worker progress
- User sees nothing happening
- If worker is stuck, nobody knows until timeout
- No opportunity for early exit or intervention

### With Roundabout

```
Supervisor spawns worker
    ↓
[Enter roundabout]
    ↻ 5s: Check status → continue
    ↻ 10s: Check status → continue
    ↻ 15s: Check status → worker done! → exit
    ↓
[Exit roundabout with result]
```

Benefits:

- Supervisor sees real-time activity
- Can exit early if answer is visible in logs
- Can intervene if worker is stuck/wrong path
- User sees activity ticker in UI

## Two Types of Persistence

### Disk Artifacts (Full Audit Trail)

Everything is logged to disk. Always.

```
/data/workers/{worker_id}/
├── metadata.json           # Status, timestamps, config
├── thread.jsonl            # Full LLM conversation
├── result.txt              # Final result
├── tool_calls/
│   ├── 001_ssh_exec.txt    # Each tool invocation
│   ├── 002_ssh_exec.txt
│   └── ...
└── monitoring/             # NEW: Roundabout check logs
    ├── check_005s.json     # What supervisor saw at 5s
    ├── check_010s.json     # What supervisor saw at 10s
    └── ...
```

This enables:

- `grep_workers` for searching across workers
- Full debugging capability
- Audit trail for compliance
- Post-hoc analysis of what went wrong

### Supervisor LLM Thread (Curated Context)

The conversation history the LLM accumulates. This is the "brain" that persists across sessions.

**What goes in the thread:**

- User messages
- Assistant responses
- Tool calls and their final results
- Worker completion summaries

**What does NOT go in the thread:**

- Roundabout monitoring checks
- "Continue waiting" decisions
- Intermediate activity logs
- Per-tool-call status updates

This keeps the thread clean. After a month of usage, the supervisor's context is meaningful conversation, not thousands of "ssh_exec still running..." messages.

## Roundabout Architecture

### Entry Condition

Roundabout begins when supervisor calls `spawn_worker()`. The tool doesn't immediately return - instead it enters monitoring mode.

```python
# Conceptual flow
def spawn_worker(task: str) -> str:
    job = create_worker_job(task)

    # Enter roundabout
    while True:
        status = check_worker_status(job.id)

        # Present to supervisor (ephemeral, not persisted to thread)
        decision = supervisor_check_in(status)

        if decision == "exit_early":
            return format_early_exit(status)
        if decision == "intervene":
            handle_intervention(job.id)
        if status.complete:
            return format_completion(status)

        sleep(5)  # Check interval
```

### What Supervisor Sees Each Iteration

Every 5 seconds, supervisor receives a status prompt:

```
┌─────────────────────────────────────────────────────────────┐
│ WORKER MONITOR - Job #1 (elapsed: 15s)                      │
├─────────────────────────────────────────────────────────────┤
│ Task: Check disk space on cube                              │
│ Status: running                                             │
│                                                             │
│ Activity Log:                                               │
│   [00:00] Worker started                                    │
│   [00:02] ssh_exec(cube, "df -h") → success (1.8s)         │
│   [00:04] Analyzing output...                               │
│   [00:05] ssh_exec(cube, "du -sh /var/*") → running (10s)  │
│                                                             │
│ Current Operation:                                          │
│   Tool: ssh_exec                                            │
│   Args: {host: "cube", command: "du -sh /var/*"}           │
│   Running for: 10s                                          │
│                                                             │
│ Options:                                                    │
│   [wait] Continue monitoring                                │
│   [exit] Return with current findings                       │
│   [cancel] Abort worker (something's wrong)                 │
│   [peek] Read full worker logs                              │
└─────────────────────────────────────────────────────────────┘
```

### Supervisor Decision Options

**wait** (default): Continue monitoring. Check again in 5s.

**exit**: Supervisor has seen enough. Maybe the answer is already visible in the activity log. Exit roundabout and return current state as result.

**cancel**: Something is wrong (stuck, wrong approach). Cancel the worker and return with explanation of why.

**peek**: Supervisor wants more detail. Read full worker thread.jsonl or specific tool output. Then continue monitoring.

### Exit Conditions

1. **Worker completes** - Natural exit. Return worker result.

2. **Supervisor exits early** - Saw the answer, doesn't need to wait.

3. **Supervisor cancels** - Worker is stuck or wrong. Abort and explain.

4. **Hard timeout** - Safety net. Default 5 minutes, configurable.

5. **Error** - Worker crashes. Return error details.

## What Gets Returned to Thread

When roundabout exits, a single tool response is added to the supervisor thread:

### Success Case

```json
{
  "status": "complete",
  "job_id": 1,
  "worker_id": "2024-12-05T10-30-00_disk-check",
  "duration_seconds": 23,
  "summary": "Checked disk on cube: 78% used, healthy",
  "result": "Full result text from worker...",
  "activity_summary": {
    "tool_calls": 3,
    "tools_used": ["ssh_exec"],
    "hosts_accessed": ["cube"]
  }
}
```

### Early Exit Case

```json
{
  "status": "early_exit",
  "job_id": 1,
  "reason": "Supervisor identified answer in activity log",
  "partial_findings": "df -h showed 78% disk usage",
  "activity_at_exit": {
    "elapsed_seconds": 8,
    "completed_operations": 1,
    "pending_operations": 1
  }
}
```

### Failure Case

```json
{
  "status": "failed",
  "job_id": 1,
  "error": "SSH connection failed - no credentials configured",
  "activity_at_failure": {
    "elapsed_seconds": 5,
    "last_operation": "ssh_exec(cube, 'df -h')",
    "failure_details": "No SSH key found at ~/.ssh/id_ed25519"
  },
  "suggestion": "Infrastructure access not configured for this environment"
}
```

## UI Integration

While the roundabout executes, the Jarvis UI shows an activity ticker:

```
┌─────────────────────────────────────────┐
│ 🔍 Investigating...                     │
│                                         │
│ Worker: Check disk on cube              │
│ ├─ ssh_exec "df -h" ✓ (1.8s)           │
│ └─ ssh_exec "du -sh /var/*" ⏳ 10s...   │
│                                         │
│ [Details] [Cancel]                      │
└─────────────────────────────────────────┘
```

This streams via SSE events:

- `worker_tool_started` - tool call begins
- `worker_tool_completed` - tool call finished
- `worker_status_update` - periodic status (every 5s)

UI receives events, displays ticker, clears when roundabout exits.

## Event Flow

```
                    ┌─────────────────────┐
                    │   Jarvis Frontend   │
                    │   (Activity Ticker) │
                    └──────────▲──────────┘
                               │ SSE events
                               │
┌──────────────┐    ┌──────────┴──────────┐    ┌─────────────────┐
│  Supervisor  │◄───│    Event Stream     │◄───│     Worker      │
│   (LLM)      │    │                     │    │   (Execution)   │
└──────┬───────┘    └─────────────────────┘    └────────┬────────┘
       │                                                │
       │ Roundabout                                     │
       │ check-ins                                      │
       │ (ephemeral)                                    │
       │                                                │
       ▼                                                ▼
┌──────────────┐                              ┌─────────────────┐
│  Supervisor  │                              │  Disk Artifacts │
│   Thread     │                              │  (Full Logs)    │
│ (Persisted)  │                              │                 │
└──────────────┘                              └─────────────────┘
```

## Implementation Phases

### Phase 1: Tool Activity Events ✅ COMPLETE

**Implementation details**: See `docs/completed/worker-tool-events-implementation.md`

Events implemented:

- `WORKER_TOOL_STARTED` - emitted when tool begins
- `WORKER_TOOL_COMPLETED` - emitted when tool succeeds
- `WORKER_TOOL_FAILED` - emitted when tool fails (via error_envelope detection)

Key components:

- `zerg/context.py` - WorkerContext via ContextVars
- `zerg/tools/result_utils.py` - Error detection + secret redaction
- Modified `zerg_react_agent._call_tool_async` for event emission
- Modified `WorkerRunner.run_worker` for context setup/teardown

### Phase 2: UI Activity Ticker ✅ COMPLETE

**Implementation details:**

Backend:

- Added tool event subscriptions to SSE endpoint (`jarvis.py`)
- Events: `WORKER_TOOL_STARTED`, `WORKER_TOOL_COMPLETED`, `WORKER_TOOL_FAILED`

Frontend (`apps/jarvis/apps/web/`):

- Added tool event types to `lib/event-bus.ts`
- Added event forwarding in `lib/tool-factory.ts`
- Enhanced `lib/supervisor-progress.ts` to track/display tool calls per worker
- Added CSS styles in `styles/supervisor-progress.css`

UI displays:

- Tool name, status icon (⏳ running, ✓ completed, ✗ failed)
- Duration (elapsed while running, final ms when done)
- Args preview (while running) or error (if failed)
- Tool calls nested under each worker with visual hierarchy

### Phase 3: Roundabout Loop ✅ COMPLETE

**Implementation details:**

Files created/modified:

- `zerg/services/roundabout_monitor.py` - New monitoring service
- `zerg/tools/builtin/supervisor_tools.py` - Modified spawn_worker

Key components:

- `RoundaboutMonitor` class with `wait_for_completion()` method
- Polling interval: 5 seconds (configurable via `ROUNDABOUT_CHECK_INTERVAL`)
- Hard timeout: 300 seconds / 5 minutes (configurable)
- Status tracking via `RoundaboutStatus` dataclass
- Result formatting via `format_roundabout_result()`

Behavior:

- `spawn_worker(task)` returns immediately (fire-and-forget, backward compatible)
- `spawn_worker(task, wait=True)` enters roundabout monitoring loop
- Subscribes to tool events via event bus for activity tracking
- Polling refreshes database session to see worker status changes
- Monitoring checks logged to `/data/workers/{worker_id}/monitoring/`
- Returns structured result with duration, summary, activity stats

Timeout semantics:

- `monitor_timeout` status distinct from job failure
- `worker_still_running` flag indicates if worker continues after monitor exits
- Clear messaging guides supervisor on next steps

Configuration constants in `roundabout_monitor.py`:

```python
ROUNDABOUT_CHECK_INTERVAL = 5  # seconds
ROUNDABOUT_HARD_TIMEOUT = 300  # 5 minutes
ROUNDABOUT_STUCK_THRESHOLD = 30  # flag as slow
ROUNDABOUT_ACTIVITY_LOG_MAX = 20  # entries to track
```

### Phase 4: Supervisor Decision Handling ✅ COMPLETE

**Implementation details:**

Files modified:

- `zerg/services/roundabout_monitor.py` - Added decision types and heuristic function

Key components:

- `RoundaboutDecision` enum: WAIT, EXIT, CANCEL, PEEK
- `DecisionContext` dataclass: Current state for decision making
- `make_heuristic_decision()` function: Rules-based decision logic

Heuristic rules (v1 - rules-based, future v2 could add LLM):

- **EXIT**: Worker status changed to success/failed, OR final answer pattern detected in tool output
- **CANCEL**: Stuck > 60s without completing operation, OR no progress for 6+ consecutive polls
- **WAIT**: Default when none of the above conditions apply
- **PEEK**: Reserved for future LLM-based decisions

Configuration constants:

```python
ROUNDABOUT_CANCEL_STUCK_THRESHOLD = 60  # seconds
ROUNDABOUT_NO_PROGRESS_POLLS = 6  # consecutive polls
FINAL_ANSWER_PATTERNS = ["Result:", "Summary:", "Completed successfully", ...]
```

Result types added:

- `early_exit`: Exited before worker completion (answer detected)
- `cancelled`: Worker cancelled due to stuck/no progress
- `peek`: Drill-down requested (returns hint for supervisor to call read_worker_file)

Tests added in `tests/test_roundabout_monitor.py`:

- Unit tests for `make_heuristic_decision()` covering all decision paths
- Integration test for cancel-on-no-progress behavior

### Phase 5: LLM-Gated Decisions ✅ COMPLETE

**Implementation details:**

Files created/modified:

- `zerg/services/llm_decider.py` - New module for LLM-based decision making
- `zerg/services/roundabout_monitor.py` - Added decision mode configuration and integration
- `zerg/tools/builtin/supervisor_tools.py` - Added `decision_mode` parameter to `spawn_worker`

Key components:

- `DecisionMode` enum: HEURISTIC (default), LLM, HYBRID
- `LLMDeciderStats` dataclass: Tracks calls, timeouts, errors, skipped calls
- `LLMDecisionPayload` dataclass: Compact payload for LLM (~1-2KB)
- `decide_roundabout_action()` function: Makes LLM decision with timeout

**Decision Modes:**

- **heuristic** (default): Rules-based decisions only. Fast, no cost, safe.
- **llm**: LLM-based decisions only. Smarter but adds latency (~500-1500ms) and cost.
- **hybrid**: Heuristic first, LLM for ambiguous cases. Best of both worlds.

**Safeguards:**

All safeguards ensure safe fallback to "wait" on any failure:

| Safeguard             | Default     | Description                                |
| --------------------- | ----------- | ------------------------------------------ |
| `llm_poll_interval`   | 2           | Only call LLM every N polls (reduces cost) |
| `llm_max_calls`       | 3           | Maximum LLM calls per job (budget limit)   |
| `llm_timeout_seconds` | 1.5s        | Max time to wait for LLM response          |
| `llm_model`           | gpt-4o-mini | Fast, cheap model for decisions            |

**Telemetry:**

The `activity_summary` in `RoundaboutResult` now includes:

```python
{
    "llm_calls": 3,              # Total LLM calls made
    "llm_calls_succeeded": 2,    # Successful calls
    "llm_timeouts": 1,           # Calls that timed out
    "llm_errors": 0,             # Calls that errored
    "llm_skipped_budget": 2,     # Skipped due to budget exhaustion
    "llm_skipped_interval": 5,   # Skipped due to poll interval
    "llm_avg_response_ms": 850,  # Average response time
    "decision_mode": "hybrid",   # Mode used for this job
}
```

**Usage:**

```python
# Default: heuristic only (fast, safe)
spawn_worker("Check disk on cube", wait=True)

# Hybrid: heuristic + LLM for ambiguous cases
spawn_worker("Complex research task", wait=True, decision_mode="hybrid")

# LLM only: full LLM control (use sparingly)
spawn_worker("Task needing smart decisions", wait=True, decision_mode="llm")
```

**Cost/Latency Expectations:**

- Model: `gpt-4o-mini` (~$0.15/1M input, ~$0.60/1M output)
- Average response time: 500-1500ms
- Cost per job (hybrid, 3 calls): ~$0.001-0.003
- Max latency impact: 3 calls × 1.5s timeout = 4.5s worst case

Tests added in `tests/test_llm_decider.py` and `tests/test_roundabout_monitor.py`:

- Unit tests for `LLMDeciderStats`, payload building, prompt generation
- Unit tests for LLM call handling (success, timeout, error fallback)
- Integration tests for all decision modes
- Integration tests for budget and interval enforcement

### Phase 6: Graceful Failure Handling

Workers fail fast and report clearly:

- Tool errors return immediately (no hanging)
- Clear error messages in worker result
- Supervisor can reason about failure

## Configuration

```python
ROUNDABOUT_CONFIG = {
    "check_interval_seconds": 5,      # How often to check
    "hard_timeout_seconds": 300,      # Max time in roundabout
    "stuck_threshold_seconds": 30,    # When to flag operation as slow
    "activity_log_max_entries": 20,   # How many recent entries to show
}
```

## Example Scenarios

### Scenario 1: Normal Completion

```
User: "Check disk on cube"
Supervisor: spawn_worker("Check disk on cube")

[Roundabout enters]
  5s: Worker running, ssh_exec complete, analyzing → wait
  10s: Worker running, generating result → wait
  15s: Worker complete!
[Roundabout exits]

Thread receives: "Worker complete: 78% disk used on cube"
Supervisor: "Your cube server is at 78% disk usage, which is healthy."
```

### Scenario 2: Early Exit (Answer Visible)

```
User: "Is cube online?"
Supervisor: spawn_worker("Check if cube is reachable")

[Roundabout enters]
  5s: ssh_exec(cube, "echo ping") → success (0.5s)

  Supervisor sees: "ssh_exec succeeded, cube is reachable"
  Supervisor decides: EXIT (answer is clear, don't need full analysis)
[Roundabout exits early]

Thread receives: "Early exit: cube is reachable (ssh_exec succeeded)"
Supervisor: "Yes, cube is online and responding."
```

### Scenario 3: Stuck Operation

```
User: "Check disk on cube"
Supervisor: spawn_worker("Check disk on cube")

[Roundabout enters]
  5s: ssh_exec running for 5s → wait
  10s: ssh_exec running for 10s → wait
  15s: ssh_exec running for 15s → wait
  20s: ssh_exec running for 20s → wait
  25s: ssh_exec running for 25s → wait
  30s: ssh_exec running for 30s ⚠️ SLOW

  Supervisor sees: "ssh_exec has been running for 30s, this is unusual"
  Supervisor decides: CANCEL (something is wrong)
[Roundabout exits with cancellation]

Thread receives: "Cancelled: ssh_exec stuck for 30s, possible connection issue"
Supervisor: "I wasn't able to reach cube - the SSH connection appears stuck.
             This might indicate a network issue or the server being unresponsive."
```

### Scenario 4: Configuration Error

```
User: "Check disk on cube"
Supervisor: spawn_worker("Check disk on cube")

[Roundabout enters]
  2s: ssh_exec failed immediately: "No SSH key found"

  Worker returns error, roundabout exits
[Roundabout exits with error]

Thread receives: "Failed: SSH not configured - no key at ~/.ssh/id_ed25519"
Supervisor: "I can't check cube right now - SSH access isn't configured
             in this environment. Please ensure SSH keys are available."
```

## Open Questions

1. **Concurrent workers**: If supervisor spawns multiple workers, do we have nested roundabouts? Or one combined monitoring view?

2. **Worker-to-supervisor communication**: Can a worker explicitly signal "I'm stuck, need help"? Or is detection purely observation-based?

3. **Intervention depth**: Beyond cancel, can supervisor send instructions to a running worker? "Try a different approach"?

4. **Roundabout nesting**: If supervisor calls peek and reads worker logs, that's a sub-operation. How deep can this go?

## Related Documents

- [Super Siri Architecture](./super-siri-architecture.md) - Overall system design
- [Worker Artifact Store](../backend/zerg/services/worker_artifact_store.py) - Disk persistence
- [Supervisor Tools](../backend/zerg/tools/builtin/supervisor_tools.py) - Current supervisor capabilities
