# Messaging & Data Architecture Decision (Draft for External Agent Review)

> Goal: minimize complexity and “spooky action at a distance” bugs in Zerg/Swarmlet’s orchestration + UI streaming, while keeping the system practical for a single developer and leveraging existing Postgres.

## 1) Problem Statement (What we’re fighting)

The system mixes:

- Async orchestration (`asyncio`, `asyncio.gather`)
- Thread offloading (`asyncio.to_thread`)
- Ambient state propagation (`contextvars`)
- Request-scoped mutable resources (SQLAlchemy `Session`) and other “live” objects stored in contextvars

This combination repeatedly produces hard-to-debug concurrency and misattribution bugs:

- `asyncio.create_task()` copies contextvars into new tasks.
- `asyncio.to_thread()` copies contextvars into threads.
- If contextvars carry *live* mutable objects (DB sessions, clients, resolvers holding sessions), those objects can be used concurrently or from the wrong context, causing:
  - event misclassification
  - cross-thread SQLAlchemy Session usage (not thread-safe)
  - “worked in serial, breaks in parallel” behavior

Recent example: tool events were misclassified (`supervisor_tool_*` emitted as `worker_tool_*`) due to contextvar leakage across `create_task()`. We fixed this by using injected emitters whose identity is fixed at construction.

## 2) Current System Inventory (Concrete paths to inspect)

### 2.1 Durable event log (DB)

- `apps/zerg/backend/zerg/models/agent_run_event.py`
  - `AgentRunEvent` table: durable event store for replay/resumable SSE.
- `apps/zerg/backend/zerg/services/event_store.py`
  - `emit_run_event(db, run_id, event_type, payload)` persists to DB and then publishes live via `event_bus`.
  - `EventStore.get_events_after(...)` for replay.

### 2.2 Resumable SSE v1 (Replay + live)

- `apps/zerg/backend/zerg/routers/stream.py`
  - `/api/stream/runs/{run_id}`
  - Subscribes to live events via `event_bus` first, replays from DB with short-lived session, then continues live from an in-memory queue.

### 2.3 Jarvis/Chat SSE (additional endpoints)

- `apps/zerg/backend/zerg/routers/jarvis_chat.py`
- `apps/zerg/backend/zerg/routers/jarvis_supervisor.py`
- `apps/zerg/backend/zerg/routers/jarvis_runs.py`
- `apps/zerg/backend/zerg/routers/jarvis_sse.py`

### 2.4 WebSockets (topic-based + runner WS)

- `apps/zerg/backend/zerg/websocket/manager.py`
  - topic subscriptions, backpressure queues, broadcasts.
- `apps/zerg/backend/zerg/services/runner_connection_manager.py`
  - runner WebSocket connections.

### 2.5 Token streaming callback & contextvars

- `apps/zerg/backend/zerg/callbacks/token_stream.py`
  - `current_thread_id_var`, `current_user_id_var`, `current_db_session_var`
  - `WsTokenCallback.on_llm_new_token` publishes supervisor tokens via `event_bus` and otherwise broadcasts via WS.
  - Contains commentary about not persisting every token to DB for perf.

### 2.6 Tool execution concurrency model

- `apps/zerg/backend/zerg/agents_def/zerg_react_agent.py`
  - Executes tool calls in parallel with `asyncio.gather`.
  - Sync tools run via `asyncio.to_thread`, which copies contextvars into threads.

### 2.7 Injected emitters (recent refactor)

- `apps/zerg/backend/zerg/events/emitter_protocol.py`
- `apps/zerg/backend/zerg/events/emitter_context.py`
- `apps/zerg/backend/zerg/events/worker_emitter.py`
- `apps/zerg/backend/zerg/events/supervisor_emitter.py`
- `apps/zerg/backend/zerg/events/null_emitter.py`

Entry points setting emitter identity:

- `apps/zerg/backend/zerg/services/worker_runner.py`
- `apps/zerg/backend/zerg/services/supervisor_service.py`
- `apps/zerg/backend/zerg/services/worker_resume.py`

Tool emission consuming identity:

- `apps/zerg/backend/zerg/agents_def/zerg_react_agent.py` (`get_emitter()`)

### 2.8 Credentials/context plumbing

- Contextvar transport:
  - `apps/zerg/backend/zerg/connectors/context.py`
- Resolver:
  - `apps/zerg/backend/zerg/connectors/resolver.py`
  - Recent mitigation: prefetch credentials at construction; avoid DB access during tool execution.

## 3) The “Gamut” of Messaging/Data Options (with tradeoffs)

### A) Function args / explicit plumbing

Pass `run_id`, `owner_id`, `thread_id`, `emitter`, etc. explicitly.

- Pros: simplest reasoning model; no hidden state; best for correctness.
- Cons: noisy; cross-cutting concerns force signature creep; can be tedious across deep call graphs.

### B) contextvars

Use task-local ambient IDs and small metadata.

- Pros: avoids plumbing; works across `await`; great for tracing/correlation IDs.
- Cons: copied into new tasks/threads; dangerous if holding live resources; can create “action at a distance”.
- Safe usage: IDs and immutable snapshots only.

### C) In-process pub/sub (EventBus)

Publish events to an in-memory bus; consumers subscribe.

- Pros: low latency; easy fanout within process; good for live streaming.
- Cons: not durable; restart loses; multi-process scaling is hard without a broker.

