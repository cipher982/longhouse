# Chat Observability & Eval System

**Status:** Phase 1 Complete
**Created:** 2025-12-27
**Last Updated:** 2025-12-27
**Protocol:** SDP-1

## Executive Summary

The Jarvis chat system lacks systematic observability for understanding and optimizing response latency. Current logging is noisy (many low-signal events), timestamps are inconsistent (regenerated at stream time vs event time), and there's no correlation ID to trace requests end-to-end.

This spec defines:
1. **Structured timeline logging** - Clear phase-based timing with correlation IDs
2. **Performance profiling API** - Backend endpoint exposing timing breakdown
3. **E2E eval tests** - Automated profiling with assertions and metric export

## Decision Log

### Decision: Console logging cleanup approach
**Context:** Current logs are noisy with many INFO-level events that obscure timing data
**Choice:** Add a `timeline` log mode separate from existing verbosity levels
**Rationale:** Preserves existing logging for debugging while enabling clean timeline view
**Revisit if:** Timeline mode proves useful enough to become the default

### Decision: Correlation ID strategy
**Context:** Need to trace requests across frontend → backend → worker → SSE
**Choice:** Generate UUID on frontend at message send, propagate through all events
**Rationale:** Frontend origin ensures ID is available before any backend call
**Revisit if:** Multiple frontends need coordinated IDs (unlikely for single-user chat)

### Decision: Timing capture points
**Context:** Need to know where time is spent without adding excessive instrumentation
**Choice:** Capture 6 key phases: send, backend_received, supervisor_started, worker_spawned, worker_complete, supervisor_complete
**Rationale:** Covers the major phases visible in the console log example provided
**Revisit if:** More granular tool-level timing needed

### Decision: Eval test approach
**Context:** Could do unit tests, integration tests, or full E2E browser tests
**Choice:** E2E Playwright tests that capture real timing data and export metrics
**Rationale:** Matches existing test infrastructure, captures true user-facing latency
**Revisit if:** E2E tests prove too slow/flaky for CI

### Decision: Metrics storage
**Context:** Could use database, files, or external service
**Choice:** JSON file export per test run in `apps/zerg/e2e/metrics/`
**Rationale:** Simple, git-ignorable, can be consumed by any dashboard later
**Revisit if:** Need historical trending across many runs

## Architecture

### 1. Correlation ID Flow

```
Frontend (message send)
│
├─ Generate correlationId (UUID)
├─ Include in POST /api/jarvis/chat body
│
Backend (jarvis_supervisor.py)
│
├─ Extract correlationId from request
├─ Store on AgentRun record
├─ Include in ALL SSE events for this run
│
Worker (zerg_react_agent.py)
│
├─ Receive correlationId in job context
├─ Include in tool events
│
Frontend (SSE listener)
│
└─ Match events by correlationId
└─ Build timeline with consistent ID
```

### 2. Timeline Event Structure

Each phase emits a structured timing event:

```typescript
interface TimelineEvent {
  correlationId: string;      // UUID linking all events
  phase: string;              // e.g., "send", "supervisor_started"
  timestamp: number;          // Unix ms (Date.now())
  serverTimestamp?: string;   // ISO 8601 from backend
  metadata?: {
    runId?: number;
    workerId?: string;
    toolName?: string;
    durationMs?: number;      // For completion events
  };
}
```

### 3. Timeline Log Format

New console output mode showing clean timeline:

```
[Timeline] correlationId=abc123
  T+0ms      send              Message dispatched
  T+45ms     backend_received  run_id=1
  T+120ms    supervisor_started
  T+850ms    worker_spawned    job_id=1
  T+1200ms   worker_started    worker_id=xyz
  T+1500ms   tool_started      ssh_exec
  T+2100ms   tool_completed    ssh_exec (600ms)
  T+2800ms   worker_complete   (1600ms total)
  T+3200ms   supervisor_complete (3155ms total)
```

### 4. Backend Timing Endpoint

New endpoint for programmatic timing access:

```
GET /api/jarvis/runs/{run_id}/timeline
Authorization: Bearer <token>

Response:
{
  "correlationId": "abc123",
  "runId": 1,
  "events": [
    {"phase": "supervisor_started", "timestamp": "2025-12-27T16:28:24.000Z", "offsetMs": 0},
    {"phase": "worker_spawned", "timestamp": "2025-12-27T16:28:24.850Z", "offsetMs": 850},
    ...
  ],
  "summary": {
    "totalDurationMs": 3200,
    "supervisorThinkingMs": 730,
    "workerExecutionMs": 1600,
    "toolExecutionMs": 600
  }
}
```

### 5. E2E Eval Test Structure

```typescript
// apps/zerg/e2e/tests/chat_performance_eval.spec.ts

test('chat response latency - simple query', async ({ page }) => {
  const timeline = new TimelineCapture(page);

  // Send message and capture all timing events
  await timeline.start();
  await sendMessage(page, 'hi there');
  await waitForResponse(page);
  const events = await timeline.stop();

  // Assertions
  expect(events.totalDurationMs).toBeLessThan(5000);
  expect(events.phases.supervisor_started.offsetMs).toBeLessThan(500);

  // Export metrics
  await timeline.exportMetrics('simple-query');
});

test('chat response latency - with worker', async ({ page }) => {
  const timeline = new TimelineCapture(page);

  await timeline.start();
  await sendMessage(page, 'check disk space on cube, be quick');
  await waitForResponse(page);
  const events = await timeline.stop();

  // Worker-specific assertions
  expect(events.phases.worker_spawned).toBeDefined();
  expect(events.phases.worker_complete.durationMs).toBeLessThan(30000);

  await timeline.exportMetrics('worker-query');
});
```

## Implementation Phases

