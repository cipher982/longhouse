# Resumable SSE v1: Durable Event Streaming with Replay

> **Status**: Implemented
> **Implementation Date**: 2025-12-26
> **Date**: 2025-12-26
> **Authors**: Code Analysis + Developer Report Validation
> **Depends on**: docs/specs/durable-runs-v2.2.md

## Executive Summary

This spec adds **durable event logging** to Zerg's streaming architecture, enabling SSE streams to survive disconnects and replays events from any point. All supervisor/worker events are persisted to a database table, allowing clients to reconnect and catch up on missed events without losing context.

### What This Fixes

**Current Reality (Validated):**
- Events only exist in-memory during SSE connection
- Disconnect = lost events forever
- No way to replay missed events
- Token streaming is ephemeral (not stored)
- Duplicate event emissions at different layers

**Post-Implementation:**
- All events stored durably in `agent_run_events` table
- SSE streams can reconnect and replay from `last_event_id`
- Token streams persisted and replayable for conversation history
- Single emit path prevents duplicates
- Complete event audit trail for debugging

---

## Decision Log

### Decision 1: Keep Numeric `run_id` (No Public ID Migration)
**Status**: ACCEPTED

**Reasoning:**
- All current code uses numeric `run_id` (int) extensively
- Adding `run_public_id` would require:
  - New column + index + unique constraint
  - Migration of all event payloads
  - Frontend changes to use public ID in URLs
  - Zero security benefit (auth already enforces owner_id)
- **Multi-tenancy is enforced at query level** (owner_id filter), not through ID obscurity

**Implications:**
- Continue using `run_id: int` in all APIs and events
- Event table uses `run_id` (integer FK to `agent_runs.id`)
- Security relies on `owner_id` filtering (already implemented)

### Decision 2: Token Streaming IS Durable
**Status**: ACCEPTED (User Requirement)

**User Quote:**
> "everything needs to be durable and persist perfectly, log all"

**Reasoning:**
- User explicitly wants complete conversation audit trail
- Tokens enable:
  - Replay exact LLM output during reconnect
  - Debugging hallucinations/prompt issues
  - Cost analysis (token counts per run)
  - Compliance/audit requirements
- Storage cost is acceptable (~1-2KB per message, compressed)

**Implications:**
- `SUPERVISOR_TOKEN` events ARE persisted to `agent_run_events`
- Each token chunk gets a separate event row (with sequence number)
- Frontend can replay token stream on reconnect for smooth UX
- Option to filter tokens in replay queries if not needed

**Implementation Note:**
- Token events include `sequence` field for ordering
- Token events have `event_type = "supervisor_token"`
- Replays can skip tokens via query param: `/stream?include_tokens=false`

### Decision 3: Emit Once at Source (Eliminate Duplicates)
**Status**: ACCEPTED

**Validated Issue:**
- `WorkerJobProcessor._process_job_by_id()` emits `WORKER_STARTED` (line 125-135)
- `WorkerRunner.run_worker()` ALSO emits `WORKER_STARTED` when `event_context` provided (line 171-181)
- Both code paths execute for the same worker spawn

**Root Cause:**
- WorkerJobProcessor controls lifecycle (queued → running → success)
- WorkerRunner executes the work
- Both emit events independently → duplicate `WORKER_STARTED`

**Solution:**
- **Remove** duplicate emission from `WorkerJobProcessor` (lines 125-135)
- **Keep** emission in `WorkerRunner` (authoritative source)
- WorkerJobProcessor only updates DB status, doesn't emit events
- All events flow through `emit_run_event()` helper (single path)

---

## Validated Root Causes

After examining the actual code, here's what's confirmed:

### ✅ Root Cause #1: No Durable Event Log (VALIDATED)

**File**: `apps/zerg/backend/zerg/routers/jarvis_sse.py`

The `stream_run_events()` generator (lines 29-168) is **purely a live forwarder**:
- Subscribes to EventBus on connect (lines 63-76)
- Queues events as they arrive (line 60)
- Yields to SSE client (lines 125-136)
- Unsubscribes on disconnect (lines 153-167)

**No persistence mechanism exists.** Events not consumed during connection are lost.

