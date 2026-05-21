# Session Identity Kernel

Status: revised after Hatch Codex review (2026-05-21); ready for implementation
Owner: Longhouse session core
Created: 2026-05-21
Related:
- `VISION.md`
- `docs/specs/realtime-truth-plane.md`
- `docs/specs/durable-transcript-live-overlay.md`
- `docs/specs/managed-codex-liveness.md`

## Why this exists

`AgentSession` (`server/zerg/models/agents.py`) is a god-object. One row currently
carries product identity, provider thread identity, continuation lineage,
launch lifecycle, managed-control state, runtime/liveness projection, summary
revisions, embedding state, and UI capability hints. Every recurring "managed
session looks unmanaged," "subagent shadows parent," "resume creates a new
session," or "client and server disagree about steerability" bug traces back to
that collapse.

We are pre-launch with no external users. We can break the schema, rip the
overloaded columns, and finish on a smaller surface. This spec defines that
smaller surface.

## Scope

In scope:

- Separate identity nouns: session, thread, run, connection.
- `thread_id` migration on the transcript/runtime/turn/input child tables that
  are session-keyed today, so subagents and resume actually fix.
- Lightweight `thread_aliases` table to carry provider/source identity evidence
  without overloading a single column.
- Real `launch_attempts` table for remote-launch lifecycle.
- Single server-derived capability projection consumed by web, iOS, CLI.
- Kill path-bound managed identity (`session_binding(path → session_id)`).

Explicitly deferred (not in this epic):

- Canonical event hashing, revision chains, branch resolvers.
- Source artifact / artifact generation / artifact snapshot tables.
- Writer leases and concurrent-managed-writer arbitration.
- Stress harness with generated transcript traces.
- Cold-rebuild-from-observations contract.

The deferred items are real future work. They plug in *under* threads without
touching session or capability semantics again. We add them when copy-vs-fork
semantics or multi-writer contention is an actual product problem, not before.

## The four nouns

**Session** — product/UI identity. What appears in the timeline, in iOS, in
search. Owns title, workspace, archive state, primary thread pointer. Nothing
else.

**Thread** — Longhouse-owned causal continuity. Survives provider quit/resume.
Today there is one thread per session in nearly all cases; the table exists so
subagents and future continuations live somewhere without overloading session
identity. A subagent is a child thread under the same session, not a new
session.

**Run** — one provider CLI process invocation. Started at launch (or first
binding for unmanaged), ended on exit. Carries pid, host, cwd, started/ended,
exit status. Restarting a laptop and resuming the same thread creates a new
run; it does not create a new session or thread.

**Connection** — Longhouse's relationship to a run. Records control plane
(`codex_bridge`, `pty`, `runner`, `log_tail`, `none`), state (`attached`,
`detached`, `degraded`, `released`, `ended`), and capability flags. Bridge
dying mid-turn flips connection.state — it does not touch thread or session
identity.

## Capability projection

One server-side view, derived from `(thread, latest run, best connection,
latest run-keyed runtime_state)`:

```text
session_capabilities(session_id) -> {
  control_label,            # "live", "reattach", "search-only", "imported"
  live_control_available,
  host_reattach_available,
  observe_only,
  search_only,
  can_send_input,
  can_interrupt,
  can_terminate,
  can_tail_output,
  can_resume,
  staleness_reason,
}
```

Web, iOS, CLI, and `/api/agents/*` all read this. **No client infers managed
state from `execution_home`, heartbeat freshness, or process liveness alone.**
That inference path is the source of half the recurring bugs and is removed
when this projection ships — old hint fields are stripped from API responses,
not left alongside the new projection.

A live process is not proof Longhouse can steer it. Active transcript updates
are not proof of live control. Live control requires an attached or degraded
connection with the relevant capability.

### "Best connection" selection rules

Without writer leases, ranking must be deterministic in the spec, not the
implementer's head:

1. State priority: `attached` > `degraded` > `detached` > `released` > `ended`.
2. Capability priority within tied state: highest count of granted capability
   flags wins (`can_send_input`, `can_interrupt`, `can_terminate`,
   `can_tail_output`).
3. Recency tiebreak: greater `last_health_at` wins.
4. Final tiebreak: greater `connections.id` wins (creation order).

Selection happens at projection read time, not write time.

## Tables

