# Parallel-First Multi-Agent Architecture

## Summary

Transform Zerg from sequential "spawn one worker, wait, resume" to **parallel-first** where:
- All non-blocking tools execute concurrently (`asyncio.gather`)
- Multiple workers spawn in one ReAct iteration
- Barrier pattern: resume only when ALL workers complete
- Single context roundtrip for N workers (vs N roundtrips today)

## Critical Race Conditions (Addressed)

**Identified by Codex + Gemini + web research:**

| Issue | Problem | Fix |
|-------|---------|-----|
| **Fast Worker** | Worker finishes before PendingWorkerSet exists | Two-phase: jobs created as CREATED, flipped to QUEUED after barrier exists |
| **Double Resume** | Two workers both see "all complete" | Single atomic transaction: `SELECT FOR UPDATE` + check + flip `resuming` flag |
| **Missing tool_call_id** | Can't map job → ToolMessage | Store `(job_id, tool_call_id)` mapping in barrier table |
| **ARRAY mutation** | ORM doesn't track list changes reliably | Normalized `pending_worker_jobs` table instead |
| **Shared DB session** | asyncio.gather with same session = races | Each parallel tool gets its own session |

## Core Principle: "Spawn All, Wait All, Resume Once"

```
LLM returns 3 spawn_worker calls
    │
    ▼
Queue all 3 jobs (no interrupt per-job)
    │
    ▼
Single AgentInterrupted with job_ids=[1,2,3]
    │
    ├─────────┼─────────┤
    ▼         ▼         ▼
 Worker1   Worker2   Worker3  (parallel)
    │         │         │
    └─────────┼─────────┘
              ▼
    Barrier: all complete
              │
              ▼
    Batch resume with 3 ToolMessages
              │
              ▼
    LLM synthesizes final answer
```

## Implementation Plan

### Phase 1: Database Schema (Normalized)

**Two tables instead of ARRAY fields** (per Codex/Gemini recommendations):

```python
class WorkerBarrier(Base):
    """Tracks a batch of parallel workers for a supervisor run."""
    __tablename__ = "worker_barriers"

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("agent_runs.id"), unique=True, index=True)
    expected_count = Column(Integer, nullable=False)  # How many workers to wait for
    completed_count = Column(Integer, nullable=False, default=0)
    status = Column(String, nullable=False, default="waiting")  # waiting, resuming, completed
    deadline_at = Column(DateTime, nullable=True)  # Timeout handling
    created_at = Column(DateTime, server_default=func.now())

class BarrierJob(Base):
    """Individual worker job in a barrier. Normalized for safe concurrent updates."""
    __tablename__ = "barrier_jobs"

    id = Column(Integer, primary_key=True)
    barrier_id = Column(Integer, ForeignKey("worker_barriers.id"), index=True)
    job_id = Column(Integer, ForeignKey("worker_jobs.id"), index=True)
    tool_call_id = Column(String, nullable=False)  # Critical: needed for ToolMessage
    status = Column(String, nullable=False, default="created")  # created, queued, completed, failed
    result = Column(Text, nullable=True)  # Cached result for batch resume
    error = Column(Text, nullable=True)
    completed_at = Column(DateTime, nullable=True)
```

**Two-Phase Commit Pattern** (fixes "fast worker" race):
1. `spawn_worker` creates `WorkerJob` with status=`CREATED` (not queued!)
2. After ALL spawn_workers processed → create `WorkerBarrier` + `BarrierJob` records
3. Atomic: flip all jobs from `CREATED` → `QUEUED`
4. Workers can now pick them up (barrier guaranteed to exist)

**Files:**
- `apps/zerg/backend/zerg/models/worker_barrier.py` (new)
- `apps/zerg/backend/zerg/models/__init__.py` (export)
- Alembic migration

### Phase 2: Parallel Tool Execution

**Change:** Replace sequential for-loop with `asyncio.gather()`

**File:** `apps/zerg/backend/zerg/services/supervisor_react_engine.py`

**Key fixes:**
1. Each parallel tool gets its own DB session (prevents shared session races)
2. Non-spawn tools execute first, results persisted before interrupt
3. Two-phase spawn: create as CREATED, then flip to QUEUED after barrier exists

