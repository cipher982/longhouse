# TODO

Capture list for substantial work. Not quick fixes (do those live).

## For Agents

- Each entry is a self-contained handoff — read it, you have context to start
- Size (1-10) indicates scope: 1 = hour, 5 = day, 10 = week+
- Check off subtasks as you go so next agent knows state
- Add notes under tasks if you hit blockers or learn something

---

## HN Launch Readiness — Zero to Ship (8)

Make Longhouse ready for Hacker News launch. The product works but the story/UX isn't ready for public scrutiny.

**Problem:** User journey has critical gaps. Install works, but then what? Empty timeline = "Is this broken?" No screenshot = no trust. Value prop unclear.

**Goal:** HN reader can install, see value immediately, understand what problem this solves, and start using it.

### Critical (Blockers — Must Fix Before HN)

- [x] **Screenshot/GIF in README** (30 min) ✅ DONE 2026-02-02
  - ✅ Created automated Playwright capture script
  - ✅ Captured timeline view with demo sessions
  - ✅ Captured landing page
  - ✅ Embedded in README
  - ✅ Files: `timeline-screenshot.png`, `landing-hero.png`
  - Script: `bun run capture:screenshots`

- [x] **Seed demo sessions infrastructure** (1 hour) ✅ DONE 2026-02-02
  - ✅ Added `/api/system/seed-demo-sessions` endpoint (routers/system.py)
  - ✅ Uses existing `demo_sessions.py` (2 sessions: onboarding review, session query)
  - ✅ Integrated into screenshot capture script
  - ⚠️ Still TODO: Auto-seed on `longhouse onboard` if timeline empty (needs frontend changes)

- [x] **Fix README branding** (15 min) ✅ DONE 2026-02-02
  - ✅ Removed confusing naming note (Oikos/Zerg explanation)
  - ✅ Added 🪵 logs emoji to title
  - ✅ Install URL already using `get.longhouse.ai`
  - ✅ Unified license to Apache-2.0 across all files

- [x] **Rewrite value proposition** (30 min) ✅ DONE 2026-02-02
  - ✅ New tagline: "Never lose an AI coding conversation again"
  - ✅ Added "The Problem" section (relatable pain: grepping JSON)
  - ✅ Added "The Solution" section (searchable timeline)
  - ✅ Bullet points with concrete use cases
  - ✅ Added "Why Longhouse?" section (logs metaphor)

- [ ] **Empty state UI** (1 hour) - Blocked by Worker 3 (frontend)
  - Replace blank timeline with "Getting Started" card when no sessions exist
  - Show: "1. Install complete ✓  2. Connect Claude Code  3. Start coding"
  - Link to setup guide
  - Show demo data option: "Or explore with demo sessions"
  - File: `apps/zerg/frontend-web/src/pages/TimelinePage.tsx` (or wherever timeline lives)

### High Priority (Polish Before Launch)

- [x] **Add GitHub badges** (15 min) ✅ DONE 2026-02-02
  - ✅ Tests passing badge (link to GitHub Actions)
  - ✅ Version badge (link to PyPI)
  - ✅ License badge (Apache-2.0)
  - ✅ Python version badge (3.12+)

- [x] **"What happens next" section in README** (20 min) ✅ DONE 2026-02-02
  - ✅ Step-by-step post-install guide
  - ✅ Clear commands section
  - ✅ Feature checklist (✅ vs 🚧)

- [ ] **Landing page / product story alignment** (1 hour) - Blocked by Worker 3 (frontend)
  - Landing says "Your Personal AI Hub" but product is timeline viewer
  - Pick ONE story: Timeline focus OR hub focus
  - Recommendation: Timeline = clearer, more focused value prop
  - Update landing hero, meta descriptions, copy
  - Files: `apps/zerg/frontend-web/src/components/landing/HeroSection.tsx`, meta tags

- [ ] **Demo mode flag** (2 hours)
  - Add `longhouse serve --demo` flag that starts with pre-loaded sessions
  - Allows exploration without Claude Code setup
  - Show banner: "Demo Mode - exploring sample data"
  - File: `apps/zerg/backend/zerg/cli/serve.py`

- [x] **HN post title + copy** (30 min) ✅ DONE 2026-02-02
  - ✅ Created `docs/HN_LAUNCH.md` with:
    - 4 title options
    - Full launch comment draft
    - Anticipated Q&A
    - Pre-launch checklist
    - Timing recommendations

### Medium Priority (Nice to Have)

- [ ] **Social proof** (if available)
  - Add testimonial or "Built by X" to README
  - Show usage stats if you have any early users
  - Link to personal Twitter/GitHub for credibility

- [ ] **Video walkthrough** (optional, 2 hours)
  - 60-90 second Loom showing:
    1. The problem (searching through JSONL files)
    2. Install in one line
    3. Timeline appears with sessions
    4. Search + detail view
  - Add to README + landing page
  - More engaging than screenshot alone

