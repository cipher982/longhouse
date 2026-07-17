# Transcript Convergence

Status: Implemented for current Cursor Shadow and Helm sources
Last updated: 2026-07-17

## Product contract

Longhouse is useful only when a session selected in Longhouse is readable in
Longhouse. Control is additive; it is never a substitute for the transcript.

> If a session is shown as active, searchable, or openable, Longhouse must
> either render its durable thread or state a concrete, retrying ingest failure.
> It must never return a missing-session error for a session it still advertises.

When the Machine Agent and Runtime Host are reachable, newly appended provider
transcript data must become durably readable on the Runtime Host within ten
seconds at p95. Historical source bytes must converge continuously whenever
safe capacity exists.

## Incident evidence

On cinder, the engine queued 245 historical Claude/Codex transcript ranges
totalling 7,469,578,965 source bytes. The largest are individual 759 MB Claude
JSONL files; many Codex JSONL files range from 75 MB to 363 MB. Archive
throughput was zero because the service ran with `--archive-repair-mode paused`
and a one-hour manual drain override had expired. Commit `0492af78b` corrects
that specific default/fallback behavior: hosted installs now default to
`trickle`, and expired non-pause overrides return to the installed mode rather
than silently forcing `paused`. Existing installed services still require
regeneration to receive the new flag.

Cursor Helm now binds the native storage-v2 source through provider-minted
identity plus matching hook/store evidence. Current Cursor text, reasoning,
tool calls, and tool results render durably, and native hook wakes keep active
turns converging without waiting for a broad filesystem scan. Cursor's source
contract remains governed by `cursor-storage-v2-source-fidelity.md`.

## Definitions

- **Live transcript**: newly appended source bytes for a currently active
  provider session.
- **Durable transcript**: ordered, raw-preserving event/source evidence that
  has been acknowledged by the Runtime Host and can be rendered after restart.
- **Convergence**: the gap between local provider source offsets and host-acked
  offsets decreases whenever the machine and host are healthy.
- **Watchable**: a session whose thread can be opened and read in browser/iOS.
  A session is not watchable merely because it has a control socket.

## Invariants

1. **No advertised-session 404.** A live/session-list entry either has a
   durable workspace or returns a bounded `syncing` workspace backed by local
   recovery evidence. A real 404 means the session is neither durable nor live.
2. **Transcript before watchability.** Providers without a transcript source
   are not advertised as watchable. They may expose a clearly labelled
   control-only capability only on an explicit technical surface while source
   support is being developed.
3. **Always-on convergence.** Pending transcript ranges are active work, not
   a manual repair queue. The existing Live/Retry/Scan scheduler drains them
   whenever there is safe capacity. An explicit user pause is the sole normal
   stop condition.
4. **Live wins, archive runs.** Live/control work has strict reservation;
   archive work uses all remaining safe capacity and decreases under observed
   pressure. Backpressure changes rate, never silently changes intent to stop.
5. **Raw evidence is retained.** Parser/renderer uncertainty cannot discard
   source bytes. Preserve source-faithful records first, interpret them later.
6. **Progress is observable.** Health exposes pending bytes, source/ack
   offsets, current bytes/sec, limiter state, most recent archive acknowledgement,
   and an ETA when a rate estimate is available.

## Desired flow

```text
provider append / native store change
  -> source watcher records changed byte range and session identity
  -> durable local pointer queue (offset, generation, provider, session)
  -> lane-aware scheduler
       L0 control / L1 live transcript / L2 current-session gap
       L3 historical convergence
  -> bounded parse + source-faithful batch
  -> Runtime Host admission and durable write acknowledgement
  -> host projection + SSE invalidation
  -> browser/iOS renders the same durable workspace
```

No stage may require an LLM, embedding, summary, or UI-derived inference.

## Provider requirements

### Codex and Claude

- Continue shipping changed JSONL byte ranges through the existing source
  cursor/spool path.
- Split large ranges at bounded source-byte and request-byte boundaries; retain
  acknowledged subrange offsets so a restart resumes rather than reprocesses a
  300+ MB source file.
- Reconcile current-session gaps ahead of broad historical work.

### Cursor