### ✅ Root Cause #2: EventBus Returns Early with No Subscribers (VALIDATED)

**File**: `apps/zerg/backend/zerg/events/event_bus.py` (lines 80-90)

```python
async def publish(self, event_type: EventType, data: Dict[str, Any]) -> None:
    subscriber_count = len(self._subscribers.get(event_type, set()))
    if subscriber_count == 0:
        logger.debug("No subscribers for %s", event_type)
        return  # ← DROPS THE EVENT
```

If an SSE client disconnects before an event fires, or if a DEFERRED run completes after the stream closed, the event is **silently dropped**.

### ✅ Root Cause #3: Duplicate Publishers (VALIDATED)

**Duplicate `WORKER_STARTED` emissions:**

1. **WorkerJobProcessor** (lines 125-135):
   ```python
   await event_bus.publish(
       EventType.WORKER_STARTED,
       {"event_type": EventType.WORKER_STARTED, "job_id": job.id, ...}
   )
   ```

2. **WorkerRunner** (lines 171-181):
   ```python
   await self._emit_event(
       EventType.WORKER_STARTED,
       {"event_type": EventType.WORKER_STARTED, "job_id": job_id, ...}
   )
   ```

Both execute for the same worker spawn, causing duplicate events on SSE streams.

### ✅ Root Cause #4: DEFERRED Runs Not Treated as Attachable (VALIDATED)

**File**: `apps/zerg/backend/zerg/routers/jarvis_runs.py` (lines 300-310)

The `/runs/{run_id}/stream` endpoint checks:
```python
if run.status == RunStatus.RUNNING:
    return EventSourceResponse(stream_run_events(...))
else:
    # Return single completion event and close
```

**Problem**: DEFERRED runs have `status == RunStatus.DEFERRED`, so they fall into the "completed" path and return a single event instead of streaming live events. Clients can't attach to background work in progress.

### ✅ Root Cause #5: JSON Validity Enforced at Edge (VALIDATED)

**File**: `apps/zerg/backend/zerg/routers/jarvis_sse.py` (lines 122-136)

Events are serialized via `json.dumps(..., default=_json_default)` at SSE yield time. If serialization fails (e.g., non-JSON-safe object in payload), the **entire stream crashes** with a 500 error.

**Problem**: No validation happens at `event_bus.publish()` time. Bad payloads can be emitted deep in supervisor/worker code and only fail when a client connects.

---

## Architecture

### Event Storage Schema

**New Table**: `agent_run_events`

```sql
CREATE TABLE agent_run_events (
    id BIGSERIAL PRIMARY KEY,

    -- Foreign keys
    run_id INTEGER NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,

    -- Event metadata
    event_type VARCHAR(50) NOT NULL,  -- supervisor_started, worker_complete, etc.
    sequence INTEGER NOT NULL,        -- Monotonic sequence within run

    -- Event payload (JSONB for efficient querying)
    payload JSONB NOT NULL,           -- Full event data

    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),

    -- Indexes for efficient queries
    CONSTRAINT agent_run_events_unique_seq UNIQUE (run_id, sequence)
);

CREATE INDEX idx_run_events_run_id ON agent_run_events(run_id);
CREATE INDEX idx_run_events_created_at ON agent_run_events(created_at);
CREATE INDEX idx_run_events_type ON agent_run_events(event_type);
```

**Payload Structure:**
```json
{
  "event_type": "supervisor_token",
  "run_id": 42,
  "owner_id": 1,
  "token": "Hello",
  "timestamp": "2025-12-26T10:30:45.123Z"
}
```

**Key Design Choices:**
- `sequence` ensures ordering within a run (critical for token replay)
- `JSONB` enables efficient filtering (`WHERE payload->>'worker_id' = ?`)
- `ON DELETE CASCADE` auto-cleans events when runs are deleted
- `created_at` uses `DEFAULT NOW()` for reliability (no app-side timestamp bugs)

### Event Emission Flow

**Single Emit Path** (eliminates duplicates):

```
Service Layer (supervisor_service.py, worker_runner.py)
    │
    └─→ emit_run_event(run_id, event_type, payload)
            │
            ├─→ Validate JSON serializability (fail fast)
            ├─→ Insert into agent_run_events (with sequence)
            ├─→ EventBus.publish() for live SSE
            └─→ Return event_id (for client acknowledgment)
```

