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

---

## [Infra] Parser fidelity: remove raw_json 32KB cap (size: 4)

Status (2026-03-02): In progress.

**Problem:** `raw_json` in the events table is supposed to be the complete original JSONL source line — the authoritative cloud copy of the user's session. It's currently hard-capped at 32KB (`MAX_RAW_LINE_BYTES` in `apps/engine/src/pipeline/compressor.rs`). A single image-containing Codex message can be 1–10MB of base64. Those lines get truncated, which means Longhouse is NOT a complete copy of the session. This violates the core product promise.

**Root cause confirmed:** In a real session (`8c81e236` on david010 instance), a user message with an attached screenshot was ~826KB. The 32KB cap truncated the raw_json. Combined with the parser bug that emitted zero events for image-only lines (fixed separately), the entire user turn was lost from the cloud record.

**What needs to change:**
- [x] Remove `MAX_RAW_LINE_BYTES` constant and the truncation logic in `compressor.rs` (lines ~38-41, ~132-140)
- The `raw_json` column in SQLite (`TEXT` type) handles arbitrary size; no schema change needed
- The HTTP ingest payload (`EventIngest.raw_json`) is just a JSON string field — no API change needed
- The gzip-compressed ingest payload will still compress well (base64 at 4:3 ratio, gzip recovers much of it)

**Scope / considerations:**
- DB will grow. A session with 50 screenshots could add 50–500MB to the user's DB. That's correct and expected — you need 5GB of images if the user has 5GB of images.
- The ingest HTTP request payload will be larger for image-heavy sessions. May need to bump any request size limits on the backend (check `MAX_CONTENT_LENGTH` / nginx/caddy config on the server).
- [x] Check `apps/zerg/backend/zerg/routers/agents.py` ingest endpoint for any body size limits. (No explicit body clamp found; gzip/zstd paths support streaming decode.)
- [x] Check the Coolify/nginx proxy config for upload limits on the zerg server. (`coolify-proxy` Caddy config has no request-body clamp directives for `david010.longhouse.ai`.)
- [x] After removing the cap, run `make test` — existing test `test_raw_line_cap_truncates` in `compressor.rs` updated to assert full preservation.
- [x] Run `make test-e2e`.

**Files to touch:**
- `apps/engine/src/pipeline/compressor.rs` — remove cap (marked with TODO comment)
- `apps/engine/src/pipeline/compressor.rs` test `test_raw_line_preserves_full_content` — validates no truncation
- Backend proxy/nginx config — check upload size limits
- Possibly `apps/zerg/backend/zerg/routers/agents.py` — check `max_content_length`

**Validation:** After the change, ingest a real Codex session with an image attachment and confirm `length(raw_json)` in the DB equals the full source line length from the JSONL file.

Notes (2026-03-02):
- Removed raw_json truncation in `apps/engine/src/pipeline/compressor.rs`; `raw_json` now forwards the full source line unchanged.
- Replaced truncation test with `test_raw_line_preserves_full_content`.
- Validation run:
  - `cargo test -p longhouse-engine pipeline::compressor::tests` → 4 passed
  - `make test` → 444 backend tests passed, 96 control-plane tests passed, 9 engine parser tests passed
  - `make test-e2e` → 59 core E2E + 4 a11y passed
  - Live ingest check (synthetic image-like payload): posted a 1,200,093-byte `raw_json` line to `https://david010.longhouse.ai/api/agents/ingest`; DB `LENGTH(raw_json)` matched exactly (`1200093`)

---

## [Product] Oikos Thread Context Window Bug (size: 3)

Status (2026-03-02): Done.

**Problem:** Oikos uses a long-lived thread, but current history loading appears to cap to an oldest-message window rather than a latest-message sliding window. On long threads, this can drop recent context and degrade decisions.

- [x] Reproduce with a long oikos thread and confirm current message window behavior end-to-end
- [x] Fix thread message retrieval to feed the most recent window to the LLM while preserving chronological order
- [x] Add regression coverage in `tests_lite/` for long-thread context selection
- [x] Run targeted backend tests and update this task with pass/fail evidence

