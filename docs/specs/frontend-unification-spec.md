# Frontend Unification Specification

**Version:** 1.0
**Date:** December 2024
**Status:** In Progress
**Depends on:** super-siri-architecture.md, supervisor-ui-spec.md

---

## 1. Executive Summary

This specification defines the unification of the Jarvis and Zerg frontends into a single-origin web application. The approach uses nginx proxy-layer routing rather than codebase merging, providing same-origin benefits while minimizing risk.

### Problem Statement

Currently, Jarvis and Zerg are served on separate ports:

- Port 30080: Jarvis chat UI
- Port 30081: Zerg dashboard

This separation causes:

- Cross-origin API calls requiring CORS configuration
- Two entry points confusing for users
- SSE reconnection complexity across origins
- Duplicate assets and branding inconsistency
- Auth context separation

### Solution

Unify at the nginx proxy layer with path-based routing:

- `/` → Marketing/landing pages (Zerg frontend)
- `/chat` → Jarvis voice/text UI
- `/dashboard` → Zerg debug/config views
- `/api/*` → Backend API (same-origin)

### Benefits

1. **Same-origin** - No CORS, simpler SSE, shared cookies
2. **Single entry point** - One URL for users
3. **Keep both codebases** - Low risk, no React port needed
4. **Rollback easy** - Just change nginx routes
5. **Auth unification path** - Share cookies on same domain

---

## 2. Background

