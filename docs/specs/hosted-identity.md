# Hosted Identity: One Longhouse Account

Status: revised after Codex architecture review (2026-06-15); ready for Phase 0 build
Owner: David
Related:
- `VISION.md`
- `AGENTS.md`
- `docs/specs/agents-machine-surface.md` (sibling contract for machine auth)
- `control-plane/control_plane/routers/auth.py` (current CP auth router, HS256 today)
- `control-plane/control_plane/routers/ui.py` (CP Jinja pages, login + dashboard)
- `control-plane/control_plane/models.py` (CP `User`, `Instance`; `Instance.user_id` is unique)
- `server/zerg/routers/auth_browser.py` (current tenant auth router)
- `server/zerg/routers/auth_sso.py` (current cross-server SSO bridge)
- `server/zerg/auth/session_tokens.py` (current tenant HS256 mint)
- `server/zerg/auth/strategy.py` (production tenant JWT strategy)
- `server/zerg/dependencies/auth.py` (machine-side `get_current_user`)
- `server/zerg/dependencies/browser_auth.py` (browser cookie / `zdt_*` device-token)
- `server/zerg/dependencies/browser_route_auth.py` (browser query-token / WS)
- `server/zerg/services/sso_keys.py` (existing CP key fetch; not `auth/sso_keys.py`)
- `server/zerg/models/user.py` (tenant `User` model; `email` is unique)
- `server/zerg/database.py` (imperative `_migrate_*` + `_auto_add_missing_columns`)
- `web/src/pages/LoginPage.tsx` (current tenant login UI)
- `web/src/lib/auth.tsx`, `web/src/lib/authApi.ts` (web auth state)
- `ios/Sources/LonghouseApp/LoginView.swift` (current iOS login)
- `ios/Sources/LonghouseApp/LonghouseApp.swift` (auth state machine: restore, hosted SSO completion, logout)
- `ios/Sources/Shared/LonghouseAPI.swift:645-663` (cookie injection + `/api/auth/refresh` retry)
- `ios/Sources/Shared/SharedAuthStore.swift` (current cookie storage)
- `ios/Sources/Shared/KeychainHelper.swift` (single global auth-token key today)
- `ios/Sources/Shared/SessionWorkspaceStream.swift:209-217` (SSE cookie injection)
- `ios/Sources/Shared/TimelineSessionsStream.swift:123-135` (timeline SSE cookie injection)

## Why this exists

Longhouse has two FastAPI services that each render a Longhouse-branded
login page on a different domain. A user landing on a hosted tenant
(`david010.longhouse.ai`) clicks "Continue to your Longhouse account" and
gets bounced to a different login on `control.longhouse.ai`. The CP login
has its own Google + GitHub + email/password buttons, its own cookie, its
own brand surface. From the user's point of view it reads as "I logged
in, but it's asking me to log in again." That's the symptom.

The disease is that the two services each own an independent identity
system:

- CP owns the account (signup, billing, subscription, recovery).
- Tenant owns the session (login, logout, refresh, dev/service auth,
  Google OAuth, password hashing, JWT minting, refresh-token rotation).

Both services know what a User is. Both can mint a cookie. Both have a
`/auth/*` surface. A user with a hosted tenant effectively has two
parallel "Longhouse accounts" — one for the CP, one for the tenant —
even though only the CP is the real one. The tenant's User row exists
purely as a derived cache of the CP's identity.

We are pre-launch with zero external users. Every later auth feature
(MFA, passkeys, SAML, account deletion, audit log, impersonation) has to
land in *two* places today. The longer we ship on this split, the more
it costs to undo.

This spec defines the smaller surface: **the CP is the only identity
authority for hosted tenants. The tenant is a resource server that
verifies CP-issued tokens. Self-host mode keeps its existing local
identity system unchanged.**

## Scope

In scope:

- CP becomes the only place that knows what a User is for hosted
  tenants, mints sessions, issues OAuth, sends verification emails, and
  handles password reset.
- Tenant stops minting sessions, refresh tokens, OAuth codes, and
  password hashes **for hosted tenants only**. Self-host tenants
  continue to use `JWTAuthStrategy` and local password auth untouched.
- Tenant `users.id` stays as the local resource-owner primary key. A
  new `users.cp_user_id` column maps a local user to the CP identity
  that owns it. CP token `sub` maps to `users.cp_user_id`, never to
  `users.id` directly.
- Tenant authenticates requests by verifying a CP-issued JWT against
  the CP's published JWKS, with `aud` exactly equal to the tenant's
  `INSTANCE_ID`. Self-host tenants use a different verifier selected at
  startup.
- Browser cookie for hosted tenants is a CP-signed JWT. iOS uses the
  same JWT as `Authorization: Bearer` in the Keychain.
- Cross-server handoff uses a short-lived one-use **handoff code**,
  not the runtime JWT in the URL. The runtime JWT is delivered
  server-to-server after the tenant exchanges the handoff code with
  the CP.
- One login UI surface for hosted users. The CP renders a tenant-aware
  login page; the tenant has no login UI for hosted users.

Explicitly deferred (not in this epic):

- MFA, passkeys, SAML, WebAuthn. Will land on the CP only, after this
  spec ships.
- Account deletion / GDPR data export. Same: CP only, after.
- Tenant-side service-account auth (engine tokens, `X-Agents-Token`)
  is untouched. The machine auth surface in
  `docs/specs/agents-machine-surface.md` already has its own rules;
  `zdt_*` device tokens must not be parsed as CP JWTs.
- iOS keychain rotation, push-notification-bound sessions. These ride
  on top of the bearer token; they don't need to land in this epic.
- Refresh-token rotation on the CP. Access tokens first. Refresh comes
  after dogfood.
- Multi-tenant per CP user. CP `Instance.user_id` is unique today; one
  CP user has exactly one hosted instance. Multi-instance per user
  requires a CP schema change and is out of scope.
- Durable global revocation. v1 runtime JWTs are valid until `exp`
  even after the CP-side session is cleared. Real revocation is
  Phase 5+ with a shared durable store.

## The decision: Path 3A

Four things the architecture must guarantee after this ships:

1. **One identity authority for hosted tenants.** Only the CP mints,
   rotates, and revokes identity-bearing tokens for hosted users. Only
   the CP knows what a User is across Longhouse. Self-host tenants are
   exempt; they run their own local identity system.

2. **Two kinds of ids, never collapsed.**
   - `cp_user_id` (integer, owned by the CP) — the *identity* of a
     person across Longhouse. Appears as the CP JWT `sub` claim, in CP
     billing, in CP signup. It is always an integer parseable from
     `sub`.
   - `tenant_user_id` (the existing `users.id`, owned by the tenant) —
     the *resource-owner key* for rows the user owns inside one tenant:
     sessions, messages, machines, agents, comments, etc.

   These are different nouns. They live in different services. The
   tenant never assumes one equals the other. A
   `cp_user_id → tenant_user_id` mapping row in `users` is the only
   place they meet.

