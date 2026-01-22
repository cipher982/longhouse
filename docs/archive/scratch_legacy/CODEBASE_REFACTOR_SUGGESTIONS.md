# 📌 Codebase Refactor & Optimisation Notes

This document is a **living checklist** of hot-spots and quick-wins discovered
during an exploratory review (May 2025).  Nothing here is *broken* – the
project already ships a healthy test-suite and >95 % coverage – but addressing
these points will pay back quickly in maintainability, performance and
operational robustness.

> The sections roughly follow the runtime flow: DB → EventBus/WS → Scheduler →
> Agent execution → Front-end, finishing with cross-cutting concerns & a
> bite-size “pick-me-first” list.

---

## 1&nbsp;· Database / CRUD layer

| Area | Issue | Suggested Improvement |
|------|-------|-----------------------|
| **Hot commit loops** | ✅ **Fixed (Jun 2025)** – `create_thread_message` now accepts `commit=False` so `ThreadService.save_new_messages` adds many rows then flushes & commits once.  New `crud.mark_messages_processed_bulk` updates rows in one statement; `ThreadService.mark_messages_processed` uses it. |
| **Sync SQL inside async routes** | All FastAPI endpoints call the synchronous SA API which blocks the event-loop. | Either migrate to `sqlalchemy.ext.asyncio`, or wrap heavy CRUD in `anyio.to_thread`. |
| **Session lifetime workaround** | ✅ **Fixed (Jun 2025)** – `load_scheduled_agents` now queries `(id, schedule)` tuples then closes the session before registering jobs, eliminating the DetachedInstanceError hack. |
| **Magic strings** | Status (`"idle"`…), roles (`"assistant"`…), run triggers are free text. | Introduce small `Enum`s (Python + DB CHECK) to catch typos at commit-time. |

---

## 2&nbsp;· Event / WebSocket layer

1. **Thread-safety** – ✅ **Fixed (Jun 2025)** – `TopicConnectionManager`
   now guards all shared maps with a single `asyncio.Lock`.  Critical sections
   are small so throughput impact is negligible; races between connect /
   disconnect / broadcast are no longer possible.

2. **Sequential fan-out** – ✅ **Fixed (Jun 2025)** – `EventBus.publish()` now
   uses `asyncio.gather(*callbacks, return_exceptions=True)` so one slow
   subscriber no longer blocks the others.

3. **Zombie sockets** – ✅ **Fixed (Jun 2025)** – `TopicConnectionManager`
   now starts a 30-second heartbeat loop on first connect that sends a `ping`
   frame to every client; sockets that raise are disconnected and cleaned up,
   so stale entries no longer linger when no further topic messages arrive.

---

## 3&nbsp;· Scheduler / long-running tasks

* **Bad cron strings crash startup** – ✅ **Fixed (Jun 2025)** – Both
  `crud.create_agent` and `crud.update_agent` now validate the cron expression
  via `apscheduler.CronTrigger.from_crontab` and raise `ValueError` on
  invalid input.  SchedulerService keeps its runtime guard as a second line
  of defence.

* **DB work in scheduler thread** – writes from the APScheduler thread share
  the same synchronous engine. When moving to Postgres use a separate async
  engine or thread-pool.

---

## 4&nbsp;· Agent execution pipeline

| Issue | Detail |
|-------|--------|
| **Tool calls claimed “parallel”** | ✅ **Fixed (May 2025)** – `zerg_react_agent.py` now awaits `asyncio.gather(...)` inside the loop and wraps each blocking tool call in `asyncio.to_thread`. |
| **Graph recompilation** | ✅ **Fixed (Jun 2025)** – `AgentRunner` now caches the compiled runnable in-process keyed by `(agent_id, updated_at, stream_flag)` so subsequent runs skip the expensive compilation. |
| **Env flag re-parsing** | ✅ **Fixed (Jun 2025)** – A single `LLM_TOKEN_STREAM` boolean in `zerg.constants` is parsed once at import-time; `AgentRunner` and `_make_llm()` now import the constant instead of calling `os.getenv()` repeatedly. |

