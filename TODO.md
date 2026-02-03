# TODO

Capture list for substantial work. Not quick fixes (do those live).

## For Agents

- Each entry is a self-contained handoff — read it, you have context to start
- Size (1-10) indicates scope: 1 = hour, 5 = day, 10 = week+
- Check off subtasks as you go so next agent knows state
- Add notes under tasks if you hit blockers or learn something

---

## Domain Split — Marketing vs Personal Instance (4)

**Goal:** longhouse.ai is marketing-only; david.longhouse.ai is the app (single-tenant).

- [x] Add marketing-only frontend flag (hostname-driven) to disable auth + app routes on longhouse.ai
- [x] Update Coolify domains: zerg-web -> david.longhouse.ai, zerg-api -> api.david.longhouse.ai
- [x] Update zerg-api env: APP_PUBLIC_URL/PUBLIC_SITE_URL to david, CORS to include longhouse.ai + david
- [x] Add Cloudflare DNS for david.longhouse.ai + api.david.longhouse.ai (and optional wildcard)

---

## HN Launch Readiness — Remaining (5)

**Goal:** HN reader can install, see value immediately, understand what problem this solves, and start using it.

### High Priority

- [ ] **Landing page redesign** (4 hours)
  - Current landing tells OLD story ("AI That Knows You", integrations)
  - New story: Timeline + Search + Resume (matches VISION.md User Value Prop)
  - **Guiding principle: Fast to Fun** — install to "oh cool" < 2 minutes

  **Hero Section:**
  - [ ] Headline: "Never lose an AI coding conversation" (or similar)
  - [ ] Subhead: Search across Claude, Codex, Cursor, Gemini / Resume from anywhere
  - [ ] Screenshot: Timeline view with sessions (swap from chat-preview.png)
  - [ ] CTA: `pip install longhouse` prominent

  **How It Works:**
  - [ ] Reframe as: Install → Connect → Search (not Connect apps → AI Learns → Automate)
  - [ ] Keep 3-step simplicity

  **Features:**
  - [ ] Cut or minimize integrations section (wrong story)
  - [ ] Add: Search (FTS5 instant), Resume (commis), Multi-tool (providers)
  - [ ] Position Oikos as power feature, not hero

  **Visuals needed:**
  - [ ] Hero screenshot: Timeline with search bar visible + demo sessions
  - [ ] Provider logos: Claude, Codex, Cursor, Gemini icons
  - [ ] Optional: Search results screenshot, session detail view

  **Files:** `HeroSection.tsx`, `HowItWorksSection.tsx`, `IntegrationsSection.tsx`, `landing.css`

- [ ] **Demo mode flag** (30 min) — infrastructure exists, just needs CLI glue
  - Add `longhouse serve --demo` flag (uses `~/.longhouse/demo.db`, builds if missing)
  - Show banner: "Demo Mode - sample data"
  - **Existing:** `scripts/build_demo_db.py`, `services/demo_sessions.py`, `scenarios/data/swarm-mvp.yaml`
  - File: `apps/zerg/backend/zerg/cli/serve.py`

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
- [ ] Decide remote auth UX for `longhouse connect` (device token vs OAuth vs API key).
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
