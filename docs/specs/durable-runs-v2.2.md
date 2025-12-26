# Durable Runs v2.2: Timeout Migration & Completion Notifications

> **Status**: Complete
> **Date**: 2025-12-25
> **Updated**: 2025-12-25
> **Authors**: Post-mortem analysis + prior art research
> **Depends on**: docs/specs/TRACE_FIRST_NORTH_STAR.md, docs/specs/worker-supervision-roundabout.md

## Executive Summary

Runs are now **durable** - they survive client disconnects, timeouts, and network blips. Timeouts stop *waiting*, not kill *work*. When background work completes, the supervisor is notified and resume automatically. The dashboard stays in sync via real-time WebSocket updates.

### Durability Scope (v2.2)

**What's durable in this version:**
- Client disconnects (SSE drops, tab closes, network blips)
- Client-side timeouts (watchdog, user impatience)
- Server-side wait timeouts (supervisor stops blocking, work continues)
- **Status Visibility**: Runs are tracked in the database and updated in real-time on the dashboard via WebSockets.
- **Re-attachment**: Clients can poll/query run status and get current results after re-connecting.

**What's NOT durable yet (future work):**
- Backend process restarts (DEFERRED runs would be orphaned)
- Server crashes (would need persistent job queue + "resume pending on boot")
- True SSE streaming re-attach (currently returns status snapshots to avoid infinite blocking in ASGI test environments)

---

This spec aligns Zerg's execution model with:
- **LangGraph** native semantics (threads + runs + join)
- **Temporal** durable execution (heartbeats, timeouts as semantics)
- **AWS Step Functions** callback token pattern (pause → resume)
- **Airflow** deferrable operators (suspend without killing)
- **Unix** job control (fg/bg/jobs)

---

## Problem Statement (Post-Mortem: 2025-12-24)

### What Happened

User sent: "check disk space on cube"

| Time | Event |
|------|-------|
| 00:33:55 | Supervisor started (run 28) |
| 00:34:25 | Worker spawned with `wait=True` |
| 00:35:55 | **Supervisor 120s timeout fires** (worker still running) |
| 00:35:55 | Error event emitted, SSE stream closes |
| 00:37:00 | Roundabout cancels job: "No progress for 6 polls" |
| 00:37:32 | Worker completes successfully (181s) |
| 00:37:34 | `No subscribers for WORKER_COMPLETE` - **result lost** |

**User saw**: "Supervisor execution timed out after 120s" toast, no answer.

### Root Causes

1. **Timeout at wrong layer**: 120s timeout wrapped entire supervisor execution, not per-worker wait
2. **Timeout kills work**: `asyncio.wait_for()` cancels the task, doesn't just stop waiting
3. **Heuristic cancellation**: Roundabout cancelled on "no tool events" during LLM thinking
4. **SSE coupled to run**: Stream close = run orphaned, events go to `/dev/null`
5. **Error = toast only**: No assistant message rendered on error

### Spec Violations

| Spec Says | Code Did |
|-----------|----------|
| "Trace is truth, prose is a view" | Error = toast, no durable message |
| "LLM decides, not heuristics" | `NO_PROGRESS_POLLS=6` auto-cancelled |
| Supervisor orchestrates, workers execute | 120s killed orchestration mid-flight |

---

## Prior Art & Borrowed Concepts

### Terminology Adopted

| Term | Source | Our Usage |
|------|--------|-----------|
| **Foreground/Background** | Unix job control | `wait=True` (fg) vs `wait=False` (bg) |
| **Defer** | Airflow deferrables | Timeout migration: fg → bg without killing |
| **Join** | LangGraph, Celery | Block until run/task completes |
| **Heartbeat** | Temporal | Progress signal during LLM thinking (not just tools) |
| **Watch/Monitor** | Akka DeathWatch | Supervisor lifecycle notifications |
| **Callback Token** | AWS Step Functions | Resume trigger on completion |
| **Chord** | Celery | Multi-worker join + callback |
| **Saga** | Garcia-Molina 1987 | Multi-step orchestration with compensation |
| **Shield** | asyncio | Prevent timeout from cancelling task |

### Key Insights from Prior Art

**asyncio**: `wait_for()` cancels by default. Use `shield()` to timeout the *wait* without killing the *work*.

**Temporal**: Heartbeats solve "no observable progress during long operations". Workers heartbeat "still working" even during LLM reasoning.

**AWS Step Functions**: `waitForTaskToken` pauses workflow indefinitely, resumes when external worker sends token back. This is our "migrate to background + notify on complete" pattern.

