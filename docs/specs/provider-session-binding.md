# Provider Session Binding

Status: proposed core invariant
Owner: Longhouse session core + Machine Agent
Created: 2026-06-23
Reviewed: Hatch Opus steelman on 2026-06-23
Related:
- `docs/specs/session-identity-kernel.md`
- `docs/specs/session-graph-invariants.md`
- `docs/specs/managed-local-launch-response-contract.md`
- `docs/specs/managed-provider-session-contract.md`

## Why This Exists

Longhouse can launch a provider session through `longhouse <provider>` and later
ingest that provider's durable transcript from another source. Those two facts
must converge on the same Longhouse thread. If they do not, the user sees the
same work twice: one row is managed but empty, the other has transcript content
but is read-only.

This spec defines the small identity rule that prevents that class of bug.

## The Rule

For providers that expose a durable native session id:

```text
provider + provider_session_id -> exactly one Longhouse thread
```

The user-facing session is projected from that thread:

```text
provider + provider_session_id -> thread_id -> session_id
```

This is a routing invariant. It does not imply steerability. Control still comes
from runs, connections, and health.

## First Principles

### Use The Existing Kernel

The session identity kernel already has the right nouns:

- **Session**: user-visible product row.
- **Thread**: causal transcript continuity.
- **Run**: one provider process invocation.
- **Connection**: Longhouse control relationship to a run.

Provider-native ids identify transcript continuity, so they route to threads.
Do not add another product identity table unless the existing graph cannot
express the invariant.

### Promote One Alias Kind

`session_thread_aliases` already records provider/source evidence. Most alias
kinds are evidence only and may legitimately repeat across copied transcripts or
old artifacts.

`alias_kind = 'provider_session_id'` is different. It is the routing key for
provider transcript continuity. Promote this alias kind to globally unique per
provider:

```python
Index(
    "ux_thread_aliases_provider_session_routing",
    SessionThreadAlias.provider,
    SessionThreadAlias.alias_value,
    unique=True,
    postgresql_where=(SessionThreadAlias.alias_kind == "provider_session_id"),
    sqlite_where=(SessionThreadAlias.alias_kind == "provider_session_id"),
)
```

The existing per-thread unique index remains useful:

```text
UNIQUE(thread_id, provider, alias_kind, alias_value)
```

The new partial unique index adds the missing global guarantee for the one alias
kind that must not split.

This applies to every `provider_session_id` alias, including child/subagent
threads. A provider-native id identifies one provider transcript continuity. If
a root and child claim the same native id, they are the same thread from the
provider's point of view and the graph resolver should attach new observations
to the canonical thread instead of materializing a competing alias.

### The Core Resolves, Adapters Report

Provider adapters emit facts:

- provider process started
- provider-native id observed
- transcript record seen
- bridge/control health observed
- provider process exited

The shared graph resolver owns identity. No parser, shipper, bridge scanner, UI
route, or client should independently decide that a provider-native id means
"new Longhouse session."

### Managed Degrades, It Does Not Become Imported

If Longhouse launched a session, then later transcript/control evidence fails to
join the managed thread, that is a broken managed contract. It is not a normal
imported session.

Allowed states:

```text
managed and bound
managed and waiting for native id
managed and degraded because binding/control/transcript proof failed
imported because no managed ownership evidence exists
```

Forbidden state:

```text
Longhouse-launched work silently appears as imported/read-only
```

### Fail Loudly At The Boundary

The first component that cannot resolve a provider-native id must stop and emit
a typed diagnostic. It must not use a fallback that creates a second identity.

For transcript ingest:

```text
provider id resolves -> attach to resolved thread
provider id unknown + no managed evidence -> create imported session/thread
provider id unknown + managed evidence exists -> degraded, not imported
provider id conflicts -> conflict diagnostic, not a second row
```

### Normalize Before Binding

Every writer and resolver must normalize provider-native ids the same way before
recording or looking them up.

Provider examples:

- Codex rollout filenames may need to normalize to the real thread id.
- Claude launch/predeclared ids must be checked against the first observed
  transcript id.
- OpenCode `ses_...` ids are already the native SQLite session ids.
- Providers with no durable native id do not fake one.

### Idempotence Beats Exactly-Once

Launchers, bridges, shippers, and heartbeats can retry. Recording the same
provider id for the same thread is a no-op. Recording the same provider id for a
different thread is a conflict diagnostic, not a silent move and not a generic
ingest crash.

## Existing Seams To Tighten

### `record_thread_alias`

