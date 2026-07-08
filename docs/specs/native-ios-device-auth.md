# Native iOS Device Auth

Status: Draft
Owner: Longhouse
Scope: hosted iOS sign-in, hosted runtime auth refresh, tenant proxy compatibility

## Executive Summary

The iOS app should behave like a modern native client: a user signs in once, and the app stays signed in across app launches, backgrounding, app updates, backend deploys, and short outages. Re-authentication should happen only after explicit logout, credential revocation, account/security events, or a long absolute device-session lifetime.

The current hosted iOS flow stores a CP-issued runtime bearer in Keychain and refreshes it by presenting that bearer back to the control plane. A short-term fix extended the expired-token refresh leeway, but that still treats a signed JWT as a long-lived refresh credential. This spec replaces that with an explicit native device session:

- Short-lived runtime access token: signed CP JWT, used as `Authorization: Bearer`.
- Long-lived native refresh token: opaque random credential, stored in iOS Keychain, hashed in the control-plane database.
- Rotation on refresh: every refresh returns a new access token and a new refresh token; the previous refresh token is consumed.
- Clear error semantics: only explicit rejection clears local auth; network, deploy, and 5xx failures defer and keep the local shell.
- Backward compatibility: existing bearer-only refresh remains as a temporary migration path and is removed after dogfood stabilizes.

## Goals

- Eliminate routine iOS re-authentication.
- Make hosted iOS auth robust to overnight sleep, backend deploys, and control-plane restarts.
- Keep access tokens short-lived and resource-server friendly.
- Store only opaque refresh-token hashes server-side.
- Rotate refresh credentials to limit replay damage.
- Preserve the current `ASWebAuthenticationSession` sign-in shape.
- Keep self-host/browser cookie auth out of this refactor.

## Non-Goals

- Full OAuth provider replacement or public OAuth server branding.
- User-visible signed-in-device management UI in this phase.
- Biometric app lock. This can be layered on top of Keychain later.
- Migrating web/browser cookies to the native device-session model.
- Shipping iOS through TestFlight/App Store.

## Current Architecture

1. iOS opens hosted sign-in with `ASWebAuthenticationSession`.
2. The control plane authenticates the user and redirects back to the app with a one-use handoff code.
3. iOS sends the handoff code to the tenant runtime.
4. Tenant runtime exchanges the code with the control plane and returns a CP runtime JWT.
5. iOS stores that runtime JWT in Keychain and sends it as `Authorization: Bearer`.
6. Tenant runtime proxies `/api/auth/refresh-runtime-token` to the CP, using the old runtime JWT as the refresh credential.

This works, but the long-lived credential is a JWT that was designed as an access token. That makes per-device revocation, rotation, and replay handling awkward.

## Target Architecture

```text
iOS Keychain
  access token: CP runtime JWT, short lifetime
  refresh token: opaque native device token, long lifetime

Tenant Runtime
  validates runtime JWT for normal API auth
  proxies native refresh/logout requests to Control Plane

Control Plane
  stores NativeDeviceSession rows
  stores refresh token hash, not token plaintext
  rotates refresh token on each refresh
  mints runtime JWTs for tenant audience
```

### Token Lifetimes

- Runtime access token: 1 hour for now, still configurable by constant.
- Native refresh token idle lifetime: 90 days.
- Native refresh token absolute lifetime: 180 days.
- Refresh response includes `runtime_token`, `expires_in`, `refresh_token`, `refresh_expires_in`, and `refresh_token_expires_at`.

The product behavior is "you should not think about login." The security behavior is "a stolen refresh credential cannot live forever, can be revoked per device, and rotates on every use."

## API Contract

### Control Plane: Exchange Handoff

`POST /api/identity/exchange-handoff`

Existing request remains:

```json
{
  "code": "one-use-code",
  "tenant": "david010",
  "tenant_state": "handoff-verifier"
}
```

Response adds native-session fields:

```json
{
  "runtime_token": "jwt",
  "token_type": "bearer",
  "expires_in": 3600,
  "refresh_token": "opaque",
  "refresh_token_expires_in": 15552000,
  "refresh_token_expires_at": "2027-01-04T18:00:00Z",
  "device_session_id": "nds_...",
  "claims": {}
}
```