**Airflow Deferrable Operators**: "Suspend itself and free the worker, hand waiting to Triggerer, resume when ready." Exactly timeout migration.

**LangGraph**: Thread = durable state, Run = execution attempt. `runs.create()` returns immediately, `runs.join()` blocks with timeout. Timeout stops waiting, doesn't kill run. Webhook for completion notification.

---

## Design

### Core Principle

> **Timeouts stop waiting, not working.**

A timeout means "I've waited long enough, I'll check back later" - not "kill everything and error out."

### Run Lifecycle States

```
                    ┌─────────────────────────────────────┐
                    │           (re-attach)               │
                    ▼                                     │
QUEUED → RUNNING → DEFERRED → RUNNING → COMPLETED ───────┤
            │         │                     │            │
            │         │                     └── webhook ─┘
            │         │                     (triggers continuation)
            │         │
            └─────────┴──→ FAILED

State Definitions:
- QUEUED: Created, waiting for executor
- RUNNING: Actively executing (supervisor or worker)
- DEFERRED: Timeout migration - still running, but caller stopped waiting
- COMPLETED: Finished successfully
- FAILED: Finished with error
```

### Events

```
Lifecycle Events:
- run:created        {run_id, thread_id, task}
- run:started        {run_id}
- run:heartbeat      {run_id, activity: "llm_thinking"|"llm_streaming"|"tool_executing"}
- run:deferred       {run_id, reason: "timeout", continue_url}
- run:completed      {run_id, result, duration_ms}
- run:failed         {run_id, error}

Worker Events:
- worker:spawned     {job_id, run_id, task}
- worker:started     {job_id, worker_id}
- worker:heartbeat   {job_id, activity}
- worker:tool_*      {job_id, tool_name, ...}
- worker:completed   {job_id, status, result_summary}
```

### Foreground vs Background Execution

**Foreground (join with timeout)**:
```python
# Caller blocks, but timeout doesn't kill the run
result = await supervisor.run(task, timeout=180)
# If timeout: run continues as DEFERRED, returns partial result
```

**Background (fire and forget)**:
```python
# Returns immediately with run_id
run_id = await supervisor.run_async(task)
# Caller can: poll status, attach to stream, or wait for webhook
```

**Timeout Migration (fg → bg)**:
```python
# When foreground timeout hits:
# 1. Run state → DEFERRED (not FAILED)
# 2. Run continues executing
# 3. Caller gets: {status: "deferred", run_id, attach_url}
# 4. On completion: webhook fires, continuation triggered
```

### Completion Notifications (Webhooks)

When a background/deferred run completes:

1. **Worker completes** → saves result to artifact store
2. **Emit `worker:completed`** event
3. **POST webhook** to continuation endpoint:
   ```
   POST /api/internal/runs/{supervisor_run_id}/continue
   {
     "trigger": "worker_complete",
     "job_id": 2,
     "worker_id": "2025-12-24T00-34-30_check-disk",
     "status": "success",
     "result_summary": "Disk check complete. 45% used on /dev/sda1..."
   }
   ```
4. **Continuation handler**:
   - Injects tool result message into supervisor thread
   - Triggers new supervisor run (same `thread_id`, **new** `run_id`)
   - Links runs via `continuation_of_run_id` for traceability
   - Supervisor synthesizes final answer from worker result
5. **User notified** via SSE (if connected) or stored for later

**Run Identity Model:**
```
Thread 1 (conversation)
  ├── Run 28 (original, status=DEFERRED)
  │     └── spawned Worker Job 2
  └── Run 29 (continuation, continuation_of=28, status=SUCCESS)
        └── processed Worker Job 2 result
```
- `thread_id` = durable conversation state (same across continuations)
- `run_id` = single execution attempt (new for each continuation)
- `continuation_of_run_id` = links attempts for debugging/tracing

### SSE Re-attach

Streams are **views** of durable runs, not the runs themselves.

```
# Start run (returns run_id)
POST /api/jarvis/chat
→ {run_id: 42, status: "running"}

# Attach to stream (can connect/reconnect anytime)
GET /api/jarvis/runs/42/stream
→ SSE stream (replays recent events if reconnecting)

# Check status without streaming
GET /api/jarvis/runs/42
→ {status: "completed", result: "...", duration_ms: 12345}

# List active runs
GET /api/jarvis/runs?status=running
→ [{run_id: 42, task: "check disk...", elapsed_ms: 45000}]
```

