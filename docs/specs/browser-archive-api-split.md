# Browser Archive API Split

Status: Done

## Scope

This phase makes the browser session archive/timeline surface stop depending on
the machine/device API namespace.

In scope:
- Add a browser-owned API for the current timeline/session-detail/briefings UI
- Move browser frontend calls from `/api/agents/*` to the new surface
- Make `/api/agents/*` read routes machine-only
- Update browser/core/live E2E to reflect the split auth model

Out of scope:
- Renaming all `agent` frontend types/hooks/files
- Redesigning the session archive product
- Deleting `AGENTS_API_TOKEN`
- Narrowing `AUTH_DISABLED`
- Reworking reflection/backfill/admin UX

## Target Shape

- Browser archive API:
  `/api/timeline/*`
- Machine/device API:
  `/api/agents/*`

Browser routes to mirror:
- `GET /timeline/sessions`
- `GET /timeline/sessions/summary`
- `GET /timeline/sessions/active`
- `GET /timeline/sessions/{id}`
- `GET /timeline/sessions/{id}/thread`
- `GET /timeline/sessions/{id}/events`
- `GET /timeline/sessions/{id}/preview`
- `POST /timeline/sessions/{id}/action`
- `GET /timeline/filters`
- `GET /timeline/briefing`
- `GET /timeline/recall`
- `POST /timeline/demo`

## Acceptance Criteria

- Browser pages no longer issue `/api/agents/*` requests
- `/api/agents/*` read endpoints require `verify_agents_token`
- Device-token live smoke coverage for `/api/agents/*` still passes
- Timeline/detail/briefings/recall browser flows still work with cookie auth
- `make test`, `make test-e2e`, and `make qa-live` pass before closeout

## Notes

- 2026-03-16: Prefer thin browser routes that reuse existing session/archive logic rather than re-implementing it.
- 2026-03-16: Keep live QA split-brained on purpose: browser pages validate cookie auth, direct API checks validate device-token auth.
- 2026-03-16: Shipped on `main` after `make test`, `make test-e2e`, `make qa-live`, and the hosted `david010` reprovision/deploy cycle passed.
