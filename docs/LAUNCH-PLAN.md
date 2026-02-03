# Longhouse Launch Plan

**Status:** Draft
**Target:** HN Launch (next week)
**Last Updated:** 2026-02-03

**Decision:** OSS GA is required. Hosted is beta/optional if time allows.

## Current Reality (As Of Today)

### Infrastructure
- **1 backend container** - serves both `api.longhouse.ai` and `api-david.longhouse.ai`
- **1 frontend container** - serves both `longhouse.ai` and `david.longhouse.ai`
- **1 SQLite database** - `/data/longhouse.db` (~2.5GB)
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
- Alternative auth methods (password, magic link)
- `longhouse` CLI package on PyPI

---

## The Two Launch Paths

### Path A: OSS-First Launch
Target: Developers who self-host

**Value prop:** "Your personal AI session archive. pip install, done."

**What we ship:**
1. `pip install longhouse` (or brew)
2. `longhouse serve` → localhost:8080
3. Timeline shows local Claude/Codex sessions
4. No cloud, no account, no Google OAuth

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
- Cross-subdomain token redirect (already implemented)

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
Current: Only Google OAuth or AUTH_DISABLED

**Fix:** Add simple password auth
```python
# New endpoint: POST /api/auth/password
# Env: LONGHOUSE_PASSWORD=xxx
# If set, enables password login
# If not set + localhost, auto-authenticate
```

### 2. CLI Package
Current: No `longhouse` command

**Fix:** Already exists at `cli/serve.py`, needs PyPI publishing
```bash
pip install longhouse
longhouse serve  # Starts server
longhouse ship   # One-time sync
```

### 3. Shipper Bundling
Current: Separate process, unclear install

**Fix:** Bundle shipper in CLI or document clearly

### 4. Landing Page
Current: Talks about "cloud workspace"

**Fix:** Update copy for OSS story
- "Your personal AI session archive"
- "pip install longhouse"
- "Runs locally, your data stays local"

---

## Hosted Beta (Stretch)

### Minimum Viable Control Plane

```
longhouse.ai/signup
  → Google OAuth
  → Create user record (Postgres control plane DB)
  → Stripe checkout ($5/month)
  → Coolify API: provision container
  → DNS: add {username}.longhouse.ai
  → Redirect to user's instance
```

**Components:**
1. Control plane backend (new FastAPI app, tiny)
2. Coolify API integration (provision/destroy)
3. Wildcard DNS (already have *.longhouse.ai)
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
- [ ] Add `LONGHOUSE_PASSWORD` env var
- [ ] Add `POST /api/auth/password` endpoint
- [ ] Auto-skip auth on localhost if no password set
- [ ] Test OSS flow end-to-end

### Day 3: CLI & Packaging (OSS)
- [ ] Verify `longhouse serve` works
- [ ] Bundle frontend dist in package
- [ ] Test `pip install` from local build
- [ ] Write install docs

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

## Post-Launch (If OSS Gets Traction)

1. Build control plane for hosted
2. Per-user container provisioning
3. Stripe billing integration
4. Add Google OAuth for hosted path
5. Cross-subdomain auth (code exists, just needs control plane)

---

## Open Questions

1. **PyPI name:** Is `longhouse` available? (Need to check)
2. **Homebrew:** Worth the effort for launch?
3. **Demo data:** Ship with example sessions or empty?
4. **Shipper:** Bundle in CLI or separate install?

---

## Files to Change

### Auth (Path A)
- `apps/zerg/backend/zerg/routers/auth.py` - Add password endpoint
- `apps/zerg/backend/zerg/config/__init__.py` - Add LONGHOUSE_PASSWORD
- `apps/zerg/frontend-web/src/components/landing/HeroSection.tsx` - Add password login UI

### Landing Page
- `apps/zerg/frontend-web/src/components/landing/HeroSection.tsx` - Update copy
- `apps/zerg/frontend-web/src/components/landing/HowItWorksSection.tsx` - OSS focus
- `apps/zerg/frontend-web/src/components/landing/PricingSection.tsx` - Self-host focus

### CLI
- `apps/zerg/backend/zerg/cli/serve.py` - Verify works
- `apps/zerg/backend/pyproject.toml` - Package config
- `apps/zerg/backend/setup.py` - If needed

### Docs
- `README.md` - Quickstart for OSS
- `docs/self-host.md` - Detailed guide