Notes (2026-03-02):
- Implemented `get_recent_thread_messages()` and switched `ThreadService.get_thread_messages_as_langchain()` to recent-window retrieval.
- Added `tests_lite/test_thread_context_window.py` (latest-100 and custom-limit window assertions).
- Added runner-path verification (`MessageArrayBuilder.with_conversation`) so the same latest-window behavior is validated through message assembly used by execution.
- Targeted test run: `./run_backend_tests_lite.sh tests_lite/test_thread_context_window.py` → 3 passed.

---

## [Product] Oikos Prompt/Tool Contract Alignment (size: 2)

Status (2026-03-02): Done.

**Problem:** Oikos prompt guidance still emphasizes deprecated `spawn_commis` patterns while runtime is workspace-first and `spawn_workspace_commis` is the canonical path.

- [x] Update Oikos base prompt examples/instructions to reflect workspace-first delegation
- [x] Remove/replace deprecated `spawn_commis` guidance from user-facing prompt text
- [x] Keep compatibility alias behavior in code, but stop teaching legacy semantics
- [x] Add/adjust tests that assert prompt/tool contract consistency

Notes (2026-03-02):
- Updated `BASE_OIKOS_PROMPT` to make `spawn_workspace_commis` primary, mark `spawn_commis` as deprecated, and replace stale `wait` parameter guidance with explicit `wait_for_commis(job_id)` usage.
- Added `tests_lite/test_oikos_prompt_contract.py` guardrails for workspace-first wording, removal of `wait=True/False` prompt guidance, and alignment with tool descriptions.
- Targeted test run: `./run_backend_tests_lite.sh tests_lite/test_oikos_prompt_contract.py tests_lite/test_thread_context_window.py` → 5 passed.

---

## [Frontend] Forum/Session Status Normalization (size: 2)

Status (2026-03-02): Done.

**Problem:** Frontend active-session handling has edge-case mismatches between `status` and `presence_state`, and session detail can miss deep anchors on long sessions due to a hard event cap.

- [x] Normalize active/inactive status mapping across Forum + session mapper
- [x] Add unknown/unsupported presence fallback handling for future hook states
- [x] Add session-detail pagination (or equivalent fetch strategy) so deep links beyond first 1000 events resolve reliably

Notes (2026-03-02):
- Added shared session-state helpers in `frontend-web/src/forum/session-status.ts` and wired Forum list/canvas mapping to a single normalization path.
- Presence badges now safely handle unsupported states (e.g. future hook values) without breaking UI state mapping.
- Session detail now uses paginated event loading (`useAgentSessionEventsInfinite`) with auto-fetch for deep-link anchors and manual "Load older events" pagination.
- Frontend verification:
  - `bunx vitest run src/forum/__tests__/session-status.test.ts src/pages/__tests__/ForumPage.test.tsx` → 8 passed
  - `bun run validate:types` → passed

---

## [QA/Test] High-Risk Guardrails (size: 2)

Status (2026-03-02): Done.

- [x] Add presence ingest tests for invalid state no-op and `tool_name` clearing on non-running states
- [x] Add migration guard test that checks SQLite table columns against agents model columns
- [x] Add deterministic dispatch tests (direct response vs quick tool vs commis delegation) in default test suite

Notes (2026-03-02):
- Extended `tests_lite/test_forum_filtering.py` with presence no-op coverage for invalid states and `tool_name` clearing when transitioning away from `running`.
- Added `tests_lite/test_sqlite_migration_guard.py` to simulate legacy SQLite tables and assert `_migrate_agents_columns()` backfills all current model columns for `sessions`, `events`, and `job_runs`.
- Added `tests_lite/test_oikos_dispatch_contract.py` for deterministic dispatch categories:
  - direct response (no tool call),
  - quick utility tool call,
  - commis delegation.
- Targeted test run: `./run_backend_tests_lite.sh tests_lite/test_forum_filtering.py tests_lite/test_sqlite_migration_guard.py tests_lite/test_oikos_dispatch_contract.py` → 15 passed.