**Key behavior**: SSE disconnect does NOT cancel the run.

### Heartbeats (Progress Without Tool Events)

Problem: Roundabout saw "no tool events for 30s" and cancelled, but worker was making progress (LLM reasoning).

Solution: Emit heartbeats during LLM activity.

**Implementation note**: The current `zerg_react_agent._call_model_async` uses `ainvoke()` (blocking), not `astream()`. Two approaches:

**Option A: Timer-based heartbeats (simpler)**
```python
async def call_llm_with_heartbeat(messages, heartbeat_fn):
    await heartbeat_fn("llm_call_started")

    # Start background heartbeat timer
    async def heartbeat_loop():
        while True:
            await asyncio.sleep(10)  # Every 10s
            await heartbeat_fn("llm_thinking")

    heartbeat_task = asyncio.create_task(heartbeat_loop())
    try:
        result = await llm.ainvoke(messages)  # Blocking call
    finally:
        heartbeat_task.cancel()

    await heartbeat_fn("llm_call_complete")
    return result
```

**Option B: Rely on token streaming (when enabled)**
When `LLM_TOKEN_STREAM=true`, token events already provide progress signals. Roundabout can treat `SUPERVISOR_TOKEN` events as heartbeats.

Roundabout tracks heartbeats as progress, not just tool events.

---

## Implementation

### Phase 1: Stop Killing Runs on Timeout (Complete)

**Goal**: Timeout stops waiting, run continues.

#### Task 1.1: Shield supervisor execution

**File**: `apps/zerg/backend/zerg/services/supervisor_service.py`

```python
# Before (line ~516):
created_messages = await asyncio.wait_for(
    runner.run_thread(self.db, thread),
    timeout=timeout,
)

# After:
run_task = asyncio.create_task(runner.run_thread(self.db, thread))
try:
    created_messages = await asyncio.wait_for(
        asyncio.shield(run_task),
        timeout=timeout,
    )
except asyncio.TimeoutError:
    # Run continues in background - don't cancel it
    logger.info(f"Run {run.id} deferred after {timeout}s timeout (still running)")
    run.status = RunStatus.DEFERRED
    self.db.commit()

    # Emit deferred event (not error)
    await event_bus.publish(
        EventType.SUPERVISOR_DEFERRED,
        {
            "run_id": run.id,
            "message": f"Still working... I'll notify you when complete.",
            "attach_url": f"/api/jarvis/runs/{run.id}/stream",
        }
    )

    # Return partial result, not error
    return SupervisorRunResult(
        run_id=run.id,
        status="deferred",
        result="I'm still working on this in the background. I'll let you know when it's done.",
    )
```

#### Task 1.2: Add DEFERRED status

**File**: `apps/zerg/backend/zerg/models/enums.py`

```python
class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DEFERRED = "deferred"  # NEW: timeout migration, still executing
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
```

#### Task 1.3: Handle deferred event in frontend

**File**: `apps/zerg/frontend-web/src/jarvis/lib/supervisor-chat-controller.ts`

```typescript
case "supervisor_deferred":
    // Render as assistant message, not error
    this.addAssistantMessage(payload.message);
    this.showReattachHint(payload.attach_url);
    break;
```

#### Task 1.4: Increase default timeout

**File**: `apps/zerg/backend/zerg/routers/jarvis_chat.py`

```python
# Line ~91: Change timeout from 120 to 600 (10 min safety net)
timeout=600,
```

---

### Phase 2: Remove Heuristic Cancellation + Frontend Watchdog (Complete)

**Goal**: Align with spec - don't auto-cancel work. Keep heuristic mode (cheap) but remove the cancel action.

#### Task 2.1: Remove NO_PROGRESS_POLLS cancellation

**File**: `apps/zerg/backend/zerg/services/roundabout_monitor.py`

```python
# Delete or comment out lines ~200-212:
# if ctx.polls_without_progress >= ROUNDABOUT_NO_PROGRESS_POLLS:
#     return (RoundaboutDecision.CANCEL, ...)

# Replace with warning/logging only:
if ctx.polls_without_progress >= ROUNDABOUT_NO_PROGRESS_POLLS:
    logger.warning(f"Job {ctx.job_id}: {ctx.polls_without_progress} polls without tool events")
    # Don't cancel - just log. LLM may be thinking.
    # Hard timeouts (ROUNDABOUT_HARD_TIMEOUT) still apply as safety net.
```

