# Local Session Truth Hardening

Status: Reviewed, implementation started
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

### Decision: Add Explicit Device Scope Before Optimizing Snapshot Work

**Context:** `mark_missing_managed_control_leases()` currently uses connection
freshness as a conservative proxy because the old managed-state table no longer
indexes previous rows by device.

**Choice:** First make missing-lease cleanup device-scoped with an explicit
nullable `SessionConnection.device_id`, then add cheap snapshot no-op
detection.

**Rationale:** Freshness as a proxy is unsafe under cross-device heartbeats,
clock skew, or out-of-order delivery. Correctness beats avoiding writes.
Optimization should not hide cross-device detach bugs.

**Revisit if:** Runtime connection ownership moves out of `SessionConnection`
into a dedicated per-device lease table.

### Decision: Unknown Device Ownership Is Sticky, Not Missing

**Context:** Existing rows will not have `device_id` populated at migration
time.

**Choice:** `device_id IS NULL` means "unknown owner"; missing-lease cleanup
must not detach those rows. Only a positive heartbeat can claim and update
them.

**Rationale:** This avoids mass-detaching legacy or ambiguous rows on the first
post-deploy heartbeat.

**Revisit if:** We add a reliable backfill from durable heartbeat history.

### Decision: Narrow Heartbeat-Only Session Semantics For This Slice

**Context:** Canary visibility has already been partially tightened in prior
commits, and `qa-live` already tries multiple candidates.

**Choice:** This spec will audit and pin the current canary/internal filtering
rule, but it will not introduce a generic transcript-backed session capability.

**Rationale:** A generic capability flag may be useful later, but it is a
larger product/API design. The launch risk here is accidental internal canary
pollution, not a full transcript-state taxonomy.

**Revisit if:** Non-canary heartbeat-only sessions become common in real user
journeys.

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
- A lightweight session snapshot signature to skip no-op missing-lease work
  when the resolved snapshot is unchanged.
- An audit and pinned tests for canary/internal session filtering.
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

### Phase 1: Device-Scoped Missing Lease Cleanup

Acceptance criteria:

- `SessionConnection` has a nullable `device_id` column.
- Positive managed lease upserts stamp `SessionConnection.device_id` with the
  heartbeat `device_id`.
- `mark_missing_managed_control_leases()` detaches only attached/degraded
  connections whose `device_id` equals the heartbeat `device_id`.
- `device_id IS NULL` rows are never detached by missing-lease cleanup.
- A heartbeat with `sessions=[]` from device A cannot detach a connection last
  refreshed by device B.
- A heartbeat from device A still detaches device A's previously attached lease
  when that lease is omitted.
- Runtime overlay still reports attached/degraded/detached correctly after the
  scoped cleanup.
- A rollback flag exists to disable missing-lease detach behavior for one
  release if dogfood finds an unexpected detach regression.
- A two-device integration-style test holds disjoint attached connections for
  devices A and B, heartbeats A with `sessions=[]`, and verifies B remains
  attached.

Test gates:

- `uv run --project server pytest server/tests_lite/test_heartbeat_endpoint.py server/tests_lite/test_timeline_runtime_overlay.py`
- `make test`

### Phase 2: Pin Canonical Contract Edge Cases

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

### Phase 3: Snapshot No-Op Optimization

Acceptance criteria:

- Engine includes stable resolved-session snapshot fields in the
  heartbeat/status payload:
  - `sessions_digest`: deterministic hash over an allowlist of identity/control
    fields
  - `sessions_sequence`: monotonic sequence that increments when the allowlist
    changes
- The digest allowlist is explicit. It includes session id, provider,
  provider-session id, control path, presentation state, normalized state,
  phase, tool name, workspace identity, process pid, bridge status, thread
  subscription status, and reason codes. It excludes heartbeat timestamps,
  observed-at timestamps, disk/build counters, and raw evidence timestamps.
- Server treats cold-start or missing digest as full work.
- Server skips missing-lease scan work when the same device repeats the same
  digest and freshness was already observed.
- Server still updates heartbeat freshness/`last_health_at` as needed on every
  heartbeat; the no-op path skips only redundant missing-lease scan/upsert work.
- Server records an observable counter/log for skipped snapshot work, such as
  `heartbeat.snapshot_skipped`.
- Tests show a changed state/phase/control path updates the signature and does
  process.
- Tests show heartbeat timestamp-only changes do not change the digest.

Test gates:

- `make test-engine`
- heartbeat endpoint tests
- a lightweight hosted or local smoke proving heartbeat ingest still updates
  liveness after a real snapshot change

### Phase 4: Canary/Internal Session Visibility Audit

Acceptance criteria:

- Existing `provider=canary` filtering is documented and covered where it
  matters: timeline, search/session list, and live QA candidate selection.
- `qa-live` uses a clear transcript-backed or non-internal candidate rule
  rather than relying only on incidental ordering.
- Session detail QA fails clearly if no suitable candidate exists.
- No new user-facing session species is introduced.

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
- A two-device dogfood or integration check verifies one device's empty
  snapshot does not flip another device's managed session state.

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
