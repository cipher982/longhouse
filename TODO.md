# TODO

Capture list for substantial work. Not quick fixes (do those live).

## For Agents

- Each entry is a self-contained handoff — read it, you have context to start
- Size (1-10) indicates scope: 1 = hour, 5 = day, 10 = week+
- Check off subtasks as you go so next agent knows state
- Add notes under tasks if you hit blockers or learn something

Classification tags (use on section headers): [Launch], [Product], [Infra], [QA/Test], [Docs/Drift], [Tech Debt], [Brand]

---

## 📊 Validation Summary (2026-02-04)

**Agent audit of all TODO sections. Use this to understand current state.**

### ✅ DONE (Close/Archive)
| Section | Status | Notes |
|---------|--------|-------|
| P0 OSS GA | ✅ 100% | All 5 items verified: auth, demo, CTAs, README, QA script |
| Post-GA Follow-ups | ✅ 100% | Rate limiting, hash support, UI fallback, demo-fresh, workflow removal |
| OSS Auth | ✅ 100% | Password login fully implemented with rate limiting |
| Prompt Cache Optimization | ✅ DONE | MessageArrayBuilder has correct layout. TODO was stale |

### ⚠️ PARTIALLY DONE
| Section | Status | Notes |
|---------|--------|-------|
| HN Launch Readiness | 95% | Hero CTAs work. Just verify in prod |
| Rebrand | ~80% | ~40 occurrences remain (NOT 229). Email templates + root openapi.json |
| FTS5 Search | 25% | Search bar UI exists but uses ILIKE not FTS5. Oikos tools don't exist |
| QA Infrastructure | 80% | Pieces exist but `make test-readmes` aggregator missing |
| Install/Onboarding | 60% | Missing: `longhouse doctor`, device token UI, fresh-shell verify |
| OSS First-Run UX | 50% | --demo works but onboarding doesn't seed demo data |

### ❌ NOT STARTED
| Section | Status | Notes |
|---------|--------|-------|
| Landing Page Redesign | 0% | All 6 phases not started. No header, no contrast fixes |
| Control Plane | 0% | No `apps/control-plane/` directory. Zero code exists |
| Forum Discovery UX | 0% | Heuristic status only. No presence events, no bucket UI |

### 📝 INACCURACIES CORRECTED
- Rebrand scope: TODO said 229 occurrences — actually ~40 (most already fixed)
- Prompt Cache: TODO said "needs reordering" — already done
- Landing CTAs: TODO said "broken" — actually work, just need prod verification

---

## [Launch] 🎯 HN Launch Priority (This Week)

**Decision:** OSS GA + Hosted Beta (optional). Hosted tasks only if P0 is done.

### P0 — OSS GA (Required)
| Priority | Task | Est |
|----------|------|-----|
| 1 | OSS Password Auth ✅ | 1 day |
| 2 | Demo mode flag ✅ | 0.5 day |
| 3 | Landing Page CTAs (self-hosted primary) ✅ | 0.5 day |
| 4 | README rewrite (Timeline + install paths) ✅ | 0.5 day |
| 5 | OSS GA QA script + README onboarding contract ✅ | 1 day |

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

- [x] Add rate limiting on `POST /auth/password`
- [x] Support `LONGHOUSE_PASSWORD_HASH` (bcrypt/argon2)
- [x] UI fallback if `/auth/methods` fails
- [x] Add `--demo-fresh` flag to rebuild demo DB
- [x] Remove workflow/canvas feature (backend + frontend + deps + tests)

---

## [Infra] ⚠️ Architecture Reality Check (Read First)

**VISION.md describes per-user isolated instances. That doesn't exist YET.**

Current reality (as of 2026-02-03):
- **ONE backend container** serves both `api.longhouse.ai` and `api-david.longhouse.ai`
- **ONE frontend container** serves both `longhouse.ai` and `david.longhouse.ai`
- **ONE SQLite database** at `/data/longhouse.db` (size varies; check on server)
- **No control plane** — can't provision per-user instances
- **"david.longhouse.ai" is cosmetic** — just DNS routing to shared infra

**Target state:** Control plane provisions isolated containers per user (Docker API + Traefik labels). See VISION.md for architecture.

See this file for the current launch analysis.

---

## [Launch] 🚨 OSS Auth — Password Login for Self-Hosters (3)

**Priority: CRITICAL for HN launch**

**Problem:** Only Google OAuth exists. OSS self-hosters must either:
- Set up their own Google Console project (unreasonable)
- Disable auth entirely (`AUTH_DISABLED=1`) (insecure for remote access)

**Solution:** Add simple password auth via environment variable.

- [x] Add `LONGHOUSE_PASSWORD` config var in `apps/zerg/backend/zerg/config/__init__.py`
- [x] Add `POST /api/auth/password` endpoint in `routers/auth.py`
  - If `LONGHOUSE_PASSWORD` is set, enable password login
  - If not set + localhost, auto-authenticate (current dev behavior)
  - If not set + remote, require password or Google OAuth
- [x] Add password login UI to `HeroSection.tsx` login modal
  - Show password field when `LONGHOUSE_PASSWORD` is configured
  - Keep Google OAuth as alternative
- [x] Update frontend config to detect password auth availability
- [x] Test full OSS flow: `install.sh` → `longhouse serve` → password login → timeline

**Files:** `config/__init__.py`, `routers/auth.py`, `HeroSection.tsx`, `config.ts`

---

## [Infra] Domain Split — Marketing vs Personal Instance (4)

**Goal:** longhouse.ai is marketing-only; david.longhouse.ai is the app (single-tenant).

**Status:** DNS routing complete. Marketing mode logic exists but has issues.