3. **Tokens are bearer, not cookie-coupled.** The session token is a
   CP-signed JWT scoped to one tenant via the `aud` claim. The browser
   stores it in a tenant-scoped cookie. iOS stores the same token in
   Keychain keyed by server host and sends it as `Authorization:
   Bearer`. Web and iOS converge on the same wire format, so the auth
   path looks identical regardless of client.

4. **Handoff is a code, not a token.** The cross-server browser
   handoff uses a one-use short-lived opaque handoff code. The
   CP stores the handoff state (the profile snapshot, tenant
   binding, `tenant_state` CSRF nonce, return_to, 60-second
   expiry, and a `consumed` flag) keyed by the code. The
   tenant exchanges the code with the CP server-to-server
   using the `X-Internal-Token` header and gets back the
   runtime JWT. The runtime JWT never appears in a URL. iOS
   receives the runtime JWT directly through the custom
   scheme deep link (the URL is captured by the OS, not stored in
   browser history).

Concretely:

- CP gets new modules
  `control_plane/services/identity_provider.py` (mint, verify) and
  `control_plane/routers/identity_api.py` (machine-readable routes
  under `/api/identity/*`, not `/api/auth/*` — the existing CP browser
  routes stay under `/auth/*`).
- Tenant gets a new module `server/zerg/auth/cp_jwks.py` that fetches
  and caches the CP's JWKS, verifies tokens, and exposes
  `verify_runtime_token(token, audience=INSTANCE_ID) -> TokenClaims`.
- The existing cross-server SSO bridge at
  `server/zerg/routers/auth_sso.py:37-156` (`_accept_token` core at
  37-128, `POST /accept-token` at 131, `GET /accept-token` redirect
  at 143-156) is repurposed: it accepts either an HS256 legacy
  bridge token (Phase 0/1 transition) or a one-use handoff code
  (Phase 2+), and either sets the runtime JWT directly as
  `longhouse_session` (hosted + `control_plane_identity: true`) or
  mints a local HS256 token (self-host or legacy).
- The existing tenant login routes at
  `server/zerg/routers/auth_browser.py` shrink to one route:
  `GET /api/auth/methods`. Hosted tenants respond with `{sso_url}` or
  redirect to `/auth/start` on the CP. Self-host tenants keep
  `{google, password}`.

## FK separation in detail

Today, the tenant has one `users` table. It carries `id` (autoincrement,
local), `email`, `display_name`, `avatar_url`, `provider`,
`provider_user_id`, `is_active`, and runtime-only fields.

After this spec:

- Keep `users.id` as the local resource-owner primary key. **Do not
  rewrite the ~30 FKs that point at it.** Sessions, messages, agents,
  machines, comments, etc. continue to FK `users.id` like they do
  today.
- Add `users.cp_user_id: integer, nullable=True, index=True`. The
  bridge populates this on first SSO login. It is the link back to
  the CP identity. It is **not** `unique=True` on the column itself
  because the runtime's `_auto_add_missing_columns` skips `unique=True`
  columns; the uniqueness is enforced by a partial index added by an
  imperative migration (see "DB migration reality").
- Keep `users.email`, `users.display_name`, `users.avatar_url`. Treat
  them as a local cache populated from the CP JWT. They are not the
  source of truth — the CP is. On cache update, if the new email is
  already owned by a different local user, keep the old cached email
  and log an `account_link_conflict` event; never silently overwrite.
- Drop `users.provider`, `users.provider_user_id`, `users.is_active` for
  hosted tenants. The CP owns these. Self-host mode keeps them.
- If `users.is_active` has other meanings in the codebase, keep it
  but in hosted mode it is always true and write-locked.

The bridge code in `server/zerg/routers/auth_sso.py:62-100` currently
upserts by email. After this spec, the priority is: if the JWT carries
a `sub`, look up `users.cp_user_id = sub` first. Only fall back to
email lookup if `sub` is absent (legacy bridge tokens, self-host). The
new order is the rule, the old order is the back-compat path.

## Token shape

```json
{
  "iss": "https://control.longhouse.ai",
  "aud": "<INSTANCE_ID>",
  "sub": "<cp_user_id, decimal string>",
  "email": "david010@gmail.com",
  "email_verified": true,
  "display_name": "David Rose",
  "avatar_url": "https://...",
  "iat": 1718448000,
  "exp": 1718451600
}
```

Rules:

- **These rules apply to hosted runtime tokens only.** Self-host
  uses the existing local HS256 JWT shape and is not affected
  by anything below.
- `iss` is `https://control.longhouse.ai`.
- `aud` is exactly the tenant's `INSTANCE_ID` (env var). There
  is no `longhouse-multi-tenant` value in v1. A token with the
  wrong `aud` for the current tenant is rejected.
- `sub` is the CP user id as a decimal string parseable to
  `int(sub)`. The CP guarantees uniqueness. The tenant maps
  `sub` → `users.cp_user_id`; it does not interpret `sub` as
  `users.id`.
- `email_verified` is a required boolean claim. The tenant
  must not re-verify email and must trust the CP's value. v1
  semantics: **the CP mints runtime JWTs for unverified
  users** (mirroring today's CP session cookie behavior — CP
  signup creates `email_verified=False` users and redirects
  them to `/verify-email`). The tenant UI surfaces a
  "verify your email" banner when the claim is `false`; the
  session is otherwise valid. (Forcing re-verification to
  use the product would be a behavior change vs. today.)
- `email` is required. `display_name` and `avatar_url` are
  optional convenience claims. If absent, the tenant preserves
  the existing cached value or falls back to email as the
  display name. They are *not* authoritative — the CP is.
- No `jti` claim. v1 has no immediate global revocation.
  Runtime tokens are valid until `exp`. Logout clears the
  tenant cookie, then optionally clears the CP cookie. CP-side
  session invalidation does not invalidate already-minted
  runtime JWTs. Durable global revocation is deferred to a
  follow-up spec.
- Lifetime: 1 hour access, 24 hour refresh (refresh is phase
  5, not shipped in the first vertical slice).
- Signing: RS256. The CP holds a shared keyset (current key +
  previous key) loaded from a single CP secret/config source,
  not generated per process. JWKS publishes all currently
  accepted public keys. New CP deploys must publish the new
  public key before minting tokens with its `kid`; old keys
  remain published until the maximum runtime-token lifetime
  has elapsed. This is the keyset model that survives rolling
  deploys and lets tenants cache by `kid`.
- `control-plane/pyproject.toml` must add `PyJWT[crypto]` (or
  `cryptography` + the existing hand-written encoder) — it
  has neither today.

## Cookie + bearer

Browser (web):

