# Local Session Truth Hardening

Status: Draft for review
Owner: Longhouse local runtime
Updated: 2026-05-24

## Executive Summary

The first kernel-canonical-sessions pass made the Rust Machine Agent emit a
resolved `engine-status.json.payload.sessions` view and moved the menu bar plus
local health onto that view.

This follow-up closes the remaining launch risks from the final reviews:

- server-side managed-control lease cleanup is not device-scoped enough
- heartbeat ingest does complete-snapshot work every heartbeat even when the
  resolved session snapshot did not change
- heartbeat-only canary sessions can look like real timeline candidates
- compatibility behavior around legacy heartbeat arrays needs pinned tests
- engine and desktop edge cases need a few missing contract tests

The product goal is unchanged: the Machine Agent owns local session truth; UI,
CLI, server, and QA consume it without reinterpreting raw local evidence.

## User Problem

The user-facing menu bar needs to be boring and trustworthy. A user should not
see red status, fake unmanaged sessions, or stale managed-control warnings just
because two machines, a canary heartbeat, or a QA selector exposed duplicated
interpretation logic.

From the user's perspective, the acceptable outcome is:

- the menu bar and CLI agree on local health
- managed sessions are attached, detached, or degraded for concrete reasons
- heartbeat-only internal/canary rows do not pollute user session journeys
- multi-machine hosted state does not flap because one machine omitted a
  session that belongs to another machine

## Decision Log

### Decision: Preserve the Engine as the Identity Source

**Context:** The prior fix moved local identity resolution into the engine.

**Choice:** Do not reintroduce Python bridge/process scans as normal-path truth.
Any fallback is compatibility-only for older installed engines.

**Rationale:** This keeps the local app, CLI, and server aligned on one
resolved session contract.

**Revisit if:** We introduce a provider that cannot provide enough local
evidence to the engine.

### Decision: Add Device Scope Before Optimizing Snapshot Work

**Context:** `mark_missing_managed_control_leases()` currently uses connection
freshness as a conservative proxy because the old managed-state table no longer
indexes previous rows by device.

**Choice:** First make missing-lease cleanup device-scoped, then add cheap
snapshot no-op detection.

**Rationale:** Correctness beats avoiding writes. Optimization should not hide
cross-device detach bugs.

**Revisit if:** The connection model cannot represent machine/device ownership
without a schema change.

### Decision: Treat Heartbeat-Only Sessions as Explicit Internal Fixtures

**Context:** Hosted QA found a `provider=canary` row with zero transcript events
and tried to assert session-detail event UI against it.

**Choice:** Make those rows distinguishable or filtered by user-facing
session-selection paths rather than relying on incidental ordering.

**Rationale:** QA should exercise real user journeys. Internal heartbeat rows
are useful, but they are not proof that session detail renders transcript
events.

**Revisit if:** Canary rows grow real transcript fixtures and become valid
session-detail candidates.

## Scope

### In Scope

- Device-scoped missing managed-control lease cleanup.
- Tests proving resolved `sessions` wins over legacy heartbeat arrays.
- Tests for unusual managed states in resolved heartbeat rows.
- Engine tests for sparse resolved managed rows and generic managed providers.
- A lightweight session snapshot signature to skip no-op heartbeat-derived
  lease updates when the resolved snapshot is unchanged.
- A user-facing or QA-facing way to avoid heartbeat-only sessions in timeline
  detail selection.
- Desktop/menu-bar stale-cache edge tests.

### Out Of Scope

- Removing legacy `managed_sessions` and `unmanaged_session_bindings` fields.
- Redesigning `/api/agents/sessions`.
- Adding a new database migration framework.
- Making canary telemetry into a user product surface.
- iOS UI changes unless a shared contract requires DTO regeneration.

## Current Architecture

### Local Runtime

The Rust engine writes `engine-status.json` with:

- transport counters and build identity
- control-channel status
- legacy arrays for old compatibility
- canonical `sessions` rows that contain control path, presentation state,
  workspace, process, bridge, evidence, and reason codes

### Local Health And Menu Bar

`longhouse-local-health --fast` and the menu bar require canonical `sessions`.
Deep local health also now prefers canonical `sessions` and falls back only for
older engines.

### Hosted Runtime

`/api/agents/heartbeat` accepts both:

- new canonical `sessions`
- legacy `managed_sessions` and `unmanaged_session_bindings`

When canonical `sessions` is present, server ingest should derive leases and
bindings from it and should not double-count legacy arrays.

## Desired Architecture

