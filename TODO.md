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

1. **Forum Discovery UX** — 3 open items: Unknown state (easy), extended state model, bucket actions. [Details](#product-forum-discovery-ux--explicit-presence-signals-7)
2. **Ingest Pipeline: consolidate title_generator.py** — `summarize_events()` already returns a title; `title_generator.py` is a separate pipeline that bypasses `models_config`. Easy consolidation. [Details](#tech-debt-ingest-pipeline-reliability--efficiency-3)
3. **Ingest Pipeline: SQLite task queue** — Replace fire-and-forget `BackgroundTasks` with a durable queue. Higher effort, higher value.
4. **Hook Presence Spool via Daemon** — Rust engine Unix socket listener. Eliminates async hook noise. [Details](#infra-hook-presence-spool-via-daemon-3)
5. **README Test CI** — Not started, well-scoped. [Details](#qatest-readme-test-ci-5)
6. **Oikos Dispatch Contract + Compaction** — Deferred until usage demands it.

---

## [Infra] Hook Presence Spool via Daemon (3)

**Goal:** Route `longhouse-presence.sh` hook through the local `longhouse-engine connect` daemon instead of calling the Longhouse API directly. Hooks become instant sync writes (<1ms), no network in the critical path.

**Context:** The presence hook fires on `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, and `Stop`. Currently fires direct `curl` to Longhouse API — ~130ms happy path, 2000ms if unreachable (`--max-time 2`). `async: true` prints noisy completion banners. Can't switch to sync without the timeout edge case causing 4s stalls on every tool call.

**Fix:** Outbox pattern. Hook writes to a Unix socket. Daemon listens and forwards async.

```
Hook (sync, <1ms)    longhouse-engine connect    Longhouse API
──────────────────   ────────────────────────    ─────────────
presence event ───►  ~/.claude/longhouse-        ──► /api/agents/presence
                     presence.sock
```

**Engine already has:** daemon loop (`daemon.rs`), SQLite spool, retry logic, HTTP client. Missing: Unix socket listener for presence events.

Engine (`apps/engine/src/`):
- [ ] `daemon.rs`: Add `tokio::net::UnixListener` on `~/.claude/longhouse-presence.sock`
- [ ] `presence.rs` (new): Presence event struct + HTTP forward to `/api/agents/presence`
- [ ] Graceful degradation: socket missing → logged warning, not crash

Hook (`~/.claude/hooks/longhouse-presence.sh`):
- [ ] Try socket first (`nc -U ~/.claude/longhouse-presence.sock`), fall back to direct HTTP
- [ ] Switch `async: false` in `~/.claude/settings.json` once socket is working

**No server changes needed** — `/api/agents/presence` endpoint already accepts the payload.

**Full context:** `~/git/obsidian_vault/AI-Sessions/2026-02-20-longhouse-hook-presence-spool.md`

---

## [Tech Debt] Ingest Pipeline Reliability + Efficiency (3)

**Goal:** Fix reliability and efficiency gaps in the post-ingest LLM/embedding pipeline.

**Background task reliability (highest priority):**
- FastAPI `BackgroundTasks` are fire-and-forget — lost on process crash, no retry, no persistence (`routers/agents.py`)
- Replace with a lightweight SQLite-backed task queue (pending/running/done rows) polled by a background worker

**Summary is not truly incremental:**
- `_generate_summary_impl` loads *all* events every run, slices in Python
- Fix: store `last_summarized_event_id` on the session; only load events after that cursor

**Embeddings lack per-event cursor:**
- Session-level `needs_embedding` flag, no per-event cursor
- Fix: per-event `embedded` bool or a high-water mark like summary cursor

**Note on title_generator.py:** Investigated — it's for Oikos *chat thread* titles (the `/conversation/title` endpoint), NOT a duplicate of the session summary pipeline. `_generate_summary_impl` already sets `summary_title` via `summarize_events()`. Leave `title_generator.py` alone.

**Subtasks:**
- [ ] Design SQLite task queue schema (session_id, task_type, status, attempts, error)
- [ ] Replace BackgroundTasks calls with task queue inserts + polling worker
- [ ] Add `last_summarized_event_id` cursor to sessions table; update summary logic
- [ ] Add per-event embedding cursor or high-water mark
- [ ] `make test` + `make test-e2e` pass

---

## [Product] Harness Simplification — Oikos Dispatch Contract (Deferred)

Phases 1–3h are 100% complete. See git history.

**Remaining deferred items (implement when usage demands):**
- [ ] Oikos dispatch contract: direct vs quick-tool vs CLI delegation, explicit backend intent routing
- [ ] Claude Compaction API (server-side) or custom summarizer for infinite thread context management

Research doc: `docs/specs/3a-deferred-research.md` (commit `16c531e6`)

---

## [Product] Forum Discovery UX + Explicit Presence Signals (7)

Make the Forum the canonical discovery UI for sessions, with **explicit** state signals (no heuristics).

**Status (2026-02-20):** Core presence infrastructure complete. Hooks emit thinking/running/idle. `session_presence` table stores state. Forum UI shows live glow/pulse. Remaining: extended state model + bucket actions.

- [x] Presence ingestion + storage — `session_presence` table, `POST /api/agents/presence`, upsert per session_id.
- [x] Presence emission — `UserPromptSubmit→thinking`, `PreToolUse→running`, `PostToolUse→thinking`, `Stop→idle`.
- [x] Forum UI with real state — active rows glow green, inactive fade, canvas entities pulse.
- [ ] Add "Unknown" state in UI for sessions without presence signals — currently they silently fall through; show a distinct "Unknown" badge instead of pretending they're idle.
- [ ] Define extended state model: `needs_user`, `blocked`, `parked`, `resumed` — beyond thinking/running/idle.
- [ ] Add user actions in Forum: Park, Snooze, Archive (emit explicit events, change display state).

---

## [QA/Test] README Test CI (5)

Automate README command verification with explicit, opt-in contracts. Use cube ARC runners.

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
