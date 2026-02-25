# TODO

Capture list for substantial work. Not quick fixes (do those live).

## For Agents

- Each entry is a self-contained handoff — read it, you have context to start
- Size (1-10) indicates scope: 1 = hour, 5 = day, 10 = week+
- Check off subtasks as you go so next agent knows state
- Add notes under tasks if you hit blockers or learn something

Classification tags: [Launch], [Product], [Infra], [QA/Test], [Docs/Drift], [Tech Debt]

---

## What's Next (Priority Order)

1. ~~**[QA/Test] Stabilize session-continuity E2E polling** — tolerate transient `/api/oikos/runs/:id/events` socket hangups.~~ Done (2026-02-25).
2. ~~**[Launch] Simplify primary navigation** — Timeline as the core surface. Hide Forum for now, move Settings out of top-level nav, and decide whether Briefings is a primary tab or secondary surface (e.g., under More or Settings).~~ Done (2026-02-24).
3. ~~**[Launch] Forum as Timeline subpane** — consider migrating the live overview into the Timeline UI if it still earns a place.~~ Done (2026-02-24).
4. **Extended hook states** (`needs_user`, `blocked`) — blocked on Claude Code hook support. Defer.
5. **Oikos Dispatch Contract** — defer until usage demands it.
6. ~~**PyPI publish**~~ — shipped as v0.1.3 (2026-02-24).

---

## [Product] Search + Discovery

**Status (2026-02-23):** Core shipped. Timeline search is fully functional.

- [x] Keyword search (FTS) — sessions page, instant
- [x] Semantic search — AI toggle (✨ icon), hybrid RRF mode, sort by relevance/recency
- [x] Recall panel — turn-level semantic search, `?event_id=X` anchoring to matched turn
- [x] Session titles — LLM summary_title preferred, cwd/project/date fallbacks
- [x] Smart search fallback — keyword→semantic auto-fallback when no keyword results
- [x] Semantic result display polish — score badge ("87% match") shown on AI search results when score ≥ 0.5.

---

## [Product] Forum + Presence

**Status (2026-02-23):** Fully working end-to-end on prod.

- [x] Presence ingestion — `session_presence` table, outbox pattern (no network in hooks)
- [x] Real-time state — thinking/running/idle via Claude Code hooks → daemon → API
- [x] Forum UI — active rows glow, canvas entities pulse, presence priority over ended_at
- [x] Bucket actions — Park/Snooze/Archive/Resume
- [ ] Extended hook states (`needs_user`, `blocked`) — deferred, hooks don't support it yet

---

## [Product] Briefings + AI Features

**Status (2026-02-23):** Core wired. Depends on LLM summarization running.

- [x] Briefings page (`/briefings`) — project selector, session summaries + insights + proposals
- [x] Reflection briefing endpoint — `GET /api/agents/briefing`
- [x] Summarization coverage gap — fixed: `enqueue_ingest_tasks` now called inside `AgentsStore.ingest_session()` so all paths (demo seeds, commis_job_processor, CLI, router) enqueue summary + embedding tasks.

---

## [Product] Harness — Oikos Dispatch Contract (Deferred)

- [ ] Oikos dispatch contract: direct vs quick-tool vs CLI delegation
- [ ] Claude Compaction API for infinite thread context management

Research doc: `docs/specs/3a-deferred-research.md`

---

## [Tech Debt] Schema Migration

**Resolved (2026-02-22):** `_migrate_agents_columns()` in `database.py` now covers all
current columns. **Rule:** every new `Column` on an agents model must get a corresponding
`ALTER TABLE` entry in that function — SQLite ignores new columns on existing tables.

- [x] `last_summarized_event_id`, `user_state`, `user_state_at` — added to migration
- [ ] No current gaps known — watch for new columns added without migration entries

---

## [Docs/Drift] Open Items

- DB size claim stale in README (prod DB reset 2026-02-05, no real users yet). Update when data exists.
- ~~PyPI `0.1.1` lags repo `0.1.2`.~~ Published as v0.1.3 (2026-02-24).

---

## [Tech Debt] CSS Legacy Patterns

- Legacy modal pattern CSS — 7+ components use `.modal-*` classes, 58 definitions in `styles/css/modal.css`.
- Legacy token aliases — present in `styles/tokens.css`, actively referenced in component CSS.