**Note**: Keep `decision_mode="heuristic"` as default - it's cheaper than LLM mode. The fix is removing the cancel action, not changing the decision engine.

#### Task 2.2: Fix frontend watchdog timeout

**File**: `apps/zerg/frontend-web/src/jarvis/lib/supervisor-chat-controller.ts`

The frontend has a 60s watchdog (`WATCHDOG_TIMEOUT_MS = 60000`) that calls `cancel()` and aborts the stream. This is another "timeout kills work" bug.

```typescript
// Current (around line ~XXX):
// if (elapsed > WATCHDOG_TIMEOUT_MS) {
//     this.cancel();  // WRONG: kills server-side work
// }

// Fixed: defer instead of cancel
if (elapsed > WATCHDOG_TIMEOUT_MS) {
    this.markDeferred();  // Show "still working, check back later"
    this.showReattachHint(this.currentRunId);
    // DON'T call cancel() - let server continue
}
```

#### Task 2.3: Remove STUCK_THRESHOLD cancellation

**File**: `apps/zerg/backend/zerg/services/roundabout_monitor.py`

Also remove the "operation stuck too long" cancellation (line ~189):

```python
# Remove or convert to warning:
# if ctx.is_stuck and ctx.stuck_seconds > ROUNDABOUT_CANCEL_STUCK_THRESHOLD:
#     return (RoundaboutDecision.CANCEL, ...)

if ctx.is_stuck and ctx.stuck_seconds > ROUNDABOUT_CANCEL_STUCK_THRESHOLD:
    logger.warning(f"Job {ctx.job_id}: operation stuck for {ctx.stuck_seconds:.0f}s")
    # Don't cancel - hard timeout is the only cancellation trigger
```

---

### Phase 3: Add Heartbeats (Complete)

**Goal**: Progress signals during LLM reasoning.

#### Task 3.1: Create heartbeat event type

**File**: `apps/zerg/backend/zerg/events/__init__.py`

```python
class EventType(str, Enum):
    # ... existing ...
    WORKER_HEARTBEAT = "worker_heartbeat"
    SUPERVISOR_HEARTBEAT = "supervisor_heartbeat"
```

#### Task 3.2: Emit heartbeats during LLM calls

**File**: `apps/zerg/backend/zerg/agents_def/zerg_react_agent.py`

```python
# In _call_model_async or equivalent:
async def _call_model_with_heartbeat(messages, run_id, owner_id):
    await event_bus.publish(
        EventType.SUPERVISOR_HEARTBEAT,
        {"run_id": run_id, "activity": "llm_call_started", "owner_id": owner_id}
    )

    async for chunk in model.astream(messages):
        # Heartbeat every ~5 seconds during streaming
        if should_heartbeat():
            await event_bus.publish(
                EventType.SUPERVISOR_HEARTBEAT,
                {"run_id": run_id, "activity": "llm_streaming", "owner_id": owner_id}
            )
        yield chunk

    await event_bus.publish(
        EventType.SUPERVISOR_HEARTBEAT,
        {"run_id": run_id, "activity": "llm_call_complete", "owner_id": owner_id}
    )
```

#### Task 3.3: Track heartbeats in roundabout

**File**: `apps/zerg/backend/zerg/services/roundabout_monitor.py`

```python
# Subscribe to heartbeat events
event_bus.subscribe(EventType.WORKER_HEARTBEAT, handle_heartbeat)

def handle_heartbeat(payload):
    if payload.get("job_id") == self.job_id:
        self._last_heartbeat = datetime.now(timezone.utc)
        self._polls_without_progress = 0  # Reset on heartbeat
```

---

### Phase 4: Status Endpoints (Complete)

**Goal**: Allow clients to check the status and result of a deferred run.

Implemented via `apps/zerg/backend/zerg/routers/jarvis_runs.py`:
- `GET /api/jarvis/runs/{run_id}`: Returns status, duration, and final result if complete.
- `GET /api/jarvis/runs/{run_id}/stream`: Re-attaches to the SSE stream of an in-progress run.

### Phase 5: SSE Re-attach & Decoupling (Complete)

**Goal**: Streams are views, runs are durable.

Implemented in `apps/zerg/backend/zerg/routers/jarvis_chat.py`:
- SSE disconnect (`asyncio.CancelledError`) no longer cancels the supervisor task.
- Background task is registered in a central registry to prevent orphan tracking issues.

#### Task 5.1: Add run status endpoint

**File**: `apps/zerg/backend/zerg/routers/jarvis_runs.py` (new file)