```python
async def _execute_tools_parallel(
    tool_calls: list[dict],
    tools_by_name: dict,
    run_id: int,
    session_factory,  # Pass factory, not session
    ...
) -> tuple[list[ToolMessage], dict | None]:
    """Execute tools in parallel with isolated DB sessions."""

    # Separate by type
    spawn_calls = [tc for tc in tool_calls if tc["name"] == "spawn_worker"]
    other_calls = [tc for tc in tool_calls if tc["name"] != "spawn_worker"]

    tool_results = []

    # Phase 1: Execute non-spawn tools in parallel (each gets own session)
    if other_calls:
        async def execute_with_session(tc):
            async with session_factory() as session:  # Fresh session per tool
                return await _execute_tool(tc, tools_by_name, session, ...)

        results = await asyncio.gather(
            *[execute_with_session(tc) for tc in other_calls],
            return_exceptions=True
        )

        # Convert exceptions to error ToolMessages, preserve order
        for tc, result in zip(other_calls, results):
            if isinstance(result, Exception):
                tool_results.append(ToolMessage(
                    content=f"<tool-error>{result}</tool-error>",
                    tool_call_id=tc["id"],
                    name=tc["name"],
                ))
            else:
                tool_results.append(result)

    # Phase 2: Two-phase spawn (CREATED, not QUEUED yet)
    if spawn_calls:
        created_jobs = []
        for tc in spawn_calls:
            job = await _create_worker_job_pending(  # New: creates with status=CREATED
                task=tc["args"]["task"],
                tool_call_id=tc["id"],
                run_id=run_id,
                status="created",  # NOT queued - workers won't pick up yet
            )
            created_jobs.append({"job": job, "tool_call_id": tc["id"]})

        # Return info for Phase 3 (barrier creation happens in supervisor_service)
        return tool_results, {
            "type": "workers_pending",
            "created_jobs": created_jobs,  # Still CREATED status
        }

    return tool_results, None
```

**Phase 3 (in supervisor_service.py):** After receiving interrupt:
```python
# Create barrier FIRST
barrier = WorkerBarrier(run_id=run.id, expected_count=len(created_jobs))
db.add(barrier)
db.flush()

# Create BarrierJob records with tool_call_id mapping
for job_info in interrupt_value["created_jobs"]:
    db.add(BarrierJob(
        barrier_id=barrier.id,
        job_id=job_info["job"].id,
        tool_call_id=job_info["tool_call_id"],
        status="queued",
    ))

# NOW flip jobs to QUEUED (workers can pick them up)
for job_info in interrupt_value["created_jobs"]:
    job_info["job"].status = "queued"

db.commit()  # All in one transaction
```

**Also remove:** Validation that forces `spawn_worker` to be alone (lines 850-859)

### Phase 3: Barrier-Based Resume (Atomic)

**File:** `apps/zerg/backend/zerg/services/worker_resume.py`

New function: `check_and_resume_if_all_complete()`

**Critical: Single atomic transaction to prevent double-resume**

```python
async def check_and_resume_if_all_complete(db, run_id, job_id, result, error=None):
    """
    Atomic barrier check. Only ONE worker triggers resume.

    Uses SELECT FOR UPDATE + status guard in single transaction.
    """
    with db.begin():  # Transaction boundary
        # 1. Lock the barrier row
        barrier = (
            db.query(WorkerBarrier)
            .filter(WorkerBarrier.run_id == run_id)
            .with_for_update()  # Row lock
            .first()
        )

        if not barrier or barrier.status != "waiting":
            return {"status": "skipped", "reason": "barrier not waiting"}

        # 2. Update the specific BarrierJob
        barrier_job = (
            db.query(BarrierJob)
            .filter(BarrierJob.barrier_id == barrier.id, BarrierJob.job_id == job_id)
            .first()
        )
        if not barrier_job or barrier_job.status == "completed":
            return {"status": "skipped", "reason": "already completed"}

        barrier_job.status = "completed" if not error else "failed"
        barrier_job.result = result
        barrier_job.error = error
        barrier_job.completed_at = func.now()

        # 3. Increment counter atomically
        barrier.completed_count += 1

        # 4. Check if ALL complete AND claim resume atomically
        if barrier.completed_count >= barrier.expected_count:
            barrier.status = "resuming"  # Claim resume (prevents double)
            db.flush()  # Persist within transaction

            # 5. Collect all results for batch resume
            all_jobs = db.query(BarrierJob).filter(BarrierJob.barrier_id == barrier.id).all()
            worker_results = [
                {"tool_call_id": j.tool_call_id, "result": j.result, "error": j.error, "status": j.status}
                for j in all_jobs
            ]

            # Commit happens at end of `with db.begin()`
            return {"status": "resume", "worker_results": worker_results}

        # Not all complete yet
        return {"status": "waiting", "completed": barrier.completed_count, "expected": barrier.expected_count}
```

