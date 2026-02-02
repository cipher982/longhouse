# TODO

Capture list for substantial work. Not quick fixes (do those live).

## For Agents

- Each entry is a self-contained handoff — read it, you have context to start
- Size (1-10) indicates scope: 1 = hour, 5 = day, 10 = week+
- Check off subtasks as you go so next agent knows state
- Add notes under tasks if you hit blockers or learn something

---

## Life Hub Dependency Removal — Sessions/Forum/Resume (7)

Make Longhouse the sole source of session truth. Remove Life Hub routes, naming, and test dependencies in OSS/runtime.

**Deliverables:** Session picker + Forum + session resume all use Longhouse agents data; no LIFE_HUB_API_KEY needed for OSS/E2E.

- [ ] Replace `/oikos/life-hub/*` endpoints with agents-backed endpoints (or reuse `/api/agents/sessions`); delete `oikos_life_hub.py` router.
- [ ] Update frontend hooks/components (`useLifeHubSessions`, `useActiveSessions`, `SessionPickerModal`, Forum) to hit the new endpoints and rename to “sessions/timeline”.
- [ ] Update session-resume flow to call `ship_session_to_zerg`/`fetch_session_from_zerg` directly (remove Life Hub naming in `session_chat`, `oikos_tools`, etc.).
- [ ] Replace Life Hub integration tests (backend + E2E) with local ingest/export tests; remove LIFE_HUB_API_KEY requirement in OSS runs.
- [ ] Update docs that still reference Life Hub: `docs/session-resume-design.md`, `docs/experiments/shipper-manual-validation.md`.

---

## Forum Discovery UX + Explicit Presence Signals (7)

Make the Forum the canonical discovery UI for sessions, with **explicit** state signals (no heuristics).

**Deliverables:** "Active/Needs You/Parked/Completed/Unknown" are driven by emitted events, not inference.

- [ ] Define a session presence/state event model (`session_started`, `heartbeat`, `session_ended`, `needs_user`, `blocked`, `completed`, `parked`, `resumed`) and document it.
- [ ] Add ingestion + storage for presence events in the agents schema (SQLite-safe).
- [ ] Update the Forum UI to group by explicit buckets and remove heuristic "idle/working" logic.
- [ ] Add user actions in Forum: Park, Snooze, Resume, Archive (emit explicit events).
- [ ] Wire wrappers to emit `session_started`/`heartbeat`/`session_ended` (Claude/Codex first).
- [ ] Add a single "Unknown" state in UI for sessions without signals (no pretending).

---

## OSS One-Liner Installer + Onboard Wizard (6)

Make the one-liner + TUI wizard the **default** OSS onboarding path.

**Deliverables:** `curl -fsSL https://longhouse.ai/install.sh | bash` installs CLI + Claude shim, verifies PATH, then runs `longhouse onboard`.

- [ ] Create `scripts/install.sh` (one-liner entrypoint) that installs CLI + Claude shim + verification.
- [ ] Implement `longhouse onboard` TUI (QuickStart default, Manual option; no .env edits).
- [ ] Add shim verification: check `command -v claude` in a fresh shell; if failed, print one exact fix line.
- [ ] Update README quickstart + onboarding contract to use the one-liner + wizard.
- [ ] Add a fallback message for unique shells (fish/other) with exact instructions.

---

## Longhouse Home Dir + Path Cleanup (4)

Unify local paths under `~/.longhouse` and remove legacy `~/.zerg` naming + env vars.

**Deliverables:** CLI defaults, shipper state, skills, and workspace paths all use Longhouse naming; no `/var/oikos` defaults in OSS.

- [ ] Rename `~/.zerg` → `~/.longhouse` across CLI defaults (`cli/serve.py`), dev scripts (`scripts/dev.sh`), and skills loader.
- [ ] Rename shipper state/token/url files in `~/.claude` from `zerg-*` to `longhouse-*`; update `connect`/`shipper` helpers.
- [ ] Rename env var `ZERG_API_URL` → `LONGHOUSE_API_URL` in session continuity + shipper; update defaults/docs.
- [ ] Change default workspace base paths from `/var/oikos/workspaces` + `/tmp/zerg-session-workspaces` to `~/.longhouse/workspaces` (server overrides via env).