- Hosted tenant sets `longhouse_session=<CP JWT>; HttpOnly; Secure;
  SameSite=Lax; Path=/; Max-Age=3600`. Scoped to the tenant's domain.
  Not shared across subdomains.
- CP sets `cp_session=<CP session JWT>; HttpOnly; Secure;
  SameSite=Lax; Path=/; Max-Age=<session>`. Scoped to
  `control.longhouse.ai`. **Not** set with `Domain=.longhouse.ai`.
  Cross-subdomain cookie sharing is a footgun.
- `SameSite=Lax` is enough because every cross-site flow uses a 302
  redirect with a one-shot handoff code in the URL. There is no POST
  cross-origin and the runtime JWT is never in a URL.
- Hosted tenant `/api/auth/logout` remains a real endpoint. It
  deletes `longhouse_session`. The web app calls tenant logout first,
  then optionally redirects to CP `/auth/logout` to clear `cp_session`.

iOS:

- The CP runtime token goes into the iOS Keychain via
  `ios/Sources/Shared/KeychainHelper.swift`, keyed by normalized
  server host (currently a single global key; Phase 4 moves to
  per-host). Each hosted tenant gets its own keychain entry.
- Every API call sends `Authorization: Bearer <token>` from the
  Keychain. The cookie jar still exists for backwards-compatible
  legacy code paths during transition, but the source of truth on
  iOS is the bearer token.
- `SharedAuthStore` is repurposed from cookie-jar to bearer-token
  storage. `LonghouseAPI.data()` swaps from injecting `cookieHeader`
  to injecting `Authorization: Bearer` for the iOS path.
- `AppState.restoreSession()` checks bearer token presence (per host)
  and verifies via tenant `/api/auth/status` (which in hosted mode
  validates the CP JWT and returns the local `AuthenticatedUser`).
- `exchangeHostedSSOToken` stores the runtime bearer directly; it
  no longer depends on the cookie path through `/api/auth/accept-token`.
- `SessionWorkspaceStream.swift:209-217` and
  `TimelineSessionsStream.swift:123-135` both attach
  `Authorization: Bearer` to their request headers. Widgets and push
  paths that create `LonghouseAPI(host:)` read the same per-host
  bearer.

Self-host:

- Self-host mode keeps the existing local `JWTAuthStrategy` in
  `server/zerg/auth/strategy.py` and the local
  `session_tokens.py`, password auth, refresh tokens, and cookie
  behavior **bit-for-bit unchanged**. The token shape self-host
  uses today (HS256, `sub`/`email`/`exp` plus optional profile
  fields) is the token shape self-host uses after this spec
  ships. The runtime check at startup selects between the local
  strategy and the CP JWKS verifier:
  `if settings.control_plane_url and settings.control_plane_identity: hosted else: self_host`.
- The `cp_jwks` module is selected only for hosted tenants with
  `CONTROL_PLANE_URL` set and `control_plane_identity: true`. The
  hosted/runtime token shape defined in this spec does not apply
  to self-host.
- iOS self-host builds continue to use the cookie path. The
  bearer-Keychain path is hosted-only.

## Handoff: opaque code, CSRF-bound, server-to-server exchange

The cross-server browser handoff is the only place a Longhouse auth
secret appears in a URL. To prevent CSRF, session-fixation, log
leakage, and referrer leakage, the secret in the URL is an *opaque
one-use code*, not the runtime JWT. The runtime JWT is delivered
server-to-server after the tenant authenticates itself to the CP and
exchanges the code.

### Handoff code (opaque handle, server-side state)

The `code` URL parameter is a 256-bit cryptographically random value,
base64url-encoded. The CP stores the handoff state server-side
keyed by this code, in a small in-process table (with disk-backed
recovery to survive CP restart — see Phase 1 implementation note).
The table row carries:

- `code` (the handle itself, also indexed)
- `cp_user_id` (the CP identity that completed login)
- `email`, `email_verified`, `display_name`, `avatar_url`
  (the profile snapshot to put into the runtime JWT)
- `tenant_subdomain` (the destination tenant; tenant must match on
  exchange)
- `instance_id` (the `aud` for the runtime JWT)
- `return_to` (validated by the tenant, not the CP, before final
  redirect)
- `tenant_state` (anti-CSRF nonce — see below)
- `expires_at` (creation + 60 seconds)
- `consumed: bool` (false initially)

The CP exposes a small server-to-server API over the existing
`X-Internal-Token: <instance_internal_api_secret>` header scheme
(the same scheme used today for the Gmail handoff, see
`control_plane/routers/auth.py:432`). No public mint endpoint.

### Anti-CSRF binding (tenant-side state)

To prevent a third-party site from initiating a CP login that
silently binds the visitor's browser to an attacker-chosen tenant
session, the tenant mints its own CSRF nonce before redirecting
the user to the CP. The cookie must be set by a server route
(browsers cannot set `HttpOnly` from JS), so the flow is:

1. Browser hits `GET /api/auth/start-handoff?tenant=...&return_to=...`
   on the tenant.
2. The server route generates `tenant_state` (256-bit random),
   stores it in a short-lived `tenant_login_state` cookie scoped
   to the tenant domain, `HttpOnly; Secure; SameSite=Lax;
   Max-Age=600`, and 302s to
   `https://control.longhouse.ai/auth/start?tenant=...&return_to=...&tenant_state=...`.
3. CP `/auth/start` round-trips `tenant_state` through the OAuth
   state JWT and back to the tenant. The CP does not need to
   validate it (the CP doesn't know about the tenant's cookies);
   the tenant validates it on the final `accept-handoff` return.
4. Tenant `accept-handoff` compares the returned `tenant_state` to
   the cookie. Mismatch → 403 Forbidden, do not set the session
   cookie, log `tenant_login_csrf_mismatch`. Missing cookie →
   same, log `tenant_login_csrf_missing`.

The web `LoginPage` does **not** set the cookie itself — it
navigates to `/api/auth/start-handoff?return_to=...`, which sets
the cookie and redirects. The React component is a thin
navigator, not a cookie-setter.

This binds the login to a browser the tenant itself initiated
login on. An attacker who tricks the user into a forged `accept-handoff`
URL has the wrong `tenant_state` (or none) and is rejected.

### Browser flow

1. User visits `https://david010.longhouse.ai/login`. React app
   calls `GET /api/auth/methods`, gets
   `{sso: true, sso_url: "https://control.longhouse.ai",
    sso_login_url: "https://control.longhouse.ai/auth/start"}`.
2. `LoginPage` is a thin navigator: it reads the current
   `return_to` from the URL and navigates the browser to
   `https://david010.longhouse.ai/api/auth/start-handoff?return_to=...`.
   The server route there generates `tenant_state`, sets the
   `tenant_login_state` cookie, and 302s to
   `https://control.longhouse.ai/auth/start?tenant=david010&return_to=...&tenant_state=...`.
