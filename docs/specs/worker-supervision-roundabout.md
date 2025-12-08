# Worker Supervision & Monitoring

**Version:** 2.0
**Date:** December 2025
**Status:** Implementation Complete (v1.0 → v2.0 Simplification Pending)
**Philosophy:** Event-driven, not polling. Trust the LLM to interpret state.

---

## Executive Summary

**v1.0 approach (DEPRECATED):**

- Supervisor enters "roundabout" monitoring loop
- Polls worker status every 5 seconds
- Hardcoded heuristics decide: WAIT, EXIT, CANCEL, PEEK
- Complex decision tree separate from LLM reasoning

**v2.0 approach (TARGET):**

- Workers emit events when state changes
- Supervisor receives notifications (not polling)
- Supervisor interprets worker state and decides actions
- Tool activity events for UI progress display (kept)

**Key insight:** We don't need a monitoring "loop" or hardcoded decision rules. Workers notify when done. Supervisor reads their state and decides what to do.

---

## What Stays vs What Goes

### ✅ KEEP: Tool Activity Events (Phase 1-2)

**Purpose:** UI progress display and supervisor visibility.

**Events:**

```
worker_tool_started    → Tool execution begins
worker_tool_completed  → Tool finishes successfully
worker_tool_failed     → Tool encounters error
```

**Why keep:**

- Users want to see real-time progress (UI activity ticker)
- Supervisor benefits from seeing "worker is SSH'ing to cube right now"
- These are async notifications, not polling

**Implementation:** Already complete and working well.

### ❌ REMOVE: Roundabout Polling Loop (Phase 3-4)

**v1.0 implementation:**

```python
def spawn_worker(task, wait=True):
    job = create_worker_job(task)

    if wait:
        # Enter polling loop
        while True:
            status = check_worker_status(job.id)  # Query DB
            decision = make_heuristic_decision(status)  # Hardcoded rules

            if decision == "EXIT":
                return format_early_exit()
            elif decision == "CANCEL":
                cancel_worker()
                return format_cancellation()

            sleep(5)  # Poll every 5 seconds
```

**Problems:**

1. Busy-wait pattern (polling DB every 5s)
2. Supervisor waits in loop asking "are you done yet?"
3. Heuristic decision engine (`make_heuristic_decision()`) duplicates LLM reasoning
4. Complex state machine separate from LLM's natural decision-making

**v2.0 replacement:**

```python
async def spawn_worker(task):
    """Spawn worker and return job handle immediately."""
    job = create_worker_job(task)

    # Worker executes asynchronously
    # Emits events: worker_started, tool_events, worker_complete

    return {
        "job_id": job.id,
        "status": "queued",
        "task": task
    }

# Later, supervisor can check status if needed
async def get_worker_status(job_id):
    """Query current worker state (on-demand, not polling)."""
    job = db.query(WorkerJob).get(job_id)
    return {
        "status": job.status,  # queued, running, success, failed
        "elapsed_ms": ...,
        "current_operation": ...,  # From latest tool event
    }
```

**Key change:** Supervisor doesn't wait in a loop. It spawns workers and receives events when they complete. If supervisor needs status, it queries on-demand.

### ❌ REMOVE: Heuristic Decision Engine (Phase 4-5)

**v1.0 implementation:**

```python
def make_heuristic_decision(context: DecisionContext) -> RoundaboutDecision:
    """Hardcoded rules for supervisor decisions."""

    # Rule 1: Exit if worker completed
    if context.job_status in ["success", "failed"]:
        return RoundaboutDecision.EXIT

    # Rule 2: Cancel if stuck
    if context.stuck_operation_seconds > 60:
        return RoundaboutDecision.CANCEL

    # Rule 3: Exit early if answer visible
    if detect_final_answer_pattern(context.recent_output):
        return RoundaboutDecision.EXIT

    return RoundaboutDecision.WAIT
```

**Problems:**

1. Pre-programs supervisor's decisions
2. "Stuck" detection (> 60s) is arbitrary - some operations take longer
3. "Final answer pattern detection" is heuristic - LLM can read output directly
4. Every edge case requires new heuristic rule

**v2.0 replacement:**