`server/zerg/services/agents/session_graph_writes.py::record_thread_alias`
already owns alias writes. It should become the only writer for
`provider_session_id` routing aliases.

Rules:

- normalize alias values before write
- duplicate same-thread provider id is a no-op
- duplicate different-thread provider id raises a typed
  `ProviderSessionAliasConflict`; callers that can surface diagnostics catch it
  and record `provider_binding_conflict`
- callers must not swallow provider-id conflicts as ordinary idempotence

### `resolve_thread_by_provider_session_id`

`server/zerg/services/agents/session_graph_writes.py::resolve_thread_by_provider_session_id`
already resolves provider ids through aliases. With the partial unique index,
this resolver should return at most one row for `provider_session_id`.

Rules:

- root transcript ingest must call this resolver before creating a new session
- parent/subagent ingest continues to use it for parent lookup
- `primary_only=True` remains available for callers that need root-only lookup
- missing provider id returns `None`, not a synthetic fallback

### `AgentsStore.ingest_session`

`server/zerg/services/agents/store.py::ingest_session` currently resolves
parent provider ids before graph projection, then falls back to lookup by
Longhouse `session_id`. Root provider ids must also resolve before session
creation.

Required order:

```text
1. If parent lineage attaches to parent, use parent/child thread logic.
2. Else if provider_session_id resolves to an existing thread, use that
   thread's session.
3. Else if data.id matches an existing Longhouse session, use that session.
4. Else create imported session/thread only when no managed ownership evidence
   blocks import.
```

If steps 2 and 3 both match but point at different sessions, provider id wins
for transcript routing and the mismatch is recorded as
`provider_binding_conflict`. The ingest must not create a third row. The
provider-native id is the durable transcript identity; `data.id` can be a local
Longhouse routing hint, a deterministic imported fallback, or stale shipper
state.

This one ordering rule prevents the OpenCode split-row bug without a new table.

## Provider Timing

| Provider | Native id expectation | Binding birth |
| --- | --- | --- |
| Claude | Expected for managed control. | Usually before launch; verify against first observed transcript id. |
| Codex | Expected once bridge/app-server has an active thread. | After bridge exposes the normalized thread id. |
| OpenCode | Expected once server/attach creates a `ses_...` row. | After `opencode serve` / attach reports or observes the SQLite session id. |
| Antigravity | Not always expected today. | Only when a stable native conversation id exists; missing id alone is not degraded if the provider contract says no durable id is expected. |

Provider adapters may differ in when they learn the id. They do not differ in
where the id is recorded.

## Diagnostics

Reason codes:

| Reason | Meaning |
| --- | --- |
| `provider_binding_missing` | Provider-native id was seen but no alias exists yet. |
| `managed_provider_id_unbound` | Managed launch evidence exists, but the provider id did not resolve. |
| `provider_binding_conflict` | Same provider-native id is claimed by competing Longhouse threads. |
| `provider_binding_import_blocked` | Import fallback was blocked because managed ownership evidence exists. |
| `provider_native_id_unexpected_missing` | A provider contract expected a durable native id, but none was observed. |

Local health and the macOS menu bar should summarize these directly:

```text
OpenCode degraded
Longhouse started this session, but its transcript is not bound to the managed thread.
```

Healthy managed sessions should be equally direct:

```text
OpenCode healthy
Control ready / transcript bound / syncing to david010
```

Antigravity-like providers that do not promise a durable native id must not show
permanent yellow solely for missing binding.

## Migration Plan

Phase 0: stop the split-row bug and make the index safe.

- Keep provider-specific compatibility lookups only as alias writers.
- If old OpenCode/Codex/Claude state proves a native id maps to a Longhouse
  thread, write `provider_session_id` alias through `record_thread_alias`.
- Change root ingest to resolve by provider id before creating a new session.
- Add diagnostics for duplicate imported rows that share provider-native id
  evidence with managed sessions.
- Before adding the unique index, clean or relink existing duplicate
  `provider_session_id` aliases that would violate the index. Full historical
  duplicate-session cleanup may be deferred, but index-blocking alias duplicates
  may not.

Phase 1: add the routing uniqueness guard.

- Add the partial unique index for
  `(provider, alias_value) WHERE alias_kind = 'provider_session_id'`.
- Add conflict handling to `record_thread_alias`.
- Keep all other alias kinds non-unique globally.

Phase 2: provider binding writers.

