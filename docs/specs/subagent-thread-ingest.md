# Subagent Thread Ingest

Status: proposed for implementation
Owner: Longhouse session core
Created: 2026-06-02
Related:
- `VISION.md`
- `docs/specs/session-identity-kernel.md`
- `docs/specs/agents-machine-surface.md`

## Problem

Claude and other provider fan-out modes can write one transcript file per
child worker. Those files are useful archive artifacts, but they are not
separate human-visible tasks. Today the Machine Agent discovers Claude
subagent files under `~/.claude/projects/**/subagents/**/*.jsonl`, assigns each
non-UUID `agent-*.jsonl` path a deterministic Longhouse session UUID, and ships
it as an independent `AgentSession`. The server then creates a primary
`SessionThread` for each child file, so the timeline shows fan-out workers as
top-level rows.

That violates the session identity kernel: a subagent is a child thread under
the same session, not a new session.

## Source Evidence

Claude sidechain JSONL records carry enough evidence to attach the child
transcript to the parent session:

- `isSidechain: true`
- `sessionId`: the parent/root Claude session id
- `agentId`: the child agent id
- `promptId`: child prompt identity
- `cwd`, `gitBranch`, `version`, timestamps
- source path:
  `.../<parent-session-id>/subagents[/workflows/<workflow-id>]/agent-<agent-id>.jsonl`
- optional sidecar metadata:
  `agent-<agent-id>.meta.json`, commonly with `toolUseId`, `agentType`, and
  `description`
- optional workflow metadata:
  `workflows/<workflow-id>.json` and
  `subagents/workflows/<workflow-id>/journal.jsonl`

The first two fields are sufficient for correctness. The rest are labels and
debug evidence.

Codex source files expose analogous sidechain evidence through `session_meta`
`forked_from_id` or `source.subagent.thread_spawn.parent_thread_id`. The Rust
parser already knows how to read some of this evidence, but the compressor does
not ship it to the server today. The server contract is therefore new plumbing,
not already-complete behavior.

## Identity Contract

### Session

The session is the user-visible task. It is the row that appears in timeline,
wall, iOS, search results, and recall results by default.

Provider child transcript files must not create timeline-visible sessions when
they can be resolved to a parent session.

### Thread

The thread is the transcript lane. A root provider transcript uses the
session's primary thread. A subagent transcript uses a non-primary child
thread:

```text
session_threads(
  session_id = <parent Longhouse session>,
  provider = <provider>,
  parent_thread_id = <parent primary thread>,
  branch_kind = 'subagent',
  is_primary = 0
)
```

Events, source lines, observations, turns, and future transcript-derived rows
for the child transcript must carry:

```text
session_id = <parent Longhouse session>
thread_id = <child subagent thread>
```

This preserves one product session while keeping the child transcript separate
from the root transcript order.

On the resolved happy path the child file's deterministic UUID is not an
`AgentSession.id`. It is evidence attached to the child thread as an alias.
This is the key difference from today's leaking behavior.

### Aliases

Aliases are evidence, not identity. Subagent ingest records at least:

- parent/root thread:
  - `provider_session_id = <parent provider session id>`
- child thread:
  - `provider_session_id = <child provider/file session id when distinct>`
  - `source_path = <full transcript path>`
  - `claude_agent_id = <agentId>` for Claude when present
  - `claude_prompt_id = <promptId>` for Claude when present
  - `claude_tool_use_id = <toolUseId>` when sidecar metadata provides it
  - `workflow_run_id = <workflow id>` when the source path or workflow metadata
    provides it
  - `forked_from_provider_session_id = <parent provider session id>`

Alias kinds may grow as provider evidence grows; readers must not treat a
single alias kind as canonical identity. `alias_kind` is a free string column,
so these new kinds require no schema migration.

## Wire Contract

The wire currently carries only `is_sidechain` and `provider_session_id`.
`SessionIngest` needs to carry parentage instead of a lossy sidechain boolean
alone:

- `is_sidechain: bool`
- `parent_provider_session_id: str | None`
- `subagent_id: str | None`
- `subagent_prompt_id: str | None`
- `subagent_tool_use_id: str | None`
- `workflow_run_id: str | None`

For Codex, the already-parsed `forked_from_session_id` must be serialized as
`parent_provider_session_id`. For Claude, the parser must extract raw
`sessionId` from `isSidechain:true` records and serialize it as
`parent_provider_session_id`.

The child file's own `provider_session_id` stays the child/file identity. For a
Claude `agent-*.jsonl` file this is the deterministic UUIDv5 derived from the
file path; the raw parent `sessionId` is carried separately. Do not replace the
child identity with the parent id on the wire.

For backward compatibility, `is_sidechain=true` without
`parent_provider_session_id` remains importable, but it cannot be attached to a
parent. This case includes the legacy `LONGHOUSE_IS_SIDECHAIN=1` environment
override, which may mark an otherwise root transcript as automated. Such rows
must not vanish from the archive. They may be hidden from the main timeline only
when durable evidence proves they are actual child sidechain files, such as a
subagent source path or child-thread alias.

## Resolution Rules

On ingest:

1. Parse and normalize sidechain evidence before creating the session row.
2. If `is_sidechain=false`, keep the existing root-session path.
3. Resolve the parent:
   - if `parent_provider_session_id` is UUID-shaped and an `AgentSession.id`
     matches it, use that session's primary thread;
   - otherwise resolve by `SessionThreadAlias(provider, 'provider_session_id',
     parent_provider_session_id)`;
   - root ingest must record the native provider id alias so future child
     resolution has evidence to find.
4. If `is_sidechain=true` and the parent resolves, ingest into that parent
   session and a child `branch_kind='subagent'` thread. Do not create a child
   `AgentSession` row.