```python
# NO heuristic decision function
# Supervisor sees worker state naturally via context:

supervisor_context = f"""
Worker job 123 is running.
Task: Check disk space on cube
Elapsed: 45 seconds
Current operation: ssh_exec(cube, "du -sh /var/*") - running for 35s

Recent activity:
- [00:00] Worker started
- [00:02] ssh_exec(cube, "df -h") completed in 1.8s
- [00:10] Analyzing output...
- [00:10] Started du -sh /var/* (still running)

What do you want to do?
"""

# Supervisor (LLM) decides naturally:
# - "du -sh takes time on large filesystems, I'll wait"
# - OR "I already have enough info from df -h, I can answer now"
# - OR "This seems stuck, let me cancel and try a different approach"
```

**Key change:** Supervisor LLM interprets worker state and makes judgment calls. No hardcoded rules.

---

## New Architecture (v2.0)

### Worker Execution Flow

```
Supervisor: spawn_worker("Check disk space on cube")
  ↓
Backend: Creates WorkerJob (status=queued)
  ↓
Returns to supervisor immediately:
  {"job_id": 123, "status": "queued"}
  ↓
Supervisor continues reasoning...
  ↓
[Meanwhile, async worker processor picks up job]
  ↓
Worker starts → Emits: worker_started event
Worker runs ssh_exec → Emits: worker_tool_started event
SSH completes → Emits: worker_tool_completed event
Worker finishes → Emits: worker_complete event
  ↓
Supervisor receives worker_complete via context (or queries status)
Supervisor reads worker result
Supervisor synthesizes answer
```

**Key difference:** Supervisor doesn't block waiting. It spawns and continues. Worker notifies when done.

### Supervisor Decision Making (Autonomous)

**If supervisor wants to check worker status:**

```python
# Supervisor can query anytime
status = get_worker_status(job_id=123)

# Status includes:
{
  "status": "running",
  "elapsed_ms": 45000,
  "current_operation": {
    "tool": "ssh_exec",
    "args": {"host": "cube", "command": "du -sh /var/*"},
    "elapsed_ms": 35000
  },
  "completed_operations": [
    {"tool": "ssh_exec", "command": "df -h", "duration_ms": 1800}
  ]
}
```

**Supervisor (LLM) interprets this and decides:**

- "du -sh can take 30-60s on large dirs, this is normal - I'll wait"
- "I already got what I need from df -h, I can answer without waiting"
- "35 seconds for du -sh seems stuck - I'll cancel and try a different command"

**No hardcoded rules.** The LLM makes these judgment calls.

### Error Handling (Autonomous)

**When worker fails:**

```
Worker encounters: SSH connection refused
  ↓
Worker emits: worker_complete(status="failed", error="...")
  ↓
Supervisor sees in worker result:
  "Worker job 123 failed.
   Error: SSH connection refused to cube (100.70.237.79)
   This usually means: SSH service down OR credentials not configured"
  ↓
Supervisor (LLM) interprets and responds:
  "I couldn't connect to cube via SSH. This could mean the server is down
   or SSH credentials aren't configured. Can you verify cube is reachable?"
```

**No error classification middleware.** Raw error → LLM interpretation → user-friendly explanation.

---

## What Gets Persisted

### Disk Artifacts (Full Audit Trail)

**Always logged:**

```
/data/swarmlet/workers/{worker_id}/
├── metadata.json           # Status, timestamps, model
├── thread.jsonl            # Full LLM conversation
├── result.txt              # Final result
└── tool_calls/
    ├── 001_ssh_exec.txt    # Each tool invocation
    ├── 002_ssh_exec.txt
    └── ...
```

**Removed in v2.0:**

```
└── monitoring/             # ❌ DELETE: Polling check logs
    ├── check_005s.json     # No longer needed
    ├── check_010s.json
    └── ...
```

**Why remove:** Polling checks were ephemeral state from the monitoring loop. In event-driven model, no loop exists.

### Supervisor Thread (Curated Context)

**What goes in thread:**

- User messages
- Supervisor responses
- Tool calls (spawn_worker, list_workers, etc.)
- Worker completion results

**What does NOT go in thread:**

- ~~Monitoring loop check-ins~~ (removed in v2.0)
- ~~Heuristic decision outputs~~ (removed in v2.0)
- Worker tool events (available via artifacts, not in thread)

**Philosophy:** Thread is the supervisor's memory. It should contain meaningful conversation, not implementation details.

---

## UI Integration

### Activity Ticker (Kept from v1.0)

While worker executes, Jarvis UI shows real-time progress:

```
┌─────────────────────────────────────────┐
│ 🔍 Investigating...                     │
│                                         │
│ Worker: Check disk on cube              │
│ ├─ ssh_exec "df -h" ✓ (1.8s)           │
│ └─ ssh_exec "du -sh /var/*" ⏳ 35s...   │
│                                         │
│ [Details] [Cancel]                      │
└─────────────────────────────────────────┘
```

**Events:**

- `worker_tool_started` → Show tool with spinner
- `worker_tool_completed` → Show checkmark + duration
- `worker_complete` → Hide ticker

**Why keep:** Users want visibility. Tool activity events provide this without architectural complexity.

---

## Migration from v1.0 to v2.0

### Code to Remove

1. **RoundaboutMonitor polling loop:**

   ```python
   # DELETE: apps/zerg/backend/zerg/services/roundabout_monitor.py
   # Lines: Polling while loop, check_worker_status calls

   class RoundaboutMonitor:
       async def wait_for_completion(self, job_id):
           while True:  # ❌ DELETE this pattern
               await asyncio.sleep(5)
               status = check_status()
               decision = make_decision()
               ...
   ```

2. **Heuristic decision engine:**

   ```python
   # DELETE: make_heuristic_decision() function
   # DELETE: RoundaboutDecision enum (WAIT, EXIT, CANCEL, PEEK)
   # DELETE: DecisionContext dataclass

   def make_heuristic_decision(context):  # ❌ DELETE
       if context.stuck > 60:
           return "CANCEL"
       ...
   ```

3. **Monitoring check logs:**
   ```python
   # DELETE: monitoring/ directory creation
   # DELETE: check_NNNs.json file writing
   ```

### Code to Keep

1. **Tool activity events:**

   ```python
   # KEEP: Event emission in zerg_react_agent._call_tool_async
   await event_bus.publish(EventType.WORKER_TOOL_STARTED, {...})
   await event_bus.publish(EventType.WORKER_TOOL_COMPLETED, {...})
   ```

2. **SSE event forwarding:**

   ```python
   # KEEP: Tool event subscriptions in jarvis.py
   event_bus.subscribe(EventType.WORKER_TOOL_STARTED, ...)
   ```

3. **UI activity ticker:**
   ```typescript
   // KEEP: apps/jarvis/apps/web/lib/supervisor-progress.ts
   // Tool call tracking and display
   ```

### Code to Simplify

1. **spawn_worker tool:**

   ```python
   # BEFORE (v1.0):
   async def spawn_worker(task, wait=True):
       job = create_job(task)
       if wait:
           return await roundabout.wait_for_completion(job.id)  # Polling
       return job.id

   # AFTER (v2.0):
   async def spawn_worker(task):
       """Spawn worker and return handle immediately."""
       job = create_job(task)
       return {
           "job_id": job.id,
           "status": "queued",
           "task": task
       }
   ```

2. **Supervisor can query status if needed:**

   ```python
   # NEW tool (optional):
   async def get_worker_status(job_id):
       """Query current worker state on-demand."""
       job = db.get(WorkerJob, job_id)

       # Include current operation from latest tool event
       current_op = get_latest_tool_activity(job.worker_id)

       return {
           "status": job.status,
           "elapsed_ms": ...,
           "current_operation": current_op,
           "completed_operations": get_completed_tools(job.worker_id)
       }
   ```

---

## Testing Strategy (Behavior-Focused)

### v1.0 Tests (Implementation-Focused)

```python
# Testing that polling loop works
def test_roundabout_waits_for_completion():
    monitor = RoundaboutMonitor()
    result = await monitor.wait_for_completion(job_id)
    assert result.check_count > 0

# Testing heuristic rules
def test_heuristic_decides_cancel_when_stuck():
    context = DecisionContext(stuck_seconds=65)
    decision = make_heuristic_decision(context)
    assert decision == RoundaboutDecision.CANCEL
```

### v2.0 Tests (Behavior-Focused)

```python
# Test that supervisor can answer questions
async def test_can_answer_backup_status():
    """Test behavior: Can system answer 'is backup working?'"""

    # Mock SSH response
    mock_ssh('cube', /kopia snapshot list/, """
        last-snapshot: 2025-12-07 03:00:15
        status: success
        size: 157GB
    """)

    # Ask naturally
    answer = await supervisor.execute("Is my backup working?")

    # Verify intelligent interpretation
    assert "successful" in answer
    assert "3am" in answer or "last ran" in answer
    assert "157GB" in answer

async def test_can_diagnose_disk_issue():
    """Test behavior: Can system diagnose disk problems?"""

    mock_ssh('cube', /df -h/, """
        Filesystem      Size  Used Avail Use% Mounted on
        /dev/sda1       500G  475G   25G  95% /
    """)

    answer = await supervisor.execute("Check disk space on cube")

    # Verify interpretation (not just repeating numbers)
    assert "95%" in answer
    assert "almost full" in answer or "critical" in answer
    assert "clean up" in answer or "add storage" in answer
```

