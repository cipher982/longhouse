# Auth Domain Split

Status: In progress

## Scope

This phase is a behavior-preserving refactor. Public routes stay the same. The goal is to stop mixing unrelated auth concerns in the same files.

In scope:
- Split tenant `/auth` into browser-session auth, hosted SSO bridge auth, and Gmail connect auth modules
- Move Oikos auth helpers out of `routers/` into a dependency module
- Move agents/device auth dependencies out of `routers/agents.py`
- Add focused regression tests around the moved auth seams

Out of scope:
- Changing the public auth contract
- Redesigning `/api/agents/*` ownership rules
- Removing `AGENTS_API_TOKEN`
- Removing `CONTROL_PLANE_JWT_SECRET` fallback
- Narrowing `AUTH_DISABLED`

## Target Ownership

- Browser login/session auth:
  `dev-login`, `service-login`, `google`, `password`, `cli-login`, `verify`, `status`, `logout`, `methods`
- Hosted SSO bridge auth:
  `accept-token`, `sso`
- Gmail connect auth:
  `google/gmail/start`, `google/gmail`
- Oikos auth dependency:
  browser cookie by default, query-token override for SSE/EventSource
- Agents/device auth dependency:
  `X-Agents-Token` and legacy device-token fallback logic

## Acceptance Criteria

- Route behavior and paths are unchanged
- `zerg/routers/auth.py` no longer owns every auth domain directly
- `zerg/routers/oikos_auth.py` is gone
- `verify_agents_token` and `verify_agents_read_access` no longer live in `zerg/routers/agents.py`
- Focused auth tests plus `make test` and `make test-e2e` pass before ship

## Notes

- 2026-03-14: Keep this as structural cleanup. Phase 3 is where `/api/agents/*` becomes machine-only.