5. Reuse an existing child thread by a deterministic lookup under the same
   parent thread. The preferred key is
   `(parent_thread_id, branch_kind='subagent', alias_kind='source_path',
   alias_value=<child file path>)`, falling back to child provider id or
   provider-specific subagent id aliases. The helper must be race-safe: two
   concurrent ingests of the same child file must not create two child threads.
6. If no parent resolves, prefer defer-and-attach semantics over hot-path row
   moves. The first implementation may keep the existing compatibility fallback
   of importing the child file as an unresolved session, but that fallback must
   be explicitly marked through thread/alias/source evidence and hidden from
   the default main timeline only when it is a proven child sidechain file.
7. When the parent later arrives, a reconciliation/backfill path should attach
   unresolved child rows to the parent session by moving transcript child rows
   to the parent session and preserving child thread aliases.

The write path must not merge child events into the parent primary thread. That
would flatten causality and make parent transcript projection incorrect.

## Read Behavior

Timeline, wall, iOS, and default search/recall should return the parent session
once. Child transcript matches may annotate the parent result with child-thread
context, but they should not appear as separate top-level rows.

Session detail should expose child transcripts as inspectable lanes. The first
implementation may expose this through server/CLI/API primitives before adding
rich browser UI, but the archive must retain every child event and source line.

Timeline filtering cannot rely on `AgentSession.is_sidechain`; that persisted
column was intentionally removed. Resolved child transcripts disappear from the
main timeline because they are not sessions. Unresolved compatibility sessions
must be filtered by durable child evidence such as a primary thread with
`branch_kind='subagent'`, a `source_path` alias under `subagents/`, or raw
source path evidence, not by a deleted session flag.

## Backfill

Dogfood data already contains leaked subagent sessions. Backfill should detect
existing rows using durable evidence, in this order:

1. `AgentSourceLine.source_path` or `AgentEvent.source_path` matching
   `/<parent-provider-session-id>/subagents/`.
2. Raw JSONL with `isSidechain: true` and `sessionId`.
3. Existing `SessionThreadAlias` evidence if a previous partial ingest wrote
   aliases.

For each resolvable child:

1. Find the parent session by `AgentSession.id` when
   `<parent-provider-session-id>` is UUID-shaped, otherwise by primary thread
   alias `provider_session_id=<parent-provider-session-id>`.
2. Create/reuse a child `SessionThread` under the parent session.
3. Move child transcript rows from the leaked child session to the parent
   session and child thread:
   - `events`
   - `source_lines`
   - `session_observations`
   - `session_turns` where applicable
   - `session_runtime_state` by re-stamping `thread_id` to the child thread
     while preserving `run_id`; child runtime must not promote parent primary
     thread control capability
4. Preserve aliases for the leaked child id and source path on the child
   thread.
5. Hide, archive, or delete the empty leaked `AgentSession` after no child rows
   still reference it.

Backfill must also preserve `AgentSessionBranch`/`branch_id` semantics and keep
FTS indexes consistent after cross-session row moves.

Backfill must be idempotent and chunked; it will run against live dogfood data.
No new database columns are required for the core model because
`session_threads.parent_thread_id`, `branch_kind`, `is_primary`, and
`session_thread_aliases.alias_kind` already exist.

## Test Requirements

Minimum coverage before this ships:

- Rust parser test: Claude `isSidechain:true` extracts parent `sessionId`,
  `agentId`, `promptId`, and marks the transcript sidechain.
- Rust compressor test: sidechain parentage fields appear in the ingest JSON.
- Server ingest test: parent root ingest followed by Claude child ingest creates
  one `AgentSession`, two `SessionThread` rows, and child events/source lines
  under the parent session and child thread.
- Server ingest test: Codex fork child attaches to a Codex parent using
  serialized `forked_from_session_id`.
- Idempotency test: replaying the same child file reuses the same child thread
  and does not duplicate aliases/events/source lines.
- Ingest-order test: child-before-parent remains stored and does not create a
  durable timeline pollution state after reconciliation/backfill.
- Timeline test: parent plus many child transcripts returns one timeline row
  with `hide_autonomous=true`.
- Fallback test: sidechain without resolvable parent is not shown on the default
  main timeline only when durable child evidence exists, but remains stored.
- Compatibility test: `LONGHOUSE_IS_SIDECHAIN=1` without child evidence remains
  importable and is not silently discarded.
- Backfill test: an existing leaked child session with source path and raw JSONL
  is moved under its parent thread.
- Race/idempotency test: duplicate child ingests cannot create two child
  threads for the same parent/source path.
- API-level test: `/api/timeline/sessions` does not emit child fan-out rows.

## Implementation Plan

1. Add wire fields to Rust `SessionMetadata`, compressor payload, and Python
   `SessionIngest`. Serialize Codex `forked_from_session_id` instead of
   dropping it.
2. Teach the Claude parser to extract `sessionId`, `agentId`, and `promptId`
   when `isSidechain:true`; use once-true/once-known semantics across lines.
3. Add kernel write helpers for parent resolution and deterministic,
   race-safe creation/reuse of subagent child threads.
4. Route `AgentsStore.ingest_session()` through parent/thread resolution before
   creating a session row. Resolved children create no `AgentSession`.
5. Prefer parent lookup by UUID-shaped `AgentSession.id` before alias lookup,
   and stop relying on legacy `AgentSession.provider_session_id` properties for
   native provider alias backfill.
6. Add a conservative unresolved-sidechain timeline filter based on durable
   child evidence, not deleted session columns.
7. Add an idempotent backfill function for already-leaked dogfood data,
   explicitly handling branch rows and FTS consistency.
8. Add the tests above, with at least one HTTP timeline test so frontend-facing
   behavior is locked at the API boundary.