- [x] Add marketing-only frontend flag (hostname-driven) to disable auth + app routes on longhouse.ai
- [x] Update Coolify domains: zerg-web -> david.longhouse.ai, zerg-api -> api-david.longhouse.ai
- [x] Update zerg-api env: APP_PUBLIC_URL/PUBLIC_SITE_URL to david, CORS to include longhouse.ai + david
- [x] Add Cloudflare DNS for david.longhouse.ai + api-david.longhouse.ai (and optional wildcard)

**Reality check:** This is DNS routing to ONE shared deployment, not isolated instances. The "david" subdomain is cosmetic. See Architecture Reality Check above.

**Remaining issues:**
- [ ] Cross-subdomain OAuth code exists (`/auth/accept-token`) but targets non-existent per-user architecture — needs control plane to work as designed
- [ ] Marketing mode defaults were removed (broke auth) — needs cleaner hostname detection
- [ ] For now, use password auth on subdomains; Google OAuth only makes sense at control plane (longhouse.ai)

---

## [Product] Landing Page Redesign — Full (6)

**Goal:** Clear user paths, visible CTAs, better contrast. Visitor instantly understands: what it is, who it's for, how to get started.

**⚠️ DEPENDS ON LAUNCH DECISION:**
- **OSS GA (current):** Hero should emphasize `install.sh`, self-host, "your data stays local"
- **Hosted Beta:** Secondary CTA or "Join waitlist" copy

Current copy is a mix of both stories. Align to OSS-first primary.

**Problems identified (2026-02-03):**
1. Sign-in button is ghost variant, bottom of hero — hard to see, weird position
2. Colors too dark — low contrast text, cards blend into background
3. No clear user path differentiation (self-hosted vs cloud vs paid)
4. No sticky header — can't navigate or sign in without scrolling up
5. Current story (AI That Knows You, integrations) is OLD — new story is Timeline + Search + Resume
6. **NEW:** Several CTA buttons don't work or lead to broken flows

### Phase 1: Header + Navigation (2 hours)

Add a persistent sticky header following dev-tool best practices (Vercel, Supabase, Railway).

- [ ] Create `LandingHeader.tsx` component with sticky positioning
- [ ] Left: Logo + "Longhouse" wordmark
- [ ] Center: Product | Docs | Pricing | Enterprise (or Self-host)
- [ ] Right: "Sign In" (secondary) + "Get Started" (primary CTA)
- [ ] Mobile: hamburger menu
- [ ] Add to `LandingPage.tsx` above hero

**Design notes:**
- Header bg: slightly lighter than page bg (elevation via lightening, not shadows)
- Use brand accent color for primary CTA (stands out on dark)
- "Sign In" visible but secondary (ghost or outline variant)

**Files:** `components/landing/LandingHeader.tsx`, `LandingPage.tsx`, `landing.css`

### Phase 2: User Path Differentiation (2 hours)

Make Cloud / Self-host / Enterprise paths explicit with distinct CTAs.

**Option A: Hero with dual path**
- [ ] Primary CTA: "Start Free" → Cloud signup (highlighted, brand color)
- [ ] Secondary CTA: "Self-host" → scrolls to install section
- [ ] Tertiary link: "Enterprise →" below

**Option B: Three-card section below hero**
- [ ] Add `DeploymentOptions.tsx` with 3 cards: Cloud Beta | Self-hosted | Enterprise
- [ ] Each card: 1-line promise, 3 features, dedicated CTA
- [ ] Cloud: "Start Free" → signup modal
- [ ] Self-host: "Install CLI" → install section
- [ ] Enterprise: "Contact Us" → mailto or form

**Recommended approach:** Option A for hero simplicity + Option B as separate section

- [ ] Update `HeroSection.tsx` CTAs to emphasize Cloud path
- [ ] Create `DeploymentOptions.tsx` section
- [ ] Add comparison table: who runs it, data residency, support, upgrade path

**Files:** `HeroSection.tsx`, `components/landing/DeploymentOptions.tsx`, `PricingSection.tsx`

### Phase 3: Color/Contrast Improvements (2 hours)

Fix dark theme accessibility issues. Target WCAG 4.5:1 for text, 3:1 for UI.

**CSS Variable Updates:**
- [ ] Audit `--color-text-secondary` and `--color-text-muted` contrast ratios
- [ ] Increase body text contrast (current ~4.0:1, need 4.5:1+)
- [ ] Add card elevation: cards should be visibly lighter than page bg
- [ ] Improve CTA button contrast: primary should pop (saturated accent on dark)
- [ ] Badge contrast: "Free during beta" badge needs better visibility

**Specific fixes:**
- [ ] `.landing-hero-subhead` — bump from `--color-text-secondary` to higher contrast
- [ ] `.landing-hero-note` — bump from `--color-text-muted`
- [ ] `.landing-step` cards — add lighter bg or visible border
- [ ] `.landing-cta-main` — increase glow/prominence (currently blends)
- [ ] `.landing-pricing-card` — more visible elevation

**Test:** Run contrast checker on all text/bg combinations

**Files:** `styles/tokens.css` (or wherever vars defined), `landing.css`

### Phase 4: Hero CTA Restructure (1 hour)

Move Sign In to header, restructure hero CTAs for clarity.

**Current (bad):**
```
[Install section with curl command]
[See How It Works ↓] [Sign In]  ← ghost buttons, same weight
```

**Target:**
```
[Start Free - Cloud] [Self-host →]  ← clear primary + secondary
[No credit card • Works offline • <2min setup]
```

