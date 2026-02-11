# TODO

Capture list for substantial work. Not quick fixes (do those live).

## For Agents

- Each entry is a self-contained handoff — read it, you have context to start
- Size (1-10) indicates scope: 1 = hour, 5 = day, 10 = week+
- Check off subtasks as you go so next agent knows state
- Add notes under tasks if you hit blockers or learn something

Classification tags (use on section headers): [Launch], [Product], [Infra], [QA/Test], [Docs/Drift], [Tech Debt], [Brand]

---

## 📊 Validation Summary (2026-02-05, rev 3 — post-work)

**Full codebase audit + 4 tasks completed this session.**

### ✅ DONE / VERIFIED
| Section | Status | Notes |
|---------|--------|-------|
| P0 Launch Core | ✅ 100% | All 6 items verified (auth, demo, CTAs, README, FTS5, QA script) |
| Post-GA Follow-ups | ✅ 100% | All five items verified in code |
| OSS Auth | ✅ 100% | Password login + rate limiting + hash support |
| FTS5 Search (Phase 1+2) | ✅ 100% | FTS5 index + triggers + search + snippets + Oikos tools all done |
| CI Stability (E2E isolation) | ✅ ~90% | Dynamic ports, per-run DB, artifact upload done; only schedule gate missing |
| Rebrand (core) | ✅ ~95% | Core Swarmlet refs removed (commit `888bc5ad`); only experiments/evidence docs remain |

### ⚠️ PARTIALLY DONE
| Section | Status | Notes |
|---------|--------|-------|
| Landing Page Redesign | ~80% | Header ✅ Hero CTAs ✅ DeploymentOptions ✅ Contrast/WCAG ✅ Story ✅; remaining: some Phase 5 copy polish |
| HN Launch Readiness | ~75% | CTAs fixed; remaining: comparison table, video, social proof |
| Prompt Cache Optimization | ~70% | Message layout done, timestamp granularity fixed; missing sort_keys, split dynamic |
| Install/Onboarding | ~65% | install.sh works, 3.12+, connect URL fixed (`426f8c9b`), doctor done; missing fresh-shell verify |
| Control Plane | ~40% | Scaffold + provisioner + admin UI + CI gate; OAuth/billing/token mismatch pending |
| OSS First-Run UX | ~40% | `--demo/--demo-fresh` works; no auto-seed on onboard, no guided empty state |

### ❌ NOT STARTED
| Section | Status | Notes |
|---------|--------|-------|
| FTS5 Phase 3 (Embeddings) | 0% | No semantic search, no sqlite-vec |
| Forum Discovery UX | 0% | Heuristic status only. No presence events, no bucket UI |

### 🐛 CRITICAL BUG
- Control plane `accept-token`: `sub=email` in token payload vs `sub=numeric_user_id` expected by instance auth → hosted login will fail

### Session Changes
> Detailed changelogs archived. See git log for 2026-02-05 and 2026-02-06 sessions.

---

## [Product] 🧠 Harness Simplification & Commis-to-Timeline (8)

**Goal:** Stop building our own agent harness. Lean on CLI agents (Claude Code, Codex, Gemini CLI). Make commis output visible in the timeline. Remove ~25K LOC of dead code.

**Spec:** `apps/zerg/backend/docs/specs/unified-memory-bridge.md` (renamed: Harness Simplification)

### Phase 1: Commis → Timeline Unification (3)
- [x] Verify workspace mode hatch produces session JSONL and find its output path
- [x] After workspace hatch completes, ingest session JSONL via `AgentsStore.ingest_session()` (`_ingest_workspace_session()` in commis_job_processor.py)
- [x] Tag commis sessions with metadata (environment=commis, commis_job_id) for filtering
- [x] Timeline UI: show commis sessions alongside shipped sessions
- [x] Add filter option in Timeline to show/hide commis vs terminal sessions
- [x] Expose `environment` filter end-to-end in Timeline UI (`commis|production|development|test|e2e`) and show source badge on session cards.
- [x] Add regression test: completed workspace commis session appears in `/api/agents/sessions?environment=commis`.

### Phase 2: Deprecate Standard Mode (3)
- [x] Make workspace mode the default (and only) execution mode for new commis
- [x] Gate standard mode behind `LEGACY_STANDARD_MODE=1` env var (escape hatch)
- [x] Update Oikos `spawn_commis` tool to always use workspace mode (deprecated, warns)
- [x] Update tests that exercise standard mode
- [x] Remove `commis_runner.py` (in-process runner) — ~1K LOC + 5 test files (~1.7K LOC) deleted
- [x] Remove 6 skipped tests in mixed files that referenced CommisRunner (test_durable_runs, test_oikos_fiche, test_supervisor_e2e, test_supervisor_tools_integration)

### Phase 3: Slim Oikos (5)

**Architecture:** Single toolbox, many agents. All ~60 tools stay as a library. Each agent (Oikos, commis, future) is configured with a subset. The loop and tool infrastructure are what get replaced, not the tools themselves.

**3a: Simplify the loop (refactor, not rewrite)**
- [x] Simplify `oikos_react_engine.py` (~1.5K → 842 LOC): consolidated quadruplicate LLM call, extracted `_call_spawn_tool`/`_extract_text_content`/`_maybe_truncate_result` helpers; all 10 critical patterns preserved
- [x] Merge `message_array_builder.py` + `prompt_context.py` into one module (`message_builder.py`, ~540 LOC): cache-optimized layout preserved, phase ceremony removed
- [x] Simplify `fiche_runner.py` (~974 → ~600 LOC): kept run lifecycle, interrupt-resume, credential injection; stripped boilerplate
- [ ] Implement Oikos dispatch contract from spec: direct vs quick-tool vs CLI delegation, with explicit backend intent routing (Claude/Codex/Gemini) and repo-vs-scratch delegation modes
- [ ] Use Claude Compaction API (server-side) or custom summarizer for "infinite thread" context management

**3b: Flatten tool infrastructure**
- [x] Remove `catalog.py`, `unified_access.py`; `tool_search.py` absorbed catalog role and retained for embedding-based discovery (~600 LOC net removed)
- [x] Simplify `lazy_binder.py` (~221 → ~194 LOC): kept allowlist filtering with wildcard support; CORE_TOOLS now derives from OIKOS_TOOL_NAMES
- [ ] Tool subsets configured per agent type (Oikos gets ~20-30 tools, commis gets different set, user-configurable)
- [x] Kill dead-weight utility tools: math_tools, uuid_tools, tool_discovery, container_tools (~530 LOC removed)

**3c: Decouple standard-mode services from FicheRunner (refactor, not delete)**

These 9 services are actively used — decouple from FicheRunner so they work with the simplified loop. Remove only deprecated code paths.
- [x] `commis_resume.py` — decoupled from FicheRunner, works as generic continuation service
- [x] `roundabout_monitor.py` — deprecated heuristic path removed (wave 4); monitoring loop + LLM decision preserved
- [x] `commis_artifact_store.py` — already modular, no changes needed
- [x] `fiche_state_recovery.py` — already modular, no changes needed
- [x] `fiche_locks.py` — already modular, no changes needed
- [x] `commis_output_buffer.py` — already modular, no changes needed
- [x] `evidence_compiler.py` — decoupled from CommisArtifactStore, Mount phase preserved
- [x] `llm_decider.py` — already well-decoupled, no changes needed
- [x] `trace_debugger.py` — already modular, no changes needed

**3d: Memory consolidation**
- [x] Consolidate 3 memory systems: kept Oikos Memory (4 tools) + Memory Files (embeddings); Fiche Memory KV evaluated and retained as lightweight config store
- [ ] Move David-specific tools (personal_tools: Traccar/WHOOP/Obsidian) to external plugin, not OSS core

**3e: Skills progressive disclosure + unified inheritance**

