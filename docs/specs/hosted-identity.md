# Hosted Identity: One Longhouse Account

Status: Phase 0 dogfooded; revised for clean-break prelaunch implementation
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

Phase 0 already fixed the visible hosted login funnel by routing tenant
login through the CP. David dogfooded that flow successfully. The
remaining work should not preserve a long hosted compatibility window:
there are no external users, and this is the cheapest moment to make the
final shape clean. Preserve hosted product data; do not preserve legacy
hosted auth behavior.

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
- Refresh-token rotation on the CP. Access tokens first. Until CP
  refresh exists, hosted `/api/auth/refresh` returns 410 Gone and
  clients reauthenticate through the CP.
- Multi-tenant per CP user. CP `Instance.user_id` is unique today; one
  CP user has exactly one hosted instance. Multi-instance per user
  requires a CP schema change and is out of scope.
- Durable global revocation. v1 runtime JWTs are valid until `exp`
  even after the CP-side session is cleared. Real revocation is a
  later shared durable-store design.

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

4. **Only the CP can sign hosted runtime identity.** The RS256
   private key never ships to a tenant runtime, tenant env var, native
   client, or build artifact. Tenants receive public JWKS material
   only. `aud == INSTANCE_ID` is an authorization-scope check; the
   cryptographic boundary is that only the CP holds signing keys.

5. **Handoff is a code, not a token.** The cross-server browser
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
  at 143-156) is replaced for hosted tenants by a one-use handoff-code
  exchange. Hosted tenants set the CP runtime JWT directly as
  `longhouse_session`. Legacy HS256 bridge token acceptance is removed
  for hosted tenants instead of kept behind a long dual-verify window.
- The existing tenant login routes in
  `server/zerg/routers/auth_browser.py` stay for self-host, but hosted
  mode disables local login routes and keeps only CP SSO, status,
  logout, and transport-auth helpers. Hosted tenants respond from
  `GET /api/auth/methods` with `{sso_url}` or redirect to `/auth/start`
  on the CP. Self-host tenants keep `{google, password}`.

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
  handoff flow populates this on first CP SSO login. It is the link
  back to the CP identity. It is **not** `unique=True` on the column
  itself because the runtime's `_auto_add_missing_columns` skips
  `unique=True` columns; the uniqueness is enforced by a partial index
  added by an imperative migration (see "DB migration reality").
- Keep `users.email`, `users.display_name`, `users.avatar_url`. Treat
  them as a local cache populated from the CP JWT. They are not the
  source of truth — the CP is. On cache update, if the new email is
  already owned by a different local user, keep the old cached email
  and log an `account_link_conflict` event; never silently overwrite.
- Drop `users.provider`, `users.provider_user_id`, `users.is_active` for
  hosted tenants. The CP owns these. Self-host mode keeps them.
- If `users.is_active` has other meanings in the codebase, keep it
  but in hosted mode it is always true and write-locked.

The current bridge code in `server/zerg/routers/auth_sso.py:62-100`
upserts by email. After this spec, hosted auth no longer uses that
bridge. The CP handoff path always carries a `sub`, so hosted lookup is
`users.cp_user_id = int(sub)` first. Email fallback is a one-time
linking aid for David's existing hosted tenant user and is allowed only
when the CP runtime JWT has `email_verified=true`. An unverified CP user
with an email matching an existing local tenant user must not adopt that
local user; create a fresh local user or reject with a logged
`account_link_conflict`. If a verified email maps to a different local
user during profile refresh, keep the old cached value and log
`account_link_conflict`.

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
- Lifetime: 1 hour access. CP refresh is not part of this clean-break
  slice. Until CP refresh exists, hosted users whose runtime JWT expires
  are sent back through CP SSO; if the CP `cp_session` cookie is still
  valid this should usually be a quick redirect, not a visible login.
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
- `settings.identity_signing_keys` is a JSON config/secret value:
  `{"active_kid":"2026-06-a","keys":[{"kid":"2026-06-a","status":"active","private_pem":"...","public_pem":"..."},{"kid":"2026-05-a","status":"accepted","public_pem":"..."}]}`.
  Only the CP reads `private_pem`. JWKS is derived from public keys.

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
  server host. Each hosted tenant gets its own keychain entry.