**Helper Function** (new file: `apps/zerg/backend/zerg/services/event_store.py`):

```python
async def emit_run_event(
    db: Session,
    run_id: int,
    event_type: str,
    payload: dict[str, Any],
) -> int:
    """Emit a run event with durable storage.

    This is the ONLY way to emit run events. All other code should call this.

    Args:
        db: Database session
        run_id: Run identifier
        event_type: Event type (supervisor_started, worker_complete, etc.)
        payload: Event data (must be JSON-serializable)

    Returns:
        event_id: Database ID of the persisted event

    Raises:
        ValueError: If payload is not JSON-serializable
    """
    # 1. Validate JSON serializability (fail fast)
    try:
        json.dumps(payload, default=_json_default)
    except (TypeError, ValueError) as e:
        logger.error(f"Event payload not JSON-serializable: {e}")
        raise ValueError(f"Invalid event payload: {e}")

    # 2. Get next sequence number (atomic)
    max_seq = db.query(func.max(AgentRunEvent.sequence)).filter(
        AgentRunEvent.run_id == run_id
    ).scalar() or 0
    next_seq = max_seq + 1

    # 3. Insert into database
    event = AgentRunEvent(
        run_id=run_id,
        event_type=event_type,
        sequence=next_seq,
        payload=payload,
    )
    db.add(event)
    db.commit()
    db.refresh(event)

    # 4. Publish to live subscribers
    await event_bus.publish(EventType(event_type), payload)

    logger.debug(f"Emitted {event_type} (seq={next_seq}) for run {run_id}")
    return event.id
```

### Streaming Endpoint (New Design)

**Endpoint**: `GET /api/stream/runs/{run_id}`

**Query Parameters:**
- `after_event_id` (optional): Resume from event ID (default: 0 = from start)
- `after_sequence` (optional): Resume from sequence number (alternative to event_id)
- `include_tokens` (optional): Include `SUPERVISOR_TOKEN` events (default: true)

**Behavior:**

```python
@router.get("/stream/runs/{run_id}")
async def stream_run_replay(
    run_id: int,
    after_event_id: int = 0,
    after_sequence: int = 0,
    include_tokens: bool = True,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_jarvis_user),
):
    """Stream run events with replay support.

    If run is complete: Replays historical events and closes.
    If run is active: Replays historical + streams live events.
    """
    # Security: verify ownership
    run = db.query(AgentRun).join(Agent).filter(
        AgentRun.id == run_id,
        Agent.owner_id == current_user.id
    ).first()

    if not run:
        raise HTTPException(404, "Run not found")

    return EventSourceResponse(
        _replay_and_stream(
            db, run_id, run.status,
            after_event_id, after_sequence, include_tokens
        )
    )

async def _replay_and_stream(
    db: Session,
    run_id: int,
    status: RunStatus,
    after_event_id: int,
    after_sequence: int,
    include_tokens: bool,
):
    """Replay historical events, then stream live if run is active."""

    # 1. Replay historical events
    query = db.query(AgentRunEvent).filter(AgentRunEvent.run_id == run_id)

    if after_event_id > 0:
        query = query.filter(AgentRunEvent.id > after_event_id)
    elif after_sequence > 0:
        query = query.filter(AgentRunEvent.sequence > after_sequence)

    if not include_tokens:
        query = query.filter(AgentRunEvent.event_type != "supervisor_token")

    historical = query.order_by(AgentRunEvent.sequence).all()

    for event in historical:
        yield {
            "id": str(event.id),  # SSE last-event-id
            "event": event.event_type,
            "data": json.dumps({
                "type": event.event_type,
                "payload": event.payload,
                "timestamp": event.created_at.isoformat(),
            }, default=_json_default),
        }

    # 2. If run is complete, close stream
    if status not in (RunStatus.RUNNING, RunStatus.DEFERRED):
        return

    # 3. Stream live events (same as current stream_run_events)
    async for live_event in stream_run_events(run_id, owner_id):
        yield live_event
```