- [ ] Remove "Sign In" from hero (it's now in header)
- [ ] Primary CTA: "Start Free" (triggers signup modal or goes to /signup)
- [ ] Secondary CTA: "Self-host →" (scrolls to install section OR links to docs)
- [ ] Keep install command section but position as "Self-host" path
- [ ] Add friction reducers: "No credit card", "Free during beta", etc.

**Files:** `HeroSection.tsx`, `InstallSection.tsx`

### Phase 5: Story Alignment (2 hours)

Update copy to match VISION.md value prop: Timeline + Search + Resume.

**Hero copy:**
- [ ] Headline: "Never lose an AI coding conversation" (or similar)
- [ ] Subhead: "Search across Claude, Codex, Cursor, Gemini. Resume from anywhere."
- [ ] Note: "Free cloud workspace during beta. Self-host anytime."

**How It Works:**
- [ ] Step 1: "Install" → Your sessions sync automatically
- [ ] Step 2: "Search" → Find where you solved it (FTS5 instant)
- [ ] Step 3: "Resume" → Continue from any device (commis)

**Cut/minimize:**
- [ ] IntegrationsSection (wrong story — we're not about connecting apps)
- [ ] SkillsSection (power user feature, not hero)
- [ ] Move Oikos chat to "Features" section, not hero

**Files:** `HeroSection.tsx`, `HowItWorksSection.tsx`, `IntegrationsSection.tsx`, `SkillsSection.tsx`

### Phase 6: Visual Assets (1 hour)

Update screenshots to show Timeline, not old dashboard.

- [ ] Capture Timeline page with demo sessions populated
- [ ] Capture search results (if FTS5 is ready)
- [ ] Capture session detail view with events
- [ ] Update `dashboard-preview.png` → `timeline-preview.png`
- [ ] Add provider logos inline (Claude, Codex, Cursor, Gemini)

**Files:** `public/images/landing/`, `HeroSection.tsx`

### Checklist (dev-tool landing page best practices 2025-26)

- [ ] Above fold: Cloud / Self-host paths with distinct CTAs
- [ ] Header: Docs + Pricing reachable in 1 click
- [ ] CTAs: hero + header + mid-page + footer; labels match next step
- [ ] Dark theme: text ≥ 4.5:1, UI components ≥ 3:1, visible focus indicators
- [ ] Sticky header doesn't obscure focus / anchors
- [ ] Self-host responsibilities spelled out (not marketing-only)

---

## [Launch] HN Launch Readiness — Remaining (4)

**Goal:** HN reader can install, see value immediately, understand what problem this solves, and start using it.

**Launch Path Decision:** OSS GA + Hosted Beta (optional).

### 🚨 Critical Blockers (Fix First)

- [x] **OSS Auth** — Password login for self-hosters (see dedicated section above)
- [x] **Password-only config bug** — `_validate_required()` now skips Google OAuth validation when password auth is configured
  - File: `apps/zerg/backend/zerg/config/__init__.py:512`
  - Fixed: Skip Google OAuth validation if `LONGHOUSE_PASSWORD` or `LONGHOUSE_PASSWORD_HASH` is set
- [ ] **Landing page CTAs** — Several buttons don't work or lead nowhere

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

### Phase 0: Routing + DNS Reality Check ✅ DONE

- [x] Wildcard DNS `*.longhouse.ai` configured in Cloudflare (A record → 5.161.92.127, proxied)
- [x] Routing layer: Caddy (existing coolify-proxy) with caddy-docker-proxy labels
- [x] Manual provision smoke test: test2, test3 instances provisioned and routed
- [ ] Add control-plane → instance auth bridge endpoint (control plane issues token; instance sets cookie for owner).

### Phase 1: Scaffold + Auth ✅ DONE

- [x] Create `apps/control-plane/` directory structure (FastAPI app, models, routers, services)
- [x] Add provisioner service (Docker API client with Caddy labels)
- [x] Add Instance model with subdomain, container_name, state
- [x] Admin API + minimal HTML UI for manual provisioning
- [ ] Add Google OAuth (control plane only, not per-instance)
- [ ] Add User model with Stripe fields

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
- [x] Add health check polling after provision
- [ ] Build and push runtime image (`docker/runtime.dockerfile`) to ghcr.io
- [ ] Update CONTROL_PLANE_IMAGE to use runtime image (currently uses backend-only)

### Phase 4: Cross-Subdomain Auth (2)

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

**Infra status:**
- ✅ Caddy (coolify-proxy) on zerg handles subdomain routing via caddy-docker-proxy labels
- ✅ Wildcard DNS `*.longhouse.ai` configured in Cloudflare (proxied)
- ✅ Docker socket access from control plane container
- ✅ Postgres for control plane DB (separate container via docker-compose)
- ⏳ Runtime image needs build + push to ghcr.io

---

## [Launch] Public Launch Checklist (6)

Ensure launch readiness without relying on scattered docs.

- [ ] Rewrite README to center Timeline value and 3 install paths.
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
- **Comment skeleton:** Problem (JSONL sprawl) → Solution (timeline + search) → Current state (Claude only, others planned, local-first) → Try it (`curl -fsSL https://get.longhouse.ai/install.sh | bash`, `longhouse serve`)
- **Anticipated Qs:** Why not Claude history? · Cursor/Codex/Gemini when? · Privacy? · Performance at scale?
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
- [ ] Improve "No Claude Code" guidance in onboard wizard (link to alternatives, explain what to do next)
- [ ] Consider demo mode flag for `longhouse serve --demo` (starts with pre-loaded sessions for exploration)

---

## [Launch] Install + Onboarding Alignment (4)

Close the gap between VISION, README, docs, and the live installer.

- [x] **Canonical install path**: `install.sh` is primary (README + landing aligned)
- [ ] **Document onboarding wizard** (steps, troubleshooting, service install) and link it from README + landing page
- [ ] **Add `longhouse doctor`** (self-diagnosis for server health, shipper status, config validity); run after install/upgrade and recommend in docs
- [ ] **Installer polish:** verify Claude shim + PATH in a *fresh* shell and print an exact fix line when it fails (VISION requirement)

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

**Scope:** 105 occurrences across 28 frontend files, 124 occurrences across 39 backend files (229 total)

- [ ] Replace "Swarmlet" with "Longhouse" in frontend HTML metadata + webmanifest
- [ ] Update `package.json` description to Longhouse naming
- [ ] Update runner README/package metadata to Longhouse (e.g., "Longhouse Runner")
- [ ] Update email templates / notification copy referencing Swarmlet
- [ ] Decide domain swap (`swarmlet.com` → `longhouse.ai`) and update hardcoded URLs if approved
- [ ] Update landing FAQ + marketing copy that still says "PostgreSQL" or "Swarmlet" (e.g., `TrustSection.tsx`)
- [ ] Update OpenAPI schema metadata (title/description/servers) to Longhouse and regenerate `openapi.json` + frontend types

---

## [Brand] Longhouse Rebrand — CLI / Packages / Images (7)

Package and binary naming so OSS users see Longhouse everywhere.

- [ ] Decide npm scope/name for runner: `@longhouse/runner` or `longhouse-runner`
- [ ] Update docker image name in README/examples (ghcr.io/.../longhouse)
- [ ] Update installer scripts to new names (12 refs across 4 scripts)

---

## [Tech Debt] Prompt Cache Optimization (5)

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

## [Product] Session Discovery — FTS5 Search + Oikos Tools (6)

Make session discovery actually useful. Two tiers: fast search bar for keywords, Oikos for complex discovery.

**Problem:** Timeline cards are just a prettier version of scrolling snippets. Real value is finding "where did I solve X?"

**Architecture:**
- **Search bar**: SQLite FTS5 over session events. Instant (<10ms), keyword-based.
- **Oikos**: Agentic multi-tool discovery. Semantic search, grep, filters, cross-referencing.

### Phase 1: FTS5 Search Bar (Timeline)

- [ ] Add FTS5 virtual table for `agents.events` content (`content_text`, `tool_name`, etc.)
- [ ] Add search endpoint `GET /api/agents/sessions/search?q=...`
- [ ] Add search bar UI to Timeline page (debounced, instant results)
- [ ] Results show matching snippets with highlights
- [ ] Click result → opens session detail at relevant event

**Files:** `models/agents.py`, `services/agents_store.py`, `routers/agents.py`, `SessionsPage.tsx`

### Phase 2: Oikos Session Discovery Tools

- [ ] Add `search_sessions` tool (FTS5 search, returns session summaries)
- [ ] Add `grep_sessions` tool (regex search over event content)
- [ ] Add `filter_sessions` tool (by project, date range, provider, tool usage)
- [ ] Add `get_session_detail` tool (fetch full session with events)
- [ ] Register tools in Oikos core tools

**Files:** `tools/builtin/session_tools.py`, `oikos_tools.py`

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

## [Docs/Drift] Findings / Drift Audit (2026-02-03)

(Former FOUND.md. Keep this list updated here only.)

- [Infra/docs] API host mismatch: backend is configured for `api-david.longhouse.ai`, but TODO Domain Split still references `api.david.longhouse.ai`. `curl https://api-david.longhouse.ai/health` returns 200; `curl https://api.david.longhouse.ai/health` fails, and container labels only include `api-david.longhouse.ai`.
- [Infra/docs] `longhouse.ai/install.sh` serves the SPA HTML (not the installer). The working installer URL is `get.longhouse.ai/install.sh` (302 to GitHub raw). ✅ FIXED: Scripts now use correct URL.
- [Infra/docs] Wildcard DNS claim is stale: `dig foo.longhouse.ai` returns no records, so `*.longhouse.ai` does not appear to be configured. ✅ FIXED: VISION now says "needs setup".
- [Infra/docs] DB size is outdated: docs say `/data/longhouse.db (~2.5GB)` but current file is ~3.5GB (`/data/longhouse.db`). Update TODO launch notes.
- [Docs vs code] Install script installs Python 3.11, but `pyproject.toml` requires `>=3.12`. `scripts/install.sh` should install 3.12+ (or lower the requirement).
- [Docs vs code] Shipper defaults to `http://localhost:47300`, while `longhouse serve` defaults to 8080 and README uses 8080. Running `longhouse connect` with no `--url` will target the wrong port.
- ✅ FIXED: [Docs vs code] VISION mentions `longhouse up` and port `30080` for OSS. CLI is `longhouse serve` and default port is `8080`. (VISION updated to `longhouse serve` and port 8080)
- ✅ FIXED: [Docs vs code] VISION claims FTS5-powered timeline search; current search is `ilike` join on events (no FTS5 tables/queries present). (VISION updated to say FTS5 is planned)
- [Docs vs code] VISION says job claiming is dialect-aware (Postgres `FOR UPDATE SKIP LOCKED`). `commis_job_queue.py` is SQLite-specific (`datetime('now')`, `UPDATE ... RETURNING`) and is imported unconditionally in `commis_job_processor.py`.
- [Docs vs code] Workspace paths in VISION are `~/.longhouse/workspaces/...` and artifacts in `~/.longhouse/artifacts`, but current defaults are `/var/oikos/workspaces` and `settings.data_dir` (`/data` in Docker or repo `data/`). Session resume temp workspaces default to `/tmp/zerg-session-workspaces`.
- [Docs vs code] VISION control-plane section lists `apps/control-plane/` file tree, but no such directory exists in repo (plan vs reality is unclear).
- [Docs vs infra] VISION control-plane routing assumes Traefik labels; current infra uses Caddy (coolify-proxy with Caddy labels). If Traefik is intended, docs should say so and note migration.
- [Docs vs release] PyPI latest is `longhouse` 0.1.1, but repo `pyproject.toml` is 0.1.2. Launch notes still claim no PyPI package; README implies `pip install` yields current features.
- [Docs vs code] README + HN launch copy claim search across Claude/Codex/Cursor/Gemini, but shipper/parser only supports Claude Code JSONL. Demo seeds include Codex/Gemini, but real ingest is Claude-only.
- [Docs] Launch notes say MIT license; repo LICENSE and pyproject are Apache-2.0.
- [Docs] Launch notes checklist says “README has screenshot (done!)” but README has no image.
- [Docs] Launch notes say demo data seeds on first run; current behavior requires `--demo/--demo-fresh` or calling the demo seed endpoint.
- ✅ FIXED: [Docs] VISION repeatedly references `brew install longhouse`, but there is no Homebrew formula in repo or release workflow. (VISION updated to mark Homebrew as planned/future)
- [Docs conflict] Launch plan notes suggest provisioning via Coolify API; VISION explicitly says not to use Coolify for dynamic provisioning.
- [Docs vs code] VISION onboarding-contract example is Docker-centric (`cp .env.example`, `docker compose up`), but README’s contract runs bun+uv + `longhouse serve`; VISION’s example is stale.
- [Docs vs code] VISION says `longhouse connect <url>` installs and starts the shipper; actual CLI only installs when `--install` is passed (default runs foreground watch/poll).
- [Docs vs code] VISION says device token is issued during `longhouse connect`; actual flow requires manual token creation in UI (`/dashboard/settings/devices`) and paste into CLI.
- [Docs vs code] VISION specifies shipper batching “1 second or 100 events”; implementation ships per file with no time-window batching (only `batch_size` for spool replay).
- [Docs vs code] VISION says shipper replay uses idempotency keys; shipper does not send idempotency keys/headers (dedupe relies on DB unique index).
- [Docs vs UI] “Resume from anywhere / Timeline resume” is not in Timeline UI; resume is only implemented in Forum Drop-In (Claude-only) and not exposed on `/timeline`.
- [Docs vs code] VISION lists Oikos discovery tools (`search_sessions`, `grep_sessions`, `filter_sessions`, `get_session_detail`) but no such tool implementations exist.
- [Docs vs code] VISION says cross-subdomain auth tokens are one-time with nonce stored server-side and validated via control plane/JWKS; current `POST /api/auth/accept-token` just validates JWT and sets cookie (no nonce/one-time guard).
- [Docs/infra] `install-claude.sh` is broken: `longhouse.ai/install-claude.sh` serves the SPA HTML, `get.longhouse.ai/install-claude.sh` redirects to `scripts/install-claude.sh` which 404s. ✅ PARTIAL: Shim script now points users to Anthropic docs instead of broken URL.
- [Docs vs code] VISION requires a PATH-based Claude shim + verification in a fresh shell; current installer only adds a hook unless `~/.longhouse/install-claude-shim.sh` already exists and does not verify in a new shell.
- STALE: [Docs] QA_PLAN says onboarding contract is still Docker-centric; README contract now uses bun+uv + `longhouse serve` (SQLite), so this gap statement is stale. (QA_PLAN docs likely removed or outdated)
- [Docs] Launch notes claim session files in `~/.codex/sessions/*` etc; current shipper/parser only reads Claude Code (`~/.claude/projects/...`).
- [Docs vs code] Password-only auth isn't actually possible in production: `_validate_required()` always requires `GOOGLE_CLIENT_ID/SECRET` when `AUTH_DISABLED=0`, even if `LONGHOUSE_PASSWORD[_HASH]` is set. README/launch notes imply password-only is supported. ✅ FIXED: Google OAuth validation now skipped when password auth is configured.
- [Docs vs code] `session_continuity.py` defaults `LONGHOUSE_API_URL` to `http://localhost:47300`, conflicting with `longhouse serve` default 8080 and README.
- [Docs vs UI] `longhouse auth` instructs users to open `/dashboard/settings/devices`, but there is no device-token UI or route; frontend only has `/settings` and no device token page.
- [Code inconsistency] `WorkspaceManager` defaults to `/var/oikos/workspaces` while settings default `OIKOS_WORKSPACE_PATH` to `~/.longhouse/workspaces`; local OSS may try to write to `/var/oikos` without permission.
- [Docs vs UI] VISION describes a 3-step guided empty state with “Load demo” CTA; Timeline empty state is a single sentence (“Run 'longhouse ship'”) with no demo button.
- [Docs vs repo] README “Docker” install says `docker compose up`, but there is no root `docker-compose.yml` or `compose.yaml`; Docker configs live under `docker/` (e.g., `docker/docker-compose.dev.yml`).
- [Docs vs code] `apps/runner/README.md` uses `LONGHOUSE_URL=http://localhost:30080` for dev/Docker; runner defaults to `ws://localhost:47300` and `longhouse serve` uses 8080, so the example points at the wrong port/service.
- STALE: [Docs vs code] `experiments/shipper-manual-validation.md` is legacy: uses `zerg` CLI, old token/url files (`zerg-device-token`, `zerg-url`), old launchd label `com.swarmlet.shipper`, old frontend port 30080, and claims no sessions UI exists (Timeline now exists). (Experiments doc is historical, can be deleted or archived)
- [Docs vs code] `apps/zerg/backend/docs/specs/shipper.md` still documents `zerg` commands and `~/.claude/zerg-device-token`; current CLI is `longhouse` and tokens are stored at `~/.claude/longhouse-device-token` (legacy `zerg-` paths are migration-only).
- [Docs] `apps/zerg/backend/docs/supervisor_tools.md` references non-existent paths/tests: `apps/zerg/backend/docs/oikos_tools.md`, `examples/oikos_tools_demo.py`, `tests/test_oikos_tools.py`, `tests/test_oikos_tools_integration.py`, and “20/20 tests passing” despite those files not existing (only `tests/tools/test_oikos_tools_errors.py` exists).
- [Docs vs code] `oikos_react_engine.py` module docstring claims “spawn_commis raises FicheInterrupted directly”; in parallel execution `_execute_tools_parallel` uses two-phase commit and does NOT raise FicheInterrupted (returns ToolMessages + interrupt_value instead).
- [Docs vs code] `jobs/git_sync.py` class docstring says “Thread-safety: Uses file lock,” but the implementation is async with asyncio + `asynccontextmanager` and `asyncio.to_thread`; it’s concurrency-safety, not thread-safety.
- [Bug] `jobs/commis.py` `_run_job` returns early if `extend_lease` fails before execution, leaving the job in `claimed` state until lease expiry (no reschedule/mark-dead handling).
- [Bug] `GitSyncService._get_auth_url()` mangles SSH-style repo URLs when `token` is set (e.g., `git@github.com:user/repo.git` → malformed `@@` URL); should reject token auth for SSH URLs or handle separately.
- [Docs vs code] Slack skill doc is wrong: `apps/zerg/backend/zerg/skills/bundled/slack/SKILL.md` references `slack_send_message` and `SLACK_BOT_TOKEN`, but the actual tool is `send_slack_webhook` and it uses incoming webhook URLs (connector/env), not a bot token.
- [Docs vs code] README features claim “FTS5-powered” search; backend search is `ilike`-based with no FTS5 tables/queries.
- [Docs vs code] `scripts/install.sh` WSL warning tells users to run `longhouse connect --foreground`, but the CLI has no `--foreground` flag (foreground is the default).
- [Docs vs code] `services/shipper/spool.py` docstring claims replay uses idempotency keys, but the shipper does not send idempotency keys (dedupe relies on DB unique constraints).
- [Docs vs code] GitHub skill doc says `GITHUB_TOKEN` env var works; `github_tools` only resolves tokens from connectors or explicit parameters (no env fallback).
- [Docs vs code] Web search skill docs omit required `TAVILY_API_KEY`: `web_search` errors when the env var is missing, but `apps/zerg/backend/zerg/skills/bundled/web-search/SKILL.md` has no env requirement and is marked `always: true`.
- [Docs/infra] `scripts/install-runner.sh` advertises `curl -sSL https://longhouse.ai/install-runner.sh | bash`, but that URL serves the SPA HTML. ✅ FIXED: Script now uses correct URL.
- [Docs vs infra] VISION Life Hub config uses `ZERG_API_URL=https://longhouse.ai/api`, but `https://longhouse.ai/api/*` returns 502; the working API host is `https://api.longhouse.ai`.
- [Docs vs UI] Backend notifications use `https://longhouse.ai/runs/{run.id}` (see `oikos_service.py`), but the frontend has no `/runs/:id` route; unknown paths redirect to LandingPage/Timeline, so run links are broken.
- [Docs vs code] CLI docs in `zerg/cli/__init__.py` and `zerg/cli/main.py` say `longhouse connect` is “continuous polling,” but the CLI defaults to watch mode (polling only with `--poll`/`--interval`).
- [Docs vs code] `scripts/install.sh` advertises `LONGHOUSE_API_URL` env var but never reads it; the comment also implies a default `http://localhost:8080`, while shipper API defaults to 47300.
- [Docs vs reality] Timeline page copy says “across providers,” but real ingest only supports Claude Code; other providers are demo-only.
- [Docs vs reality] Landing “How It Works” copy in `frontend-web/src/components/landing/HowItWorksSection.tsx` claims: (a) sessions auto-sync from Claude/Codex/Cursor, (b) FTS5-powered search, (c) resume any conversation from any device — all currently false.
- [Docs vs reality] Landing hero subhead says “Your AI agents run in the cloud. Resume from any device.” (`HeroSection.tsx`), but OSS is local-first and cross-device resume isn’t implemented.
- [Docs vs reality] Public Docs page (`frontend-web/src/pages/DocsPage.tsx`) is still the old “fiche/canvas/dashboard” workflow with Google sign-in, not the timeline-first OSS product.
- [Docs vs reality] Public info pages (`PricingPage.tsx`, `SecurityPage.tsx`, `PrivacyPage.tsx`) still describe fiches/workflows, Google-only OAuth auth, and dashboard account management, which don’t match the current timeline-first OSS flow.
- [Docs vs code] Landing “Create Your Own” skills copy says to add `SKILL.md` to `skills/` in your workspace; actual loader looks in `~/.longhouse/skills` (user) or `workspace/skills/` (requires workspace path in server), so the user doc is incomplete/unclear for OSS users.
- [Docs vs reality] Landing FAQ (`TrustSection.tsx`) claims data is stored in PostgreSQL and auth is Google-only; current OSS runtime is SQLite and supports password/local auth.
- [Docs vs reality] Landing DemoSection/Footers still use cloud-workspace + “from any device” copy (`DemoSection.tsx`, `FooterCTA.tsx`), which doesn’t match local-first OSS or missing cross-device resume.
- [Docs vs code] Landing SkillsSection says Slack skill can “manage channels,” but Slack tool is webhook-only (send message); no channel management/listing tools exist.
- [Docs vs code] `DemoSection.tsx` comment says it “Shows Chat, Dashboard, and Canvas views,” but `ProductShowcase` only displays Timeline and Session Detail tabs.
- ✅ FIXED: [Docs vs code] VISION "Runner registration" says `longhouse runner register` generates credentials, but the CLI has no `runner` command; registration happens via the runner installer hitting `/api/runners/register`. (VISION updated to describe actual flow)
- ✅ FIXED: [Docs vs code] VISION CLI section says `longhouse status` shows running jobs and `longhouse logs <job_id>` tails job logs, but `longhouse status` only prints configuration and there is no `logs` command. (VISION updated - `logs` marked as planned)
- ✅ FIXED: [Docs vs code] VISION "File Structure (After)" claims `~/.longhouse/logs/` with per-job logs and shows `→ http://0.0.0.0:8080`; actual logging is `~/.longhouse/server.log` (server) + `~/.claude/shipper.log` (shipper) and `longhouse serve` defaults to `127.0.0.1:8080` unless `--host` is set. (VISION updated with correct paths and port)
- ✅ FIXED: [Docs vs code] VISION says runners are "Node.js" daemons, but the runner is Bun-based (`apps/runner` uses Bun scripts and builds a Bun-compiled binary); Node isn't required. (VISION updated to say Bun-compiled)
- ✅ FIXED: [Docs vs code] VISION OSS local path says the shipper runs "zero config" alongside `longhouse up`; in reality the shipper is not auto-started by `longhouse serve` and requires `longhouse connect`/`onboard` to install or run. (VISION updated to note shipper requires `longhouse connect`)
- STALE: [Docs] QA_PLAN P0 backlog references `longhouse up`, but the CLI has no `up` command (it's `longhouse serve`). (QA_PLAN docs likely removed or outdated)
- ✅ FIXED: [Docs vs repo] VISION "Docker alternative" says `docker compose up` for full stack with Postgres, but there is no root compose file; Docker configs live under `docker/` (same drift as README). (VISION updated to use correct path `docker/docker-compose.dev.yml`)
- [Docs] QA job prompt (`apps/zerg/backend/zerg/jobs/qa/prompt.md`) still brands alerts as “SWARMLET QA”; should be Longhouse (brand drift).
- STALE: [Docs conflict] VISION "Prompt Cache Optimization" claims message layout already system→conversation→dynamic, but TODO still says layout busts cache and needs reordering; code comments in `fiche_runner.py` show the target layout already in place, so TODO item looks stale. (Target layout is already in place per code comments)
- ✅ FIXED: [Docs vs code] VISION Homebrew formula sketch depends on `python@3.11`, but backend requires Python 3.12+ per `pyproject.toml` (same mismatch as install.sh). (VISION updated to python@3.12)

---

## [Tech Debt] Evidence-Backed Refactor Ideas (Ranked)

(Former IDEAS.md. Each item includes an evidence script under `ideas/evidence/`.)

Best → worst. Run scripts from the repo root.

### Postgres Cleanup (SQLite-only OSS Pivot)

01. [ID 01] Remove agents schema mapping for SQLite-only core.
Evidence: `ideas/evidence/21_evidence_agents_schema_mapping.sh`

02. [ID 02] Drop ensure_agents_schema Postgres-only schema creation.
Evidence: `ideas/evidence/22_evidence_ensure_agents_schema_postgres.sh`

03. [ID 03] Replace postgresql.UUID or JSONB in agents schema migration.
Evidence: `ideas/evidence/23_evidence_alembic_0002_postgres_types.sh`

04. [ID 04] Replace postgresql.UUID in device tokens migration.
Evidence: `ideas/evidence/24_evidence_alembic_0004_postgres_uuid.sh`

05. [ID 05] Replace postgresql.UUID in memories migration.
Evidence: `ideas/evidence/25_evidence_alembic_0007_postgres_uuid.sh`

06. [ID 06] Move Postgres checkpointer to optional module.
Evidence: `ideas/evidence/26_evidence_checkpointer_postgres_path.sh`

07. [ID 07] Remove Postgres advisory lock support from fiche_state_recovery.
Evidence: `ideas/evidence/27_evidence_fiche_state_recovery_advisory.sh`

08. [ID 08] Simplify task_runner Postgres guard logic in SQLite-only mode.
Evidence: `ideas/evidence/28_evidence_task_runner_postgres_guard.sh`

09. [ID 09] Remove asyncpg stub ops_db module that raises NotImplemented.
Evidence: `ideas/evidence/29_evidence_ops_db_asyncpg_stub.sh`

10. [ID 10] Move legacy Postgres test suite out of core repo.
Evidence: `ideas/evidence/30_evidence_tests_readme_legacy_postgres.sh`

11. [ID 11] Remove run_backend_tests.sh legacy Postgres runner.
Evidence: `ideas/evidence/31_evidence_run_backend_tests_postgres.sh`

12. [ID 12] Archive dev-docker Postgres script if Docker is legacy.
Evidence: `ideas/evidence/32_evidence_dev_docker_postgres.sh`

13. [ID 13] Archive stop-docker Postgres script if Docker is legacy.
Evidence: `ideas/evidence/33_evidence_stop_docker_postgres.sh`

14. [ID 14] Move Postgres-only checkpointer tests out of default suite.
Evidence: `ideas/evidence/34_evidence_test_checkpointer_postgres.sh`

15. [ID 15] Remove device-token tests that expect Postgres-only behavior.
Evidence: `ideas/evidence/35_evidence_test_device_tokens_postgres.sh`

16. [ID 16] Remove asyncpg result handling tests once asyncpg removed.
Evidence: `ideas/evidence/36_evidence_test_qa_fiche_asyncpg.sh`

17. [ID 17] Remove advisory-lock support tests after SQLite-only pivot.
Evidence: `ideas/evidence/37_evidence_test_fiche_state_recovery_postgres.sh`

18. [ID 18] Revisit timeseries compatibility tests tied to Postgres assumptions.
Evidence: `ideas/evidence/38_evidence_test_ops_service_timeseries.sh`

### Legacy Tool Registry + Deprecated Code

19. [ID 19] Remove mutable ToolRegistry singleton once tests updated.
Evidence: `ideas/evidence/39_evidence_tool_registry_mutable_singleton.sh`

20. [ID 20] Remove legacy ToolRegistry wiring in builtin tools init.
Evidence: `ideas/evidence/80_evidence_builtin_init_legacy_registry.sh`

21. [ID 21] Drop non-lazy binder compatibility path.
Evidence: `ideas/evidence/40_evidence_lazy_binder_compat.sh`

22. [ID 22] Remove deprecated publish_event_safe wrapper.
Evidence: `ideas/evidence/41_evidence_events_publisher_deprecated.sh`

23. [ID 23] Require envelope-only WS messages, remove legacy wrapping.
Evidence: `ideas/evidence/42_evidence_websocket_legacy_wrap.sh`

24. [ID 24] Remove legacy admin routes without api prefix.
Evidence: `ideas/evidence/43_evidence_admin_legacy_router.sh`

25. [ID 25] Remove deprecated workflow start route.
Evidence: `ideas/evidence/44_evidence_workflow_exec_deprecated_route.sh`

26. [ID 26] Remove deprecated TextChannelController.
Evidence: `ideas/evidence/51_evidence_text_channel_controller_deprecated.sh`

27. [ID 27] Remove deprecated session handler API.
Evidence: `ideas/evidence/52_evidence_session_handler_deprecated.sh`

28. [ID 28] Remove compatibility methods in feedback system.
Evidence: `ideas/evidence/53_evidence_feedback_system_compat.sh`

29. [ID 29] Remove deprecated heuristic or hybrid decision modes in roundabout monitor.
Evidence: `ideas/evidence/54_evidence_roundabout_monitor_deprecated_modes.sh`

30. [ID 30] Remove HEURISTIC or HYBRID decision modes in LLM decider.
Evidence: `ideas/evidence/55_evidence_llm_decider_deprecated_modes.sh`

31. [ID 31] Simplify unified_access legacy behavior.
Evidence: `ideas/evidence/78_evidence_unified_access_legacy.sh`

32. [ID 32] Move or remove legacy ssh_tools from core.
Evidence: `ideas/evidence/77_evidence_ssh_tools_legacy.sh`

33. [ID 33] Update Swarmlet user-agent branding in web_fetch tool.
Evidence: `ideas/evidence/79_evidence_web_fetch_swarmlet_user_agent.sh`

34. [ID 34] Remove legacy workflow trigger upgrade logic in schemas/workflow.py.
Evidence: `ideas/evidence/97_evidence_workflow_schema_legacy_upgrade.sh`

35. [ID 35] Remove deprecated trigger_type field in workflow_schema.py.
Evidence: `ideas/evidence/98_evidence_workflow_schema_deprecated_trigger_type.sh`

36. [ID 36] Tighten trigger_config schema by removing extra allow compatibility.
Evidence: `ideas/evidence/99_evidence_trigger_config_extra_allow.sh`

37. [ID 37] Remove legacy trigger key scanner once legacy shapes dropped.
Evidence: `ideas/evidence/96_evidence_legacy_trigger_check_script.sh`

### Frontend Legacy CSS + Test Signals

38. [ID 38] Remove __APP_READY__ legacy test signal once tests updated.
Evidence: `ideas/evidence/45_evidence_app_ready_legacy_signal.sh`

39. [ID 39] Drop legacy React Flow selectors in CSS after test update.
Evidence: `ideas/evidence/46_evidence_canvas_react_legacy_selectors.sh`

40. [ID 40] Remove legacy buttons.css compatibility layer.
Evidence: `ideas/evidence/47_evidence_buttons_css_legacy.sh`

41. [ID 41] Remove legacy modal pattern CSS.
Evidence: `ideas/evidence/48_evidence_modal_css_legacy.sh`

42. [ID 42] Remove legacy util margin helpers once migrated.
Evidence: `ideas/evidence/49_evidence_util_css_legacy.sh`

43. [ID 43] Remove legacy token aliases after CSS migration.
Evidence: `ideas/evidence/50_evidence_tokens_css_legacy_aliases.sh`

---

## [QA/Test] QA Plan (Virtual QA Team) (2026-02-02)

(Former QA_PLAN.md. Keep this plan updated here only.)

Date: 2026-02-02
Owner: Longhouse (Zerg) core
Scope: OSS-first, SQLite-only, timeline-first product

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
1) OSS onboarding contract still Docker-centric. Vision says SQLite-only + `install.sh` + `longhouse onboard`.
2) Installer + CLI onboarding flows lack robust automated tests across OS targets.
3) Demo DB pipeline is new; no automated validation that demo DB builds and UI uses it.
4) E2E commis/session-continuity failures (timeouts) -> core suite stability risk.
5) Many E2E suites are skipped (LLM streaming, websocket, perf, visual, auth flows).
6) Shipper end-to-end is opt-in and skipped by default; no required CI gate.
7) Runner and commis execution lack full integration tests with real WebSocket channel.
8) Real-time events (SSE/WS) tests are disabled due to flakiness.
9) No formal OS matrix for OSS install (macOS/Linux/WSL).
10) OSS user QA script was missing; now covered by `scripts/qa-oss.sh` (`make qa-oss`).

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
   - /api/system/health
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
- Add installer/CLI tests (install.sh, longhouse onboard, longhouse up).
- Make demo DB build + demo load test part of OSS gate.
- Fix commis/session-continuity E2E timeouts (core suite must be 100% pass).
- Stabilize /api/system/health checks in tests (already in onboarding-sqlite).

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