Cursor's native source work follows
[`cursor-storage-v2-source-fidelity.md`](cursor-storage-v2-source-fidelity.md).
Helm-to-store identity binding uses a provider-minted native chat ID plus the
exact launched process's hook evidence; heuristic binding remains forbidden.
Cursor becomes watchable only after that source has a durable host
acknowledgement and a renderable projection.

## Scheduler and host policy

- The normal installed mode is persistent `trickle` convergence, not `paused`.
- Explicit pause is durable and user-visible. Its control record includes
  `actor`, `reason`, and `updated_at`; it is the only ordinary zero-rate state.
  Expiry of a temporary non-pause override returns to the installed mode, never
  to an inferred pause.
- The existing Retry/Scan archive work starts conservatively and adapts its
  **tick budget** using live p95, host queue wait, host execution time, archive
  bytes/sec, and backpressure rate.
- On healthy headroom, archive throughput grows toward the safe limit. On pressure, it backs
  off multiplicatively and probes again. It never waits for manual resume.
- Existing subrange acknowledgement and 413 splitting remain the source-range
  mechanism; acceptance tests must prove bounded-memory restart-resume on a
  759 MB-class fixture. Small-first ordering remains, with aging so huge sources
  cannot starve forever. Huge-source failures quarantine that source rather
  than stopping smaller work.
- Runtime Host rejects archive work cheaply with typed retry-after pressure,
  while preserving L0-L2 capacity.

## Workspace behavior

- A durable transcript workspace returns its normal projection.
- A live session whose local source is queued but not host-acknowledged returns
  a `syncing` workspace: session identity, live state, byte/offset progress,
  retry evidence, and no misleading "no messages" claim. The Machine Agent
  heartbeat carries this per-session pending evidence; machine-level backlog
  aggregates are insufficient.
- A control-only provider without transcript support is not shown in normal
  watchable session lists. If opened through a direct technical URL, it states
  that transcript capture is unsupported; it does not pretend the thread is
  empty.
- An ended session with neither durable transcript nor recoverable local source
  is terminal with an explicit reason and remediation, not endlessly retried.

## Health and UX

The primary status answers one question: **are my sessions readable in
Longhouse?** It reports:

- live append-to-host p95 and current delayed session count;
- durable convergence state: current, catching up, blocked, explicitly paused,
  or failed;
- pending source bytes/ranges, acknowledged archive bytes/sec, ETA, oldest
  missing source age, and current limiter/host constraint;
- the largest pending sources and exact error class when blocked.

`Service running` is insufficient when durable convergence is materially
behind. The menu bar and web use the same machine-health facts and offer only
actions that preserve the invariant: inspect, retry/quarantine one bad source,
or explicitly pause.

## Rollout and recovery

1. Land cursor source capture plus the generic convergence/health contracts.
2. Run parser/source-fidelity fixtures and live-provider canaries.
3. Deploy Runtime Host and Machine Agent together; regenerate every machine
   installed with the old `paused` launchd/unit mode through `longhouse machine
   repair`.
4. Start convergence with bounded chunks and adaptive L3; do not reset or
   delete pending rows.
5. Verify immediate live transcript delivery with a fresh Codex and Claude
   session, then Cursor once its native source is wired.
6. Keep the backlog drain under observation until pending bytes, not merely
   range count, reaches zero. Investigate any range that stops making progress.

## Acceptance criteria

- A fresh Codex/Claude append appears in the hosted workspace within ten
  seconds at p95 across a restart and reconnect.
- A selected live session never produces a workspace 404.
- Cursor is either genuinely watchable from its native source or honestly not
  presented as watchable; no empty-thread fiction.
- With cinder's current backlog, archive acknowledgements become continuous,
  pending bytes monotonically decline over healthy intervals, and live p95 stays
  within SLA.
- An explicit pause is the only ordinary reason archive bytes/sec is zero while
  pending ranges exist; its reason is visible in local health and the menu bar.
- UI, API, and engine tests cover source offsets, retry/backpressure, large
  source chunking, crash restart, live-session workspace reads, and provider
  transcript capability truth.

## Non-goals

- No LLM work or enrichment in shipping.
- No second local transcript payload store; provider source plus pointer/cursor
  metadata remains sufficient.
- No silently dropping, dead-lettering, or hiding source evidence to make
  health look green.
