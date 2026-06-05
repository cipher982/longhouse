# Machine Presence Notification Policy

Status: Implementation spec
Date: 2026-06-04

## Context

Longhouse now has a conservative session-alerting baseline:

- `blocked` sends an immediate APNs attention push.
- unresolved `blocked` sends one reminder after 15 minutes.
- `long_run_waiting` sends "Ready for you" only after a run has been executing
  for at least 30 minutes and no recent visible Longhouse web client exists.

That avoids phone-buzz spam, but the 30 minute rule is a sharp cliff. It is too
slow when the user has clearly walked away, and still too noisy if the user is
actively at the machine but not viewing the Longhouse web UI.

Longhouse already runs a Machine Agent on the user's dev machines. That agent
can report coarse local presence as part of the same user-owned-machine story,
as long as it stays privacy-shaped and does not become general activity
monitoring.

## Goal

Use fresh, coarse machine presence to improve notification timing:

- suppress ready nudges when the user appears active on any owned machine;
- keep the 30 minute fallback when presence is unknown or stale;
- modestly lower the ready-nudge threshold only when there is positive away
  evidence, such as a fresh locked or long-idle machine state;
- never let local idle state alone make trivial chatty turns buzz the phone.

This is not a generic user-activity tracking feature. It exists only to make
Longhouse session notifications less annoying and more useful.

## Product Rules

### Event Classes

`blocked` stays urgent:

- page immediately when a session enters `blocked`;
- remind once after 15 minutes if still unresolved;
- do not suppress `blocked` because a web tab or machine is active.

`long_run_waiting` becomes presence-tiered:

- if the owner is actively present, suppress the push;
- if presence is unknown or stale, keep the existing 30 minute threshold;
- if the owner is plausibly away, lower the threshold but keep an independent
  meaningful-run floor;
- send at most one ready nudge per execution window.

### Presence Polarity

Presence is primarily a **suppression** signal, not a license to buzz sooner.

The server should ask:

1. Is the owner active somewhere? If yes, do not send a non-urgent ready nudge.
2. Is there fresh positive away evidence? If yes, use a lower ready threshold.
3. Is the signal unknown or stale? If yes, fall back to the current 30 minute
   behavior.

### Owner-Level Resolution

Resolve machine presence at the owner level, not the session-device level.

Why:

- Longhouse supports a Runtime Host on a separate always-on machine.
- A server, VPS, or hosted box may be idle forever, but that does not mean the
  human is away.
- Multi-machine users may be active on one machine while another dev box runs
  work.

Policy:

- fresh active evidence on any owned machine suppresses ready nudges;
- recently active machine evidence may suppress for a short grace window so a
  single missed presence post does not immediately lower thresholds;
- fresh visible web-client presence also suppresses ready nudges;
- away evidence requires at least one fresh machine presence signal and no fresh
  active/visible signal;
- absence of a signal is never treated as away.

## Privacy Rules

The Machine Agent may report only coarse presence:

- `active`
- `idle_5m`
- `idle_10m`
- `locked`
- `unknown`

Allowed fields:

- coarse state;
- measured timestamp;
- bucketed idle seconds or idle bucket;
- source such as `macos_hid_idle` or `unsupported`.

Forbidden fields:

- raw keyboard or mouse events;
- key names, mouse coordinates, or click counts;
- active app, window title, browser tab, shell command, or TTY foreground state;
- long-term activity history.

Presence is a now-signal. The implementation should avoid retaining a
fine-grained activity timeline. If heartbeat history retains raw payloads for 30
days, the server should either strip the presence block from historical raw JSON
or store only a latest/coarse derived presence row for notification decisions.

The user-facing settings surface should describe this as:

> Use local Mac idle state to time session notifications.

There must be a clear kill switch before this becomes a broadly distributed
default.

## Mac Collection

Use a no-permission idle-time path. The intended macOS implementation is a
coarse HID idle-time read, not an event tap.

Do not use APIs that require Accessibility or Input Monitoring prompts for this
feature. Longhouse should not capture input events.

Lock-state detection is useful but optional. If reliable lock state is not
available from the Machine Agent's launch context, report `unknown` or idle-only
instead of guessing.

Non-macOS platforms start as `unknown`.

## Freshness

The existing server heartbeat cadence is too coarse for a 5 to 10 minute idle
policy. Presence must be fresh before it can affect notification timing.

Minimum rule:

- trust machine presence only when the server received it within a short window,
  initially 90 seconds;
- otherwise treat it as `unknown`.

Implementation options:

1. Add a small dedicated machine-presence endpoint and post it on a short
   cadence from the Machine Agent.
2. Piggyback on existing heartbeat payloads only when heartbeat frequency is
   made short enough for presence freshness.