### 2.1 Current Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    docker-compose.unified.yml (5 containers)                 │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                      reverse-proxy (nginx)                           │   │
│  │  ┌─────────────────────┐     ┌─────────────────────────────────┐   │   │
│  │  │  Port 80 (→30080)   │     │     Port 81 (→30081)            │   │   │
│  │  │  Jarvis Entry       │     │     Zerg Dashboard Entry        │   │   │
│  │  └──────────┬──────────┘     └──────────────┬──────────────────┘   │   │
│  └─────────────┼───────────────────────────────┼───────────────────────┘   │
│                │                               │                            │
│    ┌───────────┴───────────┐       ┌──────────┴──────────┐                │
│    ▼                       ▼       ▼                      ▼                │
│  ┌──────────────┐  ┌──────────────┐ ┌──────────────┐  ┌──────────────┐    │
│  │ jarvis-web   │  │jarvis-server │ │zerg-frontend │  │ zerg-backend │    │
│  │  (Vite)      │  │  (Node)      │ │   (Vite)     │  │  (FastAPI)   │    │
│  │  Port 8080   │  │  Port 8787   │ │  Port 5173   │  │  Port 8000   │    │
│  │  Vanilla TS  │  │  OpenAI RT   │ │  React       │  │  Python      │    │
│  └──────────────┘  └──────────────┘ └──────────────┘  └──────────────┘    │
│                                                              │              │
│                                                              ▼              │
│                                                        ┌──────────┐        │
│                                                        │ postgres │        │
│                                                        └──────────┘        │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 2.2 Target Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    docker-compose.unified.yml (5 containers)                 │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                      reverse-proxy (nginx)                           │   │
│  │                      Port 80 (→30080) SINGLE ENTRY                   │   │
│  │                                                                       │   │
│  │  Route              │  Backend           │  Purpose                  │   │
│  │  ───────────────────┼────────────────────┼─────────────────────────  │   │
│  │  /                  │  zerg-frontend     │  Marketing/landing        │   │
│  │  /chat              │  jarvis-web        │  Voice/text chat UI       │   │
│  │  /dashboard         │  zerg-frontend     │  Debug/config dashboard   │   │
│  │  /agent/*           │  zerg-frontend     │  Agent thread views       │   │
│  │  /api/session,tool  │  jarvis-server     │  OpenAI Realtime bridge   │   │
│  │  /api/*             │  zerg-backend      │  All other API            │   │
│  │  /ws, /api/ws       │  zerg-backend      │  WebSocket                │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
│    ┌──────────────┐  ┌──────────────┐ ┌──────────────┐  ┌──────────────┐   │
│    │ jarvis-web   │  │jarvis-server │ │zerg-frontend │  │ zerg-backend │   │
│    │  Port 8080   │  │  Port 8787   │ │  Port 5173   │  │  Port 8000   │   │
│    └──────────────┘  └──────────────┘ └──────────────┘  └──────────────┘   │
│                                                              │              │
│                                                              ▼              │
│                                                        ┌──────────┐        │
│                                                        │ postgres │        │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 2.3 Technology Stack

| Component     | Jarvis Web             | Zerg Frontend            |
| ------------- | ---------------------- | ------------------------ |
| **Framework** | Vanilla TypeScript     | React 18                 |
| **State**     | Custom stateManager    | TanStack Query           |
| **Routing**   | None (SPA single page) | React Router DOM         |
| **Build**     | Vite                   | Vite + React plugin      |
| **Styles**    | 9 CSS files            | CSS + design tokens      |
| **Auth**      | Device secret → cookie | Google OAuth (AuthGuard) |

### 2.4 Why Proxy Unification (Not Codebase Merge)

| Approach              | Pros                            | Cons                         |
| --------------------- | ------------------------------- | ---------------------------- |
| **Proxy Unification** | Low risk, quick, rollback easy  | Two codebases remain         |
| **Codebase Merge**    | Single build, shared components | High risk, React port needed |

**Decision:** Proxy unification first. Codebase merge only if needed later.

---

## 3. Auth Contract

### 3.1 Unified Cookie Specification

| Property | Value                  |
| -------- | ---------------------- |
| Name     | `swarm_session`        |
| HttpOnly | `true`                 |
| SameSite | `Lax`                  |
| Secure   | `true` (prod only)     |
| Path     | `/`                    |
| Max-Age  | `43200` (12 hours)     |
| Domain   | Same domain (implicit) |

### 3.2 JWT Payload

```json
{
  "sub": "<user_id>",
  "iss": "device" | "google",
  "iat": 1733500000,
  "exp": 1733543200
}
```

### 3.3 Issuance Flows

**Dev Mode (Current):**

```
POST /api/jarvis/auth
Body: { "device_secret": "<secret>" }
Response: Set-Cookie: swarm_session=<jwt>; HttpOnly; SameSite=Lax; Path=/
```

**Prod Mode (Future):**

```
GET /api/auth/google/callback?code=<oauth_code>
Response: Set-Cookie: swarm_session=<jwt>; HttpOnly; Secure; SameSite=Lax; Path=/
```

### 3.4 Validation Contract

Both `jarvis-server` and `zerg-backend` MUST:

1. Read `swarm_session` cookie from request
2. Verify JWT signature against shared secret
3. Check `exp` claim not passed
4. Extract `sub` as authenticated user ID
5. Reject requests with missing/invalid cookie (except public routes)

### 3.5 Security Requirements

- Device secret scoped to allowed domain only
- Rotate device secret periodically
- Session max-age: 12 hours
- No refresh tokens in browser (server-side only for OAuth)
- CSRF: SameSite=Lax provides protection for state-changing requests

---

## 4. Implementation Phases

### Phase 0: Auth Contract Documentation ✅ COMPLETE

**Duration:** 30 minutes
**Status:** Complete

**Deliverables:**

- [x] `docs/specs/auth-contract.md` created
- [x] Cookie spec documented
- [x] JWT payload documented
- [x] Validation contract documented

---

### Phase 1: Nginx Proxy Unification ✅ COMPLETE

**Duration:** 1-2 hours
**Status:** Complete

**Tasks:**

#### 1.1 Update Nginx Config

**File:** `docker/nginx/docker-compose.unified.conf`

- [x] Single server block on port 80
- [x] Path-based routing to appropriate backends
- [x] SSE endpoints with `proxy_buffering off` and long timeouts
- [x] WebSocket endpoints with upgrade headers
- [x] Legacy port 81 block commented for rollback

#### 1.2 Update Docker Compose

**File:** `docker/docker-compose.unified.yml`

- [x] Remove port 81 from reverse-proxy
- [x] Add `JARVIS_LEGACY` environment variable

#### 1.3 Update Dev Script

**File:** `scripts/dev-docker.sh`

- [x] Update success message with unified URLs

#### 1.4 Update Jarvis Base Path

**File:** `apps/jarvis/apps/web/vite.config.ts`

- [x] Add `base: '/chat/'`

#### 1.5 Update Jarvis API Calls

**Files:** `lib/config.ts`, `main.ts`, `lib/task-inbox-integration-example.ts`

- [x] Change API URL from `VITE_ZERG_API_URL` to `/api` (relative)

---

### Phase 2: Favicon and Branding ✅ COMPLETE

**Duration:** 30 minutes
**Status:** Complete

**Tasks:**

#### 2.1 Create Public Directory

- [x] `apps/jarvis/apps/web/public/` created

#### 2.2 Copy Favicon Assets

- [x] favicon.ico
- [x] favicon-16.png, favicon-32.png, favicon-512.png
- [x] apple-touch-icon.png
- [x] maskable-icon-192.png, maskable-icon-512.png
- [x] og-image.png

#### 2.3 Create Web Manifest

**File:** `apps/jarvis/apps/web/public/site.webmanifest`

- [x] PWA manifest with `/chat/` paths

#### 2.4 Update index.html

**File:** `apps/jarvis/apps/web/index.html`

- [x] Favicon links with `/chat/` base
- [x] PWA manifest link
- [x] Theme color (#0a0a0f)
- [x] SEO/OG meta tags
- [x] Umami analytics

#### 2.5 Delete Old Icons

- [x] Removed icon-192.png, icon-512.png

---

### Phase 3: SSE and API Cleanup ✅ COMPLETE

**Duration:** 1-2 hours
**Status:** Complete

**Tasks:**

#### 3.1 Tighten CORS Configuration ✅

**File:** `apps/zerg/backend/zerg/main.py`

- [x] Restrict `allow_origins` from wildcard "\*" to explicit localhost origins
- [x] Dev mode: Allow localhost:30080, 8080, 5173
- [x] Prod mode: Default to localhost:30080 (same-origin)
- [x] Keep CORS middleware but restrict it
- [x] `allow_credentials=False` already set (same-origin doesn't need it)

#### 3.2 Backend Health Ping (Skipped)

**Decision:** Skipped as unnecessary complexity.

- Backend `/health` endpoint exists but not needed for startup validation
- Nginx `/health` endpoint provides infrastructure health check
- SSE and API routes are tested via actual usage

#### 3.3 Verify SSE Configuration ✅

- [x] SSE endpoint uses `EventSourceResponse` (sets correct headers automatically)
- [x] Nginx config has `proxy_buffering off` for `/api/jarvis/supervisor/events`
- [x] Verified SSE endpoint returns proper responses (404 for invalid run_id)
- [x] No CORS errors - same-origin requests work correctly

#### 3.4 Verify FastAPI SSE Endpoint Headers ✅

**File:** `apps/zerg/backend/zerg/routers/jarvis.py`

- [x] Uses `sse-starlette` `EventSourceResponse` - headers set automatically
- [x] `Cache-Control: no-cache` - set by EventSourceResponse
- [x] `Connection: keep-alive` - set by EventSourceResponse
- [x] Nginx `proxy_buffering off` configured in Phase 1

---

### Phase 4: Navigation Integration ✅ COMPLETE

**Duration:** 1-2 hours
**Status:** Complete

**Tasks:**

#### 4.1 Add Header Navigation to Zerg Frontend

**File:** `apps/zerg/frontend-web/src/components/Layout.tsx`

- [x] Add "Chat" link pointing to `/chat`
- [x] Style consistently with existing nav (anchor styled as tab-button)

#### 4.2 Add Header Navigation to Jarvis

**File:** `apps/jarvis/apps/web/index.html`

- [x] Add "Dashboard" link pointing to `/dashboard`
- [x] Style consistently with Jarvis UI (header-button with icon)

#### 4.3 Update Landing Page

**File:** `apps/zerg/frontend-web/src/pages/LandingPage.tsx`

- [ ] Add prominent "Try Chat" CTA linking to `/chat` (deferred - nav tabs sufficient)
- [x] Header has Chat tab visible on all pages

---

### Phase 5: Testing ⏳ IN PROGRESS

**Duration:** 1 day
**Status:** Playwright smoke tests created, manual tests pending

**Tasks:**

#### 5.1 Manual Smoke Tests

- [ ] `http://localhost:30080/` → Zerg landing page
- [ ] `http://localhost:30080/chat` → Jarvis chat UI with purple robot favicon
- [ ] `http://localhost:30080/dashboard` → Zerg dashboard
- [ ] `http://localhost:30080/agent/1` → Agent chat page
- [ ] `http://localhost:30080/api/health` → Backend health response

#### 5.2 Jarvis Feature Parity Tests

- [ ] Voice PTT mode works
- [ ] Hands-free mode works
- [ ] Text input works
- [ ] Conversations persist (IndexedDB)
- [ ] Supervisor delegation triggers
- [ ] SSE events stream and display
- [ ] Task inbox updates
- [ ] Audio playback works
- [ ] Reconnection after disconnect

#### 5.3 Zerg Feature Parity Tests

- [ ] Dashboard loads agents
- [ ] Agent chat works
- [ ] Thread history displays
- [ ] WebSocket connection works
- [ ] OAuth flow works (if enabled)

#### 5.4 Playwright Smoke Tests ✅ CREATED

**File:** `apps/zerg/e2e/tests/unified-frontend.spec.ts`

Tests implemented:

- Landing page loads at /
- Chat page loads at /chat with PTT button
- Dashboard page loads at /dashboard
- Chat tab visible in Zerg dashboard nav
- Dashboard link visible in Jarvis header
- Navigation from dashboard → chat works
- Navigation from chat → dashboard works
- API health check via unified proxy

#### 5.5 Auth Contract Test

**File:** `apps/zerg/backend/tests/test_auth_contract.py` (new)

```python
# Tests to implement:
def test_jarvis_server_accepts_unified_cookie():
    """jarvis-server should accept swarm_session cookie"""
    pass

def test_zerg_backend_accepts_unified_cookie():
    """zerg-backend should accept swarm_session cookie"""
    pass

def test_rejects_missing_cookie():
    """Protected routes should reject missing cookie"""
    pass

def test_rejects_foreign_origin():
    """Should reject requests from unauthorized origins"""
    pass
```

---

### Phase 6: Rollback Testing ⏳ TODO

**Duration:** 1 hour
**Status:** Not started

**Tasks:**

#### 6.1 Test Rollback Procedure

- [ ] Set `JARVIS_LEGACY=1` in `.env`
- [ ] Uncomment port 81 block in nginx config
- [ ] Restart: `docker compose restart reverse-proxy`
- [ ] Verify old behavior restored (port 30081 works)

#### 6.2 Document Rollback Steps

- [ ] Quick rollback (nginx only)
- [ ] Full rollback (git checkout)
- [ ] Time estimates for each

---

### Phase 7: Cleanup and Documentation ⏳ TODO

**Duration:** 2 hours
**Status:** Not started

**Tasks:**

#### 7.1 Update AGENTS.md

- [ ] Update architecture diagram with unified frontend
- [ ] Update port references

#### 7.2 Update README

- [ ] Update "getting started" URLs
- [ ] Document new URL structure

#### 7.3 Remove Legacy Code

- [ ] Remove `ZGPXY_PORT` from scripts (after stable period)
- [ ] Remove commented nginx blocks (after stable period)
- [ ] Clean up unused env vars

#### 7.4 Update Specs

- [ ] Update `super-siri-architecture.md` with unified frontend
- [ ] Update `supervisor-ui-spec.md` URL references

---

## 5. URL Structure Reference

### Final URL Mapping

| URL                      | Backend       | Purpose                  |
| ------------------------ | ------------- | ------------------------ |
| `/`                      | zerg-frontend | Landing page, marketing  |
| `/pricing`               | zerg-frontend | Pricing page             |
| `/docs`                  | zerg-frontend | Documentation            |
| `/changelog`             | zerg-frontend | Changelog                |
| `/privacy`               | zerg-frontend | Privacy policy           |
| `/security`              | zerg-frontend | Security page            |
| `/chat`                  | jarvis-web    | Voice/text chat UI       |
| `/chat/*`                | jarvis-web    | Chat sub-routes (if any) |
| `/dashboard`             | zerg-frontend | Agent dashboard          |
| `/agent/:id/thread/:id?` | zerg-frontend | Agent chat thread        |
| `/canvas`                | zerg-frontend | Visual canvas            |
| `/profile`               | zerg-frontend | User profile             |
| `/settings/*`            | zerg-frontend | Settings pages           |
| `/admin`                 | zerg-frontend | Admin panel              |
| `/api/session`           | jarvis-server | OpenAI Realtime session  |
| `/api/tool`              | jarvis-server | MCP tool execution       |
| `/api/sync/*`            | jarvis-server | Conversation sync        |
| `/api/jarvis/*`          | zerg-backend  | Supervisor API           |
| `/api/*`                 | zerg-backend  | All other API            |
| `/ws`, `/api/ws`         | zerg-backend  | WebSocket                |

---

## 6. Rollback Procedure

### 6.1 Quick Rollback (Nginx Only)

**Time:** 2 minutes

1. Edit `docker/nginx/docker-compose.unified.conf`
2. Uncomment the legacy port 81 server block at the bottom
3. Edit `docker/docker-compose.unified.yml`
4. Add back `- "${ZGPXY_PORT}:81"` to reverse-proxy ports
5. Restart nginx: `docker compose -f docker/docker-compose.unified.yml restart reverse-proxy`
6. Verify: http://localhost:30081 serves Zerg dashboard

### 6.2 Full Rollback (Git)

**Time:** 5 minutes

```bash
git checkout docker/nginx/docker-compose.unified.conf
git checkout docker/docker-compose.unified.yml
git checkout scripts/dev-docker.sh
git checkout apps/jarvis/apps/web/
make stop && make dev
```

### 6.3 Rollback Toggle

Set in `.env`:

```
JARVIS_LEGACY=1
```

Then restart. This enables the legacy nginx config if uncommented.

---

## 7. Success Criteria

### 7.1 Functional Requirements

- [ ] All existing Jarvis features work at `/chat`
- [ ] All existing Zerg features work at `/dashboard`
- [ ] SSE events stream correctly (supervisor progress)
- [ ] WebSocket connections work
- [ ] Auth works (device secret or OAuth)
- [ ] No CORS errors in browser console

### 7.2 Non-Functional Requirements

- [ ] Single port entry (30080 only)
- [ ] Favicon consistent across all pages
- [ ] Page load time not degraded
- [ ] Rollback tested and documented

### 7.3 Testing Requirements

- [ ] Manual smoke tests pass
- [ ] Feature parity tests pass
- [ ] Playwright smoke tests pass
- [ ] Auth contract tests pass
- [ ] Rollback procedure tested

---

## 8. Risk Mitigation

| Risk                 | Mitigation                                  |
| -------------------- | ------------------------------------------- |
| Routing breaks       | Keep legacy nginx block, test rollback      |
| SSE fails            | Long timeouts, proxy_buffering off          |
| Auth confusion       | Document contract, single cookie format     |
| Feature regression   | Explicit parity checklist                   |
| Bundle size increase | Defer - not applicable to proxy unification |

---

## 9. Future Considerations

### 9.1 React Port (If Needed Later)

If shared components or single bundle becomes important:

1. Port Jarvis chat UI to React component
2. Add as route in Zerg frontend
3. Remove jarvis-web container
4. Update nginx config

**Estimated effort:** 2-3 days
**Trigger:** Need for shared state/components between chat and dashboard

### 9.2 OAuth Implementation

When ready for multi-user:

1. Implement Google OAuth callback in zerg-backend
2. Issue same `swarm_session` cookie format
3. Update jarvis-server to validate OAuth-issued tokens
4. Remove device-secret flow (or keep as fallback)

### 9.3 Production Deployment

Additional considerations for production:

- HTTPS termination (Cloudflare or nginx)
- `Secure` flag on cookies
- Rate limiting
- Error pages (502, 503)
- Health check endpoints for orchestrator

---

## 10. Appendix

### A. File Change Summary

**Created:**

- `docs/specs/auth-contract.md`
- `docs/specs/frontend-unification-spec.md` (this file)
- `docs/specs/frontend-unification-implementation.md`
- `apps/jarvis/apps/web/public/` (directory + 9 assets)

**Modified:**

- `docker/nginx/docker-compose.unified.conf`
- `docker/docker-compose.unified.yml`
- `scripts/dev-docker.sh`
- `apps/jarvis/apps/web/vite.config.ts`
- `apps/jarvis/apps/web/index.html`
- `apps/jarvis/apps/web/lib/config.ts`
- `apps/jarvis/apps/web/main.ts`

**Deleted:**

- `apps/jarvis/apps/web/icon-192.png`
- `apps/jarvis/apps/web/icon-512.png`

### B. Related Documents

- `docs/specs/super-siri-architecture.md` - Overall system architecture
- `docs/specs/supervisor-ui-spec.md` - Supervisor UI/UX specification
- `docs/specs/worker-supervision-roundabout.md` - Worker visibility phases

---

_End of Specification_