3. CP `/auth/start` renders the tenant-aware login page. CP
   OAuth state JWT carries `tenant`, `return_to`, and
   `tenant_state`. The CP does not currently have the user's CP
   session — proceed to login.
4. User signs in (Google/GitHub/email). CP OAuth callback
   decodes state. If the user owns `tenant`, the CP creates a
   handoff row (opaque code, profile snapshot per the
   "Handoff code" section above) and 302s to
   `https://david010.longhouse.ai/api/auth/accept-handoff?code=...&return_to=...&tenant_state=...`.
5. Tenant `accept-handoff` (server-to-server):
   - Validates the request's `tenant_state` matches the
     `tenant_login_state` cookie (anti-CSRF).
   - Calls CP `POST /api/identity/exchange-handoff` with the
     `code` and the header `X-Internal-Token: <secret>` (the
     shared instance-internal secret, same as Gmail handoff).
   - CP marks the handoff row `consumed=true`, returns the
     runtime JWT.
   - Tenant verifies the returned JWT against the CP JWKS
     (issuer, audience, expiry, `email_verified`).
   - Tenant upserts the local user by `cp_user_id` (or email
     fallback), refreshes the cached profile, sets
     `longhouse_session` to the JWT, clears
     `tenant_login_state`, and 302s to `return_to`.
6. Subsequent requests use the `longhouse_session` cookie. The
   handoff row is one-use; second exchange with the same code
   returns 410 Gone.

### Native (iOS) flow

iOS receives the runtime JWT directly in the custom-scheme
deep link (`ai.longhouse.ios://auth-callback?...&sso_token=...`).
The URL is captured by the OS and is not exposed in browser
history, logs, or referrers. iOS cannot read cookies set inside
the `ASWebAuthenticationSession` (it has no cookie jar for
that ephemeral web context), so the browser's
`tenant_login_state` cookie CSRF binding does not apply to
iOS. Instead, iOS uses its own local CSRF binding:

1. iOS generates `tenant_state` (256-bit random) in the app
   process and stores it in `UserDefaults` (or a transient
   `KeychainHelper` entry) before opening the
   `ASWebAuthenticationSession`.
2. iOS opens `https://control.longhouse.ai/auth/native/open-instance?tenant=...&return_to=...&tenant_state=...`.
3. CP `/auth/native/open-instance` round-trips `tenant_state`
   through the OAuth state and includes it in the iOS deep
   link callback: `ai.longhouse.ios://auth-callback?tenant_state=...&sso_token=...`.
4. iOS compares the returned `tenant_state` to the locally
   stored value. Mismatch → discard the JWT, surface a login
   error. Missing local value → same.

This is the same CSRF shape as the browser, just with
`UserDefaults` as the storage instead of a cookie. An attacker
who tricks the user into a forged `auth-callback` URL has the
wrong `tenant_state` and is rejected.

**Do not** put the instance-internal secret in any native
app. Native apps are public clients. The browser tenant holds
the secret because the secret is in a server-side
configuration, not in user-accessible code. iOS gets the JWT
in a deep link because it cannot hold a server-side secret
safely. This asymmetry is intentional and is a comment in
the iOS code.

### Handoff secret logging

Any CP or tenant log line that captures URL query strings must
redact the `code` parameter. Add a `redact_url()` helper used by
all request log lines. (The existing `cp_session` cookie is
already treated as sensitive; treat handoff codes the same way.)

## The six phases

### Phase 0 — Front-funnel SSO (2-4 days)

The vertical slice. Ships end-to-end with the **existing HS256 bridge
still in use**. No new token shape. No JWKS. Just the bounce UX.

- Add CP `GET /auth/start?tenant=...&return_to=...` route in
  `control_plane/routers/ui.py`. Renders a tenant-aware login page
  (header reads "Sign in to {tenant}.longhouse.ai"). Reuses the
  existing Google/GitHub/email Jinja from `ui.py:151` with `tenant`
  injected into the OAuth state.
- CP OAuth callbacks (`routers/auth.py:601`, `routers/auth.py:680`)
  read `tenant` from the decoded state. If present and the tenant
  belongs to the signed-in user, after auth, call a new
  `_finish_tenant_sso(user, instance, return_to)` helper that 302s to
  `https://{tenant}.longhouse.ai/dashboard/open-instance?return_to=...`
  — *the same endpoint that already works today*. This is the existing
  HS256 bridge, not a new token.
- Tenant `GET /api/auth/methods` returns
  `{sso: true, sso_url: "https://control.longhouse.ai",
   sso_login_url: "https://control.longhouse.ai/auth/start"}` for
  hosted tenants. Self-host tenants get `{google, password}` like
  today.
- Tenant `GET /login` React page becomes a one-effect interstitial
  that 302s to the CP `/auth/start` URL with the current `return_to`.
  (`web/src/pages/LoginPage.tsx` body collapses to ~15 lines.)
- iOS `LoginView` deletes the Google and password branches. Just one
  "Continue with Longhouse" button that calls the existing
  `startHostedSignIn` / `startHostedBootstrapSignIn` flow (which
  already hits `/auth/native/open-instance` and gets back the
  iOS-callback deep link).
- **Old `/dashboard/open-instance` bridge stays as the single auth
  path.** No new token format. No `CONTROL_PLANE_IDENTITY` flag yet.
- The CP adds no new columns to `cp_users` in this phase.

Done when: David can land on `david010.longhouse.ai`, click sign in,
see a single CP login page branded with the tenant name, and end up
signed in via the existing HS256 bridge. No more "two logins"
feeling. Token shapes and verification paths are unchanged from
today.

### Phase 1 — CP as IdP (1 week)

- New `control_plane/services/identity_provider.py`:
  - Loads the shared RS256 keyset from `settings.identity_signing_keys`
    (multiple keys supported; one is "active" for minting, all
    "accepted" are published in JWKS for verification).
  - `mint_runtime_token(user, audience, ttl=3600) -> str`.
  - `mint_handoff_code(user, instance, return_to, tenant_state, ttl=60) -> str`.
  - `verify_runtime_token(token, audience) -> TokenClaims` (used by
    CP itself for internal routes that need to validate).
  - `consume_handoff(code) -> TokenClaims | None` (server-to-server
    tenant handoff; returns the snapshot and marks the row
    `consumed=true`).
  - Keyset rotation helper that promotes a new key to "active" and
    keeps the previous "accepted" for the max token lifetime.
- New `control_plane/routers/identity_api.py`. Routes under
  `/api/identity/*`:
  - `GET /api/identity/jwks.json` — public JWKS, returns all accepted
    public keys.
  - `POST /api/identity/exchange-handoff` — tenant server-to-server
    call to swap a handoff code for a runtime JWT. Marks the
    handoff row `consumed=true`. Requires `X-Internal-Token:
    <instance_internal_api_secret>` (the same scheme as the
    existing Gmail handoff). Returns the runtime JWT plus the
    standard claim set.
  - `POST /api/identity/runtime-token` (CP-internal) — for tests and
    internal CP services that need to mint a runtime JWT directly.
