# Shipper and Session Detail Follow-Ups

Status: In progress

## Executive Summary

This spec captures the cleanup work after the engine batching/replay fixes and the live QA harness hardening. The remaining goals are:

1. make the already-good shipper and QA commits easy to move onto a clean push path,
2. replace compatibility selectors with stable session-detail test hooks,
3. make session detail render its shell even when event loading is still pending,
4. record the long-term decision about the mixed auth path in `qa-live`,
5. reduce noisy Rust engine warnings so real regressions are easier to spot,
6. expose dead-lettered spool ranges somewhere a human can notice them before they silently accumulate.

This work must avoid overlapping with the current unrelated frontend edits already in the tree. The safest path is to keep the live harness and engine cleanup slices isolated, then make the session-detail UI change with minimal edits around the current workspace implementation.

## Decision Log

### Decision: Use an artifact path for isolated push preparation
Context: `main` is already ahead of `origin/main` with unrelated local commits, and repo policy is to stay on `main` without worktrees.

Choice: Treat "pushable in isolation" as creating a documented/exportable artifact path for the relevant commits rather than rewriting history in place.

Rationale: The useful local fixes already exist as atomic commits. Exporting or documenting the exact patch set is reversible and avoids dangerous history surgery on a shared dirty tree.

Revisit if: The user explicitly wants branch surgery or a fresh clean-clone/cherry-pick workflow.

### Decision: Keep session selection on the device-token API for `qa-live`
Context: The browser-authenticated request fixture receives `403` on `/api/agents/sessions` in the hosted environment, while the page itself uses browser-cookie auth successfully after navigation.

Choice: Keep session discovery on the device-token API, but harden the page-side checks so the smoke still validates the browser path.

Rationale: This matches the real deployed auth semantics today and avoids breaking `qa-live` on an unsupported bearer-token assumption.

Revisit if: Browser-auth session listing becomes a supported and verified hosted path.

### Decision: Session detail should load progressively
Context: The current route blocks the whole page on `sessionLoading || eventsLoading`, so an event query delay can leave the entire page in a generic loading state.

Choice: Render the route shell as soon as session metadata is available. Let the timeline pane own its own loading, empty, and error presentation.

Rationale: Session metadata and the timeline body have different failure/latency characteristics. A single full-page blocker hides useful context and makes transient event delays look like a broken route.

Revisit if: The workspace becomes impossible to render meaningfully without the first event page.

### Decision: Dead-letter visibility starts with existing surfaces
Context: Oversize single-line ranges intentionally dead-letter now, but the only reliable source is the local SQLite DB.

Choice: Surface dead-letter counts and recent dead-letter signals in existing CLI/heartbeat outputs before building any dedicated UI.

Rationale: This is the smallest change that makes the issue visible to operators without creating a new product surface.

Revisit if: Operators need richer inspection or remediation flows.

## Architecture / Design

### Isolation / Push Preparation
- Record the exact commits and dependencies.
- Generate an exportable patch artifact for the known-good fixes.
- Do not rewrite local history on `main`.

### Stable Session-Detail Test Hooks
- Add durable `data-testid` hooks on session timeline rows in the workspace implementation.
- Update `qa-live` to prefer these hooks while keeping compatibility only where needed for currently deployed versions.

### Progressive Session-Detail Loading
- Route-level blocker should depend on session metadata, not the first page of timeline events.
- Timeline pane should accept a loading/error state and render a scoped empty/loading view.
- The session-detail route should still fail loudly on auth or session fetch failure.

### Engine Warning Cleanup
- Remove or narrow obvious dead-code warnings where the symbol is truly unused.
- Use targeted annotations only when a symbol is intentionally retained for compatibility or future extension.
- Keep the change set small enough to preserve confidence.

### Dead-Letter Visibility
- Add count helpers on spool state.
- Expose dead-letter counts in human-visible outputs already used in practice:
  - ship CLI summaries,
  - heartbeat payloads and/or daemon logs.

