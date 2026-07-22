# Local Health: Cursor Lineage, Managed Control, and Menu Recovery

**Status:** Finalized after independent review; implementation not started
**Owner:** Longhouse Machine Agent and macOS app
**Investigated:** 2026-07-22
**Related:** `immutable-source-outbox.md`, `macos-launch-product-shape.md`,
`storage-failure-isolation.md`

## Decision

Treat the current red macOS state as three independent faults, not one machine
or Runtime Host outage:

1. **Durable Cursor history is quarantined.** The Machine Agent skipped
   non-empty predecessor epochs after rapid Cursor database rewrites, then sent
   successors whose immediate predecessor had never existed on the Runtime
   Host. The host correctly rejected the lineage gap.
2. **One old Codex Helm control bridge is terminally detached.** It discovered
   an unrelated provider thread while its recorded thread could no longer be
   resumed, but remained alive and degraded indefinitely.
3. **The menu cannot present high-cardinality truth.** It sizes itself to all
   34 session rows, producing a 4,902-pixel-tall panel and pushing system facts
   and repair guidance off screen.

Fix these without deleting source evidence, weakening source-epoch admission,
or conflating live control, live shipping, durable quarantine, and presentation
health.

## Incident snapshot

Evidence was collected from the installed Mac build and hosted dogfood Runtime
Host at commit `d078bcd9eca6ab3476e0859dae9c4a7afd6d6dba`.

### What is healthy

- `https://david010.longhouse.ai/api/health` reports healthy and `/api/readyz`
  reports ready.
- The Mac control WebSocket is connected and exposes the expected provider
  operations.
- The live storage lane is succeeding; the ordinary spool and archive backlog
  are empty.
- The hosted container is healthy on the same build.
- The `zerg` root filesystem is 32% used (`23G / 75G`), down from the earlier
  disk-pressure incident. Current disk pressure is not causing this state.

### What is unhealthy

The local snapshot reports:

```text
health_state                 broken / red
headline                     Longhouse lost managed session control
blocked durable sources      480
blocked raw request bytes    661,071,389 in the local DB
blocked payload bytes        344,277,018 in the health projection
oldest block                 2026-07-21T23:15:50Z
repair-lane attempts (1h)    4,800 rejected + 6 successful total attempts
managed sessions             33 attached, 1 degraded
control channel              connected
spool / archive pending      0 / 0
```

The 480 rows are all Cursor `truncation` epochs blocked as
`source_epoch_conflict_unresolved`. The local shipper DB is 6.8 GiB, so the
quarantine also has a meaningful local storage cost even though it is not the
cause of VPS pressure.

## Root cause 1: the scheduler violates source lineage order

Cursor's SQLite stores can rewrite and truncate in quick succession. The local
registry correctly records each observed epoch and preserves its raw records,
but preparation currently follows the current source epoch rather than the
oldest undrained non-empty epoch in its lineage.

A representative blocked chain is:

```text
Runtime Host open
5b527268  initial     132 raw records, durable through 132
    |
    v
ee2c9e81  rewrite     121 raw records, durable through 0, no pending envelope
    |
    v
d4741b7c  truncation    0 raw records, durable through 0, no pending envelope
    |
    v
7c958930  truncation   61 raw records, frozen pending envelope, blocked
```

The Runtime Host contains only `5b527268` and correctly keeps it open. The
successor request names `d4741b7c` as its predecessor, but neither `d4741b7c`
nor `ee2c9e81` was admitted. The current reconciliation then asks for the
manifest of `7c958930`; its 404 is expected because the rejected epoch was never
created, so the client quarantines it permanently.

This pattern is corpus-wide:

```text
blocked roots                         480
maximum local lineage depth             7
unshipped non-empty predecessors       480
raw records in those predecessors   59,488
empty unshipped predecessors            480
```

The raw evidence has not been discarded. It is in
`cursor_store_raw_record`, but ended epochs are not being drained in lineage
order. The server is enforcing the right invariant; accepting an arbitrary
successor or deleting the block would hide data loss.

### Required design

Separate two concepts:

- **observed predecessor:** every local Cursor rewrite/truncation, retained for
  provenance and diagnostics;
- **wire predecessor:** the nearest prior non-empty epoch that the Runtime Host
  has durably admitted. A descendant is not prepared until that receipt exists.

The durable scheduler must build a lineage worklist for each opaque source:

1. walk from the current epoch back to the last host-admitted ancestor;
2. select the oldest epoch with raw records beyond its durable cursor;
3. freeze and ship that epoch before any descendant;
4. do not materialize empty local epochs on the wire;
5. after its receipt, use that epoch as the wire predecessor for the next
   non-empty descendant.