- [ ] **Comparison table** (30 min)
  - Show how Longhouse compares to:
    - grep through JSONL files (old way)
    - Claude Code built-in history (limited)
    - Not tracking at all (disaster)
  - Table showing: searchable, cross-tool, persistent, visual timeline

---

## Public Launch Checklist (from archived doc) (6)

Ensure launch readiness without relying on scattered docs.

- [ ] Rewrite README to center Timeline value and 3 install paths.
- [ ] Add CTA from Chat to “View session trace” after a run.
- [ ] Improve Timeline detail header (goal, repo/project, duration, status).
- [ ] Add basic metrics (tool count, duration, latency if available).
- [ ] Add filters within detail view (user/assistant/tool) + search.
- [ ] Core UI smoke snapshots pass (`make qa-ui-smoke`).
- [ ] Shipper smoke test passes (if shipper path is enabled).
- [ ] Add packaging smoke test for future install.sh/brew path (if shipped).

---

## README Test CI (Readme-Contract) (5)

Automate README command verification with explicit, opt-in contracts. Use cube ARC runners where possible.

- [ ] Define `readme-test` JSON block spec (steps, workdir, env, mode, timeout, cleanup).
- [ ] Implement `scripts/run-readme-tests.sh` (extract + run in temp clone, fail fast, save logs).
- [ ] Add `make test-readmes` target (smoke vs full mode flags).
- [ ] Add GitHub Actions workflow using `runs-on: cube` for PR smoke and nightly full.
- [ ] Add `readme-test` blocks to root README + runner/sauron/hatch-agent READMEs.
- [ ] Optional: failure triage via `hatch` agent (summarize logs, suggest fix).

---

## Life Hub Dependency Removal — Sessions/Forum/Resume (7)

Make Longhouse the sole source of session truth. Remove Life Hub routes, naming, and test dependencies in OSS/runtime.

**Deliverables:** Session picker + Forum + session resume all use Longhouse agents data; no LIFE_HUB_API_KEY needed for OSS/E2E.

- [ ] Replace `/oikos/life-hub/*` endpoints with agents-backed endpoints (or reuse `/api/agents/sessions`); delete `oikos_life_hub.py` router.
- [ ] Update frontend hooks/components (`useLifeHubSessions`, `useActiveSessions`, `SessionPickerModal`, Forum) to hit the new endpoints and rename to “sessions/timeline”.
- [ ] Update session-resume flow to call `ship_session_to_zerg`/`fetch_session_from_zerg` directly (remove Life Hub naming in `session_chat`, `oikos_tools`, etc.).
- [ ] Replace Life Hub integration tests (backend + E2E) with local ingest/export tests; remove LIFE_HUB_API_KEY requirement in OSS runs.
- [x] Update VISION session-resume section to remove Life Hub flow (doc consolidated).

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

## OSS One-Liner Installer + Onboard Wizard (6) ✅

Make the one-liner + TUI wizard the **default** OSS onboarding path.

**Deliverables:** `curl -fsSL https://get.longhouse.ai/install.sh | bash` installs CLI + Claude shim, verifies PATH, then runs `longhouse onboard`.

- [x] Create `scripts/install.sh` (one-liner entrypoint) that installs CLI + Claude shim + verification.
- [x] Implement `longhouse onboard` TUI (QuickStart default, Manual option; no .env edits).
- [x] Add shim verification: check `command -v claude` in a fresh shell; if failed, print one exact fix line.
- [x] Update README quickstart + onboarding contract to use the one-liner + wizard.
- [x] Add a fallback message for unique shells (fish/other) with exact instructions.
- [x] Publish `longhouse` package to PyPI (v0.1.1 live)
- [x] Set up trusted publishing workflow (GitHub Actions → PyPI via OIDC)
- [x] Set up vanity URL redirect (get.longhouse.ai → raw GitHub)
- [x] Cross-platform installer CI (Ubuntu 22/24, macOS Intel/ARM)

**Done 2026-02-02:** Core onboarding flow complete. PyPI package published, installer working on all platforms.

---

## OSS First-Run UX Polish (5)

Eliminate the "empty timeline" anticlimactic moment and improve discovery for users without Claude Code.

**Deliverables:** New users see value immediately; timeline isn't empty; clear next steps regardless of setup.

- [ ] Seed demo session data on first `longhouse onboard` run (shows what the timeline looks like)
- [ ] Add README screenshot/gif showing the timeline UI (visual proof of value)
- [ ] Improve "No Claude Code" guidance in onboard wizard (link to alternatives, explain what to do next)
- [ ] Add "Getting Started" card in empty timeline state (instead of just blank screen)
- [ ] Consider demo mode flag for `longhouse serve --demo` (starts with pre-loaded sessions for exploration)

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
- [x] Update VISION onboarding section (and TODO checklist) to reflect Longhouse naming
- [x] Add branding usage rules to VISION naming section

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
- [x] Update e2e helpers to reflect SQLite-only test flow (2026-02-02)
