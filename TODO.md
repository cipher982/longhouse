# TODO

Capture list for substantial work. Not quick fixes (do those live).

## For Agents

- Each entry is a self-contained handoff — read it, you have context to start
- Size (1-10) indicates scope: 1 = hour, 5 = day, 10 = week+
- Check off subtasks as you go so next agent knows state
- Add notes under tasks if you hit blockers or learn something

---

## SQLite Runtime Shift (9)

Remove Postgres-only guards so the agents schema works on SQLite. Core blocker for lightweight OSS onboarding — enables `pip install zerg && zerg serve` without requiring Postgres.

**Why:** VISION.md specifies SQLite-only runtime. Current code hard-rejects SQLite URLs and uses Postgres-specific types (UUID, JSONB, schemas).

**Reference:** `docs/LIGHTWEIGHT-OSS-ONBOARDING.md` has full plan details.

Phase 0 — Audit: ✅ DONE (2026-01-30)
- [x] Tag all Postgres-only codepaths (UUID/JSONB/schema/ILIKE)
- [x] Define SQLite-compatible schema strategy (flat tables, no schemas)
- [x] Decide: conditional dialect handling in models + services
- [x] Plan captured in `docs/LIGHTWEIGHT-OSS-ONBOARDING.md`

Phase 1 — Core DB Boot: ✅ DONE (2026-01-31)
- [x] Remove SQLite rejection in `database.py`
- [x] Skip `_apply_search_path()` for SQLite
- [x] Make `DB_SCHEMA` and `AGENTS_SCHEMA` conditional (None for SQLite)
- [x] Enforce SQLite >= 3.35 at startup (RETURNING support)
- [x] Set SQLite pragmas (WAL, busy_timeout, foreign_keys)

Phase 2 — Model Compatibility: ✅ DONE (2026-01-31)
- [x] Replace UUID columns with GUID TypeDecorator (String(36) + uuid4 defaults)
- [x] Replace JSONB with JSON.with_variant
- [x] Replace gen_random_uuid() with Python-side defaults
- [x] Update partial indexes to include sqlite_where
- [x] Extract `db_utils.is_sqlite_url()` helper

Phase 3 — Agents API + Ingest: ✅ DONE (2026-01-31)
- [x] Dialect-agnostic upsert (on_conflict_do_nothing for both dialects)
- [x] Remove `require_postgres()` guard
- [x] Dedupe works without schema-qualified names
- [x] Timeline UI works end-to-end on SQLite
- [x] Lite test suite added (`make test` runs SQLite-lite by default)

Phase 4 — Job Queue + Concurrency: 🔲 OPEN (see standalone task below)

Phase 5 — Durable Checkpoints: ✅ DONE (2026-01-31)
- [x] SqliteSaver for SQLite (sync, avoids event loop affinity issues)
- [x] workflow_engine.py uses get_checkpointer() factory
- [x] Thread-safe caching with _sqlite_cache_lock

Phase 6 — CLI + Frontend Bundle: 🔲 OPEN (see standalone task below)

Phase 7 — Onboarding Smoke + Docs: 🔲 OPEN
- [ ] Add/extend `make onboarding-smoke` for SQLite boot + API checks
- [ ] Update README quick-start to default to SQLite
- [ ] Verify onboarding contract passes

---

## SQLite Job Queue (Phase 4) (5)

Make commis job claiming work on SQLite. Currently uses `FOR UPDATE SKIP LOCKED` which is Postgres-only.

**Why:** Commis can't run concurrently on SQLite without this. Blocks single-process multi-agent use case.

**Files:**
- `jobs/queue.py` — Main job claim logic
- `services/commis_job_processor.py` — Uses queue for commis jobs

**Pattern (from LIGHTWEIGHT-OSS-ONBOARDING.md):**
```python
def claim_job(db, worker_id):
    with db.begin_immediate():  # SQLite write lock
        job = db.execute("""
            UPDATE jobs
            SET status='running', worker_id=?, started_at=NOW()
            WHERE id = (
                SELECT id FROM jobs WHERE status='pending'
                ORDER BY priority DESC, created_at ASC LIMIT 1
            )
            RETURNING *
        """, [worker_id]).fetchone()
    return job
```