```text
sessions(
  id,
  workspace_id,
  primary_thread_id,        -- nullable during create/backfill; set after root thread exists
  title,
  archived_at,
  created_at,
  updated_at,
  ui_state_json
)

session_threads(
  id,
  session_id,
  provider,
  parent_thread_id,         -- null for root; set for subagents/branches; self-FK
  parent_event_id,          -- nullable; replaces AgentSession.branched_from_event_id
  branch_kind,              -- root | subagent | continuation
  is_primary,               -- denormalized; matches sessions.primary_thread_id; defaults 0
  created_at,
  updated_at
)
-- Unique partial: one primary thread per session.
--   ux_threads_one_primary_per_session ON (session_id) WHERE is_primary = 1
-- ``is_primary`` defaults to 0 so subagent/continuation threads created without
-- an explicit override never silently become a second primary.

session_thread_aliases(
  id,
  thread_id,
  provider,
  alias_kind,               -- provider_session_id | longhouse_session_id | source_path | forked_from_provider_session_id
  alias_value,
  first_seen_at,
  last_seen_at
)
-- Unique within a thread: ux_thread_aliases_unique_per_thread
--   ON (thread_id, provider, alias_kind, alias_value)
-- Lookup index (non-unique): ix_thread_aliases_lookup
--   ON (provider, alias_kind, alias_value)
-- Aliases are evidence, not identity. The same alias may legitimately
-- appear on multiple threads (copied transcripts pre-divergence).

session_runs(
  id,
  thread_id,
  provider,
  host_id,                  -- runner/machine identity; routes commands
  boot_id,                  -- nullable; cheap insurance against pid reuse
  pid,
  process_start_time,
  cwd,
  argv_redacted_json,
  launch_origin,            -- longhouse_spawned | external_adopted
  started_at,
  ended_at,
  exit_status
)

session_connections(
  id,
  run_id,
  control_plane,            -- codex_bridge | pty | runner | log_tail | none
  acquisition_kind,         -- spawned_control | adopted_control | observe_only
  state,                    -- attached | detached | degraded | released | ended
  external_name,            -- nullable; replaces AgentSession.managed_session_name where attach/debug paths still need it
  -- typed capability gates instead of JSON: small, enumerated, queryable
  can_send_input,
  can_interrupt,
  can_terminate,
  can_tail_output,
  can_resume,
  capabilities_extra_json,  -- nullable; provider-specific diagnostics only
  acquired_at,
  released_at,
  last_health_at
)

session_launch_attempts(
  id,
  session_id,               -- attempts can exist before a run does
  thread_id,                -- nullable until thread is resolved
  run_id,                   -- nullable until process is up
  provider,
  host_id,
  client_request_id,
  command_id,
  state,                    -- pending | dispatched | failed | adopted | abandoned
  error_code,
  error_message,
  expires_at,
  created_at,
  updated_at
)
```

That is the schema delta for this epic. Six tables, no hash chains, no
generations, no segments, no leases.

> Naming note: tablenames are `session_*`-prefixed because the unprefixed
> `threads` and `runs` tables are already taken by the fiche/agent execution
> system (`server/zerg/models/thread.py`, `server/zerg/models/run.py`). The
> SQLAlchemy classes are `SessionThread`, `SessionThreadAlias`, `SessionRun`,
> `SessionConnection`, `SessionLaunchAttempt` for the same reason.

## `thread_id` on existing child tables

The four-noun model only fixes subagent shadowing if rows that *belong to a
thread* know which thread they belong to. These tables today key only by
session and must gain `thread_id`:

- `events` (`AgentEvent.session_id` at `server/zerg/models/agents.py:209-217`)
- `source_lines` (`AgentSourceLine.session_id` at `:307-313`)
- `session_observations` (`SessionObservation.session_id` at `:349-385`)
- `session_runtime_state` (`SessionRuntimeState.session_id` at `:532-535`)
- `session_turns` (`SessionTurn.session_id` at `:476-482`)
- `session_inputs` (`SessionInput.session_id` at `:731-735`)

Migration shape per table:

- Add nullable `thread_id` column with FK to `threads.id`, index on
  `(thread_id, …)` for the existing hot lookup pattern.
- Backfill `thread_id` from each row's `session_id` → primary thread.
- Make `thread_id` `NOT NULL` after backfill.
- Keep `session_id` as a denormalized column for timeline joins. It is no
  longer the parent.

`SessionRuntimeState` additionally moves to keying by `run_id` (with `thread_id`
denormalized). A stale old run cannot pollute a resumed run; child threads
cannot shadow a parent's runtime row; the capability projection has a single
unambiguous "current runtime" lookup.

`SessionTurn` keys by `thread_id` with nullable `run_id`. Unique request-id
constraints scope to thread, not session.

## What gets deleted from `AgentSession`

Move out:

- `provider_session_id`, `thread_root_session_id`, `continued_from_session_id`,
  `continuation_kind`, `is_writable_head` → `threads` + `thread_aliases`.
- `branched_from_event_id` → `threads.parent_event_id`.
- `execution_home`, `managed_transport`, `managed_session_name`,
  `source_runner_id`, `source_runner_name` → `runs.host_id` +
  `connections.control_plane` + `connections.external_name`.