---

## 5&nbsp;· ThreadService niceties

* Convert `_db_to_langchain` & `_langchain_to_create_kwargs` to
  `functools.singledispatch` for clarity.
* Currently logs “unknown role” then falls back to `AIMessage`; raise in tests
  so new roles are introduced intentionally.

---

## 6&nbsp;· Frontend (Rust / WASM)

1.  **Duplicate logic** – ✅ **Fixed (Jun 2025)** – The `ITopicManager`
    implementation now delegates to the inherent `TopicManager` methods so
    logic lives in exactly one place.

2.  **`RefCell` ergonomics** – ✅ **Fixed (Jul 2025)** – remaining
    `try_borrow_mut()` calls in production code swapped for `borrow_mut()`
    so silent failures turn into explicit panics during development.  A small
    `mut_borrow!` macro was added (`frontend/src/macros.rs`) for concise,
    self-documenting borrows going forward.

3.  **Exp-backoff leak** – `WsClientV2` clones self to schedule reconnects but
    does not cancel the timer after a successful reconnect.  Store the
    `TimeoutId` and clear it.

---

## 7&nbsp;· Cross-cutting

* **Timezone** – ✅ **Fixed (Jun 2025)** – `zerg.utils.time.utc_now()` returns
  aware UTC timestamps; all new code imports this helper.  Built-in tool
  `get_current_time` now emits UTC ISO-8601 and CRUD helpers use
  `utc_now()` instead of naïve `datetime.now()`.  Models will be upgraded to
  `timezone=True` columns in a follow-up migration once deployed.
* **CORS** – ✅ **Fixed (Jun 2025)** – When `AUTH_DISABLED=1` we keep
  wildcard.  Otherwise allowed origins come from `ALLOWED_CORS_ORIGINS`
  (comma-separated) with a safe fallback list, removing the security gap.
* **Webhook hardening** – ✅ **Fixed (Jun 2025)** – Gmail webhook now rejects
  requests larger than 128 KiB (via dependency) before any JWT/HMAC work.

---

## 🍬 Low-hanging fruit (pick-me-first)

1. ~~Batch DB commits in `ThreadService.save_new_messages` & friends.~~ **Done – Jun 2025**
2. ~~Make `EventBus.publish` concurrent via `asyncio.gather`.~~  **Done – Jun 2025**
3. ~~Protect shared maps in `TopicConnectionManager` with a lock.~~ **Done – Jun 2025**
4. ~~Cache compiled LangGraph runnables with `@lru_cache`.~~ **Done – Jun 2025**
5. ~~Replace serial tool loop with `asyncio.gather`.~~  **Done in PR #??? – May 2025**

Everything above is covered by unit tests, so each change can land in an
independent PR with high confidence.

---

## 8 · New surface-area since the original review (May 2025)

The code base has grown significantly over the last months.  The following
features are **production-ready** but introduce fresh refactor opportunities
that are *not* captured in the sections above:

| Feature | Potential follow-ups |
|---------|----------------------|
| **Google Sign-In auth layer** | ✅ **Fixed (Jul 2025)** – The frontend now appends the stored JWT as `?token=` query parameter, backend validates it via `validate_ws_jwt()` and rejects unauthenticated sockets with close code **4401**. CORS restriction shipped in the earlier June patch. |
| **Run-history tables & API** | • Add DB indexes on `(agent_id, created_at DESC)` – large tenants already show slow list queries.<br>• Expose aggregate cost metrics in a separate endpoint instead of computing them in the dashboard. |
| **HMAC-secured webhook triggers** | • Clamp request body size (≤128 KB) *before* HMAC validation.<br>• Rotate `TRIGGER_SIGNING_SECRET` via admin UI. |
| **Token-level streaming** | • Unify the three chunk types (`assistant_token`, `tool_output`, `assistant_message`) in a small `Enum` on both client & server to avoid typo-bugs.<br>• Document the feature flag exhaustively in the README. |

These items have not been prioritised yet; create tickets once the core
backlog (sections 1-7) shrinks.

---

*Happy refactoring!* 🛠️
