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

1. **Forum Discovery UX** — Presence signals wired, Forum UI shows live state. Remaining: bucket actions (Park/Snooze/Archive), extended state model. [Details](#product-forum-discovery-ux--explicit-presence-signals-7)
2. **Session titles without LLM configured** — Without a provider, all sessions say "Claude session". Needs a fallback title strategy (project + branch + first user message, no LLM required). Low effort, high value.
3. **First-session proof point** — After `longhouse connect --install`, user has no confirmation their sessions will actually ship. Add "Waiting for your first real session..." state distinct from demo data.
4. **Oikos Dispatch Contract + Compaction** — Deferred; implement when usage demands it. [Details](#product-harness-simplification--oikos-dispatch-contract-deferred)
5. **Hook Presence Spool via Daemon** — Eliminate noisy async hook banners + fix 2s timeout edge case. [Details](#infra-hook-presence-spool-via-daemon-3)

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
- FastAPI `BackgroundTasks` are fire-and-forget — lost on process crash, no retry, no persistence (`routers/agents.py:792-798`)
- Replace with a lightweight SQLite-backed task queue (pending/running/done rows) polled by a background worker

**Summary is not truly incremental:**
- `_generate_summary_impl` loads *all* events every run, slices in Python (`agents.py:537-607`)
- Fix: store `last_summarized_event_id` on the session; only load events after that cursor

**Embeddings lack per-event cursor:**
- Session-level `needs_embedding` flag, no per-event cursor (`agents.py:663-677`)
- Fix: per-event `embedded` bool or a high-water mark like summary cursor

**Duplicate title/summary pipelines:**
- `title_generator.py` bypasses `models_config` and DB fallback (`title_generator.py:94-156`)
- `summarize_events()` already produces a title — consolidate and drop `title_generator.py`

**Subtasks:**
- [ ] Design SQLite task queue schema (session_id, task_type, status, attempts, error)
- [ ] Replace BackgroundTasks calls with task queue inserts + polling worker
- [ ] Add `last_summarized_event_id` cursor to sessions table; update summary logic
- [ ] Add per-event embedding cursor or high-water mark
- [ ] Consolidate title generation into summarize_events() output; delete title_generator.py
- [ ] `make test` + `make test-e2e` pass

---

## [Product] Harness Simplification — Oikos Dispatch Contract (Deferred)

Phases 1–3h are 100% complete (Commis→Timeline, deprecated Standard mode, Slim Oikos, MCP server, quality gates, multi-provider research). See git history.

**Remaining deferred items (implement when usage demands):**
- [ ] Implement Oikos dispatch contract: direct vs quick-tool vs CLI delegation, with explicit backend intent routing (Claude/Codex/Gemini) and repo-vs-scratch delegation modes
- [ ] Use Claude Compaction API (server-side) or custom summarizer for "infinite thread" context management

Research doc: `docs/specs/3a-deferred-research.md` (commit `16c531e6`)

---

## [Product] Forum Discovery UX + Explicit Presence Signals (7)

Make the Forum the canonical discovery UI for sessions, with **explicit** state signals (no heuristics).

**Status (2026-02-20):** Core presence infrastructure complete. Hooks emit thinking/running/idle. `session_presence` table stores state. Forum UI shows live glow/pulse. Remaining: extended state model + bucket actions.

- [x] Presence ingestion + storage — `session_presence` table, `POST /api/agents/presence`, upsert per session_id.
- [x] Presence emission — `UserPromptSubmit→thinking`, `PreToolUse→running`, `PostToolUse→thinking`, `Stop→idle`.
- [x] Forum UI with real state — active rows glow green, inactive fade, canvas entities pulse.
- [ ] Define extended state model: `needs_user`, `blocked`, `parked`, `resumed` — beyond thinking/running/idle.
- [ ] Add user actions in Forum: Park, Snooze, Archive (emit explicit events, change display state).
- [ ] Add a single "Unknown" state in UI for sessions without signals (no pretending).

---

## [Product] ✅ Session Titles Without LLM

Done. `_set_structured_title_if_empty()` runs when LLM is misconfigured, emitting `project · branch` as `summary_title`. `first_user_message` added to `SessionResponse` + `AgentsStore` + `getSessionTitle()` fallback chain. (commit `95361824`)

---

## [Product] First-Session Proof Point (1)

**Problem:** After `longhouse connect --install`, users see demo data and assume setup is done. No confirmation their sessions will actually ship until they search days later and find nothing.

**Fix:** Distinguish demo-only state from "has real sessions" state. Show a visible "Waiting for your first real session..." indicator when the timeline contains ONLY demo-seeded sessions.

- [ ] Backend: demo sessions already use `device_id="demo-mac"` — can detect without a new column. Add `GET /api/agents/sessions/has-real` (or a field on the sessions list response) that returns whether any non-demo sessions exist
- [ ] Frontend: when all sessions are demo (`device_id == "demo-mac"`), show persistent "Waiting for your first session — use Claude Code, then come back" banner alongside demo cards

---

## [Product] Job Secrets UI — Remaining Polish (1)

Phases 1–2 complete (secrets CRUD, job status, enable/disable, pre-flight UI on `/settings/secrets`). Minor remaining items:

- [ ] Form rendering based on SecretField metadata: `type: "password"/"text"/"url"`, `placeholder`, `description`, `required` validation — backend `SecretField` TypedDict has `type` field but frontend only renders `type="password"` for all secrets
- [x] Keyboard shortcuts: Escape cancels form (commit `95361824`)
- [ ] Loading skeletons while fetching — spinners exist (`<Spinner>`), but no skeleton placeholder layout
- [x] Mobile-responsive layout — confirmed done; `settings.css` has `@media` query switching `.secret-form__fields` to 1 column at ≤768px

**Files:** `apps/zerg/frontend-web/src/pages/JobSecretsPage.tsx`

---

## [Product] ✅ Pre-flight Job Validation — Queue Admission

Done. `_has_missing_required_secrets()` in `commis.py` checks DB + env before `enqueue_scheduled_run()`. Jobs with unconfigured required secrets log a warning and skip instead of failing mid-run. (commit `82e1e213`)

---

## [QA/Test] README Test CI (5)

Automate README command verification with explicit, opt-in contracts. Use cube ARC runners.

- [ ] Define `readme-test` JSON block spec (steps, workdir, env, mode, timeout, cleanup).
- [ ] Implement `scripts/run-readme-tests.sh` (extract + run in temp clone, fail fast, save logs).
- [ ] Add `make test-readmes` target (smoke vs full mode flags).
- [ ] Add GitHub Actions workflow using `runs-on: cube` for PR smoke and nightly full.
- [ ] Add `readme-test` blocks to root README + runner/sauron/hatch-agent READMEs.

---

## [QA/Test] UI QA Screenshot Capture System (1)

**Mostly done.** `scripts/capture_marketing.py` is a full capture CLI (manifest-driven, Playwright, `--name`/`--list`/`--validate` modes). The `zerg-ui` skill at `.agents/skills/zerg-ui/` is the agent-friendly capture path with stable output (PNG, a11y snapshot, trace.zip, console.log, manifest.json). Only remaining gap:

- [ ] Add SCENE=empty backend reset endpoint (or CLI flag) to clear sessions before capture; currently `SCENE=empty` in the skill is frontend-only and has no reliable way to reset DB state

---

## [Docs/Drift] Open Items

- DB size claim stale; prod DB reset 2026-02-05 (no users). Update once real user data exists.
- PyPI version likely lags repo; verify `longhouse` version on PyPI before making release claims.

---

## [Tech Debt] Stable Abstractions (Don't Delete)

Reviewed and intentionally kept — not dead code:

- [ID 41] Legacy modal pattern CSS — 7+ components use `.modal-*` classes, 58 class definitions in `styles/css/modal.css`. Evidence: `ideas/evidence/48_evidence_modal_css_legacy.sh`
- [ID 43] Legacy token aliases — legacy aliases present in `styles/tokens.css`, actively referenced in component CSS. Evidence: `ideas/evidence/50_evidence_tokens_css_legacy_aliases.sh`

---

## [Infra] Life Hub Archive

Longhouse agents schema is fully self-contained. Only remaining Life Hub link: `session_continuity.py` has two backward compat aliases (`fetch_session_from_life_hub`, `ship_session_to_life_hub`) pointing to Zerg equivalents — these are harmless but could be cleaned up when convenient. Smart home + tasks remain active on Life Hub (separate concern, not Longhouse code).
