# Swarmlet Auth + Route Protection (Launch v1)

**Status**: Ready for implementation
**Last updated**: 2025-12-12
**Owner**: Engineering (backend + nginx + frontend)

## Goals

- Public marketing remains accessible without login.
- All “app” surfaces (Dashboard + Jarvis chat + agent threads) require authentication **at the server layer** (nginx).
- API remains protected with JWT validation (**already true**), but migrates browsers to **cookie-based auth** (no localStorage tokens).
- Auth state is represented by a **secure, server-set cookie** so nginx can enforce access.

## Non-Goals (v1)

- Refresh tokens / session rotation.
- Multi-device session management UI.
- Full CSRF framework (we’ll add minimal safeguards now; see CSRF section).
- Nginx-side JWT signature validation (we will validate via backend `/api/auth/verify`).

## Key Decision: Same-origin API for browsers (prod)

**Decision**: Browsers must call the API via same-origin paths on `https://swarmlet.com`:

- Browser API base URL: `https://swarmlet.com/api` (relative `/api`)
- Browser WS base URL: `wss://swarmlet.com` (relative, e.g. `/api/ws`)

**Rationale**:

- Avoids cross-origin cookie complexity and CORS-with-credentials pitfalls.
- Enables HttpOnly cookie auth without storing JWTs in JS.
- Aligns production with current dev topology (nginx already proxies `/api/*` to backend).

**Note**: `https://api.swarmlet.com` may continue to exist for non-browser clients, but the **frontend must not** point to it.

---

## Route Classification

### Public routes (no auth required)

- `GET /`
- `GET /pricing`
- `GET /docs`
- `GET /changelog`
- `GET /privacy`
- `GET /security`
- Static assets needed by those pages (CSS/JS/images/fonts)

### Private routes (auth required; otherwise redirect to `/`)

- `GET /dashboard` and any SPA deep-links under it
- `GET /agent/*` (thread/chat UI routes)
- `GET /chat/*` (Jarvis PWA)

**Behavior when unauthenticated**:

- Nginx returns `302` redirect to `/` (optionally include `?next=<original>`).

---

## Authentication Mechanism (Cookie + JWT)

### Cookie

- Name: `swarmlet_session`
- Value: platform access JWT (HS256) currently issued by backend
- Flags:
  - `HttpOnly`
  - `Secure` (**production required**; see Dev Notes)
  - `SameSite=Lax`
  - `Path=/`
  - `Max-Age=<expires_in>` (seconds)

### JWT source of truth

- The platform JWT is still the source of truth for identity/authorization.
- Backend must accept JWT from:
  1. `Authorization: Bearer <token>` (for non-browser clients / tooling)
  2. `Cookie: swarmlet_session=<token>` (for browsers)

**Browser requirement**: browser code must not store JWT in `localStorage` (or any JS-readable storage).

---

## Backend API Contract

### 1) Login: `POST /api/auth/google`

**Request**: unchanged (current)
Body JSON: `{ "id_token": "<Google ID token JWT>" }`

**Response**:

- JSON (unchanged): `{ access_token, expires_in, token_type? }` (TokenOut)
- **Also sets cookie** `swarmlet_session=<access_token>` with flags above.

**Notes**:

- Keep JSON response for now for backwards compatibility, but frontend must ignore it for storage.
- Cookie `Max-Age` must match `expires_in`.

### 2) Verify (for nginx): `GET /api/auth/verify`

**Purpose**: fast auth check for nginx `auth_request`.

**Behavior**:

- If request contains a valid, unexpired platform JWT (prefer cookie; allow bearer), return `204 No Content`.
- Otherwise return `401 Unauthorized`.

**Important**:

- Must not redirect (nginx handles redirects).
- Must be cheap (JWT decode + exp check + user lookup if required).

### 3) Logout: `POST /api/auth/logout`

**Behavior**:

- Clears cookie `swarmlet_session` (same `Path` and any `Domain` used; `Max-Age=0`).
- Returns `204 No Content`.

### 4) Dev-only login: `POST /api/auth/dev-login`

**Dev behavior**:

- When `AUTH_DISABLED=1`, endpoint continues to work.
- Also sets `swarmlet_session` cookie so devs can exercise cookie flows if desired.

---

## Backend Auth Implementation Requirements

### Token extraction (HTTP requests)

Implement a single token extraction function used by auth dependencies:

Order:

1. If `Authorization: Bearer ...` present, use it.
2. Else if cookie `swarmlet_session` present, use it.
3. Else unauthenticated.

### Token extraction (WebSocket handshake)

Browsers cannot read HttpOnly cookies, so WebSocket auth must support cookie-based auth:

Order:

1. If query param `token=<jwt>` provided (keep for non-browser clients), use it.
2. Else if cookie `swarmlet_session` present on handshake request, use it.
3. Else unauthenticated (close with 4401).

### HTTP status behavior

- Protected API endpoints must continue returning `401` on missing/invalid auth.
- Nginx-gated UI routes redirect at the proxy layer; backend should not be involved.

---

## Nginx Route Protection (Production)

### Strategy

Use `auth_request` to call backend `GET /api/auth/verify` for private routes.

**Why**: cookie presence checks are only a UX gate; `auth_request` enforces validity/expiry.

### Required Nginx behavior

- Private routes: if verify returns 2xx → allow upstream; if 401 → redirect to `/`.
- Public routes: no auth checks.
- `/api/*`: no redirects; backend returns 401/403 normally.

### Suggested config shape (high-level)

- Add an internal location, e.g. `location = /_internal/auth_verify { internal; proxy_pass .../api/auth/verify; }`
- For private routes:
  - `auth_request /_internal/auth_verify;`
  - `error_page 401 = @redirect_home;`
- `@redirect_home` returns `302 /` (optionally include `next` param).

### Files touched (expected)

- `docker/nginx/nginx.prod.conf` (prod proxy behavior)
- (If needed) `docker/nginx/nginx.prod.conf` or `docker/nginx/docker-compose.unified.conf`, depending on deploy path

---

## Frontend (Zerg Dashboard) Requirements

### Runtime config (prod)

Update `apps/zerg/frontend-web/public/config.js` so that for `swarmlet.com`:

- `window.API_BASE_URL = "/api"` (NOT `https://api.swarmlet.com/api`)
- `window.WS_BASE_URL = window.location.origin.replace("http", "ws")`

### No localStorage token

Remove all usage of `localStorage['zerg_jwt']` for auth storage.

Required behaviors:

- After login, do not persist `access_token` anywhere in JS.
- API calls rely on cookie auth; ensure `fetch` uses credentials (same-origin default is fine; be explicit if needed).
- WebSocket connects without needing JS-readable token (backend reads cookie).

### Backwards compatibility (optional, short-lived)

If you need a transition period:

- Backend accepts both cookie and bearer.
- Frontend stops writing localStorage immediately.
- (Optional) Keep reading `localStorage['zerg_jwt']` for a limited time only if present, but do not write it.

---

## Jarvis (PWA) Requirements

### Route gating

- `/chat/*` must be treated as a private route by nginx (same auth mechanism as dashboard).

### Auth mechanism

Jarvis web must stop depending on `localStorage['zerg_jwt']`.

- API calls to `/api/jarvis/*` and other protected endpoints must work via cookie auth.
- If any Jarvis components currently add `Authorization: Bearer <localStorage token>`, update to cookie-based auth.

---

## CSRF (Minimum viable safeguards for v1)

Once cookies are used for authentication, **state-changing endpoints** should enforce:

- Use non-GET verbs for state changes (`POST/PUT/PATCH/DELETE` only).
- Require `Content-Type: application/json` where applicable.
- Validate `Origin` (and/or `Referer`) for state-changing requests to be same-site (`https://swarmlet.com`).
- Keep `SameSite=Lax` on the cookie.