Local provenance remains complete while the host receives a contiguous chain
of epochs that actually carry durable evidence.

### Conflict authority

Recovery first uses the existing authenticated manifest endpoint to probe the
locally observed chain, newest to oldest, and identify the nearest admitted
ancestor. That is sufficient for the observed corpus and avoids making a
server change a prerequisite for a client lineage bug.

If no local ancestor has a manifest, or more than one candidate is open, the
client refuses repair. A later 409 improvement may add a reason code (`missing
predecessor`, `closed predecessor`, or `identity mismatch`) and the current
open epoch summary. That is optional hardening, not recovery authority and not
a broad source-list endpoint.

The client may supersede a rejected frozen request only when all of these are
proven:

- the requested epoch is absent remotely;
- the host's open epoch is an ancestor in the local observed chain;
- every intervening non-empty epoch is preserved locally and will be shipped
  first;
- every skipped epoch is empty and has no receipt, pending request, durable
  cursor progress, or hosted manifest;
- the replacement retains the exact raw record bytes, range, hashes, and
  envelope id, changing only the wire predecessor;
- a compare-and-swap proves that the old frozen request body is still current;
- the old request bytes, new request bytes, reason, and proof are recorded
  durably for audit and restart.

`predecessor_source_epoch` is deliberately outside the current
`EnvelopeIdentity`, so the envelope id remains stable while the serialized
request body changes. This is a narrow audited exception to exact-body retry,
permitted only after the rejected epoch is proven never admitted. The Runtime
Host has no exact-replay state for an epoch that does not exist. If any receipt
or manifest exists for the requested epoch, supersession is forbidden. The
same exception and proof must be added to `immutable-source-outbox.md` before
implementation.

Anything that fails this proof remains quarantined. No cursor advances and no
bytes are discarded.

### Existing 480-source recovery

Add a bounded, restart-safe reconciliation operation used by the engine and
exposed through `longhouse machine repair`. It must reuse the normal corrected
lineage scheduler instead of creating a second envelope-preparation system:

1. inventory blocked roots and print a read-only plan;
2. probe manifests along the observed chain and identify the nearest admitted
   ancestor;
3. persist a per-opaque-source repair state that gates ordinary durable
   preparation while allowing live observation to continue capturing records;
4. release quarantine for that source once and let the normal lineage-aware
   scheduler prepare every preserved non-empty epoch, oldest first;
5. collapse only ended, proven-empty local epochs from the wire lineage; never
   collapse a current empty epoch because it may gain records;
6. supersede the rejected descendant request with an audit row;
7. ship at repair-lane concurrency and backoff limits;
8. clear repair state only after exact durable receipts close the full chain.

The observed corpus contains at least 480 non-empty undrained predecessors and
59,488 records in them, but maximum lineage depth is seven. The dry run's exact
epoch, record, and byte totals are authoritative; recovery must not assume one
predecessor per root. Direct SQLite edits, deletes, cursor jumps, and broad
server-side acceptance are forbidden.

### Same-source concurrency

Repair is serialized by durable state, not a process-local mutex held across
network I/O. While an opaque source is reconciling:

- Cursor observation and raw-record capture continue;
- ordinary live and repair selectors cannot prepare or supersede that source;
- only the persisted lineage repair state selects the next epoch;
- a new rewrite appends to the observed chain and is drained after the already
  selected ancestors;
- receipt acknowledgement and request supersession use compare-and-swap.

A crash or concurrent daemon/manual repair resumes from this state. It cannot
prepare a descendant between ancestor receipt and descendant supersession.

## Root cause 2: quarantine is counted as repeated network failure

`ship_next_envelope` returns `StorageV2SourceBlocked` every time a scheduler
revisits an already-blocked row. The daemon records those no-I/O visits as
`payload_rejected`. That turns 480 one-time quarantines into roughly 4,800
apparent HTTP rejections per hour and marks the entire transport broken even
while the live lane succeeds.

### Required design

- An already-quarantined row is a `blocked_no_attempt` scheduling result, not a
  ship attempt and not a payload rejection.
- A newly received 409 increments the rejection counter once and records the
  transition into quarantine.
- Quarantined sources are excluded from ordinary live/repair selection until a
  reconciliation version or explicit repair makes them eligible once.
- Backoff and batch limits apply to reconciliation; 480 rows must never create
  a tight request loop.
- Health continues publishing its existing independent facts, but classifies
  them correctly:

```text
local_agent       running | unavailable
control_channel   connected | disconnected
live_shipping     healthy | delayed | failing
durable_archive   clear | pending | blocked
managed_control   attached/degraded counts
status_freshness  fresh | aging | stale
```

