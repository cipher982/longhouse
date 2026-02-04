# TODO

Capture list for substantial work. Not quick fixes (do those live).

## For Agents

- Each entry is a self-contained handoff — read it, you have context to start
- Size (1-10) indicates scope: 1 = hour, 5 = day, 10 = week+
- Check off subtasks as you go so next agent knows state
- Add notes under tasks if you hit blockers or learn something

---

## 🎯 HN Launch Priority (This Week)

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

## Post-GA Follow-ups (From 2026-02-03 Swarm)

- [x] Add rate limiting on `POST /auth/password`
- [x] Support `LONGHOUSE_PASSWORD_HASH` (bcrypt/argon2)
- [x] UI fallback if `/auth/methods` fails
- [x] Add `--demo-fresh` flag to rebuild demo DB

---

## ⚠️ Architecture Reality Check (Read First)

**VISION.md describes per-user isolated instances. That doesn't exist YET.**

Current reality (as of 2026-02-03):
- **ONE backend container** serves both `api.longhouse.ai` and `api-david.longhouse.ai`
- **ONE frontend container** serves both `longhouse.ai` and `david.longhouse.ai`
- **ONE SQLite database** at `/data/longhouse.db` (size varies; check on server)
- **No control plane** — can't provision per-user instances
- **"david.longhouse.ai" is cosmetic** — just DNS routing to shared infra

**Target state:** Control plane provisions isolated containers per user (Docker API + Traefik labels). See VISION.md for architecture.

See `/docs/LAUNCH.md` for detailed analysis.

---

## 🚨 OSS Auth — Password Login for Self-Hosters (3)

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
- [x] Test full OSS flow: `pip install` → `longhouse serve` → password login → timeline

**Files:** `config/__init__.py`, `routers/auth.py`, `HeroSection.tsx`, `config.ts`

---

## Domain Split — Marketing vs Personal Instance (4)

**Goal:** longhouse.ai is marketing-only; david.longhouse.ai is the app (single-tenant).

**Status:** DNS routing complete. Marketing mode logic exists but has issues.

- [x] Add marketing-only frontend flag (hostname-driven) to disable auth + app routes on longhouse.ai
- [x] Update Coolify domains: zerg-web -> david.longhouse.ai, zerg-api -> api-david.longhouse.ai
- [x] Update zerg-api env: APP_PUBLIC_URL/PUBLIC_SITE_URL to david, CORS to include longhouse.ai + david
- [x] Add Cloudflare DNS for david.longhouse.ai + api-david.longhouse.ai (and optional wildcard)

**Reality check:** This is DNS routing to ONE shared deployment, not isolated instances. The "david" subdomain is cosmetic. See Architecture Reality Check above.

**Remaining issues:**
- [ ] Google OAuth needs both `longhouse.ai` AND `david.longhouse.ai` in authorized origins (add to Google Console)
- [ ] Cross-subdomain OAuth code exists (`/auth/accept-token`) but targets non-existent per-user architecture
- [ ] Marketing mode defaults were removed (broke auth) — needs cleaner hostname detection

---

## Landing Page Redesign — Full (6)

**Goal:** Clear user paths, visible CTAs, better contrast. Visitor instantly understands: what it is, who it's for, how to get started.

**⚠️ DEPENDS ON LAUNCH DECISION:**
- **OSS GA (current):** Hero should emphasize `pip install`, self-host, "your data stays local"
- **Hosted Beta:** Secondary CTA or "Join waitlist" copy

Current copy is a mix of both stories. Align to OSS-first primary.

**Problems identified (2026-02-03):**
1. Sign-in button is ghost variant, bottom of hero — hard to see, weird position
2. Colors too dark — low contrast text, cards blend into background
3. No clear user path differentiation (self-hosted vs cloud vs paid)
4. No sticky header — can't navigate or sign in without scrolling up
5. Current story (AI That Knows You, integrations) is OLD — new story is Timeline + Search + Resume
6. **NEW:** Several CTA buttons don't work or lead to broken flows
7. **NEW:** Google OAuth only works on domains registered in Console (blocking david.longhouse.ai)

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

## HN Launch Readiness — Remaining (4)

**Goal:** HN reader can install, see value immediately, understand what problem this solves, and start using it.

**Launch Path Decision:** OSS GA + Hosted Beta (optional). See `/docs/LAUNCH.md`.

### 🚨 Critical Blockers (Fix First)

- [ ] **OSS Auth** — Password login for self-hosters (see dedicated section above)
- [ ] **Google Console** — Add `david.longhouse.ai` to authorized origins (immediate workaround)
- [ ] **Landing page CTAs** — Several buttons don't work or lead nowhere

### High Priority

- [ ] **Demo mode flag** (30 min) — infrastructure exists, just needs CLI glue
  - Add `longhouse serve --demo` flag (uses `~/.longhouse/demo.db`, builds if missing)
  - Show banner: "Demo Mode - sample data"
  - **Existing:** `scripts/build_demo_db.py`, `services/demo_sessions.py`, `scenarios/data/swarm-mvp.yaml`
  - File: `apps/zerg/backend/zerg/cli/serve.py`
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

## Control Plane — Hosted Beta (8)

**What it enables:** Users sign up at longhouse.ai → get their own instance (alice.longhouse.ai)

**Architecture:** Tiny FastAPI app that handles signup/billing/provisioning. Uses Docker API directly (not Coolify).