```python
router = APIRouter(prefix="/runs", tags=["jarvis"])

@router.get("/{run_id}")
async def get_run_status(
    run_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_jarvis_user),
):
    """Get current status of a run."""
    run = db.query(AgentRun).filter(
        AgentRun.id == run_id,
        AgentRun.agent.has(owner_id=current_user.id)
    ).first()

    if not run:
        raise HTTPException(404)

    return {
        "run_id": run.id,
        "status": run.status.value,
        "created_at": run.created_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "error": run.error,
        # Include last assistant message if completed
        "result": get_last_assistant_message(db, run.thread_id) if run.status == RunStatus.SUCCESS else None,
    }

@router.get("/{run_id}/stream")
async def attach_to_run_stream(
    run_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_jarvis_user),
):
    """Attach to an existing run's event stream.

    If run is complete, returns final result immediately.
    If run is in progress, streams remaining events.
    """
    run = db.query(AgentRun).filter(
        AgentRun.id == run_id,
        AgentRun.agent.has(owner_id=current_user.id)
    ).first()

    if not run:
        raise HTTPException(404)

    if run.status in (RunStatus.SUCCESS, RunStatus.FAILED):
        # Run already complete - return result directly
        return JSONResponse({
            "status": run.status.value,
            "result": get_last_assistant_message(db, run.thread_id),
            "error": run.error,
        })

    # Run in progress - return SSE stream
    return EventSourceResponse(
        _attach_stream_generator(run_id, current_user.id)
    )
```

#### Task 5.2: Decouple run from SSE connection

**File**: `apps/zerg/backend/zerg/routers/jarvis_chat.py`

```python
# Current: run is cancelled if SSE disconnects
# Change: run continues, SSE is just a view

async def _chat_stream_generator(...):
    # ...existing code...

    except asyncio.CancelledError:
        logger.info(f"SSE stream disconnected for run {run_id}")
        # DON'T cancel the task - let it continue
        # if task_handle and not task_handle.done():
        #     task_handle.cancel()  # REMOVE THIS

        # Just log and clean up subscriptions
```

---

### Phase 6: Frontend Updates (Complete - Simplified)

#### Task 6.1: Handle deferred state

**File**: `apps/zerg/frontend-web/src/jarvis/lib/supervisor-chat-controller.ts`

```typescript
private handleSSEEvent(event: SSEEvent) {
    switch (event.type) {
        // ... existing cases ...

        case "supervisor_deferred":
            // Show as assistant message, not error
            this.messages.push({
                role: "assistant",
                content: event.payload.message,
                metadata: {
                    status: "deferred",
                    run_id: event.payload.run_id,
                    attach_url: event.payload.attach_url,
                }
            });
            // Don't set error state - this is normal
            break;
    }
}
```

#### Task 6.2: Add re-attach UI

**Current implementation (v2.2):** no dedicated banner component.

- Deferred is shown as a normal assistant message (not an error toast).
- `attach_url` is plumbed through the controller/event payload for future UI work.

**Files**:
- `apps/zerg/frontend-web/src/jarvis/lib/supervisor-chat-controller.ts`
- `apps/zerg/frontend-web/src/jarvis/lib/worker-progress-store.ts`

---

## Success Criteria (v2.2 MVP)

1. **No lost work**: Timeout = defer, not kill
2. **Always an answer**: Even on error/timeout, assistant message rendered
3. **Background completion**: Original supervisor run continues until finished, updating DB status
4. **Re-attachable**: SSE disconnect doesn't orphan runs; `/runs/{id}/stream` allows re-attaching
5. **Progress visible**: Heartbeats emitted during LLM reasoning to prevent false "stuck" warnings

---

## References

- [Temporal Activity Timeouts](https://temporal.io/blog/activity-timeouts)
- [AWS Step Functions Callback Pattern](https://aws.amazon.com/blogs/compute/handle-unpredictable-processing-times-with-operational-consistency/)
- [Airflow Deferrable Operators](https://airflow.apache.org/docs/apache-airflow/stable/authoring-and-scheduling/deferring.html)
- [LangGraph Background Runs](https://docs.langchain.com/langsmith/background-run)
- [Akka Supervision and Monitoring](https://doc.akka.io/libraries/akka-core/current/general/supervision.html)
- [Sagas (Garcia-Molina 1987)](https://www.cs.cornell.edu/andru/cs711/2002fa/reading/sagas.pdf)
- [Python asyncio.shield](https://docs.python.org/3/library/asyncio-task.html)