The tenant runtime returns these same fields from `/api/auth/accept-native-handoff`.

### Control Plane: Native Refresh

`POST /api/identity/refresh-native-session`

Headers:

- `X-Internal-Token`: tenant internal API secret.

Body:

```json
{
  "refresh_token": "opaque",
  "tenant": "david010"
}
```

Response:

```json
{
  "runtime_token": "jwt",
  "token_type": "bearer",
  "expires_in": 3600,
  "refresh_token": "opaque-next",
  "refresh_token_expires_in": 15552000,
  "refresh_token_expires_at": "2027-01-04T18:00:00Z",
  "device_session_id": "nds_..."
}
```

Rules:

- Missing/unknown/expired/revoked token returns `401`.
- Tenant mismatch returns `403`.
- Successful refresh rotates the token hash and updates `last_used_at`.
- Refresh-token reuse returns `401` and revokes the session family if the row can be identified.

### Control Plane: Native Logout

`POST /api/identity/revoke-native-session`

Headers:

- `X-Internal-Token`: tenant internal API secret.

Body:

```json
{
  "refresh_token": "opaque"
}
```

Rules:

- Best effort and idempotent.
- Revokes the matching device session if present.

### Tenant Runtime Proxy

Tenant routes:

- `POST /api/auth/refresh-native-session`
- `POST /api/auth/revoke-native-session`

These proxy to CP with `X-Internal-Token`. iOS never talks directly to `control.longhouse.ai` after handoff, which preserves the tenant-runtime boundary and keeps CORS/network policy simple.

`POST /api/auth/refresh-runtime-token` remains temporarily for already-installed iOS builds that only have a runtime bearer.

## Data Model

Add `NativeDeviceSession` in the control plane:

```text
cp_native_device_sessions
  id: int primary key
  session_id: string unique, public identifier, "nds_" prefix
  user_id: FK cp_users.id
  instance_id: FK cp_instances.id
  tenant_subdomain: string
  token_hash: string unique
  token_family_id: string
  device_label: string nullable
  platform: string default "ios"
  app_build: string nullable
  created_at: datetime
  last_used_at: datetime nullable
  idle_expires_at: datetime indexed
  absolute_expires_at: datetime indexed
  revoked_at: datetime nullable
  revoke_reason: string nullable
```

Only `token_hash` is persisted. The raw refresh token is shown exactly once in the exchange/refresh response.

Migration is additive:

- `Base.metadata.create_all()` creates the table for new installs.
- Startup migration creates the table for existing SQLite deployments.
- No destructive schema changes.

## iOS Storage

`SharedAuthStore` stores a native session bundle per host:

- runtime token in Keychain.
- refresh token in Keychain.
- runtime expiry in app-group defaults.
- refresh expiry in app-group defaults.

Keychain accessibility remains `kSecAttrAccessibleAfterFirstUnlock` because widgets/background refresh need shared access after first unlock. This is a product/security tradeoff: the credential survives normal use and app updates, and background surfaces work; explicit logout deletes it.

## iOS Refresh Behavior

Introduce a single refresh authority instead of ad hoc refresh calls:

- On app launch, if runtime token verifies, schedule proactive refresh.
- If runtime token fails with 401 and refresh token exists, call `/api/auth/refresh-native-session`.
- If refresh succeeds, save both new tokens and retry once.
- If refresh returns 401/403, clear auth.
- If refresh fails due to network, timeout, 5xx, or bad gateway, keep local session candidate and show cached shell.
- Concurrent requests share one in-flight refresh task per host.

## Compatibility & Rollout

Phase 1 introduces native-session fields while keeping old fields.

Existing app versions:

- Ignore `refresh_token` fields and continue bearer-leeway refresh.

New app versions:

- Prefer native refresh token when available.
- Fall back to bearer refresh only if no native refresh token exists.

After dogfood proves stability:

- Shorten or remove bearer refresh leeway.
- Keep `/refresh-runtime-token` briefly for older builds, then delete.

## Decision Log

### Decision: Opaque Refresh Token Instead Of Long-Lived JWT