This is a correction to the existing local-health/menu facts, not a new health
protocol. Headline priority is deterministic: install/service failure, systemic
control-channel failure, durable evidence block, explicit needs-user state,
per-session detach, then freshness/ordinary activity. A single detached session
never outranks a durable evidence block or becomes systemic control loss.

The current truthful headline is “Durable upload blocked for 480 Cursor
sources,” with “Live shipping and remote control connected” visible beside it.
Do not say “backlog” for quarantined work and do not say the Runtime Host is
unavailable when it is serving and the control channel is connected.

## Root cause 3: a terminal Codex detach is modeled as endless degradation

Session `c3336106-d771-49ab-9737-d0a07edbe2ba` controls Codex thread
`019f7d0e-fe10-7d43-a446-b3a96c2e5260`. Its app server reports no rollout for
that thread and emits notifications for an unrelated thread. The bridge marks
`provider_thread_switched`, rejects the unrelated thread, and then remains
alive with a stale heartbeat from 2026-07-20. Both bridge and app-server
processes still exist.

This is not a transient “waiting for turn” condition. It is a terminal loss of
control for one session. The bridge already stops subscription work after
`provider_thread_switched`; the remaining loop is lifecycle, stale projection,
and repeated rejected-thread notifications. It also does not mean all managed
control is lost: 33 other sessions remain attached.

### Required design

- `provider_thread_switched` is a terminal per-session control state:
  `detached(provider_thread_switched)`, not a retrying degraded state.
- Preserve the existing subscription no-op and stop repeated rejected-thread
  notification logging after the transition.
- Continue a fresh lightweight bridge heartbeat only while the user's upstream
  TUI/proxy connection is genuinely attached; process existence alone is not
  attachment.
- Automatic reaping is allowed only when the spawn record contains the process
  group and identity and a pre-kill check matches PID, process start time, argv,
  ownership, no UI/proxy attachment, no active turn, and no owned thread. Any
  missing or mismatched fact defaults to no-kill and an explicit cleanup action.
- If the TUI remains attached, keep pass-through behavior but expose no control
  capability and do not advertise the bridge as healthy.
- Machine health reports “1 session lost remote control,” not “Longhouse lost
  managed session control.” A single detached historical session is inspect
  severity; machine-wide red is reserved for a currently required control path
  or systemic control-channel failure.

## Root cause 4: the macOS panel has no viewport contract

`MenuBarPanelSizing.measuredSize` uses the complete SwiftUI fitting height with
no screen bound. `MenuBarPanelView` renders every managed session before system
facts and repair guidance, and `StatusWindowController` applies that full
height. The live fixture produced a 752 x 4,902 PNG. The ordinary window capture
failed while raw rendering confirmed the oversized composition.

### Required design

- Keep the header and primary attention summary outside the scroll region.
- Clamp the window to the active screen's visible frame with a small menu-bar
  margin; never exceed the available height.
- Put session rows and secondary facts in a vertical `ScrollView`.
- Order attention first: detached/degraded sessions, durable block summary,
  active sessions, then idle sessions.
- Keep the system facts and repair/open action reachable without traversing 34
  rows. A compact top summary may link to the full session list; it must not hide
  the fact that sessions are alive.
- Use the same explicit viewport contract in the real window and snapshot
  renderer so visual QA represents the shipped composition.
- Add a high-cardinality fixture containing 34 managed sessions, one detached
  bridge, and 480 blocked sources.

## Implementation sequence

### Phase 0 — lock the evidence and semantics

- Add regression fixtures for the four-epoch Cursor chain above.
- Add a depth-seven chain, a chain with two or more non-empty undrained
  ancestors, and an all-ended-empty chain.
- Correct classification of the existing independent health facts and remove
  ambiguous “backlog”/“Runtime Host unavailable” assertions from fixtures.

Gate: tests fail against the current lineage selection, retry accounting,
bridge lifecycle, and 4,902-pixel panel.

### Phase 1 — stop creating lineage gaps

- Make Cursor durable selection lineage-aware and oldest-undrained-first.
- Add prepare-by-source-epoch from frozen `cursor_store_raw_record` evidence;
  it must not call live `observe_source` for an ended epoch.
- Introduce explicit wire-predecessor state without rewriting observed local
  provenance.
- Collapse only ended, empty, unadmitted epochs.
- Prevent descendants from preparing while a non-empty ancestor is undrained.

Gate: rapid rewrite -> empty truncation -> non-empty truncation ships every raw
record in order and leaves one open host epoch.

### Phase 2 — add bounded conflict proof and recovery