**Key difference:** Test outcomes (can it answer?), not implementation (does it poll?).

---

## SSE Events for UI

**Events that matter for user experience:**

### Worker Lifecycle Events

```json
{"type": "worker_started", "job_id": 123, "task": "Check servers"}
{"type": "worker_complete", "job_id": 123, "status": "success"}
{"type": "worker_summary_ready", "summary": "All healthy..."}
```

### Tool Activity Events (Real-time progress)

```json
{"type": "worker_tool_started", "tool": "ssh_exec", "args": "cube: df -h"}
{"type": "worker_tool_completed", "tool": "ssh_exec", "duration_ms": 1800}
{"type": "worker_tool_failed", "tool": "ssh_exec", "error": "Connection refused"}
```

### Supervisor Events

```json
{"type": "supervisor_started", "task": "Check my servers"}
{"type": "supervisor_complete", "result": "Your servers are healthy..."}
```

**UI displays:**

- Floating progress toast (always visible)
- Worker task description
- Real-time tool calls with status icons
- Duration counters

**Removed in v2.0:**

- ~~worker_status_update (periodic polling)~~ - Not needed with events
- ~~roundabout_decision~~ - Internal implementation detail

---

## Supervisor Prompt Guidance

**v1.0 prompt (prescriptive):**

```
When you spawn a worker:
1. Enter the roundabout monitoring loop
2. You will receive status updates every 5 seconds
3. Respond with: WAIT, EXIT, CANCEL, or PEEK
4. EXIT if you see the answer in activity logs
5. CANCEL if operation is stuck for > 60 seconds
```

**v2.0 prompt (trusting):**

```
When you spawn a worker, you'll receive events when they complete.

If you need to check worker progress:
- Use get_worker_status(job_id) to see current state
- Interpret the output - you're smart enough to judge if it's stuck
- Decide: wait longer, read the partial result, or cancel if clearly wrong

Example reasoning:
"Worker has been running du -sh for 35 seconds. This can take 30-60s
on large directories, so that's normal. I'll wait."

OR:

"Worker already completed df -h which shows 78% disk. I can answer
the user's question without waiting for du -sh to finish."
```

**Key difference:** Prompt gives context and trusts LLM judgment. No decision rules.

---

## Implementation Phases (Updated)

### ✅ Phase 1: Tool Activity Events (COMPLETE - KEEP)

- Workers emit tool start/complete/fail events
- Events include tool name, args preview, duration
- Backend → SSE → UI activity ticker
- **Status:** Working well, no changes needed

### ✅ Phase 2: UI Activity Ticker (COMPLETE - KEEP)

- Jarvis displays real-time tool calls
- Nested under each worker in progress UI
- **Status:** Working well, no changes needed

### ⚠️ Phase 3-5: Roundabout Loop + Decisions (IMPLEMENTED - MARK FOR REMOVAL)

- Polling loop every 5s
- Heuristic decision engine
- LLM-gated decisions
- **Status:** Overengineered. Replace with event-driven + autonomous decisions

### ✅ Phase 6: Graceful Failure (COMPLETE - KEEP)

- Workers detect critical errors and fail fast
- Error messages passed to supervisor
- **Status:** Working well, keep as-is

---

## Refactoring Plan

### Step 1: Make spawn_worker non-blocking (1 day)

```python
# Change spawn_worker to return immediately
# Remove wait=True parameter
# Supervisor doesn't block waiting for completion
```

### Step 2: Add get_worker_status tool (1 day)

```python
# Supervisor can query worker state on-demand
# Returns current operation, elapsed time, completed operations
# LLM interprets and decides next action
```

### Step 3: Remove RoundaboutMonitor (2 days)

```python
# Delete monitoring loop
# Delete heuristic decision engine
# Delete monitoring/ artifact directory
# Update tests to be behavior-focused
```

### Step 4: Update supervisor prompt (1 day)