- New `control_plane/routers/api_auth.py` (optional, for hosted
  `/api/auth/*` consumers) — only if we want programmatic hosted
  login. The default path remains the existing browser `/auth/*`
  routes.
- Add `display_name` and `avatar_url` columns to CP `User` model in
  `control-plane/control_plane/models.py`. Populate from
  Google/GitHub userinfo in the OAuth callbacks. Make them optional
  and nullable. Add an imperative migration alongside.
- `LonghouseAuthConfig.hostedControlPlaneURL` is unchanged.
- `control-plane/pyproject.toml` adds `PyJWT[crypto]` (or equivalent)
  for RS256 signing.

Done when: a non-tenant script can `curl` the CP JWKS, exchange a
handoff code for a runtime JWT via `/api/identity/exchange-handoff`,
and the JWT verifies against the JWKS.

### Phase 2 — Tenant CP verifier + handoff (1-1.5 weeks)

- New `server/zerg/auth/cp_jwks.py`. Fetches and caches the CP's
  JWKS from `/api/identity/jwks.json`, verifies tokens, handles `kid`
  rotation (refetch on unknown `kid`, fail closed after a max age),
  exposes `verify_runtime_token(token, audience=INSTANCE_ID) ->
  TokenClaims`.
- New `server/zerg/auth/runtime_strategies.py` (or similar) that
  selects the auth strategy at startup:
  - Hosted + `control_plane_identity: true`: `HostedCPAuthStrategy`,
    implements `get_current_user`, `validate_ws_token`, browser
    cookie auth via `browser_auth`, and bearer auth for iOS on
    browser-owned API/SSE routes. Preserves `zdt_*` device-token
    handling (must not be parsed as a CP JWT — checked before the
    `cp_jwks` verify).
  - Self-host or hosted with `control_plane_identity: false`:
    `LocalJWTAuthStrategy`, the existing
    `server/zerg/auth/strategy.py` behavior, unchanged.
- Migration: add `users.cp_user_id` (see "DB migration reality").
  Imperative only. Backfill: none, populated on first SSO.
- Update `server/zerg/routers/auth_sso.py:_accept_token` to handle
  both shapes during the rollout:
  1. If the request is `GET /api/auth/accept-handoff?code=...`: call
     CP `/api/identity/exchange-handoff` server-to-server, receive
     the runtime JWT.
  2. If the request is `GET /api/auth/accept-token?token=...` with a
     legacy HS256 bridge token: verify against `JWT_SECRET` plus
     `sso_keys_service.get_sso_keys()`, exchange for a local session
     as today.
  3. In hosted mode with `control_plane_identity: true`, set
     `longhouse_session` to the CP runtime JWT. In hosted mode with
     `control_plane_identity: false` or self-host, mint a local HS256
     session as today.
  4. Upsert the local user by `cp_user_id` first, falling back to
     email lookup only if `sub` is absent. Refresh the cached
     `email`, `display_name`, `avatar_url` from the runtime JWT
     claims, with the account-link-conflict rule (don't overwrite
     email if it would collide with a different local user).
- Keep `zdt_*` device token handling untouched. `browser_auth`,
  `browser_route_auth`, and `validate_ws_jwt` all need to be wired
  to the strategy selected at startup. The CP JWT verifier is
  tried *after* the device-token check.

Done when: a hosted tenant with `control_plane_identity: true` accepts
handoff codes from the browser, sets the runtime JWT directly as
`longhouse_session`, and serves the timeline + SSE routes with that
cookie. Self-host tenants are bit-for-bit unchanged.

### Phase 3 — Web cleanup (3-5 days)

- `web/src/lib/auth.tsx` keeps calling tenant `/api/auth/status` —
  the tenant is the source of "am I logged in" for the app. In
  hosted mode, `/api/auth/status` verifies the CP runtime JWT and
  returns the local `AuthenticatedUser` plus tenant-local integration
  state. CP `/auth/status` is only used on CP-owned
  login/account pages. Cross-origin CP status proxying is **not**
  added; it would be misleading because CP auth != tenant auth.
- `web/src/lib/authApi.ts` `loginWithPassword` /
  `loginWithDevAccount` go away in hosted mode. Only
  `loginWithGoogle` (when applicable) or "redirect to CP" remains.
- `web/src/pages/LoginPage.tsx` deleted (it's a one-effect redirect
  now). Keep the route as a thin component for back-compat.
- `web/src/components/Layout.tsx` logout button calls tenant
  `/api/auth/logout` first (clears `longhouse_session`), then
  optionally redirects to CP `/auth/logout` to clear `cp_session`.
  "Log out everywhere" goes to CP `/auth/logout` then tenant logout.

Done when: the React app cannot log in or stay logged in without
the CP being reachable for hosted users. Self-host tenants are
unaffected.

### Phase 4 — iOS bearer package (1-1.5 weeks)

iOS auth is a package, not a one-line swap. Touch:

- `ios/Sources/Shared/KeychainHelper.swift` — extend so that the
  account key includes the normalized server host. Existing global
  `longhouse_auth_token` migrates on first read to the new
  per-host key.
- `ios/Sources/Shared/SharedAuthStore.swift` — replace cookie-jar
  storage with Keychain-backed bearer, indexed by host. Add
  `clear(host:)` for logout.
- `ios/Sources/LonghouseApp/LonghouseApp.swift` —
  `restoreSession()` checks per-host bearer presence and verifies
  via tenant `/api/auth/status`. Hosted sign-in completion
  (`exchangeHostedSSOToken`) stores the returned runtime bearer
  directly. Logout posts tenant `/api/auth/logout` and clears
  per-host Keychain entries.
- `ios/Sources/LonghouseApp/LoginView.swift` — already collapsed
  in Phase 0. Phase 4 stores the returned token in the per-host
  Keychain entry.
- `ios/Sources/Shared/LonghouseAPI.swift:645-663` — swap
  `cookieHeader` injection for `Authorization: Bearer`. The
  `/api/auth/refresh` retry path becomes a CP `/api/auth/refresh`
  call (deferred refresh logic; if absent, retry returns 401 and
  the app calls `appState.logout()`).
- `ios/Sources/Shared/SessionWorkspaceStream.swift:209-217` and
  `ios/Sources/Shared/TimelineSessionsStream.swift:123-135` — both
  attach `Authorization: Bearer` to the request headers.
- Widgets and push paths that create `LonghouseAPI(host:)` read the
  same per-host bearer.

Done when: iOS can sign in, navigate, see live SSE updates, and
stay signed in across app restarts using only the bearer token.
The per-host Keychain storage supports multiple hosted tenants
when the CP schema allows it, but v1 only ships with one
hosted tenant per CP user. Self-host builds (e.g. `localhost`)
still use cookies via the same `SharedAuthStore` abstraction.