## Implementation Phases

### Phase 0: Spec and Task Tracking
Acceptance criteria:
- This spec exists under `docs/specs/`.
- Phase checklist exists or is intentionally omitted with rationale.
- Spec is committed before further implementation.

### Phase 1: Isolated Push Artifact + Stable Session Hooks
Acceptance criteria:
- The exact local fix commits are documented/exported as an isolated patch path.
- Session timeline rows have a stable test hook in the workspace DOM.
- `qa-live` uses the stable hook when available and stays compatible with current hosted output.
- `make qa-live` passes.

Implementation status:
- Completed on 2026-03-10.
- Exported isolated patch artifacts to `/tmp/zerg-isolated-patches-20260310/`:
  - `0001-fix-engine-dedupe-pending-spool-gaps.patch`
  - `0001-fix-e2e-harden-live-qa-harness.patch`
- Added stable `data-testid="session-timeline-row"` hooks to session timeline rows and updated the live harness to prefer that hook while preserving compatibility with currently deployed DOM variants.

### Phase 2: Progressive Session Detail Loading
Acceptance criteria:
- Session detail no longer full-screen blocks solely because timeline events are still loading.
- The timeline pane renders a scoped loading or empty state instead.
- Existing session-detail live QA still passes.
- No auth/session fetch regressions are introduced.

Implementation status:
- Completed on 2026-03-10.
- `SessionDetailPage` now renders the route shell once session metadata is loaded instead of waiting on the initial timeline events query.
- `TimelinePane` now owns scoped loading and event-fetch error empty states when there are no rows to render yet.
- Verification:
  - `make test-frontend-unit MINIMAL=1`
  - `make qa-live`

### Phase 3: Auth Split Decision Capture
Acceptance criteria:
- The long-term `qa-live` auth model is documented in this spec and any touched test helper comments.
- No unresolved ambiguity remains about why session discovery and browser rendering use different auth paths today.

Implementation status:
- Completed on 2026-03-10.
- The hosted `qa-live` model is now explicitly documented in both this spec and the live fixtures:
  - `agentsRequest` remains device-token-authenticated for `/api/agents/*` discovery.
  - browser rendering remains cookie-authenticated through `longhouse_session`.
- This split is intentional until hosted browser auth can list agent sessions without returning `403`.

### Phase 4: Engine Warning Noise Reduction
Acceptance criteria:
- Rust engine warnings are reduced materially from the current baseline.
- Removed warnings are backed by code deletion, narrowed visibility, or targeted intentional annotations.
- `make test-engine-fast` still passes.

Implementation status:
- Completed on 2026-03-10.
- Reduced the `make test-engine-fast` warning baseline from 22 Rust warnings to 0.
- Cleanup approach:
  - removed unused benchmark wrapper functions and dead helper methods,
  - narrowed test-only helper APIs with `#[cfg(test)]`,
  - dropped unused runtime struct fields and stale helper functions,
  - used narrow `#[allow(dead_code)]` only for retained test-only compatibility helpers.
- Verification:
  - `make test-engine-fast`

### Phase 5: Dead-Letter Visibility
Acceptance criteria:
- Dead-letter count is available in at least one operator-facing runtime output beyond raw SQLite inspection.
- Regression coverage validates the new visibility surface.
- `make test-engine-fast` and `make test-shipper-e2e` pass.

## Verification Commands

- `make qa-live`
- `make test-engine-fast`
- `make test-shipper-e2e`

## Implementation Notes

- Do not edit unrelated dirty frontend files unless the change is required and can be made without overwriting current work.
- `TODO.md` is not the source of truth for this slice while another agent is editing it; this spec owns the decisions and phase tracking for now.
- Regenerate the isolated patch bundle with:
  - `git format-patch -1 1764ec90 -o /tmp/zerg-isolated-patches-20260310`
  - `git format-patch -1 ed52fc41 -o /tmp/zerg-isolated-patches-20260310`