One heartbeat snapshot from one machine should affect only that machine's
managed-control leases. A repeated identical snapshot should be cheap. User and
QA session-detail journeys should select sessions with actual transcript
content unless they are explicitly testing heartbeat-only rows.

Conceptually:

```text
Machine Agent
  -> resolved local sessions
  -> heartbeat snapshot with stable signature
  -> server upserts this device's current managed leases
  -> server detaches only prior leases owned by this device and omitted now
  -> browser/QA selects user-visible transcript sessions for detail tests
```

## Implementation Phases

### Phase 1: Pin Existing Canonical Contracts

Acceptance criteria:

- Server heartbeat test proves `sessions` present means legacy arrays are
  ignored for session identity.
- Server heartbeat test proves an unknown managed `state` value in a resolved
  row does not crash and does not incorrectly mark the connection attached.
- Local-health deep test proves absent canonical `sessions` intentionally falls
  back for older engines, while present canonical `sessions` wins.
- Engine tests cover:
  - managed lease without matching bridge observation still emits a sparse
    resolved managed row
  - non-Codex/non-Claude managed providers use the generic resolved row path
- Desktop tests cover stale cached snapshot behavior when the latest refresh
  succeeded.

Test gates:

- `uv run --project server pytest server/tests_lite/test_heartbeat_endpoint.py server/tests_lite/test_local_health_cli.py`
- `make test-engine`
- Swift menu bar tests for changed desktop files

### Phase 2: Device-Scoped Missing Lease Cleanup

Acceptance criteria:

- `mark_missing_managed_control_leases()` detaches only connections owned by
  the heartbeat's `device_id` / machine identity.
- A heartbeat with `sessions=[]` from device A cannot detach a connection last
  refreshed by device B.
- A heartbeat from device A still detaches device A's previously attached lease
  when that lease is omitted.
- Runtime overlay still reports attached/degraded/detached correctly after the
  scoped cleanup.

Implementation note:

- Prefer using existing `SessionConnection` fields if they already carry device
  or external machine identity.
- If the current model cannot represent ownership, add the smallest additive
  nullable field needed and backfill only where evidence exists.

Test gates:

- `uv run --project server pytest server/tests_lite/test_heartbeat_endpoint.py server/tests_lite/test_timeline_runtime_overlay.py`
- `make test`

### Phase 3: Snapshot No-Op Optimization

Acceptance criteria:

- Engine includes a stable resolved-session snapshot signature or sequence in
  the heartbeat/status payload.
- Server skips missing-lease work when the same device repeats the same
  resolved session snapshot and freshness has already been observed.
- Signature excludes volatile timestamps that change every heartbeat.
- Tests show a changed state/phase/control path updates the signature and does
  process.

Test gates:

- `make test-engine`
- heartbeat endpoint tests
- a lightweight hosted or local smoke proving heartbeat ingest still updates
  liveness after a real snapshot change

### Phase 4: Heartbeat-Only Session Semantics

Acceptance criteria:

- Heartbeat-only canary/internal rows are distinguishable from transcript-backed
  user sessions in API output or QA selection.
- `qa-live` no longer needs to blindly try 25 sessions to find a transcript
  detail page.
- Session detail QA fails clearly if no transcript-backed sessions are
  available, instead of passing on internal telemetry rows.
- The product story remains capability-based: no new user-facing species of
  session unless there is a real product need.

Test gates:

- `make qa-live`
- `make test-e2e` if web selection behavior changes
- targeted API tests for any new query/filter/capability field

### Phase 5: End-To-End Ship And Dogfood

Acceptance criteria:

- `make test-ci` passes before push.
- `make test-e2e` passes if UI/runtime behavior changed.
- `make ship SHA=<exact-sha>` passes.
- Demo and canary report the exact shipped SHA healthy.
- `make qa-live` passes after hosted deploy.
- `make dogfood-refresh` runs and the menu bar is restarted.
- `make dogfood-check` reports healthy/green locally.

## Risks

- A device-scoping fix may reveal that `SessionConnection` lacks an adequate
  ownership field. Additive schema is acceptable before launch, but avoid broad
  session-kernel refactors.
- Snapshot signatures can accidentally include timestamps and become useless.
  Tests must lock the stable field set.
- Filtering heartbeat-only rows too early in `/api/agents/sessions` could hide
  useful machine telemetry from agent callers. Prefer explicit capability or QA
  selection semantics over silent removal.

## Review Plan

- Hatch Opus reviews this spec before implementation starts.
- Hatch Opus reviews after Phase 2 because that is the highest correctness
  risk.
- Hatch Opus and Hatch DeepSeek review after Phase 5 before final report.