```python
# Remove prescriptive decision rules
# Add guidance for autonomous status checking
# Include examples of natural reasoning
```

**Total effort:** ~5 days to simplify architecture by 1000+ LOC.

---

## Technical Benefits of v2.0

| Aspect               | v1.0 (Polling)                     | v2.0 (Event-Driven)                   |
| -------------------- | ---------------------------------- | ------------------------------------- |
| CPU usage            | Polls DB every 5s                  | Events trigger on state change        |
| Latency              | 0-5s delay to see events           | Immediate event propagation           |
| Complexity           | Monitoring loop + decision engine  | Workers notify, supervisor interprets |
| Supervisor reasoning | Gated by heuristics                | Fully autonomous                      |
| Code to maintain     | ~1000+ LOC (monitor + decider)     | ~200 LOC (event handlers)             |
| Test coverage        | Implementation tests (loop, rules) | Behavior tests (can it answer?)       |

---

## Appendix A: Event Schema

### worker_started

```json
{
  "type": "worker_started",
  "job_id": 123,
  "worker_id": "2025-12-07T20-33-15_...",
  "task": "Check disk space on cube",
  "timestamp": "2025-12-07T20:33:15Z"
}
```

### worker_tool_started

```json
{
  "type": "worker_tool_started",
  "worker_id": "2025-12-07T20-33-15_...",
  "tool_name": "ssh_exec",
  "tool_call_id": "call_abc123",
  "args_preview": "cube: df -h",
  "timestamp": "2025-12-07T20:33:17Z"
}
```

### worker_tool_completed

```json
{
  "type": "worker_tool_completed",
  "worker_id": "2025-12-07T20-33-15_...",
  "tool_name": "ssh_exec",
  "tool_call_id": "call_abc123",
  "duration_ms": 1800,
  "result_preview": "Filesystem 500G, 78% used",
  "timestamp": "2025-12-07T20:33:19Z"
}
```

### worker_complete

```json
{
  "type": "worker_complete",
  "job_id": 123,
  "worker_id": "2025-12-07T20-33-15_...",
  "status": "success",
  "duration_ms": 16547,
  "timestamp": "2025-12-07T20:33:32Z"
}
```

### worker_summary_ready

```json
{
  "type": "worker_summary_ready",
  "job_id": 123,
  "summary": "Cube disk at 78%, healthy, ~2-3 months capacity remaining",
  "timestamp": "2025-12-07T20:33:32Z"
}
```

---

## Appendix B: Autonomous Decision Examples

### Example 1: Normal Progress (Wait)

**Worker state:**

```
Task: Check backup status
Elapsed: 8 seconds
Current: ssh_exec(cube, "kopia snapshot list") - running 5s
```

**Supervisor reasoning:**

> "SSH to cube and running kopia can take 5-15 seconds. 8 seconds total elapsed is reasonable. The worker is actively executing - I'll wait for it to complete."

**Decision:** Continue waiting (implicit - supervisor doesn't take action)

### Example 2: Early Exit (Enough Info)

**Worker state:**

```
Task: Check disk space
Elapsed: 12 seconds
Completed: ssh_exec(cube, "df -h") → "Filesystem 78% used"
Current: ssh_exec(cube, "du -sh /var/*") - running 10s
```

**Supervisor reasoning:**

> "I already know disk is at 78% from df -h. The user just asked if disk is healthy - I can answer yes. The du -sh command would give more detail but isn't necessary to answer the question."

**Decision:** Read partial result, synthesize answer, don't wait for du completion

### Example 3: Intervention (Cancel)

**Worker state:**

```
Task: Check if API is responding
Elapsed: 95 seconds
Current: http_request(api.slow-service.com/health) - running 90s
```

**Supervisor reasoning:**

> "HTTP health check is taking 90 seconds. Normal health checks return in 1-5 seconds. This endpoint is clearly timing out or extremely slow. Waiting longer won't help."

**Decision:** Cancel worker, report: "API health check timed out - endpoint not responding. This suggests the service is down or severely degraded."

**Key point:** Supervisor makes these decisions by reading the state and reasoning, not by following hardcoded rules like "if > 60s then cancel".

---

_End of Specification v2.0_

**Summary:**

- Workers execute asynchronously and emit events
- Supervisor interprets worker state autonomously (no heuristics)
- Tool activity events provide UI progress (keep this)
- Polling loop and decision engine removed (trust LLM reasoning)
- ~1000+ LOC simpler, more flexible, equally capable
