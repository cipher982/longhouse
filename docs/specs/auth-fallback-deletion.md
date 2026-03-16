# Auth Fallback Deletion

Status: In progress

## Scope

This phase deletes the remaining auth compatibility paths left after the
browser-vs-machine boundary split and auth domain split.

In scope:
- Remove `AGENTS_API_TOKEN` support from tenant machine-auth codepaths
- Remove `CONTROL_PLANE_JWT_SECRET` fallback from tenant SSO key resolution
- Remove password-login migration of legacy local users to `OWNER_EMAIL`
- Update CLI/tests/docs to the stricter contracts

Out of scope:
- Narrowing `AUTH_DISABLED`
- Reworking the hosted login-token bridge
- Renaming `agents` terminology across the product
- Changing the browser cookie auth surface

## Target Shape

- Machine auth:
  device token only (`zdt_*`, `X-Agents-Token`)
- Tenant SSO key resolution:
  control-plane fetch + stale cache grace only
- Password auth:
  login binds to the already-canonical local/owner user, with no mutation of
  the first non-service account during authentication

## Acceptance Criteria

- Backend rejects non-device machine auth when auth is enabled
- CLI entrypoints no longer advertise or read `AGENTS_API_TOKEN`
- Tenant SSO key helper no longer falls back to `CONTROL_PLANE_JWT_SECRET`
- Password auth tests cover the tightened owner/local binding behavior
- `make test`, `make test-e2e`, and `make qa-live` pass before closeout

## Notes

- 2026-03-16: The control plane still provisions an explicit instance JWT secret; this phase only removes the tenant-side legacy fallback helper, not the hosted SSO model itself.
- 2026-03-16: Keep docs honest about device-token auth. Browser pages are cookie-session auth; `/api/agents/*` is machine auth only.
