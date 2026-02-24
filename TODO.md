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

1. **Extended hook states** (`needs_user`, `blocked`) — blocked on Claude Code hook support. Defer.
2. **Oikos Dispatch Contract** — defer until usage demands it.
3. **PyPI publish** — `pyproject.toml` is `0.1.2`, PyPI has `0.1.1`. Low priority but stale.

---

## [Product] Search + Discovery

**Status (2026-02-23):** Core shipped. Timeline search is fully functional.

- [x] Keyword search (FTS) — sessions page, instant
- [x] Semantic search — AI toggle (✨ icon), hybrid RRF mode, sort by relevance/recency
- [x] Recall panel — turn-level semantic search, `?event_id=X` anchoring to matched turn
- [x] Session titles — LLM summary_title preferred, cwd/project/date fallbacks
- [x] Smart search fallback — keyword→semantic auto-fallback when no keyword results
- [ ] Semantic result display polish — summary shown as snippet (done), but score badge not shown for transparent ranking. Consider showing confidence.

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
- [ ] Summarization coverage gap — `AgentsStore.ingest_session()` used directly from multiple paths (demo seeds, commis_job_processor, CLI) without enqueuing summary tasks. Sessions via those paths get no `summary_title`. Fix: add enqueue call at those call sites or inside `ingest_session()` itself. Risk: demo seeds will trigger LLM calls.

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
- PyPI `0.1.1` lags repo `0.1.2`. Publish when ready.

---

## [Tech Debt] CSS Legacy Patterns

- Legacy modal pattern CSS — 7+ components use `.modal-*` classes, 58 definitions in `styles/css/modal.css`.
- Legacy token aliases — present in `styles/tokens.css`, actively referenced in component CSS.