---

## Memory Store SQLite Pass (3)

Ensure Oikos memory tools are SQLite-safe and do not assume Postgres.

- [ ] Decide keep vs remove memory in OSS; if kept, rename `PostgresMemoryStore` and make queries SQLite-safe.
- [ ] Add lite tests for memory save/search/delete on SQLite.
- [ ] Update `oikos_memory_tools.py` examples/copy to remove Postgres references.

---

## OSS Packaging Decisions (3)

Close the remaining open questions from VISION.md (SQLite-only OSS Pivot section).

- [ ] Confirm PyPI availability for `longhouse` (or pick fallback name) and document final choice.
- [ ] Decide whether the shipper is bundled with the CLI or shipped as a separate package.
- [ ] Decide remote auth UX for `longhouse connect` (device token vs OAuth vs API key).
- [ ] Decide HTTPS story for local OSS (`longhouse serve`) — built-in vs reverse proxy guidance.
- [ ] Capture current frontend bundle size and set a target budget.

---

## Longhouse Rebrand — Docs + Naming Map (5)

Establish a single public brand (Longhouse) while keeping Oikos as assistant UI and Zerg as internal codename. Docs must align with VISION + OSS onboarding.

**Deliverables:** clear naming map, updated VISION + OSS docs + README, consistent user-facing language.

- [x] Decide and record naming map: **Longhouse** (umbrella), **Oikos** (assistant), **Zerg** (internal codename only)
- [x] Update `VISION.md` to “Longhouse Vision” + add a short naming note (Oikos/Zerg)
- [x] Update VISION.md (SQLite-only OSS Pivot section) to Longhouse naming + CLI examples
- [x] Update `README.md` to Longhouse branding + domain references
- [x] Update `docs/public-launch-task.md` and `VISION.md` onboarding section to reflect Longhouse naming
- [x] Add a short `docs/BRANDING.md` with the naming map + do/don’t usage rules

---

## Longhouse Rebrand — Product/Meta Strings (6)

User-facing strings, metadata, and package descriptions must stop mentioning Swarmlet/Zerg as a brand.

**Scope:** 105 occurrences across 28 frontend files, 124 occurrences across 39 backend files (229 total)

**Files:** `apps/zerg/frontend-web/index.html`, `apps/zerg/frontend-web/public/site.webmanifest`, `package.json`, runner docs, email templates

- [ ] Replace "Swarmlet" with "Longhouse" in frontend HTML metadata + webmanifest
- [ ] Update `package.json` description to Longhouse naming
- [ ] Update runner README/package metadata to Longhouse (e.g., "Longhouse Runner")
- [ ] Update email templates / notification copy referencing Swarmlet
- [ ] Decide domain swap (`swarmlet.com` → `longhouse.ai`) and update hardcoded URLs if approved
- [ ] Update landing FAQ + marketing copy that still says “PostgreSQL” or “Swarmlet” (e.g., `TrustSection.tsx`)
- [ ] Update OpenAPI schema metadata (title/description/servers) to Longhouse and regenerate `openapi.json` + frontend types

---

## Prod CSP Fixes — Longhouse (1) ✅

Unblock blob image previews + Cloudflare beacon by updating CSP in frontend nginx entrypoint.

- [x] Allow `blob:` in `img-src`
- [x] Allow `https://static.cloudflareinsights.com` in `script-src`

**Done 2026-02-01:** CSP headers updated in `entrypoint.sh`.

---

## Prod Console Noise — Auth + Funnel (1)

Eliminate unauth 401 spam and fix funnel 403 after rebrand.

- [x] Add `/auth/status` to avoid 401 on initial load
- [x] Allow `longhouse.ai` origins in funnel tracking

---

## Public Origin Config — Centralize (2)

Make public origins discoverable and derived from config instead of hard-coded lists.

- [x] Add `PUBLIC_SITE_URL`/`PUBLIC_API_URL` to settings and helpers
- [x] Use helpers for CORS + funnel origin checks and add tests

