# AgentSession Shim Deletion

Status: implementation plan
Owner: Longhouse session core

## Goal

Delete the hidden `AgentSession` compatibility layer that keeps old session
columns alive as transient ORM properties.

Pre-launch compatibility is allowed only when it protects David's existing
dogfood data or an explicitly versioned API response. It is not allowed to let
production code keep reading deleted columns through `_legacy_attrs`.

## Non-Goals

- Do not rename the public session response contract in this slice.
- Do not remove archive/raw migration tools in this slice.
- Do not redesign the session identity kernel tables.
- Do not change provider launch/control behavior.

## Rule

`AgentSession` is the durable session row only. It must not synthesize:

- thread lineage
- execution home
- managed transport
- runner/control metadata
- launch lifecycle
- sidechain/subagent flags

Those values must come from explicit projection helpers over:

- `SessionThread`
- `SessionThreadAlias`
- `SessionEdge`
- `SessionRun`
- `SessionConnection`
- `SessionLaunchAttempt`
- session-local durable columns that still actually exist

## Compatibility Boundary

The browser/iOS/API response may keep fields such as
`thread_root_session_id`, `continuation_kind`, `origin_label`, or
`launch_state` while clients still consume them. Those fields must be built in
response projection code, not through `AgentSession` properties.

That means this is acceptable:

```text
build_session_response(session, thread_meta, latest_launch_attempt) -> field
```

This is not acceptable:

```text
session.thread_root_session_id
session.execution_home
session.launch_state
```

## Implementation Plan

This lands in two behavior-preserving slices.

### Slice A — Move Reads

1. Add explicit projection helpers for the old response values that are still
   needed by API/client contracts.
2. Replace production reads of deleted `AgentSession` properties with those
   helpers or with existing kernel capability/launch projections.
3. Replace behavioral reads, especially lock-scope and thread-head selection,
   before touching the model shim.
4. Keep the model shim temporarily so Slice A proves the new projection path
   without removing the safety net.

### Slice B — Delete Shims

1. Replace production writes of deleted `AgentSession` properties with writes
   to kernel rows, launch attempts, or local variables.
2. Update tests to seed kernel rows directly when they need managed control.
3. Delete `AgentSession.__init__` legacy-key swallowing, `_legacy_attrs`, all
   deleted-column properties, and the ORM `load` listener that backfills them.

## Success Criteria

- `server/zerg/models/agents.py` contains no `_legacy_attrs`.
- `AgentSession` has no properties for deleted columns.
- Production code does not access deleted fields on `AgentSession`.
- Tests do not construct `AgentSession` with deleted-field keyword args.
- Tests that need managed sessions create `SessionThread` / `SessionRun` /
  `SessionConnection` rows or call existing launch helpers.
- API response fields remain stable for this slice.
- Behavioral reads remain stable for lock scoping and thread-head selection.
- `make test` passes.
- `make test-e2e-core` passes.

## Verification Greps

These should return no production uses outside response model field names,
schemas, tests, or this spec:

```bash
rg "\\.(thread_root_session_id|continued_from_session_id|continuation_kind|branched_from_event_id|is_writable_head|is_sidechain|execution_home|managed_transport|source_runner_id|source_runner_name|managed_session_name|launch_state|launch_error_code|launch_error_message|launch_lease_until|launch_command_id|launch_client_request_id|origin_label)\\b" server/zerg
rg "_legacy_attrs|_legacy_get|_legacy_set|_seed_legacy_attrs_on_load" server/zerg/models/agents.py
rg "AgentSession\\([^)]*(thread_root_session_id|continued_from_session_id|continuation_kind|branched_from_event_id|is_writable_head|is_sidechain|execution_home|managed_transport|source_runner_id|source_runner_name|managed_session_name|launch_state|launch_error_code|launch_error_message|launch_lease_until|launch_command_id|launch_client_request_id|origin_label)=" server
```

The implementation grep should inspect all of `server/`, not only
`server/zerg`, because most shim coupling currently lives in tests.

## Must-Have Tests

- Response contract coverage for the old API fields that this slice preserves:
  `thread_root_session_id`, `continued_from_session_id`,
  `continuation_kind`, `origin_label`, `branched_from_event_id`,
  `is_writable_head`, and `is_sidechain`.
- Lock-scope coverage proving chat/input locks no longer depend on
  `session.thread_root_session_id`.
- Detached-instance coverage proving response construction carries managed
  control metadata through explicit projections rather than ORM load shims.

## Risk

Medium. The change removes a safety net that was masking incomplete migration.
The safer version is not to change API field names in the same slice; only
change where those values are produced.