---

## [Product] Landing Screenshot Frame Contrast Variants (size: 1)

Status (2026-02-27): Done.

**Goal:** Add two visual frame variants for landing screenshots so warm-page screenshots still pop and can be compared quickly in-browser.

- [x] Add a second screenshot frame theme with cooler/high-contrast treatment
- [x] Wire screenshot frame theme through landing hero + product showcase
- [x] Add an in-browser toggle for quick visual comparison in marketing mode

## [QA/Test] Verify Session Resume End-to-End (size: 4)

Status (2026-02-26): Core verification shipped.

**Goal:** Validate the core promise ("resume from any device") with deterministic E2E coverage and fix any behavioral gaps discovered.

- [x] Run targeted resume E2E coverage and capture current failures
- [x] Fix resume path regressions (backend and/or frontend) found by E2E (none found in current flow)
- [x] Add/adjust assertions so resume guarantees are explicit (not implied by generic session continuity)
- [x] Ship with passing `make test-e2e-single TEST=tests/core/session-continuity.spec.ts` and `make test-e2e`

Notes (2026-02-26):
- Added user-facing resume coverage in `apps/zerg/e2e/tests/core/sessions.spec.ts`:
  - Claude sessions show `Resume Session` and open the chat overlay
  - Non-Claude sessions hide `Resume Session`

---

## [Docs/Drift] Resolve VISION Contradictions (size: 1)

Status (2026-02-26): Done.

- [x] Updated Principles section to cloud-first CTA (hosted primary; self-hosted supported)
- [x] Replaced old "Oikos main chat" ASCII diagram with timeline/session-first product diagram

---

## [Product] Quota Error UX in Oikos Chat (size: 2)

Status (2026-02-26): Done (first-principles UI + behavior).