- Claude writes/validates its predeclared provider id.
- Codex writes its normalized bridge-discovered thread id.
- OpenCode writes its `ses_...` id as soon as the bridge observes it.
- Antigravity writes only if a real durable native id exists.

Phase 3: remove identity archaeology.

- Delete provider-specific "find managed session by state file" helpers from
  transcript parsers/shippers after all launchers write aliases.
- Keep state files for control credentials and local diagnostics only.

Phase 4: local-health and menu bar.

- Surface binding health in `longhouse doctor`, local-health JSON, and
  Longhouse.app.
- Distinguish imported sessions from broken managed bindings.

## Test Strategy

Unit tests:

- `provider_session_id` alias is globally unique per provider
- duplicate same-thread provider id is idempotent
- duplicate different-thread provider id raises `ProviderSessionAliasConflict`
- resolver returns the existing thread for a root provider id
- resolver returns `None` for missing provider id without synthetic fallback

Ingest tests:

- managed launch + root transcript ship converge to one session/thread
- transcript-first and launch-first orderings converge to one session/thread
- provider-id-only ingest resolves to existing managed thread
- Longhouse-id-only ingest still updates the existing Longhouse session
- missing binding blocks import when managed ownership evidence exists
- copied transcript with same provider id resolves to the canonical thread

Provider adapter tests:

- Claude predeclared provider id writes alias and matches first transcript id
- Claude mismatch between predeclared id and first observed transcript id is
  diagnosed, not imported
- Codex rollout/native id normalization is consistent across binding and lookup
- Codex resume creates a new run on the same thread, not a new provider alias
- OpenCode server bridge writes `ses_...` alias before transcript ingest can
  create an imported row
- OpenCode resume reuses the bound thread
- Antigravity missing native id is healthy when the provider contract says no
  durable id is expected

End-to-end canary:

```text
longhouse opencode
provider emits native id
transcript source updates
shipper runs
timeline shows one row
row has transcript content and managed capabilities
```

Run the same shape for Claude and Codex, with provider-specific binding birth
steps.

## Acceptance Criteria

- `provider_session_id` aliases are globally unique per provider.
- Root ingest resolves by provider id before creating a session.
- Managed launch transcripts cannot silently become imported/read-only rows.
- Local-health/menu bar can distinguish imported sessions from broken managed
  provider-id binding.
- Provider-specific code discovers native ids; shared graph code decides
  identity.
- No new provider identity table exists for v1.

## Finish / Success Criteria

This work is finished when the product invariant is observable at three levels:

### Data

- For every managed launch that has a durable provider-native id, there is one
  `provider_session_id` alias for that provider id and it points to the managed
  thread.
- Transcript ingest for that provider id appends to the managed thread.
- No default timeline row is created solely because transcript ingest failed to
  find the managed thread.
- Existing duplicate rows for the dogfood OpenCode incident are detected by a
  diagnostic query or health check. Any duplicate `provider_session_id` aliases
  that would block the routing index are relinked or removed before index
  creation.

### Behavior

- Starting `longhouse opencode`, sending at least one prompt, and waiting for
  SQLite transcript ship produces one timeline row with both transcript content
  and managed capabilities.
- The same convergence shape passes for Claude and Codex, with each provider's
  native-id timing handled explicitly.
- If a managed provider id is missing or conflicting, the session projects as
  degraded managed state rather than imported/read-only.
- Providers that do not promise durable native ids, such as current
  Antigravity paths, do not show degraded solely because no provider id exists.

### User Signal

- Web, iOS, CLI, and the macOS menu bar all distinguish:
  - imported/read-only because Longhouse did not launch it
  - managed/live because transcript and control are bound
  - managed/degraded because provider identity did not bind
- The degraded copy names the broken contract piece, for example:

```text
Longhouse started this session, but its transcript is not bound to the managed thread.
```

### Guardrails

- A focused backend test covers root ingest resolving by provider id before
  session creation.
- A provider-level test covers OpenCode bridge-discovered `ses_...` aliases.
- A smoke or canary covers managed launch plus transcript ingest converging to
  one row for OpenCode.
- `record_thread_alias` cannot silently swallow a provider-id conflict between
  different threads.

## Non-Goals

- Do not make provider behavior uniform. Only identity resolution is uniform.
- Do not replace the session identity kernel.
- Do not infer steerability from provider-id aliases.
- Do not make state files a product identity source.
- Do not add a local provider binding sync protocol in v1.
- Do not merge copied transcripts by content hash in this spec.
- Do not backfill every historical duplicate before the launch path is fixed.