Skills are a platform feature shared by Oikos and commis. Match industry pattern (Claude Code, Cursor) for progressive loading.
- [x] Change `SkillIntegration` to inject **index only** (name + description, one line each) into system prompt by default — not full SKILL.md content
- [x] Load full skill content into conversation only when: user invokes `/skill-name`, OR Oikos auto-selects based on description matching the request
- [x] Add `skills` parameter to `spawn_workspace_commis` — pass selected skill content to commis prompt so CLI agents inherit user skills
- [x] Respect character budget for skill index (cap total index tokens, drop lowest-priority skills if over budget)
- [x] Supporting files in skill directories loaded only when skill is active and references them
- [ ] Document skill format compatibility: users can adapt Claude Code `.claude/skills/` and Cursor `.cursor/rules/` into `~/.longhouse/skills/`
- [ ] Support Codex-style AGENTS.md instruction chain in commis workspaces (global → repo → subdir, with override files) for cross-agent compatibility

**3f: Longhouse MCP Server — expose toolbox to CLI agents (3)**

Industry standard pattern (2025-2026): teams expose internal tooling as MCP servers so CLI agents access shared context mid-task. See VISION.md § "Longhouse MCP Server" for architecture.

- [ ] Implement MCP server exposing: `search_sessions`, `get_session_detail`, `memory_read`/`memory_write`, `notify_oikos`
- [ ] Support stdio transport (for local hatch subprocesses) and streamable HTTP (for remote/runner agents)
- [ ] Auto-register MCP server in Claude Code settings during `longhouse connect --install`
- [ ] Auto-configure MCP server for commis spawned via `hatch` (inject into workspace `.claude/settings.json`)
- [ ] Add Codex `config.toml` MCP registration path for Codex-backend commis

**3g: Commis quality gates via hooks (2)**

Verification loops (tests/browser checks before commit) boost agent reliability 2-3x (industry consensus 2025-2026). Inject quality gates into commis workspaces.

- [ ] Define default commis hook set: `Stop` hook runs `make test` (or configured verify command) before allowing completion
- [ ] Inject hooks into commis workspace `.claude/settings.json` at spawn time
- [ ] Make verify command configurable per-project (default: `make test` if Makefile exists, else skip)
- [ ] Report hook failures back to Oikos via `notify_oikos` MCP tool (when 3f lands)

**3h: Research — Codex App Server protocol + Claude Agent SDK (1)**

Evaluate newer integration paths for tighter commis control vs. current hatch subprocess approach.

- [ ] Evaluate Codex App Server (JSON-RPC over stdio) for structured event streaming from Codex-backend commis — Thread/Turn/Item primitives + approval routing
- [ ] Evaluate Claude Agent SDK (TypeScript) as alternative to `hatch` subprocess for Claude-backend commis — real-time streaming, programmatic tool injection, better lifecycle control
- [ ] Document trade-offs and recommend path forward (subprocess vs SDK vs protocol)

### Phase 4: Semantic Search (4)
- [ ] Choose embedding approach: sqlite-vec vs API-call-on-ingest
- [ ] Compute embeddings on session event ingest (background or sync)
- [ ] Add `semantic_search_sessions` Oikos tool
- [ ] Make semantic search optional: `pip install longhouse[semantic]`

### Phase 5: Historical Backfill (David-specific) (2)
- [ ] Write one-time script: pull sessions from Life Hub API → Longhouse `/api/agents/ingest`
- [ ] Verify session counts match between Life Hub and Longhouse
- [ ] Stop using Life Hub MCP for agent memory

---

## [Launch] 🎯 HN Launch Priority (This Week)

**Decision:** OSS GA + Hosted Beta in parallel (50/50 positioning). No "OSS-first" bias in copy/CTAs.

### P0 — Launch Core (Required)
> ✅ **Archived** — All 6 P0 items complete (auth, demo, CTAs, README, FTS5, QA). See git history.

### P1 — Hosted Beta (Stretch)
| Priority | Task | Est |
|----------|------|-----|
| 1 | Control Plane Scaffold | 1 day |
| 2 | Stripe Integration | 1 day |
| 3 | Docker Provisioning | 1 day |
| 4 | Cross-subdomain Auth | 0.5 day |

**Minimum for launch:** P0 only (self-hosted works end-to-end).

---

## [Launch] Post-GA Follow-ups (From 2026-02-03 Swarm)

> ✅ **Archived** — All 5 items complete (rate limiting, hash support, UI fallback, demo-fresh, workflow removal). See git history.

---

## [Infra] ⚠️ Architecture Reality Check (Read First)

**VISION.md describes per-user isolated instances. That doesn't exist YET.**

Current reality (as of 2026-02-05):
- **ONE backend container** (API served via same-host `/api` proxy; `api-*` subdomains are legacy/optional)
- **ONE frontend container** serves both `longhouse.ai` and `david.longhouse.ai`
- **ONE SQLite database** at `/data/longhouse.db` (reset 2026-02-05; no users yet)
- **No production control plane** — repo scaffold exists but not wired to signup/billing
- **"david.longhouse.ai" is cosmetic** — just DNS routing to shared infra

**Target state:** Control plane provisions isolated containers per user (Docker API + Caddy labels; Traefik optional). See VISION.md for architecture.

See this file for the current launch analysis.

---

## [Launch] 🚨 OSS Auth — Password Login for Self-Hosters (3)

> ✅ **Archived** — Password auth fully implemented. See git history.

---

## [Infra] Domain Split — Marketing vs Personal Instance (4)

**Goal:** longhouse.ai runs in demo mode; david.longhouse.ai is the app (single-tenant).

**Status:** ~~DONE~~ — `marketingOnly` concept removed. Existing `DEMO_MODE=1` env var now resolves to `AppMode.DEMO` via centralized `resolve_app_mode()`. No new env vars needed. Domain split is cosmetic routing to one deployment.

- [x] Add marketing-only frontend flag → **replaced by centralized `AppMode` enum (demo mode via existing `DEMO_MODE=1`)**
- [x] Update Coolify domains: zerg-web -> david.longhouse.ai, zerg-api -> api-david.longhouse.ai
- [x] Update zerg-api env: APP_PUBLIC_URL/PUBLIC_SITE_URL to david, CORS to include longhouse.ai + david
- [x] Add Cloudflare DNS for david.longhouse.ai + api-david.longhouse.ai (and optional wildcard)

**Remaining issues:**
- [ ] Cross-subdomain OAuth code exists (`/auth/accept-token`) but targets non-existent per-user architecture — needs control plane to work as designed
- [ ] For now, use password auth on subdomains; Google OAuth only makes sense at control plane (longhouse.ai)

---

## [Infra] Instance Health Route Returns HTML (1)

> ✅ **Archived** — /api/health returns JSON, route-order fix deployed. See git history.

---

## [Infra] Standardize Health Endpoints (2)

> ✅ **Archived** — Health routes at /api/health + /api/livez, all callers updated. See git history.

---

## [QA/Test] CI Stability — E2E + Smoke (3)

**Goal:** Stop CI spam and make signal trustworthy (E2E isolation + prod smoke correctness).

- [x] E2E on cube: remove fixed ports, use per-run DB dir, and upload artifacts on failure.
- [x] Smoke-after-deploy: target canonical `/api/health` and correct app domain(s).
- [x] Add schedule gate for smoke to prevent spam during known outages.
- [x] Replace `realtime_websocket_monitoring.spec.ts` timeouts/log-only flow with deterministic assertions (or drop the test).
- [x] Add core E2E guardrail script/check: fail CI on `waitForTimeout` or `networkidle` in `apps/zerg/e2e/tests/core/**` (allow scripts/visual/perf helpers).

**Notes:**
- Current prod endpoints returning HTTP 525 (Cloudflare origin handshake); fix infra routing or adjust smoke targets.

---

## [Product] Landing Page Redesign — Full (6)

**Goal:** Clear user paths, visible CTAs, better contrast. Visitor instantly understands: what it is, who it's for, how to get started.

**⚠️ DEPENDS ON LAUNCH DECISION:**
- **Dual-path (current):** Hosted beta + self-hosted parity in copy and CTAs

Current copy is a mix of both stories. Align to dual-path parity.