- [ ] Add `claimed_at`, `heartbeat_at` columns to commis_jobs
- [ ] Create `claim_job_sqlite()` with BEGIN IMMEDIATE pattern
- [ ] Create `claim_job_postgres()` with FOR UPDATE SKIP LOCKED
- [ ] Dispatch based on `lite_mode` in queue.py
- [ ] Add heartbeat update in job processor loop
- [ ] Add stale job reclaim (no heartbeat for 2min → reset to pending)
- [ ] Test: concurrent claims don't double-assign

---

## SQLite Advisory Locks (Phase 4b) (4)

Replace Postgres advisory locks with SQLite-safe alternatives. These are used for single-tenant guard, fiche locks, and state recovery.

**Why:** Advisory locks don't exist in SQLite. Need status columns or file locks.

**Files:**
- `services/single_tenant.py` — Uses `pg_advisory_lock` for instance guard
- `services/fiche_locks.py` — Uses advisory locks for run exclusion
- `services/fiche_state_recovery.py` — Uses advisory locks

**Options:**
1. **Status column + heartbeat** — Add `locked_by`, `locked_at` columns, check/update atomically
2. **File locks** — `fcntl.flock()` on `~/.zerg/locks/{resource}.lock`

- [ ] Audit all advisory lock callsites
- [ ] Decide: status columns vs file locks (recommend status for DB resources)
- [ ] Implement `acquire_lock_sqlite()` / `release_lock_sqlite()` helpers
- [ ] Update single_tenant.py to use SQLite-safe locking
- [ ] Update fiche_locks.py to use SQLite-safe locking
- [ ] Gate ops.job_queue behind `not lite_mode` if not making it SQLite-safe
- [ ] Test: two processes can't claim same fiche simultaneously

---

## SQLite Checkpoints (Phase 5) (3) ✅ DONE (2026-01-31)

Make LangGraph checkpoints durable on SQLite. Currently uses MemorySaver which loses state on restart.

**Why:** If server restarts mid-run, the run can't resume. Breaks "always-on" promise.

**Files:**
- `services/checkpointer.py` — Creates checkpointer instance
- `services/workflow_engine.py` — Uses checkpointer
- `services/workflow_validator.py` — Uses checkpointer (MemorySaver OK for validation)

**Solution:** Use `langgraph-checkpoint-sqlite` package with synchronous `SqliteSaver` (not AsyncSqliteSaver to avoid event loop affinity issues).

- [x] Add `langgraph-checkpoint-sqlite` dependency (was already in pyproject.toml)
- [x] Update checkpointer.py to detect lite_mode and use SqliteSaver
- [x] Thread-safe caching with `_sqlite_cache_lock`
- [x] Handle URL query params for URI mode (e.g., ?mode=memory&cache=shared)
- [x] workflow_engine.py uses `get_checkpointer()` factory instead of hardcoded MemorySaver
- [ ] Test: interrupt run, restart, resume continues from checkpoint (manual verification needed)

---

## Zerg CLI + Package (Phase 6) (5)

Create `zerg serve` command and pip-installable package. The finish line for OSS onboarding.

**Why:** `pip install zerg && zerg serve` is the north star UX.

**Files:**
- `apps/zerg/backend/zerg/cli/` — New CLI module (typer)
- `apps/zerg/backend/pyproject.toml` — Package config
- `apps/zerg/frontend-web/dist/` — Built frontend to bundle

**Implementation:**
```python
# cli/main.py
import typer
import uvicorn

app = typer.Typer()

@app.command()
def serve(host: str = "0.0.0.0", port: int = 8080):
    """Start the Zerg server."""
    uvicorn.run("zerg.main:app", host=host, port=port)
```