---

## Longhouse Rebrand — CLI / Packages / Images (7)

Package and binary naming so OSS users see Longhouse everywhere.

**Scripts needing update:** `install-runner.sh` (2 refs), `smoke-prod.sh` (2 refs), `run-prod-e2e.sh` (2 refs), `product-demo.yaml` (6 refs)

- [x] Decide PyPI package name: `longhouse` vs fallback (`longhouse-ai`) → **`longhouse`**
- [x] Decide CLI binary name: `longhouse` vs fallback (`longhousectl`) → **`longhouse`**
- [ ] Decide npm scope/name for runner: `@longhouse/runner` or `longhouse-runner`
- [ ] Update docker image name for docs/examples (ghcr.io/.../longhouse)
- [ ] Update installer scripts to new names (12 refs across 4 scripts)

---

## Frontend Bundling for pip Package (2) ✅

Bundle frontend assets into pip package using importlib.resources. Final polish for SQLite OSS pivot.

**Why:** `pip install longhouse && longhouse serve` works with bundled frontend.

**Files:** `apps/zerg/backend/pyproject.toml`, `apps/zerg/backend/zerg/main.py`

- [x] Configure hatch to include `frontend-web/dist/` in package (force-include in pyproject.toml)
- [x] Update FastAPI static mount to use `importlib.resources` for packaged assets (main.py)
- [x] Test: `pip install longhouse` → UI loads (verified locally)

---

## Prompting Pipeline Hardening (3)

Unify prompt construction across `run_thread`, `run_continuation`, and `run_batch_continuation` to eliminate divergence in tool loading, usage capture, and persistence.

**Why:** Current flows have subtle differences that cause bugs. Memory query behavior varies, tool results can duplicate.

**Files:** `managers/fiche_runner.py`, related service files

**Status: ~80% complete.** Infrastructure exists — just needs FicheRunner wiring.

- [x] Introduce `PromptContext` dataclass (system + conversation + tool_messages + dynamic_context) — exists in `prompting/context.py`
- [x] Create unified `build_prompt()` helper — exists in `prompting/builder.py`
- [x] Extract single `derive_memory_query(...)` helper — exists in `prompting/memory.py`
- [ ] Wire PromptContext through FicheRunner flows (run_thread, run_continuation, run_batch_continuation)
- [ ] Add DB-level idempotency for tool results (unique constraint or `get_or_create`)
- [ ] Add prompt snapshot test fixture for regression checks

---

## Prompt Cache Optimization (5)

**Depends on:** Prompting Pipeline Hardening (unified helper changes message layout)

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

**Files:** `managers/fiche_runner.py` (search: `_build_messages` and `_inject_dynamic_context`)

**Principles:**
- Static content at position 0 (tools, system prompt)
- Conversation history next (extends cacheable prefix)
- Dynamic content LAST (connector status, RAG, timestamps)
- Never remove tools — return "disabled" instead

- [ ] Reorder message construction in fiche_runner
- [ ] Verify cache hit rate improves (add logging/metrics)
- [ ] Document the ordering contract

---

## Live Commis Tool Events via Claude Code Hooks (8) ✅

Workspace commis currently emit only `commis_started` and `commis_complete` — no visibility during execution. This task adds **real-time tool event streaming** using Claude Code hooks.

**Why:** UI shows black box during workspace commis (30-60 min). Users can't see what's happening. Live visibility enables monitoring, early cancellation, and debugging.

**Approach:** Claude Code hooks (`PostToolUse`, `PreToolUse`) fire during hatch execution and POST events to Longhouse API. Longhouse emits SSE events to UI in real-time.

**Architecture:**
```
hatch (claude --print)
    └── PostToolUse hook → POST /api/internal/commis/tool_event
                               └── SSE: commis_tool_completed → UI
```

### Phase 1: Hook Infrastructure ✅
**Files:** `config/claude-hooks/`, `scripts/deploy-hooks.sh`

