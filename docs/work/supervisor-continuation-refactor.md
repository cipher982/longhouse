# Supervisor Continuation Architecture Refactor

**Status:** Superseded — supervisor/worker is LangGraph-free as of 2026-01-13
**Priority:** High - Current implementation has fundamental design flaws
**Created:** 2025-01-10
**Completed:** 2025-01-10
**Author:** Claude (with David Rose)

---

## Implementation Summary (Updated 2026-01-15)

The supervisor/worker path now uses a LangGraph-free ReAct loop with DB-based continuation. `spawn_worker` returns job info, the loop raises `AgentInterrupted`, and `worker_resume.py` calls `AgentRunner.run_continuation()` to inject the tool result. LangGraph interrupt/resume is no longer used for supervisor/worker execution (workflow engine still uses LangGraph).

### Files Added/Modified
- **NEW:** `zerg/services/worker_resume.py` - Resume handler with idempotent WAITING→RUNNING transition
- **Modified:** `zerg/tools/builtin/supervisor_tools.py` - `spawn_worker_async()` returns job info; loop raises `AgentInterrupted`
- **Modified:** `zerg/services/supervisor_react_engine.py` - LangGraph-free ReAct loop (supervisor)
- **Modified:** `zerg/services/worker_runner.py` - Calls resume handler (LangGraph-free)
- **Modified:** `zerg/routers/jarvis_sse.py` - Subscribes to `SUPERVISOR_WAITING`/`SUPERVISOR_RESUMED` events
- **Modified:** `zerg/events/event_bus.py` - Added new event types
- **Deleted:** `run_continuation()` method from supervisor_service.py

### Tests Updated
- `tests/test_continuation_idempotency.py` - Concurrent resume test
- `tests/test_master_worker_flow_integration.py` - Full interrupt/resume integration test
- `tests/test_durable_runs.py` - Updated for WAITING/resume pattern

### Minor Follow-up Items (Not Blockers)
1. **Idempotency key improvement** - Currently keyed on `(supervisor_run_id, task)` fallback. Consider relying purely on `tool_call_id` where possible.
2. **Event emission placement** - `emit_run_event("supervisor_resumed")` could be wrapped in try/except for best-effort semantics.
3. **E2E integration test** - Current test mocks the resume function; add test exercising real LangGraph-free continuation path.
4. **Status semantics** - `jarvis_internal.py` returns `"status": "resumed"` even when handler returns `"skipped"`.

The remainder of this document is historical context from the original LangGraph-based design.

---

## Original Design Document

---

## Executive Summary

The current supervisor/worker continuation system uses fake message injection to handle async worker results. This fights against LangGraph's native `interrupt()`/`Command(resume=...)` pattern, which was designed exactly for this use case. This document captures the current issues, research findings, and recommended refactor.

---

## Part 1: Current System Issues

### 1.1 The Bug That Exposed the Problem

When a supervisor spawns a worker and the worker completes, the continuation response was **overwriting** the original "delegating to worker" message instead of appearing as a new message.

**Root Cause:** `correlationId` was being used as both:
- Request identifier (client intent)
- Message identifier (which chat bubble to update)

One request can produce multiple messages (original + continuation), so using `correlationId` for message lookup caused overwrites.

### 1.2 Patches Applied (Technical Debt)

We applied several patches to fix symptoms without addressing root cause:

1. **`message_id` column added** - Each assistant message now has a unique ID
2. **Timeline-based rendering** - Frontend renders messages/tools as sorted events
3. **`internal` flag on ThreadMessage** - Marks messages that shouldn't be shown to users
4. **String-prefix filtering** - Initially filtered `SYSTEM NOTIFICATION:` and `[CONTINUATION]` by content

These patches work but are fundamentally wrong - we shouldn't need them.

### 1.3 Current Architecture (Fighting LangGraph)

```
User sends message
    ↓
Supervisor processes, calls spawn_worker("task")
    ↓
spawn_worker returns immediately: "Job 123 queued"
    ↓
ReAct loop ENDS (no more tool_calls)
    ↓
Supervisor says "I've delegated this to a worker"
    ↓
[Worker runs in background - separate process]
    ↓
Worker completes
    ↓
HACK: run_continuation() injects fake "user" message with tool result
    ↓
HACK: run_continuation() injects fake "[CONTINUATION]" user message
    ↓
HACK: Create NEW run and call run_supervisor() again
    ↓
Supervisor processes fake messages and generates response
    ↓
HACK: Mark fake messages as internal=True so they don't show in UI
```

**Problems with this approach:**