- [ ] Create `cli/main.py` with typer
- [ ] Add `[project.scripts]` entry in pyproject.toml
- [ ] Configure hatch to bundle `frontend-web/dist/` in package
- [ ] Update FastAPI static mount to use `importlib.resources` for packaged assets
- [ ] Add `zerg status` command (show running jobs)
- [ ] Test: `pip install -e .` → `zerg serve` → UI loads
- [ ] Test: `pip install zerg` from TestPyPI → same

---

## Prompting Pipeline Hardening (6)

Unify prompt construction across `run_thread`, `run_continuation`, and `run_batch_continuation` to eliminate divergence in tool loading, usage capture, and persistence.

**Why:** Current flows have subtle differences that cause bugs. Memory query behavior varies, tool results can duplicate.

**Files:** `managers/fiche_runner.py`, related service files

- [ ] Create unified prompt/run helper used by all three flows
- [ ] Introduce `PromptContext` dataclass (system + conversation + tool_messages + dynamic_context)
- [ ] Extract single `derive_memory_query(...)` helper for consistent memory behavior
- [ ] Add DB-level idempotency for tool results (unique constraint or `get_or_create`)
- [ ] Split dynamic context into tagged system messages for clearer auditing
- [ ] Add prompt snapshot test fixture for regression checks

---

## Prompt Cache Optimization (5)

Reorder message layout to maximize cache hits. Current layout busts cache by injecting dynamic content early.

**Why:** Cache misses = slower + more expensive. Research shows 10-90% cost reduction with proper ordering.

**Current (bad):**
```
[system] → [connector_status] → [memory] → [conversation] → [user_msg]
                ↑ BUST              ↑ BUST
```

**Target:**
```
[system] → [conversation] → [dynamic + user_msg]
 cached      cached           per-turn only
```

**Files:** `managers/fiche_runner.py` lines 340-405

**Principles:**
- Static content at position 0 (tools, system prompt)
- Conversation history next (extends cacheable prefix)
- Dynamic content LAST (connector status, RAG, timestamps)
- Never remove tools — return "disabled" instead

- [ ] Reorder message construction in fiche_runner
- [ ] Verify cache hit rate improves (add logging/metrics)
- [ ] Document the ordering contract

---

## Workspace Commis Tool Events (4)

Workspace commis emit only `commis_started` and `commis_complete` — no tool events during execution. Events exist in hatch session JSONL but aren't extracted.

**Why:** UI shows black box during workspace commis. Users can't see what tools ran.

**Files:** `services/commis_job_processor.py` workspace execution path

**Decision needed:** Post-hoc extraction vs accept reduced visibility?
- Post-hoc: Parse session log on completion, emit `commis_tool_*` events retroactively
- Status quo: Accept that headless = less visibility

- [ ] Decide approach
- [ ] Implement if post-hoc chosen
- [ ] Update UI to indicate "headless mode" if status quo

---

## Sauron /sync Reschedule (3)

`/sync` endpoint reloads manifest but APScheduler doesn't reschedule existing jobs. Changed schedules don't take effect until restart.

**Files:** `apps/sauron/sauron/main.py`

- [ ] On sync, diff old vs new jobs
- [ ] Remove jobs no longer in manifest
- [ ] Reschedule jobs with changed cron expressions
- [ ] Add test coverage

---

## Done (Recent)

- [x] SQLite Phases 0-3 complete (2026-01-31) — Core boot, models, agents API all SQLite-safe
- [x] SQLite 3.35+ enforcement (2026-01-31) — Startup fails fast if below minimum
- [x] `db_utils.is_sqlite_url()` helper (2026-01-31) — Handles quoted URLs, used by config + models
- [x] Lite test suite (2026-01-31) — `make test` runs SQLite tests by default
- [x] Parallel spawn_commis interrupt fix (2026-01-30) — commit a8264f9d
- [x] Telegram webhook handler (2026-01-30) — commit 2dc1ee0b, `routers/channels_webhooks.py`
- [x] Learnings review compacted 33 → 11 (2026-01-30)
- [x] Sauron gotchas documented (2026-01-30)
- [x] Life Hub agent migration (2026-01-28) — Zerg owns agents DB
- [x] Single-tenant enforcement in agents API (2026-01-29)
