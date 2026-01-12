# Messaging/Data Architecture Handoff (Jan 2026)

This document is the final handoff for the recent “event emitter refactor” work and the follow-on architectural conclusions about messaging, streaming, concurrency, and persistence in Zerg/Swarmlet.

It is written to be executable guidance: what happened, what we learned, what’s still risky, and what to do next.

## 0) Goals

Primary goals:

- Eliminate “spooky action at a distance” bugs caused by ambient state crossing async/task/thread boundaries.
- Make the system debuggable: one ordered, replayable run timeline that explains “what happened”.
- Keep complexity low (1 developer, no users yet). Prefer simple rules and hard boundaries over clever plumbing.

Non-goals (for now):

- Introducing Kafka/Redis/NATS/etc. unless forced by scale.
- Perfect durability for every token/heartbeat (nice-to-have, not required).

## 1) Background: the bug class we’re fixing

The recurring bug class:

- **Contextvars are copied** into new tasks (`asyncio.create_task`) and into threads (`asyncio.to_thread`).
- If code uses contextvars to *infer identity* (“am I worker or supervisor?”), or stores *live objects* (SQLAlchemy `Session`, ORM models, clients) in contextvars, concurrency creates misattribution and thread-safety hazards.

Concrete UI symptom (original trigger):

- Supervisor tool events were misclassified as `worker_tool_*` due to contextvar copying into background tasks (notably the worker → supervisor resume path).
- This produced the “spawn_worker (0ms)”/nested-tool UI artifact where supervisor tools appeared in the worker activity feed.

## 2) What we implemented: Injected Emitter Refactor

### The core decision

Stop determining event type from ambient contextvars.

Instead, inject an emitter object whose **identity is baked in at construction**:

- `WorkerEmitter` always emits `worker_tool_*`
- `SupervisorEmitter` always emits `supervisor_tool_*`

Even if `asyncio.create_task()` copies contextvars, the emitter’s identity is stable.

### New files (Phase 1)

- `apps/zerg/backend/zerg/events/emitter_protocol.py`
- `apps/zerg/backend/zerg/events/emitter_context.py`
- `apps/zerg/backend/zerg/events/worker_emitter.py`
- `apps/zerg/backend/zerg/events/supervisor_emitter.py`
- `apps/zerg/backend/zerg/events/null_emitter.py`
- `apps/zerg/backend/tests/test_emitters.py`

### Entry-point wiring (Phase 2)

- `apps/zerg/backend/zerg/services/worker_runner.py` sets `WorkerEmitter`
- `apps/zerg/backend/zerg/services/supervisor_service.py` sets `SupervisorEmitter`
- `apps/zerg/backend/zerg/services/worker_resume.py` sets `SupervisorEmitter` (critical resume fix point)

### Consumer migration (Phase 3)

- `apps/zerg/backend/zerg/agents_def/zerg_react_agent.py` uses `get_emitter()` for tool lifecycle event emission.
- Worker tool tracking (`record_tool_start/complete`, critical error handling) is still on `WorkerContext` as an explicit “Phase 3” transitional state.

## 3) Test status

- `make test` currently passes all but **one pre-existing failure**:
  - `tests/test_supervisor_tools_integration.py::test_supervisor_spawns_worker_via_tool`
  - It fails because the supervisor run is interrupted by `spawn_worker` (expected control flow) and the test expects normal completion.
  - This is unrelated to emitters and was already present.

## 4) Fix made during review: coroutine warning on resume scheduling

While running tests, we observed:

- `RuntimeWarning: coroutine ... was never awaited` originating from scheduling resume with `asyncio.create_task`.

Fix:

- `apps/zerg/backend/zerg/services/worker_runner.py` now closes the coroutine if scheduling fails, preventing the warning.

## 5) The deeper architectural finding (the real enemy)

**The real enemy is ambient mutable state crossing concurrency boundaries**, not “async complexity” itself.

The key failure mode is:

- Tools run concurrently (`asyncio.gather`) and many sync tools run in threads (`asyncio.to_thread`).
- Contextvars are copied into those threads.
- If contextvars (directly or indirectly) carry a request-scoped SQLAlchemy `Session`, it can be used:
  - from the wrong thread (not thread-safe), and/or
  - concurrently by multiple tool threads.

That yields exactly the kind of “it worked until we added parallelism” bugs we keep seeing.

### 80/20 mitigation implemented

We made one major step toward “no Session crossing boundaries”:

- `apps/zerg/backend/zerg/connectors/resolver.py`
  - `CredentialResolver` now **prefetches credentials** at construction and serves lookups from in-memory cache.
  - `clear_cache()` now re-prefetches so tests that mutate credentials mid-run still work.

And we removed some tool-level `resolver.db` usage:

- `apps/zerg/backend/zerg/tools/builtin/connector_tools.py` now opens its own `db_session()`
- `apps/zerg/backend/zerg/tools/builtin/runner_setup_tools.py` now opens its own `db_session()`

Tests adjusted:

- `apps/zerg/backend/zerg/tools/tests/test_connector_tools.py`

## 6) What the repo already has (messaging primitives)

### Canonical durable event log

- `apps/zerg/backend/zerg/models/agent_run_event.py` (`AgentRunEvent`)
- `apps/zerg/backend/zerg/services/event_store.py` (`emit_run_event`, `EventStore`)