| Issue | Impact |
|-------|--------|
| Fake user messages pollute conversation history | LLM sees fabricated context |
| Two separate runs for one logical turn | Complex state management |
| `internal` flag is a data model smell | Filtering at wrong layer |
| Message ID gymnastics | Fragile frontend logic |
| Not using LangGraph checkpointing | Reinventing the wheel |

### 1.4 Files Currently Involved

**Backend:**
- `apps/zerg/backend/zerg/services/supervisor_service.py`
  - `run_supervisor()` - Always creates a user message for the task
  - `run_continuation()` - Injects fake messages, creates new run
- `apps/zerg/backend/zerg/models/thread.py` - Has `internal` column
- `apps/zerg/backend/zerg/routers/jarvis.py` - Filters internal messages

**Frontend:**
- `apps/zerg/frontend-web/src/jarvis/app/components/ChatContainer.tsx` - Timeline rendering
- `apps/zerg/frontend-web/src/jarvis/app/hooks/useJarvisApp.ts` - Message loading
- `apps/zerg/frontend-web/src/jarvis/lib/supervisor-chat-controller.ts` - SSE handling

---

## Part 2: Research Findings

### 2.1 LangGraph's Native Pattern: `interrupt()` + `Command(resume=...)`

LangGraph was designed with async/background tools in mind. The correct pattern:

```python
from langgraph.types import interrupt, Command

# Inside a tool or node:
def spawn_worker(task: str) -> str:
    # 1. Submit job (fast, returns immediately)
    job_id = submit_job_to_queue(task)

    # 2. PAUSE - checkpoint saved, control returns to caller
    #    The interrupt payload is returned to the caller as __interrupt__
    result = interrupt({"job_id": job_id, "status": "pending"})

    # 3. AFTER RESUME - result is whatever was passed to Command(resume=...)
    return result

# Later, when worker completes (webhook/callback/polling):
graph.invoke(
    Command(resume=worker_result),
    config={"configurable": {"thread_id": same_thread_id}}
)
```

**Key insight:** The resume value is NOT a message - it's a return value for the `interrupt()` call. No fake messages needed.

### 2.2 How Checkpointing Works

From LangGraph docs:
- Checkpoint saved at every "super-step" (between graph steps)
- `thread_id` is the primary key for checkpoint lookup
- On resume, the node **restarts from the beginning** (not from the exact line)
- Code before `interrupt()` runs again - must be idempotent

**We already have this infrastructure:**
- `apps/zerg/backend/zerg/services/checkpointer.py` - PostgresSaver configured
- `@entrypoint(checkpointer=checkpointer)` in `zerg_react_agent.py`
- Thread IDs are used throughout

### 2.3 The Correct Flow

```
User sends message
    ↓
Supervisor processes, calls spawn_worker("task")
    ↓
spawn_worker submits job, calls interrupt({job_id: 123})
    ↓
LangGraph SAVES CHECKPOINT (state + execution position)
    ↓
Returns to caller with __interrupt__ payload
    ↓
Show user "Delegating to worker, job_id=123"
    ↓
[Worker runs in background]
    ↓
Worker completes
    ↓
Worker handler calls: graph.invoke(Command(resume=result), config={thread_id})
    ↓
LangGraph LOADS CHECKPOINT, resumes from interrupt()
    ↓
interrupt() returns worker_result
    ↓
spawn_worker returns worker_result
    ↓
ReAct loop continues naturally
    ↓
Supervisor generates final response
```

**No fake messages. No internal flags. No message ID gymnastics.**

### 2.4 Critical Implementation Details

#### Idempotency Requirement

From docs: "On resume, LangGraph re-executes the interrupted node from the beginning (not from the exact line). So code before `interrupt()` will run again."

**Solution:** Guard job submission:
```python
def spawn_worker(task: str, state: dict) -> str:
    # Check if job already submitted (idempotent)
    job_id = state.get("pending_job_id")
    if not job_id:
        job_id = submit_job_to_queue(task)
        # Store in state for replay safety

    result = interrupt({"job_id": job_id})
    return result
```

Or use LangGraph's `@task` decorator for durable execution.

#### Same thread_id Required

The resume call MUST use the same `thread_id` as the original run. We already track this.

#### Multiple Interrupts

If a node has multiple `interrupt()` calls, they're matched to resume values by call order. Don't conditionally skip/reorder them.

### 2.5 Alternative Patterns Considered