### D) Durable DB event log (append-only)

Persist all important events to Postgres (or similar), query and replay them.

- Pros: durable, queryable, replayable; simplest “source of truth”; pairs well with SSE.
- Cons: DB write load; design required for token volume; avoid synchronous commit in hot paths.

### E) SSE (Server-Sent Events)

HTTP stream server→client.

- Pros: simple, works well with proxies, auto-reconnect; perfect for timeline UI.
- Cons: server→client only; persistence/replay requires DB log.

### F) WebSockets

Bidirectional persistent connection.

- Pros: real-time both ways; good for interactive control channels.
- Cons: more ops complexity; scaling/stickiness concerns; still needs durability for replay.

### G) Message Queue (SQS/Rabbit/Redis Streams)

Reliable delivery for background work.

- Pros: backpressure, retry, separation between producer/consumer; good for worker job execution.
- Cons: adds infra; not a timeline store; still need DB for history/UI.

### H) Pub/Sub broker (Redis PubSub, NATS)

Fanout across processes.

- Pros: low latency; multi-process fanout.
- Cons: usually non-durable; must combine with DB log for replay.

### I) Log streaming platform (Kafka/Pulsar)

Durable + replay + consumer groups.

- Pros: unifies durable stream and distribution.
- Cons: heavy ops; likely overkill for 1 dev/no users.

### J) Filesystem logs (JSONL) + artifacts on disk

Append-only log files per run + artifact blobs.

- Pros: very inspectable; easy for local dev; can be “mentally simple”.
- Cons: locking/concurrency; multi-host coordination; querying/indexing; security boundaries; backups/retention.

## 4) Recommendation: “Least Complexity That Works”

### Final decision (recommended architecture)

**Canonical truth:** Postgres `AgentRunEvent` append-only event log per run.

**Live delivery:** Resumable SSE v1 endpoint (`/api/stream/runs/{run_id}`) that:
1) subscribes to live events,
2) replays DB events using a short-lived session,
3) streams live events via in-memory queue.

**Artifacts:** keep large outputs on disk (worker artifact store) + store only pointers/summaries in DB events.

**WebSockets:** keep only where bidirectional is needed (runner connections). De-emphasize WS for UI run timelines.

This matches what already exists in `routers/stream.py` and `services/event_store.py`.

## 5) Non-negotiable Invariants (rules to prevent recurring bugs)

1) **Never store a SQLAlchemy `Session` in a contextvar.**
   - Contextvars may contain IDs, not live resources.
2) **No “live” objects across concurrency boundaries.**
   - Don’t pass/store DB sessions, ORM objects, http clients, resolvers holding sessions, etc. into `create_task`/`to_thread` contexts.
3) **Emitter identity is explicit and immutable.**
   - Always use injected emitters (WorkerEmitter/SupervisorEmitter) for tool event typing.
4) **Every IO boundary opens its own session/client.**
   - Use `db_session()` inside tools/services that need DB, rather than sharing a request session.
5) **Event emission goes through one path.**
   - Durable event timeline: `emit_run_event()` (or successor). Avoid ad-hoc `event_bus.publish` for run timeline events.

## 6) 80/20 incremental plan (few commits, minimal churn)

1) Remove remaining uses of `current_db_session_var` for persistence; replace with local `db_session()` usage at the point of persistence.
2) Audit tools for `resolver.db` usage; replace with local `db_session()` inside the tool.
3) Make “tool wrapper” own DB session:
   - In `_call_tool_async`, open a session for event emission (and close it) rather than using a shared session held elsewhere.
4) Clarify event channels:
   - Treat `/api/stream/runs/{run_id}` as the primary timeline feed.
   - Keep Jarvis-specific SSE only as a thin wrapper or deprecate it gradually.
5) Explicitly disable parallel execution for DB-mutating tools if needed (a short-term guard), or route DB mutations through a single-writer pattern.
6) Ensure event payload includes `run_id` + `owner_id` everywhere (security/correlation).

## 7) Rewrite plan (if we choose to simplify aggressively)

Goal: collapse to one clear streaming primitive and delete redundant plumbing.

1) **One timeline stream:** standardize on `routers/stream.py` for all run timelines.
2) Deprecate Jarvis-specific SSE generators and unify them behind the resumable run stream.
3) Remove WebSocket token streaming for supervisor runs; only stream tokens via SSE (and don’t persist per-token).
4) Replace contextvar-heavy state with a small explicit `RunContext` object passed to orchestrator entrypoints, containing only IDs/snapshots.
5) Move all DB interactions behind clear boundaries:
   - each tool invocation gets its own DB session,
   - each event emission opens a session (or uses a dedicated event-writer service).
6) Delete legacy context-based event typing (already largely replaced by injected emitters).

## 8) Key Questions for the Reviewer Agent

1) Should the canonical timeline be DB-backed SSE (as above), or should we adopt a filesystem JSONL log as the canonical timeline?
2) Should we introduce a real queue for worker execution (SQS/Redis Streams), or keep the current worker processor model?
3) For simplicity: is it acceptable that “tokens are not durable” but lifecycle/tool events are durable?
4) Can we delete WebSocket token streaming and still meet UX goals?
5) How to enforce “no Session in contextvar” mechanically (lint, types, runtime assertions)?