- Probe existing per-epoch manifests along the observed local chain.
- Persist per-source repair state, reconciliation proof, and audited
  predecessor-only request supersession.
- Release quarantine once and recover through the normal lineage scheduler at
  bounded repair concurrency.
- Add structured 409 reason/authority details only if manifest probes cannot
  establish the required proof.

Gate: dry-run counts match the preserved local corpus; apply reaches zero
blocked sources with exact receipts and no cursor jump or raw-record deletion.

### Phase 3 — correct scheduling and health

- Add `blocked_no_attempt` and stop counting quarantined scans as HTTP attempts.
- Exclude a blocked opaque source before live-path rediscovery reaches request
  preparation.
- Publish live and durable lanes independently.
- Select headlines from the failed product promise, not a severity-wide union
  of unrelated reasons.

Gate: 480 quarantined fixtures generate zero ordinary ship attempts while a new
live source ships successfully and health reports both facts. Bounded manifest
probes initiated by explicit repair are counted separately.

### Phase 4 — terminalize detached Codex control

- Materialize terminal `provider_thread_switched` facts.
- Stop rejected-thread notification spam and safely reap only process groups
  that pass the full ownership/attachment proof; otherwise leave an explicit
  cleanup action.
- Project per-session degradation without machine-wide control loss.

Gate: an unrelated thread cannot be adopted, the original session becomes
detached once, log volume stays bounded, and another Helm session remains
controllable.

### Phase 5 — make the menu bounded and attention-first

- Add screen-aware sizing and scrolling.
- Reorder the attention/system facts and share sizing with snapshots.
- Prove window and raw snapshot capture for the high-cardinality fixture.

Gate: the panel fits the active screen, repair and open actions are reachable,
and the snapshot harness succeeds.

## Validation matrix

| Area | Required proof |
|---|---|
| Cursor capture | All 59,488 predecessor records plus descendant records receive exact receipts in lineage order |
| Crash safety | Restart at plan, predecessor receipt, supersession, and descendant receipt boundaries converges once |
| Conflict safety | Missing authority, changed byte, hosted non-ancestor, or non-empty skipped epoch refuses repair |
| Ended preparation | Preparing a frozen ended epoch performs no live source observation or epoch rotation |
| Concurrency | A same-source rewrite during repair is captured but cannot prepare ahead of undrained ancestors |
| Supersession | CAS failure, any hosted receipt/manifest, or changed raw identity refuses body replacement; restart preserves both audit bodies |
| Scheduling | Quarantined N does not consume live slots or increment network-attempt counters |
| Runtime Host | Existing exact replay and strict predecessor admission remain intact |
| Codex Helm | Thread switch terminalizes only that session; unrelated process/thread is never killed or adopted |
| Health | Connected control + healthy live lane + blocked durable lane render as independent facts |
| macOS | 34-session fixture remains screen-bounded in light/dark modes and actions stay reachable |
| Dogfood | Zero blocked sources, bounded repair traffic, live shipping healthy, stale bridge resolved, panel capture green |

Use targeted engine, backend, and macOS tests during each phase, then the normal
dogfood refresh and exact-SHA ship verification at cutover.

## Non-goals

- No destructive cleanup of preserved local or hosted raw evidence.
- No relaxation of source-epoch predecessor validation.
- No new queue, database, or background service.
- No automatic adoption of an unrelated provider thread.
- No claim that process existence equals a live control attachment.
- No redesign of the full browser timeline inside the menu bar.

## Review record

### Hatch Claude Fable — REQUEST_CHANGES

Confirmed all four root causes. Required explicit envelope-id/body semantics and
same-source repair exclusion. Recommended reusing the corrected scheduler,
collapsing ended-empty epochs only, deterministic headline priority, safe
process identity checks, and additional empty/deep-lineage fixtures. All were
accepted above.

### Hatch Cursor Grok — REQUEST_CHANGES

Confirmed all four root causes. Required prepare-by-ended-epoch, multi-hop
recovery, CAS/audit for the immutable-body exception, no-kill-on-uncertain Codex
cleanup, and the missing race/crash tests. It recommended manifest probes before
adding server authority and narrowing the health work to existing axes. All were
accepted above.

### Final disposition

The reviewers' request-changes verdicts applied to the first draft. The final
plan incorporates every blocking finding. Non-blocking simplifications were
also adopted: one lineage scheduler for prevention and recovery, no required
server API change, no new health protocol, and automatic Codex process cleanup
only when ownership and detachment are fully proven.

Both reviewers then re-read the revised spec. Hatch Claude Fable returned
**APPROVE** with no blockers, followed by Hatch Cursor Grok returning
**APPROVE** with no blockers.
