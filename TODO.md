# TODO

Capture list for substantial work. Not quick fixes (do those live).

## For Agents

- Each entry is a self-contained handoff — read it, you have context to start
- Size (1-10) indicates scope: 1 = hour, 5 = day, 10 = week+
- Check off subtasks as you go so next agent knows state
- Add notes under tasks if you hit blockers or learn something

Classification tags (use on section headers): [Launch], [Product], [Infra], [QA/Test], [Docs/Drift], [Tech Debt]

---

## What's Next (Priority Order)

1. **Forum extended state + bucket actions** — `needs_user`, `parked`, `resumed` states; Park/Snooze/Archive actions in Forum. [Details](#product-forum-discovery-ux--explicit-presence-signals-7)
2. **Ingest: embedding cursor** — session-level `needs_embedding` flag still loads all events; switch to per-event high-water mark. [Details](#tech-debt-ingest-pipeline-reliability--efficiency)
3. **README Test CI** — Not started, well-scoped. [Details](#qatest-readme-test-ci-5)
4. **Hook Presence Spool** — ✅ DONE. Hook writes to `~/.claude/outbox/`, daemon drains every 1s. See `apps/engine/src/outbox.rs`.
5. **Oikos Dispatch Contract** — Deferred until usage demands it.

---

## [Product] Forum Discovery UX + Explicit Presence Signals (7)

Make the Forum the canonical discovery UI for sessions, with **explicit** state signals.

**Status (2026-02-21):** Core presence + Unknown badge complete. Remaining: extended state model + bucket actions.

- [x] Presence ingestion + storage — `session_presence` table, `POST /api/agents/presence`.
- [x] Presence emission — `UserPromptSubmit→thinking`, `PreToolUse→running`, `Stop→idle`.
- [x] Forum UI with real state — active rows glow green, inactive fade, canvas entities pulse.
- [x] Unknown state — `showUnknown` prop on `PresenceBadge`; live sessions without signals show dim gray "Unknown".
- [ ] Define extended state model: `needs_user`, `blocked`, `parked`, `resumed` — beyond thinking/running/idle.
- [ ] Add user actions: Park, Snooze, Archive (emit explicit events, change display state).

---

## [Tech Debt] Ingest Pipeline Reliability + Efficiency

**Status (2026-02-21):**
- ✅ Durable task queue: `SessionTask` model + polling worker (`ingest_task_queue.py`). Replaces BackgroundTasks.
- ✅ Summary cursor: `last_summarized_event_id` on `AgentSession`. `_generate_summary_impl` now loads only new events (id > cursor) instead of all events. Legacy count-based fallback for old rows.
- [ ] Embedding cursor: session-level `needs_embedding=0/1` still has no per-event high-water mark. New events after initial embedding never get re-embedded. Fix: add `last_embedded_event_id` column (same pattern as summary cursor).

**Note on title_generator.py:** Serves Oikos *chat thread* titles (`/conversation/title` endpoint). Not a duplicate of session summary — leave it.

---

## [Infra] ✅ Hook Presence Spool via Daemon

Done. `longhouse-hook.sh` writes presence events to `~/.claude/outbox/` via atomic rename (`.tmp.X` → `prs.X.json`). Rust engine daemon drains every 1s, coalesces by session_id, POSTs to `/api/agents/presence`. No network in hook critical path. Tests in `apps/engine/src/outbox.rs`. E2E script at `scripts/test-hooks-e2e.sh`.

---

## [Product] Harness Simplification — Oikos Dispatch Contract (Deferred)

- [ ] Oikos dispatch contract: direct vs quick-tool vs CLI delegation, explicit backend intent routing
- [ ] Claude Compaction API for infinite thread context management

Research doc: `docs/specs/3a-deferred-research.md` (commit `16c531e6`)

---

## [QA/Test] README Test CI (5)

- [ ] Define `readme-test` JSON block spec (steps, workdir, env, mode, timeout, cleanup).
- [ ] Implement `scripts/run-readme-tests.sh` (extract + run in temp clone, fail fast, save logs).
- [ ] Add `make test-readmes` target (smoke vs full mode flags).
- [ ] Add GitHub Actions workflow using `runs-on: cube` for PR smoke and nightly full.
- [ ] Add `readme-test` blocks to root README + runner/sauron/hatch-agent READMEs.

---

## [Docs/Drift] Open Items

- DB size claim stale; prod DB reset 2026-02-05 (no users). Update once real user data exists.
- PyPI version lags repo: `pyproject.toml` is `0.1.2`, PyPI has `0.1.1`. Publish when ready.

---

## [Tech Debt] Stable Abstractions (Don't Delete)

- [ID 41] Legacy modal pattern CSS — 7+ components use `.modal-*` classes, 58 definitions in `styles/css/modal.css`. Evidence: `ideas/evidence/48_evidence_modal_css_legacy.sh`
- [ID 43] Legacy token aliases — present in `styles/tokens.css`, actively referenced in component CSS. Evidence: `ideas/evidence/50_evidence_tokens_css_legacy_aliases.sh`