**Why this works:**
- `FOR UPDATE` locks the barrier row → only one transaction proceeds at a time
- `status = "resuming"` claimed INSIDE the transaction → second worker sees "not waiting"
- All within single transaction → no window for race

### Phase 4: Batch Continuation

**File:** `apps/zerg/backend/zerg/managers/agent_runner.py`

New method: `run_batch_continuation()`
- Takes `worker_results: list[dict]` (multiple results)
- Creates ToolMessage for each result
- Persists all to thread
- Calls `run_supervisor_loop()` with full context

```python
async def run_batch_continuation(self, db, thread, worker_results, ...):
    # Create ToolMessages for ALL worker results
    tool_messages = []
    for wr in worker_results:
        tool_msg = ToolMessage(
            content=f"Worker completed:\n\n{wr['result']}",
            tool_call_id=wr['tool_call_id'],
            name="spawn_worker",
        )
        tool_messages.append(tool_msg)
        self.thread_service.save_new_messages(db, thread.id, [tool_msg])

    # Continue supervisor with all results visible
    return await self._run_supervisor_loop(...)
```

### Phase 5: Worker Runner Integration

**File:** `apps/zerg/backend/zerg/services/worker_runner.py`

Replace direct resume call (lines 344-350) with barrier check:

```python
# Current:
await self._resume_supervisor_if_waiting(db, run_id, ...)

# New:
await check_and_resume_if_all_complete(db, run_id, job_id)
```

### Phase 6: Create PendingWorkerSet on Interrupt

**File:** `apps/zerg/backend/zerg/services/supervisor_service.py`

When handling `AgentInterrupted` with `type="workers_pending"`:
- Create `PendingWorkerSet` record
- Store all `job_ids` from interrupt payload

### Phase 7: Timeout & Deadline Handling

**Problem:** If one worker hangs, barrier waits forever.

**Solution:** `deadline_at` field + background reaper

```python
# In supervisor_service.py when creating barrier:
barrier = WorkerBarrier(
    run_id=run.id,
    expected_count=len(created_jobs),
    deadline_at=datetime.utcnow() + timedelta(minutes=10),  # Configurable
)

# Background task (can run via Sauron or asyncio task):
async def reap_expired_barriers():
    expired = db.query(WorkerBarrier).filter(
        WorkerBarrier.status == "waiting",
        WorkerBarrier.deadline_at < datetime.utcnow()
    ).all()

    for barrier in expired:
        # Collect whatever results we have
        jobs = db.query(BarrierJob).filter(BarrierJob.barrier_id == barrier.id).all()
        for job in jobs:
            if job.status == "created" or job.status == "queued":
                job.status = "timeout"
                job.error = "Worker timed out"

        barrier.status = "resuming"
        # Trigger resume with partial results + timeout errors
        await _batch_resume_supervisor(db, barrier.run_id, barrier)
```

### Phase 8: Guardrails (Prevent Infinite Respawns)

```python
# Config limits
MAX_WORKERS_PER_RUN = 20
MAX_RETRIES_PER_TOOL_CALL = 3

# Track in BarrierJob:
attempt_count = Column(Integer, default=1)

# In spawn logic, check:
if total_workers_this_run >= MAX_WORKERS_PER_RUN:
    return ToolMessage(content="<tool-error>Max workers exceeded</tool-error>", ...)

# In retry logic:
if attempt_count >= MAX_RETRIES_PER_TOOL_CALL:
    # Don't respawn, include error in final results
```

## Files to Modify

| File | Changes |
|------|---------|
| `models/worker_barrier.py` | **New** - WorkerBarrier + BarrierJob models |
| `models/__init__.py` | Export new models |
| `services/supervisor_react_engine.py` | Parallel tool execution, remove spawn_worker-alone validation, session factory pattern |
| `services/worker_resume.py` | Atomic barrier-based resume logic |
| `services/worker_runner.py` | Call barrier check instead of direct resume |
| `services/supervisor_service.py` | Two-phase commit: create barrier, flip jobs to QUEUED |
| `managers/agent_runner.py` | `run_batch_continuation()` method |
| `tools/builtin/supervisor_tools.py` | Create jobs as CREATED (not QUEUED) |
| Alembic migration | Two new tables: worker_barriers, barrier_jobs |

## Example Flow

**User:** "Check disk space on cube, clifford, and bremen"