**Problems identified (2026-02-05):**
1. ✅ FIXED: Hero CTAs were ghost + not dual-path (both self-host + hosted now visible)
2. Colors too dark — low contrast text, cards blend into background
3. ✅ FIXED: Explicit self-host vs hosted paths (hosted waitlist + self-host install in hero/CTA)
4. ✅ FIXED: Story copy overpromises cross-provider + FTS5 + resume-anywhere
5. ✅ FIXED: CTAs now route to pricing/install (sign-in only when explicitly chosen)

### Phase 1: Header + Navigation (done)

Add a persistent sticky header following dev-tool best practices (Vercel, Supabase, Railway).

- [x] Create `LandingHeader.tsx` component with sticky positioning
- [x] Left: Logo + "Longhouse" wordmark
- [x] Center: Product | Docs | Pricing | Enterprise (or Self-host)
- [x] Right: "Sign In" (secondary) + "Get Started" (primary CTA)
- [x] Mobile: hamburger menu
- [x] Add to `LandingPage.tsx` above hero

**Design notes:**
- Header bg: slightly lighter than page bg (elevation via lightening, not shadows)
- Use brand accent color for primary CTA (stands out on dark)
- "Sign In" visible but secondary (ghost or outline variant)

**Files:** `components/landing/LandingHeader.tsx`, `LandingPage.tsx`, `landing.css`

### Phase 2: User Path Differentiation (2 hours)

Make Self-host / Hosted Beta / Enterprise paths explicit with distinct CTAs.

**Option A: Hero with dual path**
- [x] Primary CTA: "Hosted Beta" → waitlist modal
- [x] Secondary CTA: "Self-host Now" → install section
- [ ] Tertiary link: "Enterprise →" below

**Option B: Three-card section below hero**
- [x] Add `DeploymentOptions.tsx` with 3 cards: Self-hosted | Hosted Beta | Enterprise
- [x] Each card: 1-line promise, 3 features, dedicated CTA
- [x] Self-host: "Install CLI" → install section
- [x] Hosted: "Join Waitlist" → waitlist modal
- [x] Enterprise: "Contact Us" → mailto or form

**Recommended approach:** Option A for hero simplicity + Option B as separate section

- [x] Update `HeroSection.tsx` CTAs to show dual-path parity (hosted + self-host)
- [x] Create `DeploymentOptions.tsx` section
- [ ] Add comparison table: who runs it, data residency, support, upgrade path

**Files:** `HeroSection.tsx`, `components/landing/DeploymentOptions.tsx`, `PricingSection.tsx`

### Phase 3: Color/Contrast Improvements (2 hours) ✅

Fix dark theme accessibility issues. Target WCAG 4.5:1 for text, 3:1 for UI.

**CSS Variable Updates:**
- [x] Audit `--color-text-secondary` and `--color-text-muted` contrast ratios
- [x] Increase body text contrast (current ~4.0:1, need 4.5:1+)
- [x] Add card elevation: cards should be visibly lighter than page bg
- [x] Improve CTA button contrast: primary should pop (saturated accent on dark)
- [x] Badge contrast: "Free during beta" badge needs better visibility