- `launch_state`, `launch_error_code`, `launch_error_message`,
  `launch_lease_until`, `launch_command_id`, `launch_client_request_id` →
  `launch_attempts`.
- `cwd`, `git_repo`, `git_branch` on the session row → canonical on `runs`. If
  any of these stay on `sessions` for timeline display, rename to make the
  denormalization explicit (`last_cwd`, `last_git_branch`).
- `loop_mode`, `loop_thread_id` → already legacy; delete.
- `is_sidechain` → delete from `sessions`. Subagent fact is
  `threads.branch_kind = 'subagent'`. Timeline hiding filters by thread, not
  by session.

`AgentSession` keeps: `id`, `provider` (denormalized for query speed), `title`,
`project`, `device_id`/`device_name` (denormalized for timeline display),
`started_at`, `last_activity_at`, `archived_at`, summary fields, embedding
fields, message counters, `user_state`. Product display metadata only.

`UnmanagedSessionBinding`, `ManagedSessionControlState`, and the engine's
`session_binding(path → session_id)` shim (`engine/src/state/db.rs:69-74`) are
deleted. Their state moves to `runs` + `connections` + `thread_aliases`.

`AgentSessionBranch` (rewind branches at `:159-190`) is *out of scope* for
this epic. It is a different concept — intra-thread rewind to an earlier event
— and should not be conflated with `threads.branch_kind`. Decide its fate in a
follow-up.

## Migration shape

Pre-launch, no external users, no compatibility projections. Work proceeds in
a worktree with one phase per commit batch. Each phase ends with `make
test-ci` and a Hatch Codex review checkpoint before moving on.

### Phase 1 — additive schema

Deliverables:

- Add `threads`, `thread_aliases`, `runs`, `connections`, `launch_attempts`.
- Add nullable `thread_id` columns on `events`, `source_lines`,
  `session_observations`, `session_runtime_state`, `session_turns`,
  `session_inputs`.
- Add nullable `run_id` on `session_runtime_state` and `session_turns`.
- Add nullable `sessions.primary_thread_id`.

Tests:

- New tables exist with correct constraints.
- Backfill helper produces stable 1:1 session→thread mapping and is
  idempotent.
- Existing API responses unchanged (compatibility code still reads old
  columns).

Codex review gate: schema shape, FK/index choices, idempotency of backfill.

### Phase 2 — write-path migration

Deliverables:

- Managed launch creates `launch_attempts` → `runs` → `connections` and
  resolves/creates a thread before any old binding write.
- Managed resume creates a new `run` and `connection` for the existing thread
  when alias evidence matches.
- Bridge attach/detach/degrade updates `connections.state`, never thread or
  session identity.
- Engine `session_binding(path → session_id)` becomes a derived shim populated
  from new tables, then deleted at end of phase.
- Ingest writes `thread_id` on every new event, source line, observation,
  turn, input.

Tests (full combinations):

- Quit/resume same provider transcript: same thread/session, new run +
  connection, runtime state keyed to new run.
- Bridge restart mid-turn: connection.state changes, no new session/thread.
- Subagent under managed parent: child thread, parent connection unchanged,
  no event/runtime collision.
- External provider CLI adopted: `launch_origin = external_adopted`, no
  launch attempt row, thread created via alias evidence.
- Failed remote launch: `launch_attempts.state = failed` with no run.
- Path move/rename of provider transcript: same thread, alias updated, no new
  session.
- PID reuse on same host across reboots: `boot_id` distinguishes runs.

Codex review gate: write-path correctness on the matrix above.

### Phase 3 — backfill and NOT NULL

Deliverables:

- Backfill `thread_id`, `run_id`, and `primary_thread_id` for all existing
  rows in dev and dogfood DBs.
- Flip backfilled columns to `NOT NULL`.
- Migrate `SessionRuntimeState` to run-keyed; old rows fold into the latest
  run for their session.
- Delete `UnmanagedSessionBinding`, `ManagedSessionControlState`, engine
  `session_binding`, and the `AgentSession` columns listed above.

Tests:

- Backfill is idempotent and order-independent.
- Every event/source_line/observation/turn/input has a `thread_id` after
  backfill.
- No FK violations on flip to `NOT NULL`.

Codex review gate: backfill completeness; column-deletion blast radius
through services/routers/views.

### Phase 4 — capability projection

Deliverables:

- `session_capabilities` view materialized from
  `(thread, latest run, best connection, latest run-keyed runtime_state)`
  using the deterministic best-connection rules above.
- `/api/agents/*`, `/api/sessions/*`, web, and iOS switch to consuming the
  projection.