Context: The current fix makes expired runtime JWTs refreshable for months.

Choice: Use an opaque random refresh token stored hashed in CP.

Rationale: Opaque tokens are easy to revoke per device, rotate, hash, and expire without encoding long-lived bearer authority in a self-contained token.

Revisit if: We implement sender-constrained tokens using device-bound private keys.

### Decision: Tenant Runtime Proxies Refresh

Context: iOS currently talks to the tenant runtime for API traffic.

Choice: Keep refresh/logout behind tenant routes that proxy to CP.

Rationale: The app only needs its tenant base URL after login; CP remains the issuer and tenant remains the resource boundary.

Revisit if: We add a first-class public OAuth/OIDC issuer URL and universal app callback flow.

### Decision: Rotate On Every Refresh

Context: Native clients are public clients and cannot keep a client secret.

Choice: Refresh token rotation is mandatory.

Rationale: Rotation limits replay and gives us reuse detection. It is the simple modern baseline before more complex sender-constrained schemes.

Revisit if: Rotation causes concurrency failures that single-flight cannot solve.

### Decision: Defer On Ambiguous Failure

Context: Backend deploys and temporary network failures should not log users out.

Choice: Only explicit auth rejection clears credentials. Ambiguous failures keep the local shell and cached data.

Rationale: Product trust is destroyed by false logout. A 5xx does not prove the credential is invalid.

Revisit if: We add a server-pushed account-disabled signal.

## Implementation Phases

### Phase 0: Spec And Review

Acceptance criteria:

- Spec is committed.
- `hatch claude fable` reviews the spec from first principles.
- Review synthesis is folded back into this document.

### Phase 1: Control-Plane Native Device Sessions

Acceptance criteria:

- `NativeDeviceSession` model exists.
- Startup creates/migrates `cp_native_device_sessions`.
- Handoff exchange returns refresh-token fields.
- `refresh-native-session` rotates token hashes and mints runtime token.
- `revoke-native-session` revokes matching session idempotently.
- Tests cover exchange, refresh rotation, tenant mismatch, expiry, revoked token, reuse detection, and logout.

Test command:

```bash
uv sync --frozen --extra dev
uv run ruff check .
uv run pytest tests/test_identity_api.py
```

### Phase 2: Tenant Runtime Proxy

Acceptance criteria:

- `/api/auth/accept-native-handoff` returns native refresh fields.
- `/api/auth/refresh-native-session` proxies refresh token to CP with internal token.
- `/api/auth/revoke-native-session` proxies logout best-effort.
- Existing `/api/auth/refresh-runtime-token` remains compatible.
- Tests cover proxy success, CP rejection, CP network failure, and missing token.

Test command:

```bash
make test
```

### Phase 3: iOS Native Session Storage And Refresh

Acceptance criteria:

- `SharedAuthStore` persists and clears native refresh token per host.
- Hosted sign-in stores both runtime and refresh token when present.
- Proactive refresh and 401 retry prefer native refresh.
- Refresh is single-flight across concurrent API requests.
- Explicit 401/403 clears auth; network/5xx preserves local session candidate.
- Logout calls native revoke before clearing local Keychain state.
- Legacy bearer refresh fallback works when no refresh token is present.

Test command:

```bash
make test-ios
```

### Phase 4: End-To-End Review And Rollout

Acceptance criteria:

- Hatch DeepSeek reviews each implementation phase before the next phase.
- Full control-plane tests pass.
- Longhouse relevant tests pass.
- Branches are pushed.
- Changes are merged to `main`.
- Control-plane is deployed and health verified.
- Public Longhouse workflows are checked for the pushed SHA.

Test command:

```bash
uv sync --frozen --extra dev
uv run ruff check .
uv run pytest tests
make check-push-readiness
make test-ios
```

## Open Risks

- Refresh rotation plus concurrent requests can invalidate a session if the client does not single-flight correctly.
- Widgets may refresh from a separate process. They need either read-only access to the latest Keychain token or a conservative no-refresh fallback.
- SQLite migration code must be explicit enough for existing control-plane deployments.
- Older iOS builds will still use bearer refresh until replaced.