**Specific fixes:**
- [x] `.landing-hero-subhead` — bump from `--color-text-secondary` to higher contrast (#d0d0d6, ~11:1)
- [x] `.landing-hero-note` — bump from `--color-text-muted` to `--color-text-secondary` (7.9:1)
- [x] `.landing-step` cards — bumped bg to 0.06, border to rgba(255,255,255,0.10), inset glow
- [x] `.landing-cta-main` — increased glow radius/opacity + brighter border (#818cf8/70%)
- [x] `.landing-pricing-card` — bumped bg to 0.06, added box-shadow + inset highlight

**Also fixed:** All small-text muted usages (badges, footer nav headings, provider descriptions, install section, pricing period, dividers, etc.) bumped from muted (#9898a3, 5.3:1) to secondary (#b4b4bc, 7.9:1). Card elevation improved across provider cards, integration items, and trust badges. Remaining muted usages are UI components (close buttons, toggle icons), placeholders, or decorative elements — all meeting 3:1 for non-text.

**Files:** `landing.css` (component-level overrides, no token changes needed)

### Phase 4: Hero CTA Restructure (1 hour)

Move Sign In to header, restructure hero CTAs for clarity.

**Current (bad):**
```
[Install section with curl command]
[See How It Works ↓] [Sign In]  ← ghost buttons, same weight
```

**Target:**
```
[Install (Self-host)] [Hosted waitlist →]  ← clear primary + secondary
[Works offline • <2min setup • Your data stays local]
```

- [x] Remove "Sign In" from hero (it's now in header) — login modal also removed
- [x] Primary CTA: "Self-host Now" (scrolls to install section)
- [x] Secondary CTA: "Hosted Beta →" (scrolls to pricing/waitlist)
- [x] Keep install command section but position as "Self-host" path
- [x] Add friction reducers: "Works offline", "<2min setup", "Your data stays local"

**Files:** `HeroSection.tsx`, `InstallSection.tsx`

### Phase 5: Story Alignment (2 hours)

Update copy to match VISION.md value prop: Timeline + Search + Resume.

**Hero copy:**
- [ ] Headline: "Never lose an AI coding conversation" (or similar)
- [ ] Subhead: "Claude Code sessions in one searchable timeline. Other providers coming soon."
- [ ] Note: "Local-first. Self-host anytime. Hosted beta waitlist."

**How It Works:**
- [ ] Step 1: "Install" → Claude Code sync today (others coming)
- [ ] Step 2: "Search" → Keyword search now (FTS5-powered)
- [ ] Step 3: "Resume" → Forum resume is Claude-only; Timeline resume planned

**Cut/minimize:**
- [ ] IntegrationsSection (wrong story — we're not about connecting apps)
- [ ] SkillsSection (power user feature, not hero)
- [ ] Move Oikos chat to "Features" section, not hero

**Files:** `HeroSection.tsx`, `HowItWorksSection.tsx`, `IntegrationsSection.tsx`, `SkillsSection.tsx`

### Phase 6: Visual Assets (1 hour)

Update screenshots to show Timeline, not old dashboard.

- [ ] Capture Timeline page with demo sessions populated
- [ ] Capture search results
- [ ] Capture session detail view with events
- [ ] Update `dashboard-preview.png` → `timeline-preview.png`
- [ ] Add provider logos inline (Claude, Codex, Cursor, Gemini)

**Files:** `public/images/landing/`, `HeroSection.tsx`

### Checklist (dev-tool landing page best practices 2025-26)

- [ ] Above fold: Self-host primary, hosted beta secondary
- [ ] Header: Docs + Pricing reachable in 1 click
- [ ] CTAs: hero + header + mid-page + footer; labels match next step
- [x] Dark theme: text ≥ 4.5:1, UI components ≥ 3:1 (Phase 3 done; focus indicators still TODO)
- [ ] Sticky header doesn't obscure focus / anchors
- [ ] Self-host responsibilities spelled out

---

## [Launch] HN Launch Readiness — Remaining (4)

**Goal:** HN reader can install, see value immediately, understand what problem this solves, and start using it.

**Launch Path Decision:** OSS GA + Hosted Beta (optional).

### 🚨 Critical Blockers (Fix First)

- [x] **OSS Auth** — Password login for self-hosters (see dedicated section above)
- [x] **Password-only config bug** — `_validate_required()` now skips Google OAuth validation when password auth is configured
  - File: `apps/zerg/backend/zerg/config/__init__.py:512`
  - Fixed: Skip Google OAuth validation if `LONGHOUSE_PASSWORD` or `LONGHOUSE_PASSWORD_HASH` is set
- [ ] **Landing page CTAs** — Copy/flow not dual-path; some CTAs route to sign-in modal instead of install/waitlist

### High Priority

- [x] **Demo mode flag** — `longhouse serve --demo` and `--demo-fresh` implemented
- [x] Installer enforces Python 3.12+ (align with `pyproject.toml`)

### Medium Priority

- [ ] **Comparison table** (30 min)
  - Show how Longhouse compares to:
    - grep through JSONL files (old way)
    - Claude Code built-in history (limited)
    - Not tracking at all (disaster)
  - Table showing: searchable, cross-tool, persistent, visual timeline

- [ ] **Social proof** (if available)
  - Add testimonial or "Built by X" to README
  - Show usage stats if you have any early users
  - Link to personal Twitter/GitHub for credibility

- [ ] **Video walkthrough** (optional, 2 hours)
  - 60-90 second Loom showing install → timeline → search
  - Add to README + landing page

---

## [Infra] Control Plane — Hosted Beta (8)

**What it enables:** Users sign up at longhouse.ai → get their own instance (alice.longhouse.ai)

**Architecture:** Tiny FastAPI app that handles signup/billing/provisioning. Uses Docker API directly (not Coolify).

**Scope:** Only if P0 OSS GA is complete.

**Decisions / Notes (2026-02-04):**
- Control plane + user instances will live on **zerg** (single host for now).
- Do **not** use Coolify for dynamic provisioning; control plane talks to Docker directly.
- Proxy uses existing Coolify Caddy (caddy-docker-proxy) with caddy labels.
- Wildcard DNS `*.longhouse.ai` ✅ configured (2026-02-04), proxied through Cloudflare.
- Runtime image: `docker/runtime.dockerfile` bundles frontend + backend in single container.

### Phase 0: Routing + DNS Reality Check ⚠️ PARTIAL

- [x] Wildcard DNS `*.longhouse.ai` resolves via Cloudflare (verified 2026-02-05)
- [ ] Routing layer: Caddy (existing coolify-proxy) with caddy-docker-proxy labels (needs live verify)
- [ ] Manual provision smoke test: test2/test3 instances provisioned + routed (needs rerun)
- [ ] Add control-plane → instance auth bridge endpoint (login-token exists but payload/secret mismatch with instance)

### Phase 1: Scaffold + Auth ⚠️ PARTIAL

- [x] Create `apps/control-plane/` directory structure (FastAPI app, models, routers, services)
- [x] Add provisioner service (Docker API client with Caddy labels)
- [x] Add Instance model with subdomain, container_name, state
- [x] Admin API + minimal HTML UI for manual provisioning
- [ ] Add Google OAuth (control plane only, not per-instance)
- [x] Add User model with Stripe fields (fields only; no Stripe logic yet)

### Phase 2: Stripe Integration (3)

- [ ] Add `POST /checkout` → create Stripe checkout session
- [ ] Add `POST /webhooks/stripe` → handle payment events
- [ ] On `invoice.paid` → trigger provisioning
- [ ] On `customer.subscription.deleted` → trigger deprovisioning
- [ ] Add billing portal link (`POST /billing/portal`)

### Phase 3: Docker Provisioning ✅ MOSTLY DONE

- [x] Implement Docker API client via local socket
- [x] Provision container with Caddy labels for subdomain routing
- [x] Create SQLite volume per user at `/var/lib/docker/data/longhouse/{subdomain}`
- [x] Implement deprovision (stop + remove container)
- [ ] Add health check polling after provision (method exists, not wired in routes/UI)
- [ ] Build and push runtime image (`docker/runtime.dockerfile`) to ghcr.io
- [ ] Update CONTROL_PLANE_IMAGE to use runtime image (currently uses backend-only)

### Phase 3.5: Provisioning E2E Gate ✅

- [x] Add CI provisioning script (`scripts/ci/provision-e2e.sh`) with real control-plane + instance smoke checks
- [x] Add GitHub workflow on cube ARC runners (`.github/workflows/provision-e2e.yml`)
- [x] Add CI-only port publishing + writable instance data root for provisioning tests

### Phase 4: Cross-Subdomain Auth (2)

- Note: current control-plane `/api/instances/{id}/login-token` uses `sub=email` + control-plane JWT secret; instance `/api/auth/accept-token` expects numeric user_id + instance secret → will fail until aligned.
- [ ] Control plane issues JWT on successful OAuth
- [ ] Redirect to `{user}.longhouse.ai?auth_token=xxx`
- [ ] Instance validates token at `/api/auth/accept-token` (code exists)
- [ ] Instance sets session cookie, user is logged in

### Phase 5: Landing Page Integration (1)

- [ ] Update landing page CTAs to call control plane endpoints
- [ ] "Get Started" → `/signup` (OAuth) → `/checkout` (Stripe) → provision → redirect
- [ ] "Sign In" → `/login` (OAuth) → redirect to existing instance
- [ ] Show instance status on landing page if logged in

**Files:** `apps/control-plane/`, `docker/runtime.dockerfile`

**Infra status (needs live verify unless noted):**
- ⚠️ Caddy (coolify-proxy) on zerg handles subdomain routing via caddy-docker-proxy labels
- ✅ Wildcard DNS `*.longhouse.ai` resolves (verified 2026-02-05)
- ⚠️ Docker socket access from control plane container
- ⚠️ Postgres for control plane DB (separate container via docker-compose)
- ⏳ Runtime image needs build + push to ghcr.io

---

## [Launch] Public Launch Checklist (6)

Ensure launch readiness without relying on scattered docs.

- [x] Rewrite README to center Timeline value and 3 install paths (FTS5 + resume/provider copy aligned).
- [ ] Add CTA from Chat to "View session trace" after a run.
- [ ] Improve Timeline detail header (goal, repo/project, duration, status).
- [ ] Add basic metrics (tool count, duration, latency if available).
- [ ] Add filters within detail view (user/assistant/tool) + search.
- [ ] Core UI smoke snapshots pass (`make qa-ui-smoke`).
- [ ] Shipper smoke test passes (if shipper path is enabled).
- [ ] Add packaging smoke test for future install.sh/brew path (if shipped).

---

## [Launch] HN Post Notes (Condensed)

Keep the HN post short and problem-first. Use install.sh as the canonical path.

- **Title options:** "Show HN: Longhouse – Search your Claude Code sessions" · "Show HN: Never lose a Claude Code conversation again" · "Show HN: Longhouse – A local timeline for AI coding sessions"
- **Angle to emphasize (from industry research):** Context durability is the unsolved problem — benchmarks ignore post-50th-tool-call drift. Longhouse is the session archive that makes agent work durable and searchable. Lean into "your agents do great work, then it vanishes into JSONL" pain point.
- **Comment skeleton:** Problem (JSONL sprawl + context loss) → Solution (timeline + search + resume) → Current state (Claude only, others planned, local-first) → Try it (`curl -fsSL https://get.longhouse.ai/install.sh | bash`, `longhouse serve`)
- **Anticipated Qs:** Why not Claude history? · Cursor/Codex/Gemini when? · Privacy? · Performance at scale? · How does this compare to just grepping JSONL?
- **Timing:** Tue–Thu mornings PT

---

## [QA/Test] README Test CI (Readme-Contract) (5)

Automate README command verification with explicit, opt-in contracts. Use cube ARC runners where possible.

- [ ] Define `readme-test` JSON block spec (steps, workdir, env, mode, timeout, cleanup).
- [ ] Implement `scripts/run-readme-tests.sh` (extract + run in temp clone, fail fast, save logs).
- [ ] Add `make test-readmes` target (smoke vs full mode flags).
- [ ] Add GitHub Actions workflow using `runs-on: cube` for PR smoke and nightly full.
- [ ] Add `readme-test` blocks to root README + runner/sauron/hatch-agent READMEs.
- [ ] Optional: failure triage via `hatch` agent (summarize logs, suggest fix).

---

## [Product] Forum Discovery UX + Explicit Presence Signals (7)

Make the Forum the canonical discovery UI for sessions, with **explicit** state signals (no heuristics).

**Deliverables:** "Active/Needs You/Parked/Completed/Unknown" are driven by emitted events, not inference.

- [ ] Define a session presence/state event model (`session_started`, `heartbeat`, `session_ended`, `needs_user`, `blocked`, `completed`, `parked`, `resumed`) and document it.
- [ ] Add ingestion + storage for presence events in the agents schema (SQLite-safe).
- [ ] Update the Forum UI to group by explicit buckets and remove heuristic "idle/working" logic.
- [ ] Add user actions in Forum: Park, Snooze, Resume, Archive (emit explicit events).
- [ ] Wire wrappers to emit `session_started`/`heartbeat`/`session_ended` (Claude/Codex first).
- [ ] Add a single "Unknown" state in UI for sessions without signals (no pretending).

---

## [Product] OSS First-Run UX Polish (5)

Eliminate the "empty timeline" anticlimactic moment and improve discovery for users without Claude Code.

- [ ] Seed demo session data on first `longhouse onboard` run (shows what the timeline looks like)
- [ ] Add guided empty state with "Load demo" CTA + connect shipper steps (VISION)
- [ ] Improve "No Claude Code" guidance in onboard wizard (link to alternatives, explain what to do next)
- [x] `longhouse serve --demo` / `--demo-fresh` supported (demo DB)

---

## [Launch] Install + Onboarding Alignment (4)

Close the gap between VISION, README, docs, and the live installer.

- [ ] **Canonical install path**: `install.sh` is primary (README aligned; landing still needs dual-path CTA)
- [ ] **Document onboarding wizard** (steps, troubleshooting, service install) and link it from README + landing page
- [x] **Add `longhouse doctor`** (self-diagnosis for server health, shipper status, config validity); run after install/upgrade and recommend in docs
- [x] **Fix `longhouse connect` default URL** — `connect` + `ship` fallback changed from 47300 to 8080 (commit `426f8c9b`)
- [ ] **Installer polish:** verify Claude shim + PATH in a *fresh* shell and print an exact fix line when it fails (VISION requirement)
- [ ] **Hook-based shipping:** `longhouse connect --install` should inject a Claude Code `Stop` hook (`longhouse ship --session $SESSION_ID`) into `.claude/settings.json` — eliminates need for watcher daemon for Claude Code users. See VISION.md § "Shipper" for architecture. Verify hook env vars expose session ID.
- [ ] **AGENTS.md chain support:** Support Codex-style AGENTS.md chain (global → repo → subdir) in commis workspaces. Auto-inject Longhouse context (MCP server config, memory pointers) into workspace AGENTS.md when spawning commis.

---

## [Infra] OSS Packaging Decisions (3)

Close the remaining open questions from VISION.md.

- [ ] Decide whether the shipper is bundled with the CLI or shipped as a separate package.
- [ ] Decide shipper auth UX for `longhouse connect` (device token flow).
  - Current: manual token creation in UI + paste into CLI
  - VISION target: `longhouse connect` issues token automatically
  - Note: This is separate from web UI auth (password/OAuth) — shipper needs device tokens
- [ ] Decide HTTPS story for local OSS (`longhouse serve`) — built-in vs reverse proxy guidance.
- [ ] Capture current frontend bundle size and set a target budget.

---

## [Brand] Longhouse Rebrand — Product/Meta Strings (6)

User-facing strings, metadata, and package descriptions must stop mentioning Swarmlet/Zerg as a brand.

**Scope (2026-02-05):** ~13 Swarmlet matches in core runtime code, ~50 total incl tests/docs/experiments.

- [x] Replace "Swarmlet" with "Longhouse" in frontend HTML metadata + webmanifest
- [x] Update `package.json` description to Longhouse naming
- [x] Update runner README/package metadata to Longhouse (e.g., "Longhouse Runner")
- [x] Update landing FAQ + marketing copy that still says "PostgreSQL" or "Swarmlet" (`TrustSection.tsx`)
- [x] Update OpenAPI schema metadata (title/description/servers) to Longhouse
- [x] Remove deprecated `swarmlet_url` / Swarmlet defaults (RunnerSetupCard + runners API + openapi types)
- [x] Update shipper defaults/docs still pointing at `api.swarmlet.com`
- [x] Rename `/tmp/swarmlet/commis` artifact path → `/tmp/longhouse/commis`
- [x] Rename `SWARMLET_DATA_PATH` env var → `LONGHOUSE_DATA_PATH` (with backwards compat)
- [x] Remove `swarmlet.com`/`swarmlet.ai` from CORS fallback
- [x] Clean up tests referencing old Swarmlet names (env vars, URLs, launchd labels)
- [ ] Clean up `experiments/shipper-manual-validation.md` (historical, low priority)
- [x] Regenerate OpenAPI types (`src/generated/openapi-types.ts` still has `swarmlet_url`)

---

## [Brand] Longhouse Rebrand — CLI / Packages / Images (7)

Package and binary naming so OSS users see Longhouse everywhere.

- [x] Decide npm scope/name for runner: `@longhouse/runner` (package.json already uses this)
- [x] Update docker image name in README/examples (ghcr.io/.../longhouse)
- [x] Update installer scripts to new names (install-runner still points at `daverosedavis/zerg`)
- [x] Update default runner image name (`RUNNER_DOCKER_IMAGE` defaults to `ghcr.io/cipher982/zerg-runner:latest`)

---

## [Tech Debt] Prompt Cache Optimization (5)

Message layout is already system → conversation → dynamic. Remaining work is cache-busting fixes.

**Verified layout:**
```
[system] → [conversation] → [dynamic]
 cached      cached         per-turn only
```

**Remaining cache-busters (from VISION):**
- ~~Timestamps are too granular (changes every request)~~ → fixed: minute-level
- Connector status JSON ordering is non-deterministic
- Memory context varies per query (should be separated or cached)

**Files:** `managers/fiche_runner.py`, `managers/prompt_context.py`

- [x] MessageArrayBuilder layout is system → conversation → dynamic
- [x] Reduce timestamp granularity in dynamic context (minute-level)
- [x] Sort connector status keys for deterministic JSON — already done in `status_builder.py:438`
- [x] Split dynamic context into separate SystemMessages (time / connector / memory)
- [ ] Add cache hit logging/metrics

---

## [Product] Session Discovery — FTS5 Search + Oikos Tools (6)

Make session discovery actually useful. Two tiers: fast search bar for keywords, Oikos for complex discovery.

**Problem:** Timeline cards are just a prettier version of scrolling snippets. Real value is finding "where did I solve X?"

**Architecture:**
- **Search bar**: SQLite FTS5 over session events. Instant (<10ms), keyword-based.
- **Oikos**: Agentic multi-tool discovery. Semantic search, grep, filters, cross-referencing.

### Phase 1: FTS5 Search Bar (Timeline)
> ✅ **Archived** — FTS5 virtual table, search bar UI, snippets, highlights all done. See git history.

### Phase 2: Oikos Session Discovery Tools
> ✅ **Archived** — 4 session tools (search, grep, filter, get_detail) implemented and registered. See git history.

### Phase 3: Embeddings for Oikos (Optional)

- [ ] Embed session events on ingest (background job or sync)
- [ ] Add `semantic_search_sessions` tool for Oikos
- [ ] Vector search via sqlite-vec or pgvector

**Test:** "Find where I implemented retry logic" returns relevant sessions in <100ms (search bar) or with reasoning (Oikos).

---

## [QA/Test] UI QA Screenshot Capture System (4)

**Goal:** Flexible, low-friction screenshot capture for agents + humans; clear instructions; minimal token cost.

- [ ] Inventory current screenshot/Playwright flows and pain points
- [ ] Prototype a simple capture CLI/API (local dev + headless) and document usage
- [ ] Add agent-friendly capture path (MCP/tool or skill) with stable output paths
- [ ] Add docs + examples; ensure instructions are short and reproducible
- [x] Fix ui-capture a11y snapshot: Playwright 1.57 has no `page.accessibility`; use `locator.ariaSnapshot()` or guard missing API and still write trace/manifest on partial failure
- [ ] Add SCENE=empty reset endpoint (or CLI) to clear sessions; update docs to note current no-op until available

---

## [Docs/Drift] Findings / Drift Audit (2026-02-05)

(Former FOUND.md. Keep this list updated here only. Fixed items stripped — see git history.)

- [x] [Docs] Clarify `VISION.md` semantics: explicitly mark target architecture vs current implementation snapshots to reduce ambiguity. (2026-02-10)
- [x] [Docs] Oikos first-principles alignment: codify dispatch contract (direct/quick/delegate), backend keyword routing (Claude/Codex/Gemini), and reconcile `spawn_commis` semantics across VISION/spec/tools docs. (2026-02-10)
- [x] [Docs] Prune `AGENTS.md` learnings to durable invariants only; convert code-fixable confusion into tracked TODO engineering tasks. (2026-02-10)
- [x] [Docs] Slim `AGENTS.md` by removing duplicated feature catalog and pointing to canonical `VISION.md` Product Surface + deep-dive docs. (2026-02-10)
- [Infra/docs] Wildcard DNS is now configured (dig `test-longhouse-audit.longhouse.ai` resolves); VISION still says "needs setup" in Control Plane section.
- [Infra/docs] DB size claim stale; prod DB reset 2026-02-05 (no users). Update docs/launch notes once data exists.
- [Docs vs code] `longhouse connect` fallback still uses `http://localhost:47300` while `longhouse serve` + README use 8080.
- [Docs vs code] VISION says job claiming is dialect-aware (Postgres `FOR UPDATE SKIP LOCKED`). `commis_job_queue.py` is SQLite-specific (`datetime('now')`, `UPDATE ... RETURNING`) and is imported unconditionally in `commis_job_processor.py`.
- [Docs vs code] Workspace paths in VISION are `~/.longhouse/workspaces/...` and artifacts in `~/.longhouse/artifacts`, but current defaults are `/var/oikos/workspaces` and `settings.data_dir` (`/data` in Docker or repo `data/`). Session resume temp workspaces default to `/tmp/zerg-session-workspaces`.
- ~~[Docs vs infra] VISION control-plane routing assumes Traefik labels; current infra uses Caddy (coolify-proxy with Caddy labels). If Traefik is intended, docs should say so and note migration.~~ ✅ Fixed — VISION already uses Caddy labels (2026-02-10)
- [Docs vs release] PyPI version likely lags repo; verify `longhouse` version on PyPI before making release claims.
- [Docs] Launch notes checklist says "README has screenshot (done!)" but README has no image.
- [Docs] Launch notes say demo data seeds on first run; current behavior requires `--demo/--demo-fresh` or calling the demo seed endpoint.
- [Docs conflict] Launch plan notes suggest provisioning via Coolify API; VISION explicitly says not to use Coolify for dynamic provisioning.
- ~~[Docs vs code] VISION onboarding-contract example is Docker-centric (`cp .env.example`, `docker compose up`), but README's contract runs bun+uv + `longhouse serve`; VISION's example is stale.~~ ✅ Fixed — VISION onboarding now references `pip install longhouse`/`longhouse serve` (2026-02-10)
- ~~[Docs vs code] VISION says `longhouse connect <url>` installs and starts the shipper; actual CLI only installs when `--install` is passed (default runs foreground watch/poll).~~ ✅ Fixed — VISION current-state and commands section document `--install` correctly (2026-02-10)
- ~~[Docs vs code] VISION says device token is issued during `longhouse connect`; actual flow requires manual token creation in UI (`/dashboard/settings/devices`) and paste into CLI.~~ ✅ Fixed — VISION current-state says `longhouse auth` handles device-token setup (2026-02-10)
- [Docs vs code] VISION specifies shipper batching "1 second or 100 events"; implementation ships per file with no time-window batching (only `batch_size` for spool replay).
- [Docs vs code] VISION says shipper replay uses idempotency keys; shipper does not send idempotency keys/headers (dedupe relies on DB unique index).
- [Docs vs UI] "Resume from anywhere / Timeline resume" is not in Timeline UI; resume is only implemented in Forum Drop-In (Claude-only) and not exposed on `/timeline`.
- [Docs vs code] VISION says cross-subdomain auth tokens are one-time with nonce stored server-side and validated via control plane/JWKS; current `POST /api/auth/accept-token` just validates JWT and sets cookie (no nonce/one-time guard).
- [Docs vs code] VISION requires a PATH-based Claude shim + verification in a fresh shell; current installer only adds a hook unless `~/.longhouse/install-claude-shim.sh` already exists and does not verify in a new shell.
- [Docs] Launch notes claim session files in `~/.codex/sessions/*` etc; current shipper/parser only reads Claude Code (`~/.claude/projects/...`).
- [Docs vs UI] `longhouse auth` instructs users to open `/dashboard/settings/devices`, but there is no device-token UI or route; frontend only has `/settings` and no device token page.
- ~~[Code inconsistency] `WorkspaceManager` defaults to `/var/oikos/workspaces` while settings default `OIKOS_WORKSPACE_PATH` to `~/.longhouse/workspaces`; local OSS may try to write to `/var/oikos` without permission.~~ ✅ Fixed (2026-02-10)
- [Docs vs UI] VISION describes a 3-step guided empty state with "Load demo" CTA; Timeline empty state is a single sentence ("Run 'longhouse ship'") with no demo button.
- [Docs vs repo] README "Docker" install says `docker compose up`, but there is no root `docker-compose.yml` or `compose.yaml`; Docker configs live under `docker/` (e.g., `docker/docker-compose.dev.yml`).
- [Docs vs code] `apps/runner/README.md` uses `LONGHOUSE_URL=http://localhost:30080` for dev/Docker; runner defaults to `ws://localhost:47300` and `longhouse serve` uses 8080, so the example points at the wrong port/service.
- [Docs vs code] `apps/zerg/backend/docs/specs/shipper.md` still documents `zerg` commands and `~/.claude/zerg-device-token`; current CLI is `longhouse` and tokens are stored at `~/.claude/longhouse-device-token` (legacy `zerg-` paths are migration-only).
- ~~[Docs vs code] `oikos_react_engine.py` module docstring claims "spawn_commis raises FicheInterrupted directly"; in parallel execution `_execute_tools_parallel` uses two-phase commit and does NOT raise FicheInterrupted (returns ToolMessages + interrupt_value instead).~~ ✅ Fixed (2026-02-10)
- ~~[Docs vs code] `jobs/git_sync.py` class docstring says "Thread-safety: Uses file lock," but the implementation is async with asyncio + `asynccontextmanager` and `asyncio.to_thread`; it's concurrency-safety, not thread-safety.~~ ✅ Fixed (2026-02-10)
- ~~[Bug] `jobs/commis.py` `_run_job` returns early if `extend_lease` fails before execution, leaving the job in `claimed` state until lease expiry (no reschedule/mark-dead handling).~~ ✅ Fixed (2026-02-10)
- ~~[Bug] `GitSyncService._get_auth_url()` mangles SSH-style repo URLs when `token` is set (e.g., `git@github.com:user/repo.git` → malformed `@@` URL); should reject token auth for SSH URLs or handle separately.~~ ✅ Fixed (2026-02-10)
- ~~[Docs vs code] Slack skill doc is wrong: `apps/zerg/backend/zerg/skills/bundled/slack/SKILL.md` references `slack_send_message` and `SLACK_BOT_TOKEN`, but the actual tool is `send_slack_webhook` and it uses incoming webhook URLs (connector/env), not a bot token.~~ ✅ Fixed (2026-02-10)
- ~~[Docs vs code] `services/shipper/spool.py` docstring claims replay uses idempotency keys, but the shipper does not send idempotency keys (dedupe relies on DB unique constraints).~~ ✅ Fixed (2026-02-10)
- ~~[Docs vs code] GitHub skill doc says `GITHUB_TOKEN` env var works; `github_tools` only resolves tokens from connectors or explicit parameters (no env fallback).~~ ✅ Fixed (2026-02-10)
- ~~[Docs vs code] Web search skill docs omit required `TAVILY_API_KEY`: `web_search` errors when the env var is missing, but `apps/zerg/backend/zerg/skills/bundled/web-search/SKILL.md` has no env requirement and is marked `always: true`.~~ ✅ Fixed (2026-02-10)
- [Docs vs infra] VISION Life Hub config uses `ZERG_API_URL=https://longhouse.ai/api`, but `https://longhouse.ai/api/*` returns 502; the working API host is `https://api.longhouse.ai`.
- ~~[Docs vs UI] Backend notifications use `https://longhouse.ai/runs/{run.id}` (see `oikos_service.py`), but the frontend has no `/runs/:id` route; unknown paths redirect to LandingPage/Timeline, so run links are broken.~~ ✅ Fixed — URLs now point to /timeline (2026-02-10)
- ~~[Docs vs code] CLI docs in `zerg/cli/__init__.py` and `zerg/cli/main.py` say `longhouse connect` is "continuous polling," but the CLI defaults to watch mode (polling only with `--poll`/`--interval`).~~ ✅ Fixed (2026-02-10)
- [Docs vs code] `scripts/install.sh` only documents `LONGHOUSE_API_URL`; CLI reads it, but `longhouse connect` fallback still uses 47300 (docs imply 8080).
- ~~[Docs vs reality] Timeline page copy says "across providers," but real ingest only supports Claude Code; other providers are demo-only.~~ ✅ Fixed (2026-02-10)
- ~~[Docs vs reality] Public info pages (`PricingPage.tsx`, `SecurityPage.tsx`, `PrivacyPage.tsx`) still describe fiches/workflows, Google-only OAuth auth, and dashboard account management, which don't match the current timeline-first OSS flow.~~ ✅ Fixed (2026-02-10)
- ~~[Docs vs code] DocsPage skills section says to add `SKILL.md` to `workspace/skills`; default loader path for OSS is `~/.longhouse/skills` unless a workspace path is configured.~~ ✅ Fixed (2026-02-10)
- ~~[Docs vs code] Landing SkillsSection says Slack skill can "manage channels," but Slack tool is webhook-only (send message); no channel management/listing tools exist.~~ ✅ Fixed (2026-02-10)
- ~~[Docs] QA job prompt (`apps/zerg/backend/zerg/jobs/qa/prompt.md`) still brands alerts as "SWARMLET QA"; should be Longhouse (brand drift).~~ ✅ File doesn't exist (2026-02-10)

---

## [Tech Debt] Evidence-Backed Refactor Ideas (Ranked)

(Former IDEAS.md. Each item includes an evidence script under `ideas/evidence/`.)

Best → worst. Run scripts from the repo root.

### Postgres Cleanup (SQLite-only OSS Pivot)
(Archived 2026-02-05 — alembic migrations removed; per user request ignore migration cleanup items. See git history or `ideas/evidence/` if needed.)

### Legacy Tool Registry + Deprecated Code

19. ~~[ID 19] Remove mutable ToolRegistry singleton once tests updated.~~ ✅ Done (2026-02-10)
Evidence: `ideas/evidence/39_evidence_tool_registry_mutable_singleton.sh`

20. ~~[ID 20] Remove legacy ToolRegistry wiring in builtin tools init.~~ ✅ Done (2026-02-10)
Evidence: `ideas/evidence/80_evidence_builtin_init_legacy_registry.sh`

21. ~~[ID 21] Drop non-lazy binder compatibility path.~~ ✅ Done (2026-02-10)
Evidence: `ideas/evidence/40_evidence_lazy_binder_compat.sh`

22. ~~[ID 22] Remove deprecated publish_event_safe wrapper.~~ ✅ Already gone (2026-02-10)
Evidence: `ideas/evidence/41_evidence_events_publisher_deprecated.sh`

23. [ID 23] Require envelope-only WS messages, remove legacy wrapping.
Evidence: `ideas/evidence/42_evidence_websocket_legacy_wrap.sh`

24. ~~[ID 24] Remove legacy admin routes without api prefix.~~ ✅ Done (2026-02-10)
Evidence: `ideas/evidence/43_evidence_admin_legacy_router.sh`

25. ~~[ID 25] Remove deprecated workflow start route.~~ ✅ Already gone (2026-02-10)
Evidence: `ideas/evidence/44_evidence_workflow_exec_deprecated_route.sh`

26. ~~[ID 26] Remove deprecated TextChannelController.~~ ✅ Already gone (2026-02-10)
Evidence: `ideas/evidence/51_evidence_text_channel_controller_deprecated.sh`

27. ~~[ID 27] Remove deprecated session handler API.~~ ✅ Done (2026-02-10)
Evidence: `ideas/evidence/52_evidence_session_handler_deprecated.sh`

28. [ID 28] ~~Remove~~ Relabel compatibility methods in feedback system — methods are actively called, not dead code.
Evidence: `ideas/evidence/53_evidence_feedback_system_compat.sh`

29. ~~[ID 29] Remove deprecated heuristic or hybrid decision modes in roundabout monitor.~~ ✅ Done (2026-02-10)
Evidence: `ideas/evidence/54_evidence_roundabout_monitor_deprecated_modes.sh`

30. ~~[ID 30] Remove HEURISTIC or HYBRID decision modes in LLM decider.~~ ✅ Already gone (2026-02-10)
Evidence: `ideas/evidence/55_evidence_llm_decider_deprecated_modes.sh`

31. ~~[ID 31] Simplify unified_access legacy behavior.~~ ✅ Removed entirely in Phase 3b (2026-02-10)
Evidence: `ideas/evidence/78_evidence_unified_access_legacy.sh`

32. [ID 32] Move or remove legacy ssh_tools from core.
Evidence: `ideas/evidence/77_evidence_ssh_tools_legacy.sh`

33. ~~[ID 33] Update Swarmlet user-agent branding in web_fetch tool.~~ ✅ Already done (2026-02-10)
Evidence: `ideas/evidence/79_evidence_web_fetch_swarmlet_user_agent.sh`

34. ~~[ID 34] Remove legacy workflow trigger upgrade logic in schemas/workflow.py.~~ ✅ Already gone (2026-02-10)
Evidence: `ideas/evidence/97_evidence_workflow_schema_legacy_upgrade.sh`

35. ~~[ID 35] Remove deprecated trigger_type field in workflow_schema.py.~~ ✅ Already gone (2026-02-10)
Evidence: `ideas/evidence/98_evidence_workflow_schema_deprecated_trigger_type.sh`

36. ~~[ID 36] Tighten trigger_config schema by removing extra allow compatibility.~~ ✅ Done (2026-02-10)
Evidence: `ideas/evidence/99_evidence_trigger_config_extra_allow.sh`

37. ~~[ID 37] Remove legacy trigger key scanner once legacy shapes dropped.~~ ✅ Done (2026-02-10)
Evidence: `ideas/evidence/96_evidence_legacy_trigger_check_script.sh`

### Frontend Legacy CSS + Test Signals

38. ~~[ID 38] Remove __APP_READY__ legacy test signal once tests updated.~~ ✅ Done (2026-02-10)
Evidence: `ideas/evidence/45_evidence_app_ready_legacy_signal.sh`

39. ~~[ID 39] Drop legacy React Flow selectors in CSS after test update.~~ ✅ Already gone (2026-02-10)
Evidence: `ideas/evidence/46_evidence_canvas_react_legacy_selectors.sh`

40. ~~[ID 40] Remove legacy buttons.css compatibility layer.~~ ✅ Done (2026-02-10)
Evidence: `ideas/evidence/47_evidence_buttons_css_legacy.sh`

41. [ID 41] ~~Remove~~ Relabel legacy modal pattern CSS — actively used by 8+ components; not dead code, would be a refactor.
Evidence: `ideas/evidence/48_evidence_modal_css_legacy.sh`

42. ~~[ID 42] Remove legacy util margin helpers once migrated.~~ ✅ Done (2026-02-10)
Evidence: `ideas/evidence/49_evidence_util_css_legacy.sh`

43. [ID 43] ~~Remove~~ Relabel legacy token aliases — 95+ active CSS refs; stable abstraction, not harmful to keep. Schedule with broader CSS refactor.
Evidence: `ideas/evidence/50_evidence_tokens_css_legacy_aliases.sh`

---

## [QA/Test] QA Plan (Virtual QA Team) (2026-02-02)

(Former QA_PLAN.md. Keep this plan updated here only.)

Date: 2026-02-02
Owner: Longhouse (Zerg) core
Scope: SQLite-only, timeline-first product with dual-path positioning (self-host + hosted)

### Goals (Vision-Aligned)
- Zero-friction OSS onboarding (install + onboard + demo) works on first run.
- Timeline/demo data feels alive immediately (no API keys required).
- Session ingest is reliable and lossless (shipper -> ingest -> timeline).
- Background agents (commis/runners) are stable and debuggable.
- No waiting for bug reports: automated QA catches regressions before users do.

### Current QA Inventory (What We Already Have)
- Makefile test tiers: `make test` (SQLite-lite), `make test-legacy`, `make test-e2e` (core + a11y), `make test-zerg-e2e`, `make test-frontend-unit`, `make test-hatch-agent`, `make test-runner-unit`, `make test-shipper-e2e`, `make onboarding-sqlite`, `make onboarding-funnel`, `make qa-oss`.
- Playwright E2E with core suite + a11y, visual baselines, perf tests (some skipped).
- Backend pytest suites: unit + integration; SQLite-lite tests in `tests_lite/`.
- Docs-as-source onboarding contract + Playwright test for README contract.
- Shipper tests (unit + integration), runner unit tests.

### Gaps vs Vision (What’s Missing / Fragile)
1) Docs/landing copy still overpromise features (cross-device resume, multi-provider); no automated drift checks in CI.
2) Installer + CLI onboarding flows lack robust automated tests across OS targets.
3) Demo DB pipeline is new; no automated validation that demo DB builds and UI uses it.
4) E2E commis/session-continuity failures (timeouts) -> core suite stability risk.
5) Remaining E2E skips are perf/visual suites only; dev-only/event-bus + unimplemented-feature specs removed and tracked in TODO instead.
6) Shipper end-to-end is opt-in and skipped by default; no required CI gate.
7) Runner and commis execution lack full integration tests with real WebSocket channel.
8) Real-time events (SSE/WS) core coverage is enabled; advanced WS protocol/queue tests are deferred until backend ack support exists.
9) No formal OS matrix for OSS install (macOS/Linux/WSL).
10) OSS user QA script exists (`scripts/qa-oss.sh`), but CI wiring is still pending.
11) ✅ FIXED: Timeline search E2E is now part of `test-e2e-core` gating.
12) ✅ FIXED: Oikos session discovery tools now have unit coverage.
13) ✅ FIXED: FTS trigger integrity tests cover update/delete index consistency.
14) Scheduling/trigger management UI remains unimplemented; E2E specs removed until product work lands.

