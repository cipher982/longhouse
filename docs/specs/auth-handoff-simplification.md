# Hosted Auth Handoff Simplification

Status: Done

## Scope

This phase simplifies the hosted browser login bridge between the control plane
and tenant instances. The goal is to make the control plane own the "sign in
and open my instance" flow cleanly, while the tenant owns one canonical token
acceptance path.

In scope:
- Add an explicit hosted control-plane login URL to tenant `/auth/methods`
- Preserve hosted `return_to` intent through control-plane email/password and
  Google login
- Canonicalize tenant browser token acceptance on `/auth/accept-token`
- Delete `/auth/sso` and the landing-page `auth_token` JS bridge
- Add focused tests around the new handoff behavior

Out of scope:
- Gmail OAuth/connect flow changes
- Device or runner auth changes
- Broad control-plane UI redesign
- Multi-instance-per-user routing

## Target Shape

- Hosted tenant login CTA:
  redirect to control-plane `/dashboard/open-instance`
- Control-plane anonymous access to `/dashboard/open-instance`:
  redirect to login while preserving the intent to come back
- Control-plane successful login:
  redirect back through `/dashboard/open-instance`, mint a tenant login token,
  and send the browser to tenant `/api/auth/accept-token?token=...`
- Tenant browser handoff:
  accept token, set `longhouse_session`, redirect to `/timeline`

## Acceptance Criteria

- Hosted browser login has one tenant handoff route: `/api/auth/accept-token`
- `/api/auth/sso` is gone
- Landing page no longer parses `auth_token` from the URL
- `/auth/methods` returns an explicit hosted `sso_login_url`
- Control-plane password and Google login can preserve a safe hosted return
  target
- `make test`, `make test-e2e`, and `make qa-live` pass before closeout

## Notes

- 2026-03-16: The cleanup is aimed at coherence, not adding more auth surface.
- 2026-03-16: Programmatic POST `/auth/accept-token` can stay for tests and
  smoke flows; the simplification target is the browser redirect path.
- 2026-03-16: Tenant `/api/auth/methods` now exposes `sso_login_url`, the
  control plane preserves a signed local `return_to` through password and
  Google login, and browser handoff is canonicalized on
  `/api/auth/accept-token?token=...`.
- 2026-03-16: Verification passed through focused auth regression suites,
  `make test`, `make test-e2e`, live deploys for `longhouse-demo` and
  `longhouse-control-plane`, reprovision of `david010`, and live `make qa-live`
  (8/8).
