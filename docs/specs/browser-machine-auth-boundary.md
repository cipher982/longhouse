# Browser vs Machine Auth Boundary

Status: In progress

## Executive Summary

This phase tightens one specific auth boundary: browser UI auth should be clearly cookie-session based, while machine and CLI surfaces keep their own credentials. The goal is not a full auth rewrite. It is a bounded cleanup that removes the most confusing overlap in the current codebase.

## Decision Log

### Decision: Keep the phase narrow
**Context:** Auth has several real domains: browser sessions, hosted SSO bridges, device tokens, runner secrets, and internal service auth.
**Choice:** Only clean the browser-vs-machine boundary in this pass.
**Rationale:** This gives us a meaningful reduction in confusion without destabilizing hosted login or machine workflows.
**Revisit if:** We finish this pass cleanly and want to remove legacy machine fallbacks next.

### Decision: Do not create more tracking docs
**Context:** The repo already has enough planning markdown, and this task is small enough to keep in one spec plus TODO.
**Choice:** Use a single short spec file and the TODO entry.
**Rationale:** Keeps the cleanup legible without adding more doc sprawl.
**Revisit if:** The work expands beyond the current bounded scope.

## Scope

In scope:
- Add explicit browser-session auth helpers in the backend
- Use those helpers on browser-owned routes that were incorrectly leaning on mixed auth helpers
- Remove dead browser token-era frontend API surface and stale tests
- Update comments/docstrings so browser auth is described honestly

Out of scope:
- Hosted SSO redesign
- Device-token redesign
- Runner auth redesign
- Removing `AGENTS_API_TOKEN`
- Removing `CONTROL_PLANE_JWT_SECRET` fallback

## Implementation Phases

### Phase 1: Explicit browser-session helpers
Status: Done

Acceptance criteria:
- Backend has explicit dependencies for required and optional browser-session auth
- Browser session validation paths use the cookie-first helper rather than generic mixed auth language
- Oikos browser-owned auth helper uses the browser-session path when it is not handling a special query-token case

### Phase 2: Human-only routes leave the mixed machine helper
Status: Done

Acceptance criteria:
- `insights` read routes use browser-session auth
- `proposals` routes use browser-session auth
- Those routers no longer import `verify_agents_read_access`

### Phase 3: Frontend token baggage removal
Status: Done

Acceptance criteria:
- Frontend auth context no longer exposes a dead `getToken()` API
- Legacy `zerg_jwt` cleanup code is removed
- Tests and mocks no longer pretend the browser app uses a JS auth token

## Verification

- Focused backend auth/router tests for the new browser-session boundary
- Focused frontend tests/typecheck for the auth-context cleanup
- `make test`
- `make test-e2e`
- Push, deploy, reprovision, and `make qa-live`

## Implementation Notes

- 2026-03-14: Added `get_current_browser_user` and `get_optional_browser_user` so browser-owned routes can be explicit about cookie-session auth instead of leaning on mixed browser-or-machine helpers.
- 2026-03-14: Updated `/api/auth/status`, `/api/auth/verify`, hosted Gmail browser entrypoints, and the Oikos browser auth helper to use the browser-session path.
- 2026-03-14: `insights` and `proposals` read routes now use browser-session auth; `POST /api/insights` intentionally stays on the machine-auth path.
- 2026-03-14: Removed the dead frontend `getToken()` API, removed the leftover `zerg_jwt` localStorage cleanup path, and cleaned the stale test mocks that still implied browser auth used JS-readable tokens.