- Delete client-side inference: scan for any reads of `execution_home`,
  `managed_transport`, raw heartbeat freshness, or process liveness in
  capability decisions; remove or replace.
- Strip the now-unused hint fields from API responses; do not leave parallel
  truth.

Tests:

- Capability matrix: managed-attached, managed-degraded, managed-detached,
  managed-process-closed, unmanaged-running, unmanaged-gone, imported-only,
  subagent-child. Every combination produces a deterministic
  `session_capabilities` payload.
- Web and iOS read identical capability values for the same session.
- Liveness flapping (bridge degrade, heartbeat skip, process exit) cannot
  flip `live_control_available` to true; cannot flip a managed session to
  `search-only`.

Codex review gate: projection correctness; client cleanup completeness.

### Phase 5 — cleanup

Deliverables:

- Delete dead code paths in
  `server/zerg/services/session_views.py` (`:624-633`, `:1141-1150`),
  `server/zerg/services/session_capabilities.py` (`:275-327`), and any other
  capability inference site.
- Update generated API contracts and iOS client models.
- Run `make test-ci`. Fix what breaks.
- `make dogfood-refresh` + iOS Xcode rebuild + sanity dogfood.

Codex review gate: final read; ship.

This will break in-flight sessions on hosted at deploy time. That is acceptable
pre-launch. David's dogfood instance is the only real consumer.

## Testing approach

Combinatorial corners are the failure mode. Reasoning by hand is unreliable
here, so the test plan emphasizes table-driven coverage:

- **Identity invariants** (every phase): for every operation that mutates
  liveness/control state, assert thread.id and session.id are unchanged.
- **Resume matrix**: parametrize across (provider, alias evidence kind, host
  same/different, boot_id same/different, transcript continues/diverges).
- **Subagent matrix**: parametrize across (parent control state, child kind,
  ingest order parent-first/child-first/interleaved).
- **Launch matrix**: parametrize across (`launch_attempts.state` transitions,
  whether a run gets attached, whether the user retries with same
  `client_request_id`).
- **Capability matrix** (Phase 4): full enumeration of (run state × connection
  state × runtime freshness) with golden expected `session_capabilities`
  output.
- **Backfill property tests** (Phase 3): random ordering of insert/backfill
  steps must converge to the same final state.

`make test-ci` is the per-phase gate; full `make test-full` runs before Phase
5 ship. Engine tests cover binding-shim removal; iOS Xcode tests cover the
capability contract change.

## Resolved questions (from Codex review)

1. **Run identity** — keep both `boot_id` and `process_start_time`. Boot id is
   cheap insurance against pid reuse and clock weirdness; process start time
   distinguishes within a boot.
2. **Capability shape** — typed boolean columns, not JSON. Small enumerated
   set, queryable, action-critical. `capabilities_extra_json` reserved for
   provider-specific diagnostics only.
3. **Aliases** — `thread_aliases` from day one. The second alias kind is
   already real (provider id, Longhouse override id, source path, forked-from
   id).
4. **`SessionTurn`** — keys on `thread_id`, nullable `run_id`, `session_id`
   denormalized.
5. **`is_sidechain`** — deleted from `sessions`. `threads.branch_kind =
   'subagent'` is the truth. Timeline filters by thread.

## Open questions

1. ~~Does `thread_aliases.alias_value` need a uniqueness constraint within
   `(provider, alias_kind)`, or are duplicate aliases legitimate (e.g. copied
   transcripts pre-divergence)?~~ **Resolved (Phase 1, Codex review):** scoped
   to thread. `(thread_id, provider, alias_kind, alias_value)` is unique;
   global `(provider, alias_kind, alias_value)` is not. The lookup index on
   `(provider, alias_kind, alias_value)` is non-unique by design. Aliases
   remain evidence, not identity.
2. Where does `AgentSessionBranch` (rewind branches) ultimately live? Out of
   scope here, but its relationship to `threads.branch_kind` should be
   decided before any future revision/branch work starts.
3. Does the engine's local SQLite need a parallel slim model, or can it stay
   path-keyed internally and only emit thread/run identifiers in shipped
   payloads? Bias: keep engine local store as evidence, ship thread/run on
   the wire.

## Non-goals

- No revision/canonical-event/branch-resolver work. Future epic.
- No multi-machine concurrent writer arbitration.
- No stress harness or cold-rebuild contract.
- No compatibility shim for `AgentSession`. We rip and replace.
- No exposure of "thread" or "run" as user-facing copy. These are internal
  nouns; UX continues to say "session."

## Decision filter

When ambiguous, prefer the option that:

1. removes a column from `AgentSession`;
2. lets liveness/control state change without touching thread or session;
3. moves capability inference from client to server;
4. defers branch/revision/artifact work without painting us into a corner if
   we add it later.