**Key Features:**
- **Idempotent reconnects**: Client sends `Last-Event-ID` header, server replays from that point
- **Token filtering**: Can exclude tokens for bandwidth (UI pagination uses this)
- **Hybrid replay**: Historical + live in one stream (seamless UX)

---

## Implementation Status

All phases have been completed as of 2025-12-26.

### Nginx Configuration (Phase 5)

The new `/api/stream/` endpoint has been configured in nginx with proper SSE handling:

**Configuration Files:**
- `docker/nginx/docker-compose.unified.conf` (dev environment)
- `docker/nginx/docker-compose.prod.conf` (production environment)

**SSE Settings Applied:**
- `proxy_buffering off` - Disables response buffering
- `proxy_cache off` - Disables caching
- `proxy_read_timeout 86400s` - 24-hour timeout for long-lived connections
- `proxy_send_timeout 86400s` - 24-hour send timeout
- `chunked_transfer_encoding off` - Required for proper SSE streaming
- `Connection ''` header - Prevents connection upgrade interference
- `X-Accel-Buffering no` - Additional nginx buffering control

**Backward Compatibility:**
Legacy SSE endpoints (`/api/jarvis/supervisor/events`, `/api/jarvis/chat`, `/api/jarvis/runs/<id>/stream`) remain configured and functional.

### JSON Serialization Workaround

The `_json_default()` function remains in use across all SSE endpoints:
- **Event Store** (`event_store.py`): Uses it for emit-time validation
- **SSE Routers** (`jarvis_sse.py`, `stream.py`): Uses it for stream-time serialization
- **Purpose**: Provides consistent datetime serialization and graceful degradation
- **Status**: This is intentional defense-in-depth, not a workaround to be removed

## Implementation Phases

### Phase 1: Event Store Foundation
**Goal**: Add durable storage without changing existing behavior.

**Tasks:**
1. Create `agent_run_events` table migration (Alembic)
2. Create `AgentRunEvent` SQLAlchemy model
3. Implement `emit_run_event()` helper function
4. Add `EventStore` service class for queries
5. Write unit tests for event storage

**Success Criteria:**
- Events can be stored and queried
- No changes to SSE behavior yet (live-only still works)

### Phase 2: Replace Event Publishers
**Goal**: Migrate all event emissions to use `emit_run_event()`.

**Tasks:**
1. Audit all `event_bus.publish()` calls (30 files found via grep)
2. Replace supervisor event emissions:
   - `supervisor_service.py`: All SUPERVISOR_* events
   - `zerg_react_agent.py`: SUPERVISOR_TOKEN, SUPERVISOR_HEARTBEAT
3. Replace worker event emissions:
   - `worker_runner.py`: WORKER_STARTED, WORKER_COMPLETE, WORKER_SUMMARY_READY
   - Remove duplicate from `worker_job_processor.py`
4. Tool events: WORKER_TOOL_STARTED/COMPLETED/FAILED
5. Update tests to check database, not just EventBus

**Success Criteria:**
- All run events persist to database
- No duplicate events in logs/tests
- Existing SSE streams still work (backward compatible)

### Phase 3: Resumable SSE Endpoint
**Goal**: Enable reconnects with replay.

**Tasks:**
1. Create `/api/stream/runs/{run_id}` endpoint
2. Implement `_replay_and_stream()` generator
3. Handle `Last-Event-ID` header from EventSource clients
4. Add integration tests for reconnect scenarios
5. Update frontend to use new endpoint

**Success Criteria:**
- Client disconnects and reconnects, sees all events
- Token streams replay on reconnect
- DEFERRED runs are streamable (not just completed)

### Phase 4: Frontend Integration
**Goal**: Use resumable streams in Jarvis chat.