| Pattern | Use When | Notes |
|---------|----------|-------|
| **Wait + Stream Progress** | Can keep connection open for minutes | Use `get_stream_writer()` for progress |
| **Interrupt/Resume** | Can't keep connection open, need durability | Our case |
| **Split into Multiple Steps** | Need crash-safe incremental progress | Overkill for us |
| **State-based job tracking** | Results are large, don't want in messages | Could complement interrupt pattern |

### 2.6 Source Documentation

**Primary Sources:**
- [LangGraph Human-in-the-Loop](https://docs.langchain.com/oss/python/langgraph/human-in-the-loop)
- [LangGraph Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [LangGraph Durable Execution](https://docs.langchain.com/oss/python/langgraph/durable-execution)
- [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)

**Key Quotes:**

> "interrupt() pauses execution, checkpoints state, and surfaces payload to the caller under `__interrupt__`"

> "Command(resume=...) resume value becomes the return value of interrupt() inside the node/tool"

> "On resume, LangGraph restarts from the beginning of the node where the interrupt occurred, so any code before interrupt() will run again. Design for idempotency."

---

## Part 3: Recommended Implementation

### 3.1 High-Level Changes

1. **Modify `spawn_worker` tool** to call `interrupt()` instead of returning immediately
2. **Create worker completion handler** that calls `graph.invoke(Command(resume=result))`
3. **Delete `run_continuation()`** - no longer needed
4. **Delete `internal` column and filtering** - no fake messages to hide
5. **Simplify frontend** - remove message ID binding complexity

### 3.2 Detailed Implementation Plan

#### Step 1: Update spawn_worker Tool

**File:** `apps/zerg/backend/zerg/tools/builtin/supervisor_tools.py`

```python
from langgraph.types import interrupt

async def spawn_worker_async(
    task: str,
    model: str | None = None,
    # Remove wait parameter - always async now
) -> str:
    """Spawn a worker agent and wait for completion via interrupt/resume."""

    resolver = get_credential_resolver()
    db = resolver.db
    owner_id = resolver.owner_id
    supervisor_run_id = get_supervisor_run_id()

    # Create worker job (idempotent - check if already exists)
    # TODO: Need to access graph state for idempotency check
    worker_job = WorkerJob(
        owner_id=owner_id,
        supervisor_run_id=supervisor_run_id,
        task=task,
        model=model or DEFAULT_WORKER_MODEL_ID,
        status="queued",
    )
    db.add(worker_job)
    db.commit()
    db.refresh(worker_job)

    # Emit event for UI
    await event_bus.publish(EventType.WORKER_SPAWNED, {...})

    # INTERRUPT - checkpoint saved, control returns to caller
    # The __interrupt__ payload tells the UI what's happening
    worker_result = interrupt({
        "type": "worker_pending",
        "job_id": worker_job.id,
        "task": task,
        "message": f"Worker job {worker_job.id} started. Working on: {task}"
    })

    # AFTER RESUME - worker_result contains the completed output
    return worker_result
```

#### Step 2: Create Resume Handler

**File:** `apps/zerg/backend/zerg/services/worker_resume.py` (new)

```python
from langgraph.types import Command

async def resume_supervisor_with_worker_result(
    thread_id: int,
    job_id: int,
    worker_result: str,
):
    """Resume a supervisor run after worker completion.

    Called by worker_runner when a job finishes.
    """
    # Get the graph/runnable for this thread
    # Load with same thread_id used in original run
    config = {"configurable": {"thread_id": str(thread_id)}}

    # Resume - this continues from the interrupt() call
    result = await graph.ainvoke(
        Command(resume=worker_result),
        config=config,
    )

    return result
```

#### Step 3: Update Worker Runner

**File:** `apps/zerg/backend/zerg/services/worker_runner.py`

When worker completes, instead of calling `run_continuation()`:

```python
async def on_worker_complete(job: WorkerJob, result: str):
    # Get the supervisor run that spawned this worker
    supervisor_run = get_supervisor_run(job.supervisor_run_id)
    thread_id = supervisor_run.thread_id

    # Resume the supervisor with the result
    await resume_supervisor_with_worker_result(
        thread_id=thread_id,
        job_id=job.id,
        worker_result=result,
    )
```

#### Step 4: Handle __interrupt__ in SSE

When the graph returns with `__interrupt__`, emit appropriate SSE event:

```python
# In supervisor execution code
result = await graph.ainvoke(messages, config=config)

if "__interrupt__" in result:
    interrupt_payload = result["__interrupt__"]
    await emit_run_event(
        run_id=run.id,
        event_type="supervisor_waiting",  # New event type
        payload={
            "job_id": interrupt_payload.get("job_id"),
            "message": interrupt_payload.get("message"),
        }
    )
    # Run is paused, not complete
    run.status = RunStatus.WAITING  # New status
```

#### Step 5: Delete Dead Code

- Delete `run_continuation()` from `supervisor_service.py`
- Delete `internal` column from `ThreadMessage` model
- Delete `internal` filtering from `jarvis.py` history endpoint
- Remove migration for `internal` column (or add removal migration)
- Simplify frontend message handling (remove `BIND_MESSAGE_ID_TO_CORRELATION_ID` etc.)

### 3.3 Migration Strategy

1. **Phase 1:** Implement interrupt/resume alongside existing system
2. **Phase 2:** Feature flag to switch between old/new
3. **Phase 3:** Validate with tests
4. **Phase 4:** Remove old code

### 3.4 Testing

**Unit Tests:**
- spawn_worker returns interrupt payload
- Resume with Command continues from interrupt
- Idempotency: re-running interrupted node doesn't re-submit job

**Integration Tests:**
- Full flow: user message → spawn worker → interrupt → worker completes → resume → final response
- Verify no fake messages in thread history
- Verify checkpoint is saved/loaded correctly

**E2E Tests:**
- Chat UI shows "working on it" during interrupt
- Chat UI shows final response after resume
- Page refresh during interrupt can reconnect

---

## Part 4: Open Questions

1. **Idempotency mechanism:** How to access graph state from within a tool to check if job already submitted? May need `InjectedState` or similar.

2. **Thread ID mapping:** Current system uses integer thread IDs, LangGraph expects string. Need to verify checkpointer handles this.

3. **SSE during interrupt:** How to keep SSE stream alive during interrupt phase so frontend knows when resume happens?

4. **Multiple workers:** If supervisor spawns multiple workers, need to handle multiple interrupts or batch them.

5. **Timeout handling:** What happens if worker never completes? Need timeout + cleanup.

---

## Part 5: References

### Current Code Locations

| Component | Location |
|-----------|----------|
| Supervisor service | `apps/zerg/backend/zerg/services/supervisor_service.py` |
| spawn_worker tool | `apps/zerg/backend/zerg/tools/builtin/supervisor_tools.py` |
| Agent definition | `apps/zerg/backend/zerg/agents_def/zerg_react_agent.py` |
| Checkpointer | `apps/zerg/backend/zerg/services/checkpointer.py` |
| Worker runner | `apps/zerg/backend/zerg/services/worker_runner.py` |
| Frontend chat | `apps/zerg/frontend-web/src/jarvis/` |

### LangGraph Documentation

- [Interrupts Guide](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [Human-in-the-Loop](https://docs.langchain.com/oss/python/langgraph/human-in-the-loop)
- [Durable Execution](https://docs.langchain.com/oss/python/langgraph/durable-execution)
- [Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- [PostgresSaver](https://pypi.org/project/langgraph-checkpoint-postgres/)

### Related Commits

- `e9876b4` - feat: fix supervisor continuation with message_id and timeline rendering
- `295bd92` - fix: filter internal orchestration messages server-side via internal flag
- `7ed7889` - fix: filter internal SYSTEM NOTIFICATION messages from chat history

---

## Appendix: Research Notes

### Web Search 1: LangGraph Interrupt/Resume Pattern

> To implement a "run tool in the background, pause now, resume later" pattern in LangGraph, you generally don't keep the graph invocation open. Instead you:
> 1. kick off (enqueue) the long-running work (returns a job_id)
> 2. interrupt() the graph (this checkpoints state and returns control to your app)
> 3. when the job finishes, your app re-invokes the graph with Command(resume=...) (same thread_id) and execution continues.

### Web Search 2: Checkpointer Best Practices

> With a checkpointer enabled, LangGraph saves a checkpoint at every super-step (between graph steps), keyed by a thread_id. If you truly want minutes-long jobs, you generally should not make the LLM "wait" inside a tool call. The practical pattern is: submit job fast → persist job_id/status → later ingest result → optionally trigger a follow-up run.

### Web Search 3: Async Tool Patterns

> If you truly want the agent run to "wait", LangGraph supports pausing/resuming using interrupt(...) and later resuming with Command(resume=...). This can be used for "external input", not only humans. This still avoids fake messages because the resume value is not a user/assistant message; it's a resume payload.

### Web Search 4: Interrupt Tutorial

> On resume, LangGraph re-executes the interrupted node from the beginning (not from the exact line). So code before interrupt() will run again. Keep node code idempotent, and isolate side effects.