### Resumable SSE v1 (replay + live)

- `apps/zerg/backend/zerg/routers/stream.py`
  - `/api/stream/runs/{run_id}`
  - replays from DB with short-lived session, then streams live via in-proc `event_bus`.

### WebSockets

- `apps/zerg/backend/zerg/websocket/manager.py` (topic-based WS broadcasting)
- `apps/zerg/backend/zerg/services/runner_connection_manager.py` (runner WS)

### Token streaming callback

- `apps/zerg/backend/zerg/callbacks/token_stream.py`
  - WS tokens for non-supervisor contexts.
  - publishes supervisor tokens via `event_bus`.
  - contains contextvars for thread/user and (currently) a DB session var.

## 7) External review verdict (high-signal feedback)

Key takeaways:

- **Direction is correct:** one canonical append-only run timeline (Postgres `AgentRunEvent`) + one primary delivery mechanism (resumable SSE) is the “least moving parts that debugs well”.
- **Root cause diagnosis is correct:** ambient mutable state crossing concurrency boundaries is the main enemy.

However, the reviewer correctly called out that we still violate the “no Session in contextvar” principle in multiple places:

- `apps/zerg/backend/zerg/services/supervisor_context.py` stores `db: Session` in a contextvar.
- `apps/zerg/backend/zerg/context.py` stores `db_session` in `WorkerContext` (also contextvar-transported).
- `apps/zerg/backend/zerg/events/worker_emitter.py` and `apps/zerg/backend/zerg/events/supervisor_emitter.py` keep `db: Session` on emitter objects that are carried via contextvars.
- `apps/zerg/backend/zerg/services/event_store.py` requires a `db: Session`, encouraging “pass sessions everywhere”.

Two concrete omissions we should explicitly track:

- **Heartbeats are non-durable** (not just tokens): they’re event_bus-only today and won’t replay.
- **Backpressure risk:** some streams use unbounded queues; a slow client can become a memory leak.

## 8) Final “Carmack-style” principles (what must be true)

These are the system invariants we should enforce going forward:

1) **One source of truth:** the run’s observable history is an ordered, append-only log.
2) **No hidden shared mutability:** anything that can do I/O (DB sessions, ORM objects, clients) must not be ambient or implicitly shared across tasks/threads.
3) **Hard boundaries:** be explicit about the roles:
   - “append to the log”
   - “stream the log”
   - “run tools”
4) **Backpressure is designed, not accidental:** every per-client/per-run queue is bounded with a defined drop/coalesce policy (especially tokens/heartbeats).
5) **Events are self-authenticating:** every event required for routing/security includes `run_id` + `owner_id` (and usually `message_id`, `tool_call_id`, `job_id/worker_id` as relevant).

## 9) Recommended architectural end-state (least complexity that works)

- **Canonical truth:** Postgres `AgentRunEvent` is the run timeline.
- **Primary delivery:** Resumable SSE v1 (`/api/stream/runs/{run_id}`) streams that timeline (replay + live).
- **Artifacts:** keep large blobs on disk (worker artifact store); DB events reference summaries/pointers.
- **WebSockets:** keep only for truly bidirectional channels (e.g., runner connections). Avoid parallel “timeline over WS” and “timeline over SSE” long-term.
- **Contextvars:** allowed for IDs/snapshots only; banned for live I/O resources.

See also: `docs/messaging-architecture-decision.md` (earlier draft).

## 10) Next steps (80/20 plan)

These are the highest-leverage changes to cut off recurring bug sources:

1) **Bound streaming queues**:
   - `apps/zerg/backend/zerg/routers/stream.py` uses `asyncio.Queue()` unbounded; add bounds + drop/coalesce policy.
   - Audit `apps/zerg/backend/zerg/routers/jarvis_sse.py` for similar.
2) **Design out Sessions from context-transported objects**:
   - Remove `Session` from `WorkerContext`, `SupervisorContext`, and emitter objects.
   - Replace “pass session everywhere” with “open short-lived session at the IO boundary”.
3) **Make event emission own its DB lifecycle**:
   - E.g., `append_run_event(run_id, event_type, payload)` opens its own `db_session()` and appends to `AgentRunEvent`.
   - Then publish live via `event_bus` (transport).
4) **Unify on one timeline stream**:
   - Treat `/api/stream/runs/{run_id}` as primary.
   - Gradually deprecate redundant run timeline streaming endpoints.
5) **Keep tokens/heartbeats explicitly best-effort**:
   - Document that they’re non-durable and may be dropped under backpressure.

## 11) Longer-term rewrite plan (preferred if we commit to simplification)

If we choose to “rewrite to simpler” (lines are cheap, complexity isn’t), the direction is:

- Collapse to one run timeline concept (DB log + resumable SSE).
- Remove WS token streaming for supervisor runs (and potentially entirely for UI), keeping WS only where bidirectional control is needed.
- Replace contextvar-stored live objects with explicit boundary-owned resources:
  - contextvars store IDs
  - tools/services open their own DB sessions
  - event append opens its own DB session or goes through a single event-writer
- Delete legacy contexts once migration is complete.

## 12) Known limitations in this environment (FYI)

The `agent-mesh/*_run` MCP tools appear to time out after ~60s (“deadline has elapsed”), so we could not rely on them for a long “second agent” deep dive. The analysis above is based on direct repo inspection + local test runs.