### Virtual QA Team (Agent Roles)
Use commis/runners + hatch agents to form a lightweight QA org that runs locally or in CI.

- QA Lead (Coordinator): owns test matrix + gating; assigns tasks to agents.
- Spec Guardian: parses VISION/README, flags drift, updates onboarding contract tests.
- Installer Guardian: validates `install.sh` and CLI `longhouse onboard` flows on macOS + Linux.
- Shipper Guardian: validates JSONL -> ingest -> timeline continuity.
- Commis/Runner Guardian: validates background jobs and runner_exec end-to-end.
- E2E Explorer: maintains Playwright core suite + a11y + visual baselines.
- Fuzzer: property-based + fuzz tests for APIs, websocket envelopes, ingest parser.
- Perf/UX Agent: enforces latency budgets and visual baseline stability.

### QA System Architecture (How It Runs)

#### 1) QA Matrix (what must be tested)

User Paths
- OSS local: install -> onboard -> demo -> timeline -> ingest -> search
- Hosted: signup -> instance -> timeline -> ingest -> session query
- Power user: runner -> exec -> commis -> session continuity

System Layers
- Unit (fast, deterministic)
- Integration (real DB, real services, mocked external LLMs)
- E2E (UI + API)
- Contract/Docs-as-Source
- Perf + Visual + A11y
- Security + Dependency hygiene