- Every API call sends `Authorization: Bearer <token>` from the
  Keychain. Self-host can keep the existing cookie path behind the
  shared auth-store abstraction, but hosted iOS is bearer-only.
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
  `if settings.control_plane_url: hosted else: self_host`.
- The `cp_jwks` module is selected only for hosted tenants with
  `CONTROL_PLANE_URL` set. The hosted/runtime token shape defined in
  this spec does not apply to self-host.
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
base64url-encoded. The CP stores only `code_hash` in a durable CP table,
using the same posture as refresh-token hashing. A CP database read must
not reveal live handoff codes.
The table row carries:

- `code_hash` (indexed)
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

Current hosted tenants share the same instance-internal secret. That
secret is a coarse internal gate, not a per-tenant proof. The one-use
256-bit code, 60-second expiry, bound tenant/instance fields, and
server-side consumption are the real handoff protections. The exchange
request includes the caller's tenant subdomain or instance id; CP
verifies it matches the handoff row before returning a runtime JWT.

### Anti-CSRF binding (tenant-side state)

To prevent a third-party site from initiating a CP login that
silently binds the visitor's browser to an attacker-chosen tenant
session, the tenant mints its own CSRF nonce before redirecting
the user to the CP. The cookie must be set by a server route
(browsers cannot set `HttpOnly` from JS), so the flow is:

1. Browser hits the existing
   `GET /api/auth/start-handoff?tenant=...&return_to=...` route on the
   tenant. Phase 0 created this route without CSRF state; this
   implementation modifies it.
2. The server route generates `tenant_state` (256-bit random),
   stores it in a short-lived `tenant_login_state` cookie scoped
   to the tenant domain, `HttpOnly; Secure; SameSite=Lax;
   Max-Age=600`, and 302s to
   `https://control.longhouse.ai/auth/start?tenant=...&return_to=...&tenant_state=...`.
