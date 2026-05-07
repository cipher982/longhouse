# Agents Session Listing Use Case

Status: Proposed
Owner: prelaunch codebase cleanup
Updated: 2026-05-07

## Goal

Make `GET /api/agents/sessions` easier to understand and change without
changing its external behavior.

This is the first cleanup task because the route is launch-critical, highly
complex, and central to the product story:

- `/api/agents/*` is the canonical machine surface.
- session listing/search feeds CLI, MCP, browser veneers, and agent workflows.
- the current route mixes auth constraints, query validation, lexical search,
  hybrid search, semantic snippets, runtime overlays, response projection, and
  response headers in one function.

The desired outcome is not a new abstraction framework. The desired outcome is
one obvious use-case function that a future agent can read, test, and change
without spelunking through a 300-line route handler.

## Current Problem

`server/zerg/routers/agents_sessions.py::list_sessions` currently owns too many
responsibilities:

1. Managed-local hook-token restrictions.
2. Query parameter validation and effective sort selection.
3. Lexical listing and sorting.
4. Hybrid lexical/semantic search and RRF fusion.
5. Semantic fallback snippets.
6. Runtime/liveness overlay loading.
7. `SessionResponse` projection.
8. `has_real_sessions` calculation.
9. Optional `X-Search-Mode` response header.
10. Error logging and HTTP exception mapping.

This makes the most important archive/search endpoint harder to reason about
than it should be before launch.

## Target Shape

Keep the route thin:

```python
@router.get("/sessions", response_model=SessionsListResponse)
async def list_sessions(...):
    result = await list_agent_sessions(db=db, auth=_auth, params=params)
    return result.to_fastapi_response()
```

Add a service module, likely:

```text
server/zerg/services/session_listing.py
```

The service owns the use case:

- input parameter object
- auth-token constraint validation
- search/list strategy selection
- response-session projection
- optional response header metadata

The route remains responsible for:

- FastAPI parameter declarations
- dependency injection
- converting service exceptions to `HTTPException`
- returning `JSONResponse` only when headers are needed

## Non-Goals

- Do not change `/api/agents/sessions` response shape.
- Do not change query parameter names, defaults, limits, or error messages.
- Do not change search ranking or hybrid search behavior.
- Do not merge `/api/timeline/sessions` into this endpoint yet.
- Do not rewrite `AgentsStore`.
- Do not introduce a new generic router/use-case framework.
- Do not remove semantic search, active-context behavior, or managed-local hook
  token support in this task.

## Implementation Plan

### Phase 1 - Characterization Tests

Add or expand backend tests around the current endpoint behavior before moving
logic.

Cover at least:

- default listing returns `SessionsListResponse`
- `sort=None` resolves to recency without query
- `sort=None` resolves to relevance with query
- `sort=balanced` without query returns `400`
- `mode=hybrid` with `offset > 0` returns `400`
- invalid `context_mode` returns `400`
- managed-local hook token accepts only bounded recent project lookup
- managed-local hook token rejects broader filters
- lexical response still includes runtime overlays and match snippets
- hybrid fallback header behavior remains unchanged where currently emitted

Reuse the existing coverage before adding new tests:

- `server/tests_lite/test_summary_api.py`
- `server/tests_lite/test_sessions_search_context_mode.py`
- `server/tests_lite/test_managed_local_hook_tokens.py`
- `server/tests_lite/test_timeline_runtime_overlay.py`
- `server/tests_lite/test_has_real_sessions.py`
- `server/tests_lite/test_datetime_e2e.py`

Acceptance: tests fail if route behavior drifts during extraction.

### Phase 2 - Extract Parameter and Result Types

Introduce small explicit types in `session_listing.py`:

```python
@dataclass(frozen=True)
class SessionListParams:
    project: str | None
    provider: str | None
    environment: str | None
    include_test: bool
    hide_autonomous: bool
    device_id: str | None
    days_back: int
    query: str | None
    limit: int
    offset: int
    sort: str | None
    mode: str | None
    context_mode: str

@dataclass(frozen=True)
class SessionListResult:
    response: SessionsListResponse
    headers: dict[str, str]
```

Keep these types local to the use case unless another endpoint needs them.

### Phase 3 - Move Listing Logic

Move behavior out of the route in small chunks:

1. `_validate_managed_hook_scope(...)`
2. `_resolve_effective_sort(...)`
3. `_list_hybrid_sessions(...)`
4. `_list_lexical_sessions(...)`
5. `_build_session_list_response(...)`
6. `_has_real_sessions(...)`

The extracted functions may stay private. The public seam should be one
function:

```python
async def list_agent_sessions(
    *,
    db: Session,
    auth: object,
    params: SessionListParams,
) -> SessionListResult:
    ...
```

### Phase 4 - Thin Route

Reduce `list_sessions` to:

- construct `SessionListParams`
- call `list_agent_sessions`
- return `SessionsListResponse` or `JSONResponse` with headers
- preserve existing top-level error logging

The route should be boring enough that future work happens in the service.

## Test Commands

Run focused backend tests first:

```bash
cd server
./run_backend_tests_lite.sh \
  tests_lite/test_summary_api.py \
  tests_lite/test_sessions_search_context_mode.py \
  tests_lite/test_managed_local_hook_tokens.py \
  tests_lite/test_timeline_runtime_overlay.py \
  tests_lite/test_has_real_sessions.py \
  tests_lite/test_datetime_e2e.py
```

Then run:

```bash
make test
```

Do not run broad E2E for this refactor unless behavior changes or focused
tests expose a browser contract gap.

## Done Criteria

- `GET /api/agents/sessions` route behavior is unchanged.
- The route no longer contains search/list/projection orchestration.
- The new service has focused tests for validation and listing paths.
- `agents_sessions.py::list_sessions` is small enough to read in one screen.
- No frontend, iOS, engine, database, or schema changes are required.
- Complexity report should show `list_sessions` dropping materially even if the
  moved service functions still carry the same business branches.

## Follow-Up Tasks

After this extraction lands, the next cleanup tasks should be easier:

1. Compare `/api/timeline/sessions` and `/api/agents/sessions` projection logic
   and remove remaining duplication.
2. Move semantic fallback-snippet logic behind a named helper with direct tests.
3. Decide whether `mode=hybrid` should remain on the launch path or move to a
   separate search endpoint.
4. Apply the same pattern to `local_health` classifiers.