Data States
- Empty DB
- Demo DB (seeded SQLite)
- Real ingest from JSONL

Providers
- Claude Code, Codex, Gemini, Cursor (at least schema + ingest tests)

#### 2) Tiered Test Gates

Tier 0 (local fast)
- lint-test-patterns, type checks, OpenAPI contract validation
- `make test` (SQLite-lite backend)
- `make test-frontend-unit`

Tier 1 (OSS path gate)
- `make onboarding-sqlite`
- Build demo DB + verify demo UI loads sessions
- CLI smoke: `longhouse onboard --quick --no-shipper` (headless)

Tier 2 (Core UX gate)
- `make test-e2e-core` (Playwright core, no skips)
- `make test-e2e-a11y`

Tier 3 (System gate)
- Shipper E2E with local backend (no skip)
- Runner + commis integration (websocket + task execution)

Tier 4 (Nightly)
- Full E2E suite, visual baselines, performance tests
- Optional live evals (requires API keys; runs on schedule)

#### 3) OSS QA Script (User-Run)

New script target: `scripts/qa-oss.sh` (or `longhouse doctor --full`).
Purpose: emulate the exact OSS user journey and catch regressions early.

Suggested flow:
1. Environment checks (Python/uv/bun, sqlite version)
2. Build demo DB (`demo-db`) and validate schema
3. Run `make onboarding-sqlite`
4. Boot demo stack (short-lived) and verify:
   - /api/health
   - /api/agents/sessions
   - demo timeline displays sessions
