# Thread Projection

Status: In progress
Spec: `docs/specs/thread-projection.md`
Last updated: 2026-03-19

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
- [ ] Implement frontend projection consumer and tests
- [ ] Add or update robust E2E coverage
- [ ] Merge to `main`
- [ ] Deploy and reprovision hosted runtime
- [ ] Re-run hosted QA and continuation smoke

## Notes

- Reuse the existing lineage fields on `AgentSession`; do not add new persistence unless the implementation proves it is necessary.
- Keep raw session event APIs intact for audit/debug/MCP workflows.
- Backend phase verification:
  - `make test` is still red on this branch, but the same five browser-cookie auth-boundary failures also reproduce on untouched `origin/main`.
  - Focused verification passed with `./run_backend_tests_lite.sh tests_lite/test_session_projection_api.py tests_lite/test_browser_machine_auth_boundary.py tests_lite/test_timeline_api_auth_boundary.py`.
