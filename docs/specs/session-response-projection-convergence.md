# Session Response Projection Convergence

Status: Proposed
Owner: prelaunch codebase cleanup
Updated: 2026-05-07

## Goal

Remove duplicated `SessionResponse` batch materialization between the canonical
machine API and the browser timeline without changing either route contract.

This is the next cleanup phase after extracting `GET /api/agents/sessions` into
`session_listing.py`. The agents endpoint should remain the canonical raw
session list. The timeline endpoint should remain a browser-owned thread-card
view. The shared part is only how an `AgentSession` becomes a `SessionResponse`
with runtime, liveness, first-message, thread, match, and binding overlays.

## Current Problem

Two code paths now build the same response projection:

- `server/zerg/services/session_listing.py` builds `SessionResponse[]` for
  `/api/agents/sessions`.
- `server/zerg/routers/timeline.py` builds a private `SessionResponse` map for
  `/api/timeline/sessions` thread cards.

Both paths load the same side data: last activity, runtime state, first user
message, thread metadata, and unmanaged binding overlay. If one path changes,
timeline and agents session rows can drift even though they represent the same
underlying session.

## Target Shape

Add a neutral service module:

```text
server/zerg/services/session_response_projection.py
```

It owns:

- `build_session_response_list(...)`
- `build_session_response_map(...)`
- `has_real_sessions(...)`

`session_listing.py` uses the list helper and keeps search-specific match
snippets/scores local to the agents listing use case.

`timeline.py` uses the map helper and keeps browser-specific thread-card
assembly local to the timeline router.

## Non-Goals

- Do not change `/api/agents/sessions` response shape.
- Do not make `/api/agents/sessions` return timeline cards.
- Do not change `/api/timeline/sessions` default card response.
- Do not change the timeline query/hybrid compatibility path, which still
  returns raw session hits for client-side grouping.
- Do not rewrite `AgentsStore` or combine raw session pagination with thread
  pagination.

## Done Criteria

- Agents listing and timeline card construction share one `SessionResponse`
  projection implementation.
- Existing route behavior and response shapes are preserved.
- Focused backend tests cover agents listing, timeline card listing, runtime
  overlay projection, browser/auth boundary, and `has_real_sessions`.