5. Run `make test` + `make test-frontend-unit`
6. Run `make test-e2e-core` (optional flag for CI vs local)
7. Print a short “OK / FAIL” summary

#### 4) LLM/Agent-Driven QA

- Test Synthesizer: generate Playwright tests from “journey specs” (YAML) and Vision changes.
- Failure Triage: summarize Playwright/pytest failures into reproducible steps + suspect areas.
- Regression Miner: when a bug is fixed, auto-suggest a new test case in the same area.
- Drift Checker: diff VISION/README to current UI selectors (CTA drift).

#### 5) Flake/Skip Elimination Strategy

- Replace “skipped until LLM mocking” with deterministic mock server.
- Convert flaky tests to stable selectors or API-assisted setup.
- Establish “no skip in core suite” rule; allow skips only in nightly/optional suites.

### Priority Backlog (Execution Plan)

P0 (now)
- Align README onboarding-contract with SQLite-first path.
- Add installer/CLI tests (install.sh, longhouse onboard, longhouse serve).
- Make demo DB build + demo load test part of OSS gate.
- Fix commis/session-continuity E2E timeouts (core suite must be 100% pass).
- Stabilize /api/health checks in tests (already in onboarding-sqlite).

P1 (next)
- Shipper E2E run in CI with a local backend (no skip).
- Runner + commis integration E2E (spawn runner, execute, verify run log).
- Unskip websocket/SSE tests by adding deterministic harness.
- Add LLM mock server for streaming tests (unskip chat_streaming, token tests).

P2 (after)
- Performance budgets (chat latency, timeline load) + baseline alerts.
- Visual baselines for landing + timeline + forum.
- Security/dependency scanning (npm audit + pip/uv audit).
- OS matrix for installer (macOS + Linux + WSL).

### Reporting & Artifacts
- Always collect Playwright traces and screenshots on failure.
- Export concise summaries: failed test, repro steps, suspected area.
- Store “last-known-good” test results and compare on regressions.

### Ownership & Cadence
- Per-PR: Tier 0 + Tier 1 + Tier 2 (core must pass).
- Nightly: Tier 3 + Tier 4.
- Release: all tiers + live evals (if keys available).

### Immediate Next Steps
1. Update onboarding contract to match SQLite-only path (no Docker). ✅
2. Add OSS QA script (new target) and wire to CI. ✅ (CI wiring pending)
3. Fix commis/session-continuity E2E failures and remove skip if possible.
4. Introduce deterministic LLM mock server so streaming tests can run.
5. Add demo DB validation to onboarding and E2E flows.