Phase 4 also requires a coordinated CP-side change. The current
`/auth/native/open-instance` route
(`control-plane/control_plane/routers/auth.py:744-773`) mints
the legacy HS256 `instance_sso_token` and 302s iOS to
`ai.longhouse.ios://auth-callback?...&sso_token=...`. The CP
must update this route to issue a CP runtime JWT (not the legacy
HS256 bridge token) so iOS receives a token that the tenant's new
bearer-Keychain path can store. The CP change is a single-route
edit, not a phase — it can ship in Phase 1 with the rest of the
CP identity-provider work. Document in the CP code that iOS is
the only consumer that gets the runtime JWT in a deep link.
Android and future native clients are out of scope for this
epic; when they arrive, they should use the handoff-code path
over a server-side channel, not a deep link.

### Phase 5 — Delete hosted local auth + refresh (1-2 weeks)

- **`server/zerg/routers/auth_sso.py:_accept_token` stops accepting
  HS256 legacy bridge tokens** when `control_plane_identity: true`. The
  dual-verify path goes away. `accept-token?token=...` returns 410
  Gone for legacy tokens; only the handoff-code path (`accept-handoff`)
  remains. The HS256 fallback stays for self-host only.
- `server/zerg/routers/auth_browser.py` — when `CONTROL_PLANE_URL`
  is set and `control_plane_identity: true`, force `google: false` and
  `password: false` from `/api/auth/methods`. `/api/auth/google`,
  `/api/auth/password`, `/api/auth/dev-login` return 410 Gone.
  `/api/auth/logout`, `/api/auth/status`, and the SSE-auth
  read paths stay (cookie clearing, status, SSE handshake) and
  route through the new strategy. `/api/auth/refresh` returns 410
  Gone until refresh is implemented on the CP, then becomes a CP
  proxy.
- `server/zerg/auth/session_tokens.py` — the local HS256 mint
  helpers become self-host-only behind the strategy selection
  check.
- `control_plane/services/provisioner.py:199-200` — stop writing
  `LONGHOUSE_PASSWORD` to hosted tenant env. The hosted Google
  credentials for Gmail integration (`provisioner.py:202-209`) stay.
- `control_plane/routers/auth.py` gains a refresh-token handler
  that mints a new runtime JWT from a stored refresh-token record.
  Tenant proxies `/api/auth/refresh` to the CP.
- **`CONTROL_PLANE_IDENTITY` flag disappears from the runtime**
  once the legacy bridge is gone. The runtime check becomes
  simply `if settings.control_plane_url: hosted else: self_host`.
  The provisioner stops writing the flag. The
  `control_plane_identity` setting is removed from
  `server/zerg/config/__init__.py`.
- Self-host code paths stay. The runtime check is
  `if settings.control_plane_url: hosted else: self_host`.

Done when: hosted tenant has zero local auth state. Self-host
tenants are bit-for-bit unchanged. Refresh-token rotation works
on the CP and the tenant proxies it. The
`CONTROL_PLANE_IDENTITY` flag no longer exists.

## First vertical slice — exact ship checklist (Phase 0)

This is what David dogfoods end-to-end before any later phase lands.

1. CP deploys `/auth/start` route and `tenant`-aware OAuth state.
   Old `/dashboard/open-instance` keeps working. No new token
   shape. No JWKS. No `CONTROL_PLANE_IDENTITY` flag.
2. Runtime deploys thin `LoginPage` interstitial and updated
   `/api/auth/methods` response shape. Old bridge acceptance still
   works. The bridge still mints the same HS256 `instance_sso_token`.
3. iOS ships SSO-only `LoginView`. The deep link and bridge token
   flow are unchanged.
4. David signs in to `david010.longhouse.ai` from a clean browser
   session. Records: how many distinct "Longhouse" login pages did
   he see? (Goal: 1.) How long did the round-trip take? (Goal:
   <2s.)
5. David signs in from iOS. Records: same.
6. David signs out, then signs back in. Records: same.

If 1 = 1, the front-of-funnel is done. Move to Phase 1.

## Tests to write

CP:

- `/auth/start` unauthenticated → CP login with `tenant` preserved
  in OAuth state.
- Google and GitHub callbacks with `tenant` state → existing
  `/dashboard/open-instance` (or `/auth/native/open-instance` for
  iOS) flow continues to work.
- `/auth/start` rejects unknown `tenant` (302 to `longhouse.ai`).
- `/auth/start` rejects `tenant` not owned by the signed-in user.
- `GET /api/identity/jwks.json` returns the active and accepted
  public keys with `kid` and `alg`.
- A token minted with one key verifies against the JWKS; a token
  signed by an unknown `kid` triggers refetch and then fails
  closed.
- Keyset rotation: minting a token with a new `kid` succeeds, the
  old key remains published for `max_token_ttl` seconds, then
  expires.
- `POST /api/identity/exchange-handoff` with a valid handoff code
  returns the runtime JWT and marks the handoff row
  `consumed=true`; a second call with the same `code` returns
  410 Gone.
- Handoff code with wrong `aud`, wrong `tenant`, or wrong `sub`
  fails.
- Handoff code lifetime: 60 seconds, no longer.

Tenant:

- `cp_jwks.verify` rejects expired token, wrong `aud`, wrong
  `iss`, unknown `kid`, bad signature, missing `email_verified`.
- `accept-handoff` calls CP exchange, stores the runtime JWT as
  `longhouse_session`, links the local user by `cp_user_id` on
  first SSO, sets `cp_user_id` if missing on subsequent SSO for
  the same email, and does not change `users.id`.
- `accept-token` legacy HS256 path still works for
  `control_plane_identity: false` hosted tenants and self-host.
- `accept-handoff` account-link conflict: a new `email` claim
  collides with another local user's email — keep the old
  cached email and emit an `account_link_conflict` log event.
- `/api/auth/status` returns `authenticated: false` after tenant
  cookie expiry even if CP `cp_session` is still present.
- `/api/timeline/sessions`, `/api/agents/...`, SSE all work with
  the CP JWT cookie.
- `browser_auth`, `browser_route_auth`, and `validate_ws_token`
  accept the CP JWT in hosted mode for the timeline and SSE
  routes.
- `zdt_*` device-token bearer still works and is not parsed as
  a CP JWT.
- Self-host password login still works with `CONTROL_PLANE_URL`
  unset (regression guard).
- Hosted `/api/auth/methods` advertises only `{sso, sso_url,
  sso_login_url}`. No `google`, no `password`.
- Hosted `/api/auth/password` returns 410 Gone.
- Hosted `/api/auth/refresh` returns 410 Gone (until Phase 5).
- Hosted tenant `/api/auth/logout` clears `longhouse_session`
  even when CP `/auth/logout` fails.
