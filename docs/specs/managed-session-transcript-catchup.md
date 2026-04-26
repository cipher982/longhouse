# Managed Session Transcript Catch-Up

## Problem

Managed sessions can reach a terminal phase before the final transcript rows are
durably ingested. In the observed Claude session `73c0f01d-e813-4673-a348-ea8b8e805dc0`,
the hook/outbox path delivered `idle` and `needs_user` quickly, but the final
assistant text shipped only after fallback scan. During that gap, web and iOS
could still render the last durable rows as active tool calls.

Filesystem watcher events are useful wakeups, but they are not a correctness
contract. Terminal hook signals are stronger evidence that the transcript file
must be tailed to EOF.

## Goal

When a managed provider emits a stop/idle/needs-user phase, Longhouse should
ship the corresponding transcript tail within a few seconds, even if the OS file
watcher missed the append.

## Design

1. Keep the filesystem watcher as the low-latency happy path.
2. Add an engine-side active catch-up queue keyed by `session_id`.
3. When outbox drain persists or posts a phase signal, return the latest
   `(provider, session_id, phase)` to the daemon.
4. Resolve that session to a known transcript path from `file_state` /
   `session_binding`.
5. For active phases (`thinking`, `running`), enqueue one catch-up pass.
6. For terminal/attention phases (`idle`, `needs_user`, `blocked`), enqueue a
   short delayed sequence: immediate, +1s, +3s. This covers providers that write
   final rows just before, during, or just after the stop hook.
7. Each catch-up pass reuses the normal shipper path. Shipping stays idempotent
   through byte offsets and event UUIDs.

The server/UI watermark work is deferred unless testing still shows stale UI
after engine catch-up. The immediate product bug is the transcript tail not
being shipped, not the reducer model.

For managed sessions, the launcher/hook binds the transcript path before the
phase outbox file is written, so catch-up can resolve the path even before a
first successful transcript ship. For unmanaged sessions, catch-up uses
`file_state`; a first-ever unmanaged turn that has never shipped remains
covered only by watcher/fallback scan.

## Success Criteria

- Local engine unit tests prove terminal outbox phases schedule multiple
  transcript catch-up passes for the bound transcript path.
- Local engine unit tests prove active phases schedule a single catch-up pass.
- Existing outbox coalescing and local phase persistence behavior remains
  unchanged.
- `make test-engine` passes.
- `make test` passes for backend/API fallout.
- End-to-end dogfood repro:
  - Start or use a managed Claude session.
  - Send a short turn through Longhouse.
  - Verify hosted receives the final assistant row within 5 seconds of the
    terminal hook event.
  - Verify local `file_state.acked_offset` reaches the transcript byte size
    without waiting for the fallback scan interval.
- Production verification:
  - Runtime image for the exact commit is deployed to demo and hosted canary.
  - `make qa-live` passes.
  - Dogfood machine is refreshed with `make dogfood-refresh` and app restart.
  - Hosted logs show final assistant ingestion within 5 seconds for a fresh
    managed Claude test turn.

## Non-Goals

- Do not introduce another transcript ingest stack.
- Do not rely on direct network I/O inside provider hooks.
- Do not reduce the fallback scan interval globally as the primary fix.
- Do not change provider binary ownership or managed/unmanaged terminology.