Preferred implementation for this slice:

- add a dedicated `/api/agents/machine-presence` endpoint using the existing
  device token auth;
- post coarse presence every 60 seconds while the Machine Agent is online;
- store only the latest presence per owner/device;
- keep normal heartbeat cadence unchanged.

## Server Data Model

Add a latest-state table, not a historical activity ledger:

`machine_presence`

- `id`
- `owner_id`
- `device_id`
- `state`
- `source`
- `idle_seconds`
- `measured_at`
- `received_at`
- `updated_at`

Uniqueness:

- `(owner_id, device_id)`

Indexes:

- `(owner_id, received_at)`
- `(owner_id, state, received_at)`

The endpoint should validate that `device_id` comes from the authenticated
device token, not an arbitrary client-supplied value.

## Notification Policy

Initial constants:

- `MACHINE_PRESENCE_FRESHNESS = 90s`
- `MACHINE_ACTIVE_SUPPRESSION_GRACE = 3m`
- `LONG_RUN_WAITING_THRESHOLD_UNKNOWN = 30m`
- `LONG_RUN_WAITING_THRESHOLD_IDLE_10M = 15m`
- `LONG_RUN_WAITING_THRESHOLD_LOCKED = 10m`
- `LONG_RUN_WAITING_MIN_MEANINGFUL_RUN = 5m`

Owner presence resolution:

| Fresh owner presence | Ready nudge behavior |
| --- | --- |
| visible web client | suppress |
| any machine `active` | suppress |
| any machine recently `active` within grace window | suppress |
| any machine `locked`, no active/visible signal | threshold 10m |
| all fresh machines at least `idle_10m`, no active/visible signal | threshold 15m |
| only `idle_5m`, no active/visible signal | threshold 30m |
| no fresh machine presence | threshold 30m |

The lowered threshold must still satisfy the meaningful-run floor. A two minute
turn should not push just because the laptop is idle.

The copy can stay "Ready for you" only when elapsed time is credible. If the
threshold is lowered below 10 minutes, consider softer copy such as "Longhouse
is waiting" or keep the floor high enough that "Ran 10m" feels honest.

## Implementation Phases

### Phase 0 - Spec And Review

- write this spec;
- ask Hatch Opus for a product/architecture review;
- incorporate the review into the spec before code.

### Phase 1 - Machine Presence Endpoint

- add `MachinePresence` model;
- add `POST /api/agents/machine-presence`;
- authenticate with `X-Agents-Token`;
- write through `WriteSerializer`;
- validate states, idle seconds, measured time, and device ownership;
- add backend unit/integration tests.

### Phase 2 - Engine Presence Producer

- add a small Rust machine-presence module;
- on macOS, collect coarse idle state without input-monitoring/event-tap APIs;
- on unsupported platforms, emit `unknown`;
- post to `/api/agents/machine-presence` on a 60 second cadence;
- include local status-file presence for menu bar/debug visibility if cheap;
- add Rust unit tests for bucketing and payload shape.

### Phase 3 - Presence Resolver And Notification Policy

- add a server helper that resolves owner-level presence from latest machine
  presence plus existing web-client presence;
- keep presence logic inside `prepare_long_run_waiting_push` so both presence
  and runtime routes inherit the same behavior;
- update the long-run threshold selection;
- keep `blocked` behavior unchanged;
- add backend tests for active suppression, stale fallback, idle_10m lowering,
  locked lowering, unknown fallback, multi-machine owner resolution, and
  no-duplicate ready nudges.

### Phase 4 - Review And Validation

- ask Hatch Opus after Phase 1/2 and again after Phase 3;
- fix all correctness blockers;
- run focused tests after each phase;
- run `make test`, `make test-frontend` if generated/types change,
  `make test-e2e`, and `make test-ci` before merge/push.

## Success Criteria

The feature is complete only when:

- implementation is on a new worktree branch;
- commits are small and coherent;
- Hatch Opus reviews the main large phases;
- many unit and integration tests cover the endpoint, engine bucketing, resolver,
  and notification policy;
- validation passes or any skipped/failed check is explicitly explained;
- the branch is merged or fast-forwarded to `main`;
- `main` is pushed to `origin`.

## Non-Goals

- Web Push / service worker notifications.
- Browser IdleDetector.
- Terminal foreground detection.
- App/window/title tracking.
- Cross-device behavioral analytics.
- Making `needs_user` globally interruptive.

## Open Questions

- Should the setting be default-on for local-only/self-hosted and default-off
  for hosted, or simply default-off everywhere until the UI is explicit?
- Can the current Machine Agent launch context reliably observe screen lock on
  macOS, or should v1 ship idle-only?
- Should lowered-threshold ready nudges use different copy from 30 minute
  "Ready for you" nudges?