- CP down: existing cached JWKS + mapped user works until `exp`;
  unknown `kid` fails closed.
- Handoff code cannot be replayed; rejects wrong `tenant` /
  `audience`; rejects expired code.
- **`POST /api/identity/exchange-handoff` requires a valid
  `X-Internal-Token` header.** A request with the right code
  but wrong/missing token returns 401, not the runtime JWT.
- **`POST /api/identity/runtime-token` (CP-internal mint) is not
  exposed publicly** — it requires the CP admin/internal auth.
  Verified by integration test that an unauthenticated request
  gets 401/403.
- **Anti-CSRF: tenant `accept-handoff` rejects requests where
  the `tenant_state` parameter does not match the
  `tenant_login_state` cookie, or where the cookie is missing.**
  Both 403 with a `tenant_login_csrf_*` log event. A successful
  login always clears the `tenant_login_state` cookie.
- **`email_verified=false` is a valid runtime JWT claim and
  produces a valid session.** The tenant UI shows the existing
  "verify your email" banner; the session is otherwise accepted.
  Verified by integration test that mints with `email_verified:
  false` and confirms a successful timeline load.
- **`control_plane_identity: false` (default for new tenants
  before the flag flip) keeps the legacy HS256-only behavior**
  even after the dual-verification code is deployed. Verified by
  setting the flag off, attempting the new handoff flow, and
  confirming the runtime falls back to legacy bridge acceptance.
- **Old bridge stop-acceptance (Phase 5).** When
  `control_plane_identity: true`, `accept-token?token=...` with
  an HS256 legacy bridge token returns 410 Gone. The
  `accept-handoff?code=...` path is the only valid path.
- **Old bridge stop-issuance (Phase 5).** CP
  `/dashboard/open-instance` returns 410 Gone for tenants with
  `control_plane_identity: true` after the bridge code is
  deleted.
- **CP user deleted at the CP.** Tenant cookie still validates
  the runtime JWT until `exp` (1 hour). The tenant does not
  block the user. The user is told at the CP level that the
  account is gone; the tenant session is naturally
  short-lived. Document the behavior; no test enforces it
  because the CP user-deletion flow is out of scope for this
  epic.

iOS:

- `LonghouseAPI.data()` sends `Authorization: Bearer` when a
  per-host token exists; falls back to cookies only for self-host
  builds.
- `LonghouseAPI.data()` retries CP refresh on 401, calls
  `appState.logout()` if CP refresh also 401s.
- `SessionWorkspaceStream` and `TimelineSessionsStream` both
  send bearer in hosted mode.
- `SharedAuthStore` keyed-by-host store: two hosted tenants
  don't share a bearer; logout of one doesn't log out the other.
- `SharedAuthStore.clear(host:)` removes the per-host Keychain
  entry and zeros the in-memory token.
- `AppState.restoreSession()` works from per-host bearer with
  no cookies.
- Widgets and push paths read the same per-host bearer.

## Deploy sequence

Two repos + native clients + JWKS rotation = use flags even at
zero users. The blast radius for a buggy CP auth change is "every
hosted tenant can't sign in," and we don't want to debug that on a
Friday afternoon.

1. **Deploy CP with `/auth/start`, `tenant`-aware OAuth state.**
   No new token shape. No JWKS yet. Old `/dashboard/open-instance`
   keeps working. No flag needed yet — `/auth/start` is purely
   additive.
2. **Deploy runtime thin `LoginPage` and updated
   `/api/auth/methods`.** Old bridge still mints HS256. The
   runtime continues to verify HS256 bridge tokens exactly as
   today. No flag needed.
3. **Ship iOS SSO-only `LoginView`.** No protocol change; the
   existing deep link flow is the same.
4. **David dogfoods web and iOS for ≥1 week.** This is Phase 0
   done.
5. **Deploy CP `/api/identity/*` and shared keyset.** New
   `PyJWT[crypto]` dep. JWKS published. Handoff code mint +
   exchange route. New CP deploys add a new public key but keep
   the old one published for `max_token_ttl` seconds. The CP
   `/dashboard/open-instance` path is unchanged — it still mints
   HS256 bridge tokens.
6. **Deploy runtime dual-verification.** Per-tenant feature flag
   `control_plane_identity: bool` is added to the tenant's env
   (sourced from `settings.control_plane_identity` in
   `server/zerg/config/__init__.py`, default `False`). The
   provisioner sets it to `True` for any new hosted tenant after
   the deploy ships. For the first dogfood tenant, set it
   manually. When `False`, runtime accepts only the legacy HS256
   bridge token, mints a local HS256 session, behavior is
   identical to today. When `True`, runtime accepts both HS256
   bridge (legacy) and CP runtime JWTs (new), and sets the
   runtime JWT directly as `longhouse_session` for the new
   path. The handoff-code `/api/auth/accept-handoff` endpoint is
   wired up but the CP still issues HS256 bridge tokens. The
   flag is removed in Phase 5 once the legacy bridge is
   deleted.
7. **Flip CP `/dashboard/open-instance` for the flagged tenant
   to issue a handoff code instead of an HS256 bridge token.**
   Runtime accepts the new path.
8. **Flip only `david010.longhouse.ai` to
   `control_plane_identity: true`.** The CP open-instance for
   that tenant issues handoff codes. The runtime exchanges them
   for runtime JWTs and sets them as `longhouse_session`.
9. **Dogfood web:** login, status, timeline, SSE, logout, switch
   account, password reset, signup, OAuth, all the flows.
10. **Ship iOS bearer build.** Dogfood: hosted login, timeline,
    SSE, APNs registration, widget.
11. **Flip all hosted tenants** to `control_plane_identity:
    true`. All-at-once is fine at zero users.
12. **Remove hosted tenant password injection in
    `control_plane/services/provisioner.py:199-200`.** Hosted
    Google credentials for Gmail integration stay.
13. **Delete the legacy HS256 bridge code from the CP and the
    runtime.** Only when every dogfooded build has been on CP
    JWTs for ≥2 weeks.

## DB migration reality

There is no Alembic on the runtime. Migrations live as imperative
`_migrate_*` functions in `server/zerg/database.py` plus a
`_auto_add_missing_columns()` helper that derives
`ALTER TABLE ADD COLUMN` from SQLAlchemy model metadata at startup.
`_auto_add_missing_columns` **explicitly skips** `unique=True`
columns (`server/zerg/database.py:1709-1710`).

For this spec:

- The `users.cp_user_id` column addition: **do not** rely on
  `_auto_add_missing_columns`. The model field must be
  `Column(Integer, nullable=True, index=True)` without `unique=True`.
  Add an explicit imperative migration
  `_migrate_users_cp_user_id()` that runs at startup *before* any
  code can query by `User.cp_user_id`:
  - `ALTER TABLE users ADD COLUMN cp_user_id INTEGER` (if missing).
  - `CREATE UNIQUE INDEX IF NOT EXISTS uq_users_cp_user_id
     ON users(cp_user_id) WHERE cp_user_id IS NOT NULL` (SQLite
     partial unique index).
