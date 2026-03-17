# AUTH_DISABLED And Startup Hardening

Status: In progress

## Scope

This phase narrows `AUTH_DISABLED` so it only relaxes browser/dev ergonomics,
and hardens single-tenant startup so auth-enabled instances must declare their
canonical owner explicitly.

In scope:
- Remove `AUTH_DISABLED` bypass from machine/device auth
- Remove `AUTH_DISABLED` bypass from internal-route auth
- Keep local dev token bootstrap workable through the browser/dev user path
- Require `OWNER_EMAIL` when single-tenant auth is enabled
- Fail startup on single-tenant auth misconfig or invariant violation
- Update tests/docs for the stricter contracts

Out of scope:
- Hosted SSO bridge redesign
- Broader auth naming cleanup
- Multi-tenant auth work
- Runner auth redesign

## Target Shape

- Browser/dev auth:
  `AUTH_DISABLED` may still provide a dev browser user
- Machine auth:
  always requires `X-Agents-Token` with a real `zdt_*` device token
- Internal auth:
  always requires `X-Internal-Token`
- Single-tenant auth startup:
  auth-enabled instances must set a valid `OWNER_EMAIL`; `ADMIN_EMAILS` is no
  longer treated as the owner-binding fallback

## Acceptance Criteria

- `verify_agents_token` fails closed even when `AUTH_DISABLED=1`
- `require_internal_call` fails closed without `X-Internal-Token` even when `AUTH_DISABLED=1`
- Dev/local flows can still create a device token without manual browser login setup
- Startup raises on missing/invalid `OWNER_EMAIL` when auth is enabled
- `make test`, `make test-e2e`, and `make qa-live` pass before closeout

## Notes

- 2026-03-16: This phase intentionally keeps `AUTH_DISABLED` for local browser ergonomics and dev-only helper routes; the change is that machine/internal surfaces stop inheriting that bypass implicitly.
- 2026-03-16: Hosted instances already get `OWNER_EMAIL` from the control plane, so this hardening mainly removes ambiguous OSS/auth-enabled fallback behavior.
