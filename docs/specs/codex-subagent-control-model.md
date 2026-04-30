# Codex Subagent Control Model

Status: focused implementation plan reviewed by Hatch Opus

Longhouse must treat Codex subagents as first-class provider threads, not as
replacement identities for the managed parent session.

## Incident

The managed Codex session for `/Users/davidrose/git/chaos` degraded after a
Codex `spawn_agent` call. Local state showed:

- Parent Codex thread: `019dd708-573a-7131-a4d9-9ee855520483`
- Child Codex thread `Kepler`: `019ddadb-ecc7-7473-9373-53e05b2ec900`
- Child Codex thread `Ptolemy`: `019ddb6e-114f-7643-89db-86c31a2aa706`
- Longhouse session: `c3026405-5e99-447f-ae5c-baacd848ac47`

Codex's own `thread_spawn_edges` table records both child threads as
`ThreadSpawn` children of the parent. Longhouse correctly left Kepler as a
separate transcript, but the bridge bound Ptolemy's rollout path to the parent
Longhouse session. Local health then reported a generic
`thread_subscription_failed` state because the bridge state, transcript binding,
and active control path no longer described the same provider thread.

## Upstream Codex Model

Codex models subagents explicitly:

- `SessionSource::SubAgent(SubAgentSource)` exists in the protocol.
- `SubAgentSource::ThreadSpawn` carries `parent_thread_id`, `depth`,
  `agent_path`, `agent_nickname`, and `agent_role`.
- `spawn_agent` creates a new provider thread with this source.
- The app-server intentionally broadcasts newly created threads to initialized
  clients so they can subscribe to or drain those child threads.
- `thread/started` responses include a full thread object with `id`, `path`, and
  `source`.
- Rollout `session_meta` writes the same source data.
- Codex's local state database stores `thread_spawn_edges`.

Therefore a managed Longhouse bridge should expect to see child thread events.
Those events are not noise and are not evidence that the parent managed session
has changed identity.

## Root Cause

Longhouse currently collapses three different concepts:

- provider thread identity
- transcript ingest grouping
- managed control attachment

The Codex bridge reads `thread/started`, `turn/started`, and related
notifications, extracts only thread `id` and `path`, ignores `source`, and calls
`adopt_thread_identity`. That path also writes the selected rollout path into
the shipper `session_binding` table for the managed Longhouse session.

When a subagent thread appears, the bridge can mistake the child for the
managed primary thread. This is why one child rollout path became bound to the
parent Longhouse session.

The narrower parser bug is that Longhouse only recognizes old Codex
`forked_from_id` metadata. Current Codex subagents may instead describe
parentage through `source.subagent.thread_spawn.parent_thread_id`.

## Design

Longhouse should mirror Codex's provider-thread graph and keep live control
attachment separate from transcript ingest.

For launch, fix the observed collapse first. Do not add the full
provider-thread/control-attachment schema until the focused source-aware bridge
and parser path is proven. The existing shipper override guard already preserves
child Codex session ids once parser metadata marks current Codex
`source.subagent.thread_spawn` files as sidechains.

### Provider Thread Graph

Persist provider thread facts independently from Longhouse session control:

- `provider`
- `provider_thread_id`
- `source_kind`
- `parent_provider_thread_id`
- `rollout_path`
- `cwd`
- `agent_nickname`
- `agent_role`
- `updated_at`

Initial source kinds:

- `root`
- `subagent_thread_spawn`
- `subagent_review`
- `subagent_compact`
- `internal_memory`
- `unknown`

### Managed Control Attachment

Track the controlled provider thread explicitly:

- `longhouse_session_id`
- `provider`
- `primary_provider_thread_id`
- `primary_rollout_path`
- `bridge_pid`
- `generation`
- `attach_state`
- `updated_at`

Only explicit bridge start, resume, repair, or reattach operations should mutate
this attachment. Plain transcript ingest must not.

### Bridge Rules

The Codex bridge must parse thread source before adopting identity:

- A root thread may become the managed primary thread.
- A `subagent_thread_spawn` thread may be recorded and drained, but must not
  replace the managed primary thread.
- `thread/resume` responses must also be source-validated before using
  `allow_replace_locked`.
- If the bridge cannot find a valid root thread, it should degrade with a
  precise reason such as `primary_thread_missing` or
  `control_attached_to_subagent`.

### Parser Rules

The rollout parser should classify Codex source metadata from both shapes:

- Raw rollout shape: `{"subagent":{"thread_spawn":{...}}}`
- App-server TypeScript shape: `{"subAgent":{"threadSpawn":{...}}}`

It must keep support for old `forked_from_id`.

If a Codex file is a subagent, the parser should set:

- `forked_from_session_id` to the parent provider thread id when available
- `is_sidechain = true`

The shipper's existing override guard can then keep subagent transcripts from
being forced onto the parent Longhouse UUID.

### Local Health

Local health should report exact control-state failures:

- `control_attached_to_subagent`
- `primary_thread_mismatch`
- `primary_rollout_missing`
- `subscription_stale`
- `bridge_orphaned`

It should not infer managed control truth from the newest transcript binding.

## Implementation Sequence

1. Add a small Codex source parser in Rust that classifies root vs subagent and
   extracts parent thread id, nickname, role, and path when present.
2. Use it in `engine/src/pipeline/parser.rs` so current Codex subagent rollout
   metadata sets `forked_from_session_id` and `is_sidechain`.
3. Use it in `engine/src/codex_bridge.rs` so `thread/started` and
   `thread/resume` cannot adopt a subagent as the managed primary.
4. Guard id-only `turn/started` / `item/*` / `thread/status/changed`
   notifications so they cannot replace an already-known candidate with a
   different thread id. Those notifications do not reliably carry source, so
   they may update state for the same thread but must not become an identity
   election path.
5. Add a belt-and-suspenders check before bridge session binding so a rollout
   file that is known to be a subagent is not written as the managed parent
   Longhouse binding.
6. Add focused engine tests for raw rollout source, app-server source, bridge
   notification adoption, and resume validation.
7. Improve local-health reason strings once bridge/parser state is stable.
8. Add the fuller provider-thread/control-attachment schema if the focused fix
   leaves any remaining inference in health or repair flows.

## Success Criteria

- A Codex parent thread with two spawned subagents leaves the managed bridge
  attached to the parent.
- Subagent rollout files remain separate transcripts or explicit children; they
  are never bound as the managed parent Longhouse session by bridge adoption.
- `source.subagent.thread_spawn.parent_thread_id` is parsed from current Codex
  rollout files.
- Legacy `forked_from_id` behavior still works.
- The bridge rejects subagent `thread/resume` results as control targets.
- Id-only per-turn/item/status notifications cannot swap an already-known
  managed thread candidate to a different thread.
- Local health no longer reports a misleading generic
  `thread_subscription_failed` for this class of bug.
- Focused engine tests pass, and any local-health tests touched by the change
  pass.