- [x] Create `config/claude-hooks/settings.json` with PreToolUse + PostToolUse hooks
- [x] Create `config/claude-hooks/scripts/tool_event.py` — POSTs to Longhouse
- [x] Create `scripts/deploy-hooks.sh` — deploys to zerg server
- [x] Test locally: verify hooks fire and POST correctly

### Phase 2: Backend API ✅
**Files:** `routers/oikos_internal.py`, `events/commis_emitter.py`

- [x] Add `POST /api/internal/commis/tool_event` endpoint
- [x] Validate job_id exists and is running
- [x] Auth: internal token (X-Internal-Token)
- [x] Emit SSE events: `commis_tool_started`, `commis_tool_completed`

### Phase 3: Environment Plumbing ✅
**Files:** `services/commis_job_processor.py`, `services/cloud_executor.py`

- [x] Pass env vars to hatch: `LONGHOUSE_CALLBACK_URL`, `COMMIS_JOB_ID`, `COMMIS_CALLBACK_TOKEN`
- [x] Use internal token for auth (`COMMIS_CALLBACK_TOKEN = INTERNAL_API_SECRET`)
- [x] Ensure hooks can reach Longhouse API (loopback default; override via `LONGHOUSE_CALLBACK_URL`)

### Phase 4: Frontend ✅
**Files:** `frontend-web/src/hooks/`, `frontend-web/src/components/`

- [x] Handle new SSE events in existing listener
- [x] UI component showing live tool calls during commis
- [x] Icons/labels per tool type (Edit, Bash, Read, etc.)

### Phase 5: Polish (optional)
- [ ] Error handling in hook script (retry, timeout)
- [ ] Rate limiting (debounce rapid tool calls)
- [ ] Optional: persist events for replay after completion
- [ ] Update docs with hook deployment instructions

**Done 2026-02-01:** All 4 core phases complete. Hook infrastructure, backend API, env plumbing, and frontend all implemented and working.

**Docs:** Claude Code hooks reference: https://docs.anthropic.com/en/docs/claude-code/hooks

---

## Sauron /sync Reschedule (3) ✅

~~`/sync` endpoint reloads manifest but APScheduler doesn't reschedule existing jobs.~~

**Status:** Complete. Implementation in `registry.py:sync_jobs()`. Verified in prod 2026-02-01.

- [x] On sync, diff old vs new jobs
- [x] Remove jobs no longer in manifest
- [x] Reschedule jobs with changed cron expressions
- [ ] Add test coverage (optional — works in prod)

---

## Docker Build: uv sync Failure (2) ✅

`zerg-api` Coolify deploy fails at `uv sync --frozen --no-install-project --no-dev` (exit code 2).

**Error location:** `docker/backend.dockerfile:71`

**Files:** `docker/backend.dockerfile`, `apps/zerg/backend/uv.lock`, `apps/zerg/backend/pyproject.toml`

- [x] Investigate uv sync failure — likely lockfile mismatch or missing dependency
- [x] Test build locally: `docker build -f docker/backend.dockerfile .`
- [x] Fix and redeploy

**Done 2026-02-01:** Docker builds successfully after lockfile fixes.

---

## E2E SQLite Database Init Failure (2)

E2E tests fail with "no such table: users" / "no such table: commis_jobs". The per-worker SQLite databases aren't being initialized with schema.

**Error:** `sqlite3.OperationalError: no such table: users`

**Files:** `apps/zerg/e2e/global-setup.ts`, `apps/zerg/e2e/playwright.config.ts`

- [x] Investigate why globalSetup creates DB files but doesn't run migrations (2026-02-01: Added /health/db endpoint)
- [x] Ensure `initialize_database()` is called for each per-worker SQLite (2026-02-01: Playwright now waits for /health/db)
- [x] Fix reset-database to include AgentsBase tables (2026-02-01: Added to admin.py)
- [x] Verify E2E tests pass after fix (2026-02-02)
- [x] Remove Postgres schema isolation assumptions; keep `X-Test-Commis` for SQLite routing (2026-02-02)
- [x] Update `apps/zerg/e2e/README.md` + helpers to reflect SQLite-only test flow (2026-02-02)