### Phase 1: Correlation ID Infrastructure ✅ COMPLETE
**Goal:** Add correlation ID propagation through the entire system

**Status:** Complete (2025-12-27)

**Changes Implemented:**
- Frontend: UUID generation already in place (useTextChannel.ts line 69)
- Backend: Added `correlation_id` column to AgentRun model (apps/zerg/backend/zerg/models/run.py)
- Backend: Store correlationId from request in jarvis_chat.py endpoint
- Backend: correlationId already included in ALL SSE events via jarvis_sse.py (line 131)
- Frontend: correlationId already parsed from SSE wrapper (supervisor-chat-controller.ts line 432)

**Acceptance Criteria:**
- [x] Message send generates unique correlationId (useTextChannel.ts generates UUID)
- [x] correlationId sent in POST /api/jarvis/chat request body
- [x] correlationId stored on AgentRun record
- [x] correlationId appears in SSE connected event (ConnectedPayload.client_correlation_id)
- [x] correlationId appears in all SSE event wrappers (SSEEventWrapper.client_correlation_id)
- [x] correlationId appears in supervisor_complete event
- [x] correlationId appears in worker_* events (when workers spawn)

**Test:** `make test-e2e-single TEST="tests/chat_correlation_id.spec.ts"`

**Implementation Notes:**
- Much of the infrastructure was already in place; Phase 1 primarily added database persistence
- The `client_correlation_id` field was already defined in SSE schema and included in events
- Frontend already had UUID generation and backend parsing
- Key addition: `correlation_id` column on AgentRun for persistent storage and future querying

### Phase 2: Timeline Logging (Frontend)
**Goal:** Add clean timeline console output

**Changes:**
- Add TimelineLogger class in `src/jarvis/lib/timeline-logger.ts`
- Capture events from eventBus with timestamps
- Calculate offsets from first event
- Output formatted timeline on completion
- URL param `?timeline=true` to enable

**Acceptance Criteria:**
- [ ] `?timeline=true` shows condensed timeline in console
- [ ] Timeline shows T+offset for each phase
- [ ] Timeline shows durations for completion events
- [ ] Existing verbose logging unchanged when timeline disabled

**Test:** Manual verification with `?timeline=true`

### Phase 3: Backend Timeline Endpoint
**Goal:** Expose timing data via API

**Changes:**
- Add `GET /api/jarvis/runs/{run_id}/timeline` endpoint
- Query agent_run_events for run
- Calculate offsets and summaries
- Return structured timeline response

**Acceptance Criteria:**
- [ ] Endpoint returns timeline for completed run
- [ ] Summary includes totalDurationMs, supervisorThinkingMs, workerExecutionMs
- [ ] Events sorted by timestamp with offsetMs calculated
- [ ] 404 for non-existent run_id

**Test:** `make test` (add backend unit test)

### Phase 4: E2E Performance Eval Tests
**Goal:** Automated profiling with metric export

**Changes:**
- Add `TimelineCapture` helper class in `apps/zerg/e2e/helpers/`
- Add `chat_performance_eval.spec.ts` with timing tests
- Add metrics export to `apps/zerg/e2e/metrics/` (gitignored)
- Add `make test-perf` target

**Acceptance Criteria:**
- [ ] `make test-perf` runs performance eval tests
- [ ] Tests capture full timeline for each scenario
- [ ] Tests assert on key latency thresholds
- [ ] Metrics exported to JSON file
- [ ] CI can optionally run perf tests

**Test:** `make test-perf`

### Phase 5: Log Noise Reduction
**Goal:** Clean up existing logging for better signal

**Changes:**
- Review and adjust log levels in `logger.ts`
- Remove or demote low-signal logs (heartbeat, routine events)
- Add `?log=timeline` as shortcut for timeline-only mode
- Document logging modes in AGENTS.md

**Acceptance Criteria:**
- [ ] Default logging shows less noise
- [ ] `?log=timeline` shows only timeline events
- [ ] `?log=verbose` preserves current behavior for debugging
- [ ] AGENTS.md documents logging options

**Test:** Manual verification

## Files to Modify

### Frontend
- `apps/zerg/frontend-web/src/jarvis/core/logger.ts` - Add timeline mode
- `apps/zerg/frontend-web/src/jarvis/lib/timeline-logger.ts` - New file
- `apps/zerg/frontend-web/src/jarvis/lib/supervisor-chat-controller.ts` - Generate correlationId
- `apps/zerg/frontend-web/src/jarvis/lib/event-bus.ts` - Propagate correlationId

### Backend
- `apps/zerg/backend/zerg/routers/jarvis_supervisor.py` - Extract correlationId, new endpoint
- `apps/zerg/backend/zerg/services/supervisor_service.py` - Store correlationId on run
- `apps/zerg/backend/zerg/models/agent_run.py` - Add correlation_id column (if needed)
- `apps/zerg/backend/zerg/generated/sse_events.py` - Include correlationId in events

### E2E Tests
- `apps/zerg/e2e/helpers/timeline-capture.ts` - New file
- `apps/zerg/e2e/tests/chat_performance_eval.spec.ts` - New file
- `apps/zerg/e2e/metrics/.gitkeep` - New directory

### Config
- `Makefile` - Add `test-perf` target
- `AGENTS.md` - Document logging modes

## Out of Scope

- OpenTelemetry/distributed tracing integration (future)
- Historical metric trending dashboard (future)
- Voice latency profiling (different system)
- Worker-internal timing breakdown (low priority)

## Success Metrics

1. **Developer experience:** Can understand chat latency breakdown in <30 seconds
2. **Test coverage:** Performance regression detected automatically in CI
3. **Noise reduction:** Console logs reduced by 50%+ in default mode
4. **Traceability:** Any event can be traced to original message via correlationId