| Time | Event | Run State |
|------|-------|-----------|
| T+0s | LLM returns 3 spawn_worker calls | RUNNING |
| T+0.1s | Jobs 1,2,3 queued | RUNNING |
| T+0.2s | PendingWorkerSet created | RUNNING |
| T+0.3s | AgentInterrupted raised | WAITING |
| T+1s | Workers 1,2,3 start (parallel) | WAITING |
| T+30s | Worker 2 completes → barrier: 1/3 | WAITING |
| T+45s | Worker 1 completes → barrier: 2/3 | WAITING |
| T+60s | Worker 3 completes → barrier: 3/3 | WAITING |
| T+60.1s | All complete, batch resume | RUNNING |
| T+62s | LLM synthesizes answer | SUCCESS |

**Latency improvement:** 60s (parallel) vs 135s (serial: 30+45+60)

## Failure Handling: Adaptive Recovery

**Philosophy:** The supervisor LLM handles failures intelligently, not the infrastructure.

When a worker fails, include the failure context in the ToolMessage and let the supervisor decide:

```python
# In batch resume, include failure metadata:
ToolMessage(
    content=f"""Worker failed:
Task: {task}
Error: {error}
Attempt: {attempt_count}
Error type: {classify_error(error)}  # "transient", "prompt_issue", "fatal"
""",
    tool_call_id=tool_call_id,
    name="spawn_worker",
)
```

**Supervisor prompt guidance** (add to system prompt):
```
When a worker fails:
- If error is transient (timeout, rate limit): respawn with same task
- If error suggests prompt issue (tool not found, invalid args): respawn with clarified task
- If error is fatal (auth, permissions): report to user, continue with other results
- If multiple workers fail: synthesize what you have, note gaps
```

**The LLM decides** - not hardcoded retry logic. This keeps infrastructure simple while enabling smart recovery.

### Error Classification Helper

```python
def classify_error(error: str) -> str:
    """Hint for LLM about error type."""
    transient = ["timeout", "rate limit", "503", "connection reset"]
    prompt = ["tool not found", "invalid argument", "missing required"]

    error_lower = error.lower()
    if any(t in error_lower for t in transient):
        return "transient"
    if any(p in error_lower for p in prompt):
        return "prompt_issue"
    return "unknown"
```

### Respawn Flow

When supervisor decides to respawn:
1. Calls `spawn_worker` again with modified task
2. New job created (different tool_call_id)
3. New barrier check starts
4. Original failed result stays in context (LLM learns from it)

## Verification

### Unit Tests

| Test | What it verifies |
|------|------------------|
| `test_parallel_tool_execution` | asyncio.gather executes tools concurrently (mock timings) |
| `test_two_phase_spawn` | Jobs created as CREATED, flipped to QUEUED only after barrier exists |
| `test_barrier_atomic_resume` | Only ONE worker triggers resume when multiple complete simultaneously |
| `test_fast_worker_race` | Worker completing before barrier exists doesn't break (handled by two-phase) |
| `test_batch_continuation` | Multiple ToolMessages injected in correct order |
| `test_timeout_reaper` | Expired barriers resume with partial results + timeout errors |
| `test_max_workers_guardrail` | Exceeding MAX_WORKERS_PER_RUN returns error |

### Race Condition Tests (Critical)

```python
async def test_double_resume_prevented():
    """Two workers completing simultaneously should only trigger one resume."""
    # Create barrier with 2 workers
    # Simulate both workers completing at "same time" (concurrent calls)
    results = await asyncio.gather(
        check_and_resume_if_all_complete(db, run_id, job_1, "result1"),
        check_and_resume_if_all_complete(db, run_id, job_2, "result2"),
    )
    # Exactly ONE should return {"status": "resume"}
    resume_count = sum(1 for r in results if r["status"] == "resume")
    assert resume_count == 1
```

### Integration Test

```bash
# Spawn 3 workers, verify parallelism
make test-e2e-single TEST="tests/parallel-workers.spec.ts"
```

Test assertions:
- All 3 workers start within 1 second of each other (parallel, not serial)
- Resume happens only after all 3 complete
- Single LLM call receives all 3 results
- Trace debugger shows parallel execution in timeline

### Manual E2E

```
User: "Check uptime on cube, clifford, and zerg servers"
→ UI shows 3 workers spawned (parallel in timeline)
→ Single response synthesizes all 3 results
→ Total latency ≈ max(worker_times), not sum(worker_times)
```