- Backfill: for hosted tenants, on first SSO after the new code
  ships, the `accept-handoff` handler populates `cp_user_id` from
  the runtime JWT. There is no batch backfill step.
- Self-host: `cp_user_id` stays NULL forever. `accept-handoff` is
  never called in self-host mode. The column is just unused.
- The `users` table itself is not dropped, not renamed, not
  migrated in shape. Just one new column.
- CP `User` model in `control-plane/control_plane/models.py` gains
  `display_name` and `avatar_url` columns (Phase 1). These are
  nullable. Imperative migration alongside.

## What we explicitly don't do

- **Don't rewrite the FKs.** Sessions, messages, agents, etc. keep
  pointing at `users.id`. The local id is the resource-owner key.
  CP identity is *adjacent*, not *replacing*.
- **Don't share cookies across subdomains.** `Domain=.longhouse.ai`
  on `cp_session` is a footgun. Tenants get their own cookies.
- **Don't put the runtime JWT in a URL.** Browser handoff uses a
  handoff code, exchanged server-to-server.
- **Don't add MFA / passkeys / SAML in this epic.** They land on
  the CP only, after this ships, in a separate spec.
- **Don't change self-host mode.** `JWTAuthStrategy` and local
  password auth stay bit-for-bit identical. The CP/JWKS verifier is
  selected only for hosted tenants.
- **Don't add a multi-tenant `aud: longhouse-multi-tenant`.** CP
  schema forbids one-user-many-instances today. Multi-instance
  per user is a separate spec.
- **Don't add in-memory `jti` revocation.** v1 runtime tokens are
  valid until `exp`. Durable revocation is deferred.
- **Don't batch-update existing `cp_session` cookies.** The token
  rotation is natural — old cookies expire on their own. Let it
  happen.

## Tradeoffs we're accepting

- **Tenants and CP are now coupled at deploy time for hosted
  tenants.** A bug in the CP's JWKS publication or keyset
  rotation breaks every hosted tenant's sign-in. Today the blast
  radius is the CP login. After: it includes every existing
  session too. This is the *right* tradeoff — the alternative is
  two identity systems forever — but it's real.
- **v1 runtime JWTs have no global revocation.** A token issued by
  the CP is valid until `exp` even if the CP user is deleted or
  the user logs out everywhere. The cost is a 1-hour window
  where a stolen cookie still works. The benefit is a system
  that works when the CP is briefly unreachable. Durable global
  revocation is a follow-up spec.
- **Self-host and hosted share a runtime but not an auth
  strategy.** The strategy selection at startup is the seam.
  Adding a third strategy later is straightforward; the
  abstraction is a class with a small interface
  (`get_current_user`, `validate_ws_token`, `verify_token`).
- **iOS app loses its cookie-based read of the tenant session.**
  The app is now bearer-first. This is consistent with how
  native apps should work, but it is a behavior change for any
  iOS code that inspected cookies for auth state. Audit pass
  during Phase 4.

## Why now

We are pre-launch with zero external users. The current
architecture is correct enough to dogfood but the wrong shape to
scale. Every auth feature we add in 2026 lands in two places if
we don't fix this. The 3.5-5 week cost is the cheapest it will
ever be. The risk of doing this with users on the platform is a
force-migration that touches every authed client, every tenant,
every iOS build. The cost of *not* doing it grows with every
ship.

## Locked design decisions

These are the answers to the open questions raised during the
draft. If you want to revisit any of them, do it before Phase 0
ships.

1. **RS256 keyset management.** Shared CP keyset from a single
   config/secret source, not per-process keys. The CP publishes
   the active and previous public keys in JWKS. New deploys add
   the new public key but keep the old one published for
   `max_token_ttl` seconds (1 hour by default). Automatic
   rotation is not in v1; the manual procedure is "add a new
   key, deploy, wait `max_token_ttl`, remove the old key."

2. **`aud` shape.** Always exactly the tenant's `INSTANCE_ID`
   for hosted. There is no `longhouse-multi-tenant` value, and
   the runtime token shape defined in this spec does not apply
   to self-host (which keeps its existing local HS256 JWT
   shape). A token with the wrong `aud` for the verifying
   tenant is rejected.

3. **Two hosted tenants per CP user.** Out of scope. CP
   `Instance.user_id` is unique today. The iOS Keychain becomes
   per-host in Phase 4 to support this when the CP schema
   changes, but the v1 spec does not promise it works.

4. **`email_verified` claim.** Required boolean in the runtime
   JWT. The tenant must not re-verify email and must trust the
   CP. v1 mints runtime JWTs for unverified users (mirroring
   today's CP session cookie behavior). The tenant UI surfaces
   a "verify your email" banner when the claim is `false`;
   the session is otherwise valid. The tenant
   `users.email_verified` column becomes redundant on hosted
   tenants; deletion is a follow-up cleanup in a later spec.

5. **Handoff model.** The cross-server browser handoff uses an
   *opaque one-use code*, not a signed handoff JWT. The CP
   stores the handoff state (cp_user_id, profile snapshot,
   tenant subdomain, return_to, tenant_state CSRF nonce, 60-second
   expiry) in a server-side table keyed by the code. The
   tenant exchanges the code with the CP server-to-server
   using the `X-Internal-Token` header (same auth scheme as
    the existing Gmail handoff). The runtime JWT is never in a
    URL. iOS receives the runtime JWT in the custom-scheme deep
    link because the iOS app cannot hold the instance-internal
    secret; iOS uses its own local CSRF binding (a
    `tenant_state` value stored in `UserDefaults` and verified
    on the deep-link return) instead of the browser's cookie
    binding. Anti-CSRF for the browser is enforced by a
    tenant-side `tenant_login_state` cookie that the tenant
    sets in a server route before redirecting to the CP, and
    verifies on `accept-handoff` return. The CSRF binding is
    enforced by a server route, not the React `LoginPage`,
    because `HttpOnly` cookies cannot be set from JavaScript.

6. **Logout.** Hosted tenant `/api/auth/logout` stays as a
   cookie-clearing endpoint. The web app calls tenant logout
   first, then optionally redirects to CP `/auth/logout` to
   clear `cp_session`. CP logout cannot clear tenant cookies.

7. **`CONTROL_PLANE_IDENTITY` flag lifecycle.** Introduced in
   Phase 2 as a per-tenant env setting (`control_plane_identity:
   bool`) on hosted tenants. Removed in Phase 5 once the
   legacy HS256 bridge is deleted; the runtime check then
   becomes simply `if settings.control_plane_url: hosted else:
   self_host`. The provisioner sets the flag for new hosted
   tenants during Phase 2-4 and stops writing it in Phase 5.