**Scope:** Only if P0 OSS GA is complete.

### Phase 1: Scaffold + Auth (2)

- [ ] Create `apps/control-plane/` directory structure
  ```
  apps/control-plane/
  ├── main.py           # FastAPI app
  ├── config.py         # Settings (Stripe keys, Docker host, etc.)
  ├── models.py         # SQLAlchemy models (User, Instance)
  ├── routers/
  │   ├── auth.py       # Google OAuth
  │   ├── billing.py    # Stripe checkout/webhooks
  │   └── instances.py  # Provision/deprovision
  └── services/
      ├── provisioner.py  # Docker API client
      └── stripe_service.py
  ```
- [ ] Add Google OAuth (control plane only, not per-instance)
- [ ] Add User model: email, stripe_customer_id, instance_id, subscription_status
- [ ] Add Instance model: user_id, container_name, subdomain, state, created_at

### Phase 2: Stripe Integration (3)

- [ ] Add `POST /checkout` → create Stripe checkout session
- [ ] Add `POST /webhooks/stripe` → handle payment events
- [ ] On `invoice.paid` → trigger provisioning
- [ ] On `customer.subscription.deleted` → trigger deprovisioning
- [ ] Add billing portal link (`POST /billing/portal`)

### Phase 3: Docker Provisioning (3)

- [ ] Implement Docker API client (SSH to zerg server or Docker socket)
- [ ] Provision container with Traefik labels for subdomain routing
  ```python
  docker run -d \
    --name longhouse-{user} \
    --label traefik.enable=true \
    --label "traefik.http.routers.{user}.rule=Host(`{user}.longhouse.ai`)" \
    -v /data/longhouse-{user}:/data \
    -e INSTANCE_ID={user} \
    -e SINGLE_TENANT=1 \
    ghcr.io/cipher982/longhouse:latest
  ```
- [ ] Create SQLite volume per user
- [ ] Implement deprovision (stop + remove container, archive data)
- [ ] Add health check polling after provision

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

**Files:** New `apps/control-plane/` directory

**Infra requirements:**
- Traefik on zerg server (for subdomain routing)
- Wildcard DNS `*.longhouse.ai` (already configured)
- Docker socket access from control plane
- Postgres for control plane DB (can be existing Coolify-managed instance)

---

## Public Launch Checklist (6)

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

## README Test CI (Readme-Contract) (5)

Automate README command verification with explicit, opt-in contracts. Use cube ARC runners where possible.

- [ ] Define `readme-test` JSON block spec (steps, workdir, env, mode, timeout, cleanup).
- [ ] Implement `scripts/run-readme-tests.sh` (extract + run in temp clone, fail fast, save logs).
- [ ] Add `make test-readmes` target (smoke vs full mode flags).
- [ ] Add GitHub Actions workflow using `runs-on: cube` for PR smoke and nightly full.
- [ ] Add `readme-test` blocks to root README + runner/sauron/hatch-agent READMEs.
- [ ] Optional: failure triage via `hatch` agent (summarize logs, suggest fix).

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

## OSS First-Run UX Polish (5)

Eliminate the "empty timeline" anticlimactic moment and improve discovery for users without Claude Code.

- [ ] Seed demo session data on first `longhouse onboard` run (shows what the timeline looks like)
- [ ] Improve "No Claude Code" guidance in onboard wizard (link to alternatives, explain what to do next)
- [ ] Consider demo mode flag for `longhouse serve --demo` (starts with pre-loaded sessions for exploration)

---

## OSS Packaging Decisions (3)

Close the remaining open questions from VISION.md.

- [ ] Decide whether the shipper is bundled with the CLI or shipped as a separate package.
- [x] Decide remote auth UX for `longhouse connect` (device token vs OAuth vs API key).
  - **Decision:** Password auth via `LONGHOUSE_PASSWORD` env var (see OSS Auth section)
  - Google OAuth is fallback for users who want it
  - Device token / API key deferred to post-launch
- [ ] Decide HTTPS story for local OSS (`longhouse serve`) — built-in vs reverse proxy guidance.
- [ ] Capture current frontend bundle size and set a target budget.

---

## Longhouse Rebrand — Product/Meta Strings (6)

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

## Longhouse Rebrand — CLI / Packages / Images (7)

Package and binary naming so OSS users see Longhouse everywhere.

- [ ] Decide npm scope/name for runner: `@longhouse/runner` or `longhouse-runner`
- [ ] Update docker image name for docs/examples (ghcr.io/.../longhouse)
- [ ] Update installer scripts to new names (12 refs across 4 scripts)

---

## Prompt Cache Optimization (5)

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

## Session Discovery — FTS5 Search + Oikos Tools (6)

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

## UI QA Screenshot Capture System (4)

**Goal:** Flexible, low-friction screenshot capture for agents + humans; clear instructions; minimal token cost.

- [ ] Inventory current screenshot/Playwright flows and pain points
- [ ] Prototype a simple capture CLI/API (local dev + headless) and document usage
- [ ] Add agent-friendly capture path (MCP/tool or skill) with stable output paths
- [ ] Add docs + examples; ensure instructions are short and reproducible
- [x] Fix ui-capture a11y snapshot: Playwright 1.57 has no `page.accessibility`; use `locator.ariaSnapshot()` or guard missing API and still write trace/manifest on partial failure
- [ ] Add SCENE=empty reset endpoint (or CLI) to clear sessions; update docs to note current no-op until available