This is sufficient for launch for a same-origin, cookie-auth browser app, assuming no sensitive state changes are performed via GET.

---

## Dev / Local Notes (important)

- `Secure` cookies are not set/sent over plain `http://localhost`.
- For local dev, either:
  1. Run the dev proxy under HTTPS (preferred for realistic testing), or
  2. Allow `Secure=False` only in dev mode (never in prod).

Document which approach is used and keep production strictly `Secure`.

---

## Acceptance Criteria

- Incognito:
  - Visiting `/dashboard` redirects to `/` (no dashboard UI shown).
  - Visiting `/chat/` redirects to `/`.
- Authenticated:
  - After Google sign-in, visiting `/dashboard` and `/chat/` works without redirects.
- API:
  - Protected API endpoints return `401` if auth missing/invalid (cookie or bearer).
- Cookie:
  - `swarmlet_session` is `HttpOnly` and `Secure` in production.
  - Cookie is not readable from JS.
- WebSockets:
  - `wss://swarmlet.com/api/ws` works after login without passing a token in JS.

---

## Implementation Task List (hand this to dev)

### A) Frontend runtime config

- [x] Update `apps/zerg/frontend-web/public/config.js` to remove `api.swarmlet.com` usage for `swarmlet.com`.
- [ ] Confirm the production build loads config.js before app boot (existing behavior).

### B) Backend: cookie set/clear + verify endpoint

- [ ] Update `POST /api/auth/google` to also set `swarmlet_session` cookie.
- [ ] Update `POST /api/auth/dev-login` to also set `swarmlet_session` cookie (dev only).
- [ ] Add `GET /api/auth/verify` (returns 204 or 401).
- [ ] Add `POST /api/auth/logout` (clears cookie; returns 204).

### C) Backend: accept cookie auth everywhere

- [ ] Update HTTP auth dependency to accept token from cookie when bearer is missing.
- [ ] Update WebSocket auth to accept cookie on handshake when query token missing.
- [ ] Ensure all protected endpoints still return 401 when neither cookie nor bearer is present.

### D) Zerg dashboard: remove localStorage JWT

- [ ] Remove writes to `localStorage['zerg_jwt']` (e.g. login flow).
- [ ] Remove reads used for API Authorization headers.
- [ ] Ensure API client uses cookie auth (set `credentials` explicitly if needed).
- [ ] Update WebSocket client to connect without token query param (cookie handshake).
- [ ] Update/adjust unit tests that mock `zerg_jwt` in localStorage.

### E) Jarvis: remove localStorage JWT dependency

- [ ] Update Jarvis web client code paths that read `localStorage['zerg_jwt']`.
- [ ] Ensure Jarvis API calls work via cookie auth.
- [ ] Remove/adjust Jarvis messaging/tooling paths that attach bearer from localStorage.

### F) Nginx: server-layer gating

- [ ] Update `docker/nginx/nginx.prod.conf` to add `auth_request` gating:
  - [ ] Gate `/dashboard` + `/dashboard/*`
  - [ ] Gate `/agent/*`
  - [ ] Gate `/chat` + `/chat/*`
  - [ ] Redirect unauth → `/`
- [ ] Add internal auth verify location that proxies to backend `/api/auth/verify`.
- [ ] Ensure `/api/*` never redirects (returns status codes).

### G) Validation (required before merge)

- [ ] Manual: incognito redirect checks for `/dashboard` and `/chat/`.
- [ ] Manual: login then reload private routes; ensure no redirects.
- [ ] Manual: logout then confirm private routes redirect.
- [ ] Manual: verify `Set-Cookie` flags in production-like HTTPS environment.
- [ ] Automated: add/update tests as appropriate (backend unit tests + any Playwright coverage you already have).