- [x] Parse 429 JSON detail in Oikos chat requests (don't discard backend detail)
- [x] Convert run-cap/budget errors into clear reset-time messaging
- [x] Show quota context in-chat for the active assistant bubble instead of raw generic failure
- [x] Add always-visible quota panel in Oikos header (health/warning/blocked, progress, remaining, runs)
- [x] Block new input only on true quota exhaustion (keep normal post-run unblock behavior intact)

## [Tech Debt] Commis model selection — remove implicit defaults (size: 3)

Status (2026-02-26): Done (shipped in `c0872450`).

**Problem:** `DEFAULT_COMMIS_MODEL_ID` resolves via `models.json` tiers to `gpt-5.2` (OpenAI). This gets stored on `CommisJob.model` and passed as `--model gpt-5.2` to hatch, which passes it to Claude Code CLI — nonsensical for non-OpenAI backends. Two spawn paths are both broken:
- `oikos_tools.py:103` — falls back to `DEFAULT_COMMIS_MODEL_ID`
- `oikos_react_engine.py:615` — hardcoded fallback to `"gpt-5-mini"`

`CloudExecutor` always passes `--model`, so hatch backend defaults never kick in.

**Fix:**
Make `model` an explicit override only, not a forced default. New execution logic in `cloud_executor.py`:
- `backend + model` both provided → pass both (`-b <backend> --model <model>`)
- `backend` only → pass `-b` only, let hatch pick the backend's default model
- `model` only → infer backend via compat mapping (see below)
- neither → full hatch defaults (zai + glm-5)

**Backend ↔ model compat mapping** (for backward compat when bare model ID is passed):
- `gpt-*`, `o1-*`, `o3-*`, `o4-*` → `codex` backend
- `claude-*`, `us.anthropic.*` → `bedrock` backend (or direct anthropic if key available)
- `glm-*` → `zai` backend
- `gemini-*` → `gemini` backend
- anything else → error loudly, don't silently use wrong backend

**Files to change:**
- `apps/zerg/backend/zerg/tools/builtin/oikos_tools.py:103` — remove `DEFAULT_COMMIS_MODEL_ID` fallback
- `apps/zerg/backend/zerg/services/oikos_react_engine.py:615,704` — remove `"gpt-5-mini"` hardcode
- `apps/zerg/backend/zerg/services/cloud_executor.py:75,203` — implement new logic (backend-only path)
- `apps/zerg/backend/zerg/models_config.py:182` — remove or deprecate `DEFAULT_COMMIS_MODEL_ID`
- `apps/zerg/backend/zerg/models/models.py:220` — keep `CommisJob.model` nullable (don't rebuild SQLite table; just allow None)

**Don't:** make `CommisJob.model` NOT NULL migration on SQLite — table rebuild is risky. Just allow None and skip `--model` flag when absent.

**Tests to write first** (`tests_lite/`): no backend tests exist for the commis→cloud_executor→hatch path. Add at least: model+backend passed → correct hatch args; backend-only → no --model flag; unknown model → error.

**Follow-up (2026-03-02):**
- [x] Scenario seeding no longer forces `"gpt-5-mini"` when `commis_jobs[].model` is omitted (`scenarios/seed.py` + regression test).

---

~~**[Tech Debt] Reconcile historical Codex orphan sessions (size: 3)**~~ Done (2026-02-25). 129 orphans merged (7,446 events re-parented) via `scripts/fix_codex_orphan_sessions.py`. Used `source_path` filename to extract canonical UUID — no time-window ambiguity. Prod DB updated, 0 null-project Codex sessions remain.


1. ~~**[Launch] Move Settings into user menu** — remove it from any primary nav surface; add to header user dropdown + mobile menu.~~ Done (2026-02-25).
2. ~~**[Launch] Briefings as Timeline secondary action** — add a Timeline header action to open `/briefings` instead of a primary nav tab.~~ Done (2026-02-25).
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

## [Product] Oikos Fiche Surface

**Status (2026-03-02):** Fixed.

- [x] `/api/oikos/fiches` now returns persisted `fiche.next_run_at` (was always `null` even when scheduler populated it).
- [x] Added API-lite regression test coverage for owner scoping + `next_run_at` serialization.

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

Research doc: `apps/zerg/backend/docs/specs/3a-deferred-research.md`

---

## [Tech Debt] Schema Migration

**Resolved (2026-02-22):** `_migrate_agents_columns()` in `database.py` now covers all
current columns. **Rule:** every new `Column` on an agents model must get a corresponding
`ALTER TABLE` entry in that function — SQLite ignores new columns on existing tables.

- [x] `last_summarized_event_id`, `user_state`, `user_state_at` — added to migration
- [ ] No current gaps known — watch for new columns added without migration entries

---

## [Docs/Drift] Open Items

- ~~`docs/install-guide.md` still references `longhouse connect --poll` behavior that no longer matches runtime engine behavior.~~ Fixed in docs + CLI help sync (2026-03-02).
- ~~Keep VISION "Current State" sections in sync with hook installation/runtime details whenever hook registration behavior changes.~~ Synced shipper/hook examples + command docs (2026-03-02).
- ~~PyPI `0.1.1` lags repo `0.1.2`.~~ Published as v0.1.3 (2026-02-24).

---

## [Tech Debt] CSS Legacy Patterns

- Legacy modal pattern CSS — `.modal-*` classes still exist across multiple components/stylesheets and both modal systems remain loaded.
- Legacy token aliases — present in `styles/tokens.css`, actively referenced in component CSS.

---

## [Tech Debt] Newly Verified Follow-ups (2026-03-02)

- [x] Surface `register_all_jobs()` import/registration failures as actionable health status (exposed via `registration_warnings` on `GET /api/jobs` and shown in Jobs UI) (2026-03-02).
- [x] Added frontend coverage for JobsPage registration warning rendering (`pages/__tests__/JobsPage.test.tsx`).
- [x] Eliminated control-plane test deprecation warnings by migrating to `SettingsConfigDict` and FastAPI lifespan startup hook (2026-03-02).
- [x] Added semantic-search regression tests to enforce `hide_autonomous` filtering of sidechain and zero-user sessions.
- [x] Added SSRF regression coverage for LLM provider base URLs (metadata hostname + DNS-rebinding-to-private-IP cases).
