# Launch Plan (OSS GA + HN)

**Status:** Draft
**Target:** HN Launch (next week)
**Last Updated:** 2026-02-04

**Doc scope:**
- Strategy lives in `VISION.md`
- Execution tracking lives in `TODO.md`
- User onboarding lives in `README.md`
- This file is the short-lived launch checklist + HN draft

---

## Current Reality (As Of Today)

### Infrastructure
- **1 backend container** - serves both `api.longhouse.ai` and `api-david.longhouse.ai`
- **1 frontend container** - serves both `longhouse.ai` and `david.longhouse.ai`
- **1 SQLite database** - `/data/longhouse.db` (size varies; check on server)
- **Server:** `zerg` VPS (Coolify-managed)

### What Exists
- Timeline UI (session archive viewer)
- Session sync (shipper → ingest)
- Oikos chat interface
- Commis (background agents)
- Google OAuth (single provider)

### What Does NOT Exist
- Control plane (signup → provision → route)
- Per-user instance isolation
- Magic link auth

---

## The Two Launch Paths

### Path A: OSS-First Launch
Target: Developers who self-host

**Value prop:** "Your personal AI session archive. pip install, done."

**What we ship:**
1. `pip install longhouse`
2. `longhouse serve` → localhost:8080
3. Timeline shows local Claude Code sessions
4. No cloud, no account required

**Auth strategy:**
- Default: `AUTH_DISABLED=1` for localhost
- Optional: Simple password (`LONGHOUSE_PASSWORD=xxx`)

**What we DON'T ship:**
- Hosted instances
- User subdomains
- Cloud sync

### Path B: Hosted-First Launch
Target: Users who want cloud convenience

**Value prop:** "Close laptop, keep coding. $5/month."

**What we ship:**
1. Sign up at longhouse.ai
2. Get your instance (david.longhouse.ai)
3. Install shipper, sessions sync
4. Access from any device

**Auth strategy:**
- Google OAuth on main domain
- Cross-subdomain token redirect (code exists)

**What we need to build:**
- Control plane (signup, Stripe, provision via Docker API)
- Per-user container provisioning
- OR: Accept multi-tenant single instance for launch

---

## Recommended: OSS GA + Hosted Beta (Optional)

**Why:**
1. OSS GA is achievable in a week and matches the HN story
2. Hosted requires new infrastructure (control plane + provisioning)
3. Beta hosted can exist as a stretch without blocking OSS launch

**P0 Blockers to fix (OSS GA):**

### 1. Auth for OSS Self-Hosters
Current: Google OAuth or AUTH_DISABLED

**Fix:** Password auth exists, but prod validation still requires Google OAuth when AUTH_DISABLED=0. This blocks password-only OSS setups.

### 2. CLI Package Release
Current: PyPI package exists but lags repo; ensure release matches current code.

### 3. Shipper Bundling
Current: Separate process, unclear install

**Fix:** Bundle shipper in CLI or document clearly

### 4. Landing Page
Current: Talks about "cloud workspace"

**Fix:** Update copy for OSS story
- "Your personal AI session archive"
- "pip install longhouse"
- "Runs locally, your data stays local"

### 5. Installer URL
Current: `longhouse.ai/install.sh` serves the SPA

**Fix:** Make `get.longhouse.ai/install.sh` the canonical installer URL everywhere

---

## Hosted Beta (Stretch)

### Minimum Viable Control Plane

```
longhouse.ai/signup
  → Google OAuth
  → Create user record (Postgres control plane DB)
  → Stripe checkout ($5/month)
  → Docker API: provision container
  → DNS: add {username}.longhouse.ai
  → Redirect to user's instance
```

**Components:**
1. Control plane backend (new FastAPI app, tiny)
2. Docker API integration (no Coolify provisioning)
3. Wildcard DNS (not configured yet)
4. Stripe integration

**Timeline:** 1-2 weeks additional work

