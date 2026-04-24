# Transport Health Single Source Of Truth

Status: Draft
Owner: launch product
Updated: 2026-04-23

## Goal

Make local Health/menu bar and hosted Health derive machine transport state
from the same reducer so they stop disagreeing on the meaning of the same raw
shipping counters.

This slice is specifically about **shipping transport health**, not every
machine-health concern.

## Problem

Today both surfaces read the same engine-originated heartbeat/status payload
shape, but they classify it differently:

- hosted Health uses `agent_heartbeat_health.py`
- local Health/menu bar uses `local_health.py`

That creates avoidable drift:

- one surface can downgrade on a transport error pattern the other ignores
- one surface can look healthy while the other looks degraded even though the
  underlying transport counters match
- future agent callers have no single derived transport answer to rely on

The recent `menu bar healthy` vs `hosted machine degraded` confusion was partly
freshness lag, but the deeper issue is that transport classification lives in
two places.

## Decision

Introduce one canonical Python transport-health reducer and have both surfaces
consume it.

Layers:

1. **Raw transport truth**
   - engine heartbeat/status payload fields
   - examples: `spool_pending`, `spool_dead`, `consecutive_failures`,
     `ship_connect_errors_1h`
2. **Shared derived transport truth**
   - one reducer computes:
     - `status`
     - `status_reason`
     - `status_summary`
     - `reasons`
3. **Surface overlays**
   - hosted adds heartbeat freshness and fleet filtering
   - local adds engine-status freshness, install/config drift, outbox age,
     managed-session control state, and build drift

The shared reducer owns transport semantics such as:

- dead letters
- payload rejection / too-large rejection
- reported offline state
- parse errors
- consecutive failures
- connect/server/rate-limit/retryable-client error bursts
- pending spool state

It does **not** own:

- heartbeat staleness
- local status-file freshness
- service stopped / not installed
- managed-session attachment or bridge state
- launch/config repair state
- disk warnings

Those remain surface-specific overlays.

## Non-Goals

- no attempt in this slice to make local health call the hosted API
- no menu bar redesign
- no startup heartbeat cadence change
- no fleet/admin dashboard change
- no redefinition of every local `headline`; only transport derivation is being
  unified

## Success Criteria

1. A single reducer module exists for transport-health assessment.
2. Hosted machine health uses that reducer for transport classification.
3. Local health uses that reducer for transport classification instead of
   re-deriving offline/failure/burst semantics inline.
4. Given equivalent transport payload values, hosted and local derive the same:
   - `status`
   - `status_reason`
   - `status_summary`
   - transport `reasons`
5. Surface disagreement is only allowed when freshness or local-only overlays
   differ, and that difference is explicit in code and tests.
6. Local health exposes a machine-readable `transport_health` block so future
   agent callers can consume the canonical derived transport answer directly.

## Rollout

1. Add the shared reducer module and unit tests for the raw transport contract.
2. Move hosted machine health onto the shared reducer without changing hosted
   API shape.
3. Move local health onto the shared reducer for transport semantics while
   keeping local-only overlays intact.
4. Add parity tests that prove hosted and local agree for the same raw
   transport inputs.
