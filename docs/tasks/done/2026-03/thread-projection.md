# Thread Projection

Status: Done
Spec: `docs/specs/thread-projection.md`
Last updated: 2026-03-20

## Goal

Make `/timeline/:sessionId` behave like one continued conversation instead of a raw session page with continuation chrome bolted on. The center pane should show the selected branch path stitched together with inline seams, and the page should keep one composer with explicit head vs stale-branch behavior.

## Done when

- The backend exposes a stitched projection API for a selected session's lineage path.
- The detail page renders projected seam + event items instead of only one session's raw events.
- Unit coverage exists for projection ordering, pagination, and UI behavior.
- E2E coverage proves the stitched continuation flow locally and on the hosted instance.
- The changes are merged, deployed, reprovisioned, and verified on prod.

## Checklist

- [x] Commit the phase-0 spec/task slice
- [x] Implement backend projection API and tests
- [x] Implement frontend projection consumer and tests
- [x] Add or update robust E2E coverage
- [x] Merge to `main`
- [x] Deploy and reprovision hosted runtime
- [x] Re-run hosted QA and continuation smoke

## Notes

- Reuse the existing lineage fields on `AgentSession`; do not add new persistence unless the implementation proves it is necessary.
- Keep raw session event APIs intact for audit/debug/MCP workflows.
- 2026-03-19: Phase 1 added the server-side projection API for a focused session lineage path. The core projection logic lives in `agents_store.py`, with browser/machine routes in `routers/agents.py` and `routers/timeline.py`, and focused verification passed with `./run_backend_tests_lite.sh tests_lite/test_session_projection_api.py tests_lite/test_browser_machine_auth_boundary.py tests_lite/test_timeline_api_auth_boundary.py`.
- 2026-03-19: Phase 2 switched session detail to a stitched thread view with inline seam rows and one composer. The client now consumes projected items in `useSessionWorkspace.ts`, renders seams in `TimelinePane.tsx`, and keeps the cloud composer aligned with head vs stale-branch state in `SessionDetailPage.tsx` and `SessionChat.tsx`. `make test-frontend-unit MINIMAL=1` passed for the frontend slice.
- 2026-03-20: Phase 3 hardened continuation E2E by making the fake session-chat backend persist real user/assistant events into the DB before sending the `done` SSE. That let the continuation redirect, seam rendering, and transcript assertions match production behavior instead of an SSE-only stub.
- 2026-03-20: Shipped to `main` in the phase stack `36bc6ed9` / `d8915479` / `08f800f0` / `4caacc21`. Local verification passed with `./run_backend_tests_lite.sh tests_lite/test_session_resume_prep.py tests_lite/test_session_projection_api.py`, `make test-frontend-unit MINIMAL=1`, and `make test-e2e`.
- 2026-03-20: Hosted rollout completed after `Publish Runtime Image` run `23326212503` and `Deploy and Verify` run `23326254069` succeeded. `david010` was reprovisioned, `make qa-live` passed `10/10`, the dedicated hosted spec `./scripts/run-prod-e2e.sh tests/live/session-continuation-lineage.spec.ts` passed `2/2`, and `longhouse-david010` restarted healthy at `2026-03-20T02:20:15Z`.
- 2026-03-20: Residual CI noise is unrelated to thread projection. `make test` and the matching CI sqlite-lite job still hit the preexisting five browser-cookie auth-boundary failures already reproducible on untouched `origin/main`, and the model-smoke workflow also failed independently.