### Alternative: Multi-Tenant Single Instance

Keep current single deployment, add multi-user support:
- Remove SINGLE_TENANT enforcement
- Add owner_id to all queries
- Users share one instance

**Pros:** No infra work
**Cons:** No isolation, harder to scale, not aligned with VISION

---

## Immediate Actions (This Week)

### Day 1-2: Fix Auth (OSS)
- [x] Add `LONGHOUSE_PASSWORD` env var
- [x] Add `POST /api/auth/password` endpoint
- [x] Auto-skip auth on localhost if no password set
- [ ] Allow password-only auth in prod without requiring Google OAuth

### Day 3: CLI & Packaging (OSS)
- [ ] Verify `longhouse serve` works end-to-end from PyPI build
- [ ] Bundle frontend dist in package
- [ ] Publish PyPI release matching repo
- [ ] Write install docs (include canonical installer URL)

### Day 4: Landing Page (OSS)
- [ ] Update hero copy for OSS
- [ ] Add "pip install" as primary CTA
- [ ] Remove/hide hosted-specific features
- [ ] Add self-host docs link

### Day 5: Testing & Polish (OSS)
- [ ] Fresh machine test (pip install → working UI)
- [ ] Write quickstart guide
- [ ] Record demo GIF
- [ ] Prepare HN post

---

## HN Draft (Updated)

### Title Options
1. **Show HN: Longhouse – Search your Claude Code sessions**
2. **Show HN: Never lose a Claude Code conversation again**
3. **Show HN: Longhouse – A local timeline for AI coding sessions**

### Launch Comment (First Comment)

```
Hey HN! I built Longhouse to solve a problem I kept hitting: losing track of AI coding conversations.

THE PROBLEM:
Claude Code sessions live in JSONL files under ~/.claude/projects/*. When I need to find "that conversation from last week where the AI fixed my auth bug," I end up grepping huge files or redoing the work.

THE SOLUTION:
Longhouse watches those session files and unifies them into a single, searchable timeline. Now I can:
- Search by keyword/project/date
- See tools and outcomes in context
- Keep a long-term archive of what I actually tried

CURRENT STATE:
- ✅ Works with Claude Code (real-time syncing)
- 🚧 Codex/Cursor/Gemini support planned
- ✅ Local-first (your data stays on your machine by default)
- ✅ Demo mode available: `longhouse serve --demo`

TECH:
- Python 3.12+ backend (FastAPI, SQLAlchemy)
- SQLite for local-first storage
- React frontend
- CLI for session syncing

Try it:
  pip install longhouse
  longhouse serve

Repo is public and Apache-2.0 licensed:
https://github.com/cipher982/longhouse

Feedback welcome! Especially:
- Which AI tools should I support next?
- What makes this indispensable for you?
- Any bugs or rough edges you hit
```

### Timing Recommendations
- Best days: Tuesday–Thursday
- Best times: 8–10am PT

### Anticipated Questions & Answers

**Q: Why not just use Claude Code history?**
A: Claude Code history is Claude-only, not cross-session searchable, and not built for long-term archive/querying.

**Q: What about Cursor/Codex?**
A: Planned. The ingest pipeline is provider-agnostic, but only the Claude parser is done today.

**Q: Privacy concerns?**
A: Local-first by default. Your data stays on your machine unless you explicitly configure remote sync.

**Q: Performance with lots of sessions?**
A: SQLite handles thousands of rows well; the timeline is indexed and snappy in practice.

### Pre-Launch Checklist
- [ ] README has at least one screenshot
- [ ] Demo data works via `longhouse serve --demo`
- [ ] PyPI package works on a clean machine
- [ ] Installer URL is correct everywhere
- [ ] Production is healthy (check before post)
- [ ] Clear next steps in README

### Fallback Plans
- Post to /r/programming
- Tweet with demo video
- Product Hunt launch
- Reach out to AI tool communities directly
