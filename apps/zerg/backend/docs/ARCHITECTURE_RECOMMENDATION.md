# Architecture Recommendation: Messaging & Concurrency Simplification

**Date:** 2026-01-11
**Target:** `apps/zerg/backend`

## Executive Summary

The current architecture suffers from **accidental complexity** in three key areas:
1.  **Dual Streaming Paths:** Redundant mechanisms for SSE (Chat/Supervisor) vs. WebSockets (Dashboard/Runners).
2.  **Context Leakage:** Heavy reliance on `contextvars` for state transport (`run_id`, `emitter`) causes "leakage" bugs and necessitates complex workarounds (e.g., explicit context clearing).
3.  **Hybrid Concurrency:** Mixing `threading.Event` with `asyncio` for tool execution (`RunnerJobDispatcher`) creates unnecessary friction and sync/async bridging.

**Recommendation:** Unify around **Async-First Tooling**, **Explicit Context Passing**, and **Single-Source Events**.

## 1. Final Decision

### A. Event Architecture (The "Resumable SSE" Core)
**Keep `EventStore` as the Single Source of Truth.**
*   **Write Path:** All system events (Worker, Supervisor, Tool) MUST be persisted to DB via `EventStore.emit_run_event`.
*   **Read Path:** `EventBus` subscribes to these emissions and broadcasts to:
    *   **SSE Router:** For chat clients (resumable via `last-event-id`).
    *   **WebSocket Manager:** For dashboard/runner real-time updates.
*   **Invariant:** Never emit to `EventBus` directly. Always go through `EventStore` (Persist -> Publish).

### B. State Management
**Move from Implicit `ContextVars` to Explicit `RunnableConfig`.**
*   **Deprecated:** `zerg.context.ContextVar` usage for `run_id`, `job_id`, `owner_id`.
*   **Adopt:** LangGraph/LangChain standard `RunnableConfig` to pass runtime state down to tools.
    ```python
    # Tools access state via config, not global context
    @tool
    async def my_tool(arg: str, config: RunnableConfig):
        run_id = config["configurable"]["run_id"]
        ...
    ```

### C. Concurrency
**Pure Async Execution.**
*   **Remove:** `_run_coro_sync` and `threading.Event` usage in `RunnerJobDispatcher`.
*   **Refactor:** Convert all IO-bound tools (e.g., `runner_exec`) to `async def`.
*   **Benefit:** Eliminates thread pool overhead and sync/async context switching issues.

## 2. Invariants

1.  **Async All The Way:** No synchronous tools that perform IO.
2.  **No Context Leakage:** Do not use `contextvars` to transport request-scoped identifiers (`run_id`, `worker_id`) across async boundaries.
3.  **Persistence First:** If it's not in `AgentRunEvent` table, it didn't happen.

## 3. 80/20 Implementation Plan (High Impact, Low Effort)

This plan fixes the most fragile parts without a total rewrite.

### Step 1: Fix `RunnerJobDispatcher` (Concurrency)
*   **Action:** Convert `runner_exec` tool in `tools/builtin/runner_tools.py` to `async def`.
*   **Action:** Remove `_run_coro_sync` helper.
*   **Action:** Replace `threading.Event` with `asyncio.Future` (or `asyncio.Event`) in `RunnerJobDispatcher`.
*   **Why:** Removes the fragile sync-to-async bridge and `threading` complexity.

### Step 2: Unify Event Emission (Reliability)
*   **Action:** Audit `WorkerRunner` and `SupervisorService`. Ensure *every* event emission goes through `EventStore.emit_run_event`.
*   **Action:** Remove direct `event_bus.publish` calls for run events.

### Step 3: Explicit Context (Stability)
*   **Action:** Update `WorkerRunner` to inject `run_id`, `worker_id`, `owner_id` into the `configurable` dictionary of the agent's `ainvoke` call.
*   **Action:** Update `WorkerEmitter` to accept these values in `__init__` rather than reading from `ContextVars`.
*   **Why:** Prevents "leakage" bugs where resumed tasks inherit stale context.

## 4. Rewrite Plan (Long Term)

1.  **Refactor Tool Signatures:** Update all tools to accept `config: RunnableConfig` for context access.
2.  **Delete Context Modules:** Remove `zerg.context`, `zerg.events.emitter_context`, and `zerg.callbacks.token_stream` contextvars.
3.  **Unified Streaming Router:** Merge SSE and WS endpoints into a single subscription service that handles both protocols from the same event stream.
