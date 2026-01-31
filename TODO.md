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

**Files:** `database.py`, `models/agents.py`, `routers/agents.py`, `alembic/versions/0002_agents_schema.py`

Phase 0 — Audit:
- [ ] Tag all Postgres-only codepaths (UUID/JSONB/schema/ILIKE)
- [ ] Define SQLite-compatible schema strategy (no schemas; table prefixes or flat)
- [ ] Decide: separate migration path or conditional migrations?

Phase 1 — Unblock:
- [ ] Remove SQLite rejection in `database.py`
- [ ] Replace UUID/JSONB in agents models with SQLite-compatible types
- [ ] Remove `require_postgres()` guards on agents endpoints
- [ ] Update `0002_agents_schema.py` to be dialect-aware

Phase 2 — Verify:
- [ ] Ingest + demo seed work on SQLite (no schema-qualified queries)
- [ ] Fix queries using `ILIKE` or schema-qualified names
- [ ] Timeline UI works end-to-end on SQLite

Phase 3 — Ship:
- [ ] `make onboarding-smoke` passes
- [ ] `make onboarding-funnel` passes from fresh clone
- [ ] README quick start updated to make SQLite the default

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
