# Worker Supervision Roundabout

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

### Phase 1: Tool Activity Events

Add events for tool execution:

- `worker_tool_started` - emitted before tool call
- `worker_tool_completed` - emitted after tool call

Modify `WorkerRunner` to emit these events.

### Phase 2: UI Activity Ticker

Frontend component that:

- Subscribes to tool events
- Displays scrolling activity log
- Shows elapsed time per operation
- Clears on worker complete

### Phase 3: Roundabout Loop

Implement the supervision loop:

- Polling interval (default 5s)
- Status aggregation
- Decision prompt to supervisor
- Ephemeral context (not persisted to thread)

### Phase 4: Supervisor Decision Handling

Handle supervisor decisions:

- Continue (default)
- Exit early
- Cancel worker
- Peek at details

### Phase 5: Graceful Failure Handling

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