**Tasks:**
1. Update `EventSource` URL to `/api/stream/runs/{run_id}`
2. Add reconnect logic with `Last-Event-ID`
3. Show "reconnecting..." state during network blips
4. Handle token replay (don't duplicate UI messages)
5. Add E2E tests for disconnect/reconnect

**Success Criteria:**
- Page refresh doesn't lose conversation context
- Network blips recover gracefully
- Token streaming shows complete message on reconnect

### Phase 5: DEFERRED Run Attachability
**Goal**: Fix DEFERRED runs not being streamable.

**Tasks:**
1. Update `/runs/{run_id}/stream` to treat DEFERRED as RUNNING
2. Test attaching to a run that's been deferred
3. Verify continuation runs emit events to same stream
4. Add E2E test: start run → timeout → reconnect → see continuation

**Success Criteria:**
- DEFERRED runs stream live events
- Continuation runs appear in same stream
- No "run already complete" errors for background work

---

## Migration Guide

### For Developers

**Before (Current Code):**
```python
# In supervisor_service.py
await event_bus.publish(
    EventType.SUPERVISOR_STARTED,
    {
        "event_type": EventType.SUPERVISOR_STARTED,
        "run_id": run.id,
        "task": task,
        "owner_id": owner_id,
    }
)
```

**After (Resumable SSE v1):**
```python
# In supervisor_service.py
from zerg.services.event_store import emit_run_event

await emit_run_event(
    db=self.db,
    run_id=run.id,
    event_type="supervisor_started",
    payload={
        "task": task,
        "owner_id": owner_id,
    }
)
```

**Key Changes:**
- `emit_run_event()` instead of `event_bus.publish()`
- `db` session required (for persistence)
- `event_type` is a string (not enum)
- `run_id` is a parameter (not in payload)
- Payload validation happens at emit time (fail fast)

### For Frontend

**Before:**
```typescript
// Connect to SSE stream
const eventSource = new EventSource(`/api/jarvis/chat`);
```

**After:**
```typescript
// Connect to resumable stream
const eventSource = new EventSource(
  `/api/stream/runs/${runId}?include_tokens=true`
);

// Automatic reconnect with Last-Event-ID
// (EventSource handles this natively)
```

---

## Validation Against Report

### Report Claim #1: "No durable event log"
**VALIDATED**: `stream_run_events()` is purely live forwarding, no persistence.

### Report Claim #2: "EventBus.publish() drops events with no subscribers"
**VALIDATED**: Lines 87-90 in `event_bus.py` confirm early return.

### Report Claim #3: "Duplicate WORKER_STARTED publishers"
**VALIDATED**: Both `WorkerJobProcessor` (line 125) and `WorkerRunner` (line 171) emit.

### Report Claim #4: "DEFERRED runs not attachable"
**VALIDATED**: Line 300 in `jarvis_runs.py` treats DEFERRED as completed.

### Report Claim #5: "JSON validity enforced at edge"
**VALIDATED**: `_json_default()` at line 16 catches issues at SSE yield time, not emit time.

All root causes from the report are **confirmed by code inspection**.

---

## Success Criteria (v1)

1. **No lost events**: All supervisor/worker events persist to database
2. **Reconnect works**: Client disconnect → reconnect → sees all events
3. **Token durability**: Token streams replay on reconnect
4. **DEFERRED streams**: Background work is attachable
5. **No duplicates**: Single emit path, no redundant events
6. **Fail fast**: JSON validation at emit time, not stream time

---

## Future Work (Post-v1)

### Event Retention Policy
- Auto-delete events older than 30 days (via cron/scheduler)
- Compress token events (gzip payloads) for long-term storage

### Event Filtering API
- `/api/stream/runs/{run_id}?event_types=worker_complete,supervisor_complete`
- Useful for dashboards that only care about status changes

### Checkpoint Snapshots
- Store "checkpoint" events that summarize run state
- Enables "resume from checkpoint" without replaying all events

### Cross-Run Queries
- `/api/events?run_ids=1,2,3` (multi-run event stream)
- Useful for debugging concurrent worker spawns

---

## References

- **EventBus Implementation**: `apps/zerg/backend/zerg/events/event_bus.py`
- **Current SSE Streaming**: `apps/zerg/backend/zerg/routers/jarvis_sse.py`
- **Supervisor Events**: `apps/zerg/backend/zerg/services/supervisor_service.py`
- **Worker Events**: `apps/zerg/backend/zerg/services/worker_runner.py`
- **Duplicate Publisher**: `apps/zerg/backend/zerg/services/worker_job_processor.py` (line 125)
- **Durable Runs v2.2**: `docs/specs/durable-runs-v2.2.md`
