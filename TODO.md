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

Phase 4 — Job Queue + Concurrency: 🔲 OPEN
- [ ] Replace `FOR UPDATE SKIP LOCKED` with `BEGIN IMMEDIATE` + `UPDATE ... RETURNING`
- [ ] Add heartbeat fields + stale job reclaim logic
- [ ] Replace advisory locks with status-guarded updates or file locks
- [ ] Gate `ops.job_queue` behind `not lite_mode`
- [ ] Test: spawn 3 commis, kill server, restart, jobs resume

Phase 5 — Durable Checkpoints: 🔲 OPEN
- [ ] Replace MemorySaver with `langgraph-checkpoint-sqlite` for SQLite
- [ ] Ensure migrations/setup idempotent
- [ ] Test: interrupt run, restart server, resume continues

Phase 6 — CLI + Frontend Bundle: 🔲 OPEN
- [ ] Add `zerg serve` command (typer, uvicorn with sane defaults)
- [ ] Bundle frontend `dist` in python package (hatch config)
- [ ] Update FastAPI static mount to use packaged assets
- [ ] Test: fresh venv → `pip install zerg` → `zerg serve` → UI works

Phase 7 — Onboarding Smoke + Docs: 🔲 OPEN
- [ ] Add/extend `make onboarding-smoke` for SQLite boot + API checks
- [ ] Update README quick-start to default to SQLite
- [ ] Verify onboarding contract passes

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

- [x] Parallel spawn_commis interrupt fix (2026-01-30) — commit a8264f9d
- [x] Telegram webhook handler (2026-01-30) — commit 2dc1ee0b, `routers/channels_webhooks.py`
- [x] Learnings review compacted 33 → 11 (2026-01-30)
- [x] Sauron gotchas documented (2026-01-30)
- [x] Life Hub agent migration (2026-01-28) — Zerg owns agents DB
- [x] Single-tenant enforcement in agents API (2026-01-29)