3. CP `/auth/start`, `_issue_login_return_state`,
   `_decode_login_return_state`, and all OAuth callbacks round-trip
   `tenant_state` through the OAuth state JWT and back to the tenant.
   The CP does not need to validate it (the CP doesn't know about the
   tenant's cookies); the tenant validates it on the final
   `accept-handoff` return.
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

1. iOS generates `tenant_state` (256-bit random) in the app process
   and stores it in a transient `KeychainHelper` entry before opening
   the `ASWebAuthenticationSession`. `UserDefaults` is acceptable only
   for non-secret fallback flow state if the Keychain path becomes
   awkward; Keychain is the default.
2. iOS opens `https://control.longhouse.ai/auth/native/open-instance?tenant=...&return_to=...&tenant_state=...`.
3. CP `/auth/native/open-instance` round-trips `tenant_state`
   through the OAuth state and includes it in the iOS deep
   link callback:
   `ai.longhouse.ios://auth-callback?instance_url=...&tenant_state=...&sso_token=...`.
   The `sso_token` value becomes the CP runtime JWT. Keep
   `instance_url` in the callback so iOS can key the bearer by host.
4. iOS compares the returned `tenant_state` to the locally
   stored value. Mismatch → discard the JWT, surface a login
   error. Missing local value → same.

This is the same CSRF shape as the browser, just with a transient
Keychain entry instead of a cookie. An attacker who tricks the user
into a forged `auth-callback` URL has the wrong `tenant_state` and is
rejected.

**Do not** put the instance-internal secret in any native
app. Native apps are public clients. The browser tenant holds
the secret because the secret is in a server-side
configuration, not in user-accessible code. iOS gets the JWT
in a deep link because it cannot hold a server-side secret
safely. This asymmetry is intentional and is a comment in
the iOS code.

### Handoff secret logging

Any CP or tenant log line that captures URL query strings must
redact `code`, `token`, `sso_token`, and `state` parameters. Add a
`redact_url()` helper used by all request log lines. The existing
`cp_session` cookie is already treated as sensitive; treat handoff codes
and runtime tokens the same way.

## Clean-break implementation plan

Phase 0 is historical and accepted: tenant login now funnels through a
single CP login page. The remaining rollout is a clean break for hosted
identity, implemented in repo-sized chunks with short-lived deploy
ordering, not compatibility phases.

### Chunk A — CP becomes the hosted identity provider

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
- New durable CP `HandoffCode` model/table. It stores the opaque code
  hash, CP user id, instance id, tenant subdomain, profile snapshot,
  return target, tenant CSRF state, expiry, and consumed timestamp.
- New `control_plane/routers/identity_api.py`. Routes under
  `/api/identity/*`:
  - `GET /api/identity/jwks.json` — public JWKS, returns all accepted
    public keys.
  - `POST /api/identity/exchange-handoff` — tenant server-to-server
    call to swap a handoff code for a runtime JWT. Marks the
    handoff row consumed. Requires `X-Internal-Token:
    <instance_internal_api_secret>` (the same scheme as the
    existing Gmail handoff). Returns the runtime JWT plus the
    standard claim set.
  - `POST /api/identity/runtime-token` — CP-internal/test-only helper
    for minting a runtime JWT directly.
- Add `display_name` and `avatar_url` columns to CP `User` model in
  `control-plane/control_plane/models.py`. Populate from
  Google/GitHub userinfo in the OAuth callbacks. Make them optional
  and nullable. Add the CP imperative migration alongside.
- `control-plane/pyproject.toml` adds `PyJWT[crypto]` (or equivalent)
  for RS256 signing.
- CP `/auth/start` and OAuth callbacks issue handoff codes, not
  HS256 bridge tokens, for hosted browser login.
- CP `/auth/native/open-instance` issues a CP runtime JWT for iOS
  deep-link completion. Native apps are public clients, so they never
  receive the instance-internal exchange secret.

Done when: a non-tenant script can `curl` the CP JWKS, exchange a
handoff code for a runtime JWT via `/api/identity/exchange-handoff`,
and the JWT verifies against the JWKS.

### Chunk B — Runtime becomes a CP-token resource server for hosted

- New `server/zerg/auth/cp_jwks.py`. Fetches and caches the CP's
  JWKS from `/api/identity/jwks.json`, verifies tokens, handles `kid`
  rotation (refetch on unknown `kid`, fail closed after a max age),
  exposes `verify_runtime_token(token, audience=INSTANCE_ID) ->
  TokenClaims`.
- New `server/zerg/auth/runtime_strategies.py` (or similar) that
  selects the auth strategy at startup:
  - Hosted with `CONTROL_PLANE_URL` set: `HostedCPAuthStrategy`,
    implements `get_current_user`, `validate_ws_token`, browser
    cookie auth via `browser_auth`, and bearer auth for iOS on
    browser-owned API/SSE routes. Preserves `zdt_*` device-token
    handling (must not be parsed as a CP JWT — checked before the
    `cp_jwks` verify).
  - Self-host with `CONTROL_PLANE_URL` unset: `LocalJWTAuthStrategy`,
    the existing `server/zerg/auth/strategy.py` behavior, unchanged.
- Migration: add `users.cp_user_id` (see "DB migration reality").
  Imperative only. Backfill: none, populated on first CP SSO.
- Add `GET /api/auth/accept-handoff?code=...&tenant_state=...`.
  Runtime validates the `tenant_login_state` cookie, calls CP
  `/api/identity/exchange-handoff` server-to-server with the code and
  caller tenant/instance id, verifies the returned runtime JWT against
  CP JWKS, links/upserts the local user by `cp_user_id`, sets
  `longhouse_session` to the CP JWT, clears any stale
  `longhouse_refresh` cookie and the CSRF cookie, and redirects to the
  tenant-local `return_to`.
- Hosted `GET/POST /api/auth/accept-token` returns 410 Gone. It is
  not a compatibility bridge. If any self-host-only local SSO use
  remains, keep it explicitly selected by the local strategy and
  unreachable in hosted mode.
- Hosted `/api/auth/methods` advertises only CP SSO. Hosted
  `/api/auth/google`, `/api/auth/password`, `/api/auth/dev-login`,
  and `/api/auth/refresh` return 410 Gone. `/api/auth/logout`,
  `/api/auth/status`, and SSE/WebSocket auth stay and route through
  the hosted strategy.
- Hosted `/api/auth/status` returns `email_verified` in the user
  payload. The tenant verify-email banner is new UI work; it is not the
  existing CP dashboard banner.
- All hosted entry points funnel through the same CP verifier:
  `get_current_user`, browser cookie auth, `validate_ws_token`,
  WebSocket handshakes, SSE, and bearer auth must enforce signature,
  `kid`, `iss`, `aud`, `exp`, and required claims identically.
- Hosted mode rejects CP JWTs supplied through `?token=` query
  parameters. Cookie auth and `Authorization: Bearer` are valid hosted
  browser/native paths; query tokens are limited to narrow machine or
  device-token cases where the credential is not a CP runtime JWT.
- Upsert the local user by `cp_user_id` first. Email fallback is only
  for linking the first CP login to David's existing hosted tenant user,
  and only when `email_verified=true`. If the CP email collides with a
  different local user, or the matching CP email is unverified, keep the
  old cached email and emit `account_link_conflict`.
- Hosted user upsert sets `is_active=True`; hosted CP identity owns
  account disablement, and stale local `is_active=False` must not lock
  David out after the clean break.
- Keep `zdt_*` device token handling untouched. The CP JWT verifier
  is tried only after device-token handling declines the credential.
- Smoke-only routes such as `/api/auth/service-login` remain
  self-host/dev-test helpers. Hosted runtimes must not use them to mint
  local HS256 browser sessions.

Done when: a hosted tenant accepts handoff codes from the browser, sets
the CP runtime JWT directly as `longhouse_session`, serves the timeline
and SSE routes with that cookie, rejects hosted local auth routes, and
self-host tenants are bit-for-bit unchanged.

### Chunk C — iOS uses hosted bearer auth

iOS auth is a package, not a one-line swap. Touch:

- `ios/Sources/Shared/KeychainHelper.swift` — extend so that the
  account key includes the normalized server host. Existing global
  `longhouse_auth_token` migrates on first read to the new
  per-host key.
- `ios/Sources/Shared/SharedAuthStore.swift` — store hosted bearer
  tokens in Keychain, indexed by host. Keep the local cookie path for
  self-host through the same abstraction. Add `clear(host:)` for logout.
- `ios/Sources/LonghouseApp/LonghouseApp.swift` —
  `restoreSession()` checks per-host bearer presence and verifies
  via tenant `/api/auth/status`. Hosted sign-in completion stores
  the returned runtime bearer directly. Logout posts tenant
  `/api/auth/logout` and clears per-host Keychain entries.
- `ios/Sources/Shared/LonghouseAPI.swift` — attach
  `Authorization: Bearer` for hosted requests. Without CP refresh,
  401/410 refresh responses log the app out and send the user back
  through hosted sign-in.
- `ios/Sources/Shared/SessionWorkspaceStream.swift` and
  `ios/Sources/Shared/TimelineSessionsStream.swift` attach
  `Authorization: Bearer` for hosted SSE.
- Widgets and push paths that create `LonghouseAPI(host:)` read the
  same per-host bearer.

Done when: iOS can sign in, navigate, see live SSE updates, register
for push, and stay signed in across app restarts using only the hosted
bearer token. Self-host builds such as `localhost` still use cookies.

The runtime deploy is a flag day for hosted iOS: once hosted
`accept-token` and `/api/auth/refresh` return 410, any iOS build still
using the cookie path can no longer sign in or refresh. That is
acceptable only because there are zero external users and David can
install the bearer build in lockstep.

### Chunk D — Delete dead hosted auth and provisioning behavior

- `control_plane/services/provisioner.py` stops writing
  `LONGHOUSE_PASSWORD` to hosted tenant env. Hosted Google
  credentials for Gmail integration stay.
- Remove CP issuance of hosted HS256 bridge tokens. CP
  `/dashboard/open-instance` and `/auth/start` both use the handoff
  code path for browser login.
- Remove any runtime `control_plane_identity` setting or flag. Hosted
  is selected by `CONTROL_PLANE_URL`; self-host by its absence.
- Keep local HS256 session helpers only for self-host.

Done when: hosted tenant has zero local auth state. Self-host tenants
are unchanged. The legacy hosted bridge cannot mint or accept tokens.

## Phase 0 acceptance

Phase 0 shipped before this clean-break revision. David dogfooded
hosted web sign-in, sign-out, and sign-in again on
`david010.longhouse.ai` and found no user-facing issues. The visible
login funnel is accepted. Do not continue extending the HS256 hosted
bridge; replace it with the CP identity-provider flow below.

## Tests to write

CP:

- `/auth/start` unauthenticated → CP login with `tenant` preserved
  in OAuth state.
- Google and GitHub callbacks with `tenant` state → browser handoff
  code path for web or CP runtime JWT deep link for iOS.
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
- Hosted `accept-token` returns 410 Gone for HS256 bridge tokens.
- `accept-handoff` account-link conflict: a new `email` claim
  collides with another local user's email — keep the old
  cached email and emit an `account_link_conflict` log event.
- Unverified email fallback guard: an unverified CP user whose email
  matches an existing local tenant user does not link to that existing
  user and emits `account_link_conflict`.
- `/api/auth/status` returns `authenticated: false` after tenant
  cookie expiry even if CP `cp_session` is still present.
- `/api/auth/status` returns `email_verified` for hosted users.
- `/api/timeline/sessions`, `/api/agents/...`, SSE all work with
  the CP JWT cookie.
- Browser cookie auth, bearer auth, and `validate_ws_token` accept the
  CP JWT in hosted mode for timeline, WebSocket, and SSE routes.
- WebSocket/SSE/cookie auth with a CP JWT whose `aud` belongs to a
  different tenant is rejected on every hosted entry point.
- CP JWTs supplied as `?token=` query parameters are rejected in hosted
  mode; the same route still accepts valid non-CP device/query-token
  credentials where supported.
- `zdt_*` device-token bearer still works and is not parsed as
  a CP JWT.
- Self-host password login still works with `CONTROL_PLANE_URL`
  unset (regression guard).
- Hosted `/api/auth/methods` advertises only `{sso, sso_url,
  sso_login_url}`. No `google`, no `password`.
- Hosted `/api/auth/password` returns 410 Gone.
- Hosted `/api/auth/refresh` returns 410 Gone until CP refresh exists.
- Hosted tenant `/api/auth/logout` clears `longhouse_session`
  even when CP `/auth/logout` fails.
- CP down: existing cached JWKS + mapped user works until `exp`;
  unknown `kid` fails closed.
- Handoff code cannot be replayed; rejects wrong `tenant` /
  `audience`; rejects expired code.
- **`POST /api/identity/exchange-handoff` requires a valid
  `X-Internal-Token` header.** A request with the right code
  but wrong/missing token returns 401, not the runtime JWT.
- **`POST /api/identity/exchange-handoff` validates the caller's
  tenant/instance id against the handoff row.** A request with a valid
  code but mismatched tenant returns 403.
- Tenant runtime has no RS256 private key material in settings or env;
  tests assert the hosted verifier is configured from JWKS/public keys
  only.
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
  produces a valid session.** The tenant UI shows a new "verify your
  email" banner; the session is otherwise accepted. Verified by
  integration test that mints with `email_verified: false` and confirms
  a successful timeline load.
- **No hosted compatibility flag.** With `CONTROL_PLANE_URL` set,
  hosted auth uses CP JWTs and rejects the legacy bridge. With
  `CONTROL_PLANE_URL` unset, self-host local auth is unchanged.
- **Old bridge stop-issuance.** CP `/dashboard/open-instance` and
  `/auth/start` issue handoff codes for browser login, never hosted
  HS256 bridge tokens.
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

Two repos + native clients + JWKS rotation still require sequencing,
but not a long-lived compatibility mode.

1. **Deploy CP identity provider.** Publish `/api/identity/jwks.json`,
   handoff-code mint/exchange, runtime JWT minting, and iOS runtime JWT
   deep links. CP continues to host the login page, but no longer
   issues hosted HS256 bridge tokens.
2. **Install or stage the iOS bearer build for David.** There is no App
   Store/TestFlight rollout buffer in this prelaunch plan; David's
   dogfood device moves with the runtime change.
3. **Deploy runtime CP verifier.** Hosted runtime requires CP JWTs,
   accepts handoff codes, sets `longhouse_session` to the CP JWT, and
   rejects hosted local auth routes. Self-host local auth remains
   selected by missing `CONTROL_PLANE_URL`. Existing hosted web/iOS
   `longhouse_session` cookies are invalidated and users must re-enter
   through CP SSO.
4. **Dogfood web and iOS.** Verify login, status, timeline, SSE,
   logout, switch account, password reset, signup, OAuth, APNs
   registration, and widget behavior.
5. **Delete dead hosted bridge/provisioner residue.** Remove hosted
   password injection and any unreachable hosted HS256 bridge code.

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
  `display_name` and `avatar_url` columns. These are nullable.
  Imperative migration alongside.
- CP `HandoffCode` is a new model/table created through the CP's
  `Base.metadata.create_all` startup path. Existing CP user columns
  such as `display_name` and `avatar_url` need explicit `ALTER TABLE
  cp_users ...` additions in the CP startup migration block, matching
  the existing `control_plane/main.py` pattern.

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
- **Hosted has no refresh in this slice.** The runtime JWT TTL is the
  hosted session TTL. With the default 1-hour access token, expired
  tenant sessions bounce through CP SSO again. This is a UX regression
  versus today's long-lived tenant refresh cookie, but it keeps the
  clean break small; CP refresh is the follow-up if dogfood says hourly
  reauth is too annoying.
- **Self-host and hosted share a runtime but not an auth
  strategy.** The strategy selection at startup is the seam.
  Adding a third strategy later is straightforward; the
  abstraction is a class with a small interface
  (`get_current_user`, `validate_ws_token`, `verify_token`).
- **Hosted iOS app loses its cookie-based read of the tenant session.**
  The app is now bearer-first. This is consistent with how
  native apps should work, but it is a behavior change for any
  iOS code that inspected cookies for auth state. Self-host iOS keeps
  the cookie path.

## Why now

We are pre-launch with zero external users. The current architecture is
correct enough to dogfood but the wrong shape to scale. Every auth
feature we add in 2026 lands in two places if we don't fix this. The
clean-break cost is the cheapest it will ever be. The risk of doing
this with users on the platform is a force-migration that touches every
authed client, every tenant, every iOS build. The cost of *not* doing it
grows with every ship.

## Locked design decisions

These are the answers to the open questions raised during the draft.
If you want to revisit any of them, do it before implementation ships.

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
   per-host to support this when the CP schema changes, but the v1
   spec does not promise it works.

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
   expiry) in a server-side table keyed by the code hash. The
   tenant exchanges the code with the CP server-to-server
   using the `X-Internal-Token` header (same auth scheme as
    the existing Gmail handoff). The runtime JWT is never in a
    URL. iOS receives the runtime JWT in the custom-scheme deep
    link because the iOS app cannot hold the instance-internal
    secret; iOS uses its own local CSRF binding (a
    `tenant_state` value stored in a transient Keychain entry and
    verified on the deep-link return) instead of the browser's cookie
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

7. **No `CONTROL_PLANE_IDENTITY` flag.** Hosted identity is selected
   by `CONTROL_PLANE_URL` being set. Self-host identity is selected by
   its absence. There is no per-tenant dual-auth flag and no staged
   hosted bridge compatibility mode.
