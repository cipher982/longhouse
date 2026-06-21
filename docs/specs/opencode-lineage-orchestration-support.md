# OpenCode Lineage And Orchestration Support

Status: implemented core lineage; release-watch follow-ups remain
Owner: Longhouse session core
Created: 2026-06-20
Related:
- `VISION.md`
- `docs/specs/session-identity-kernel.md`
- `docs/specs/subagent-thread-ingest.md`
- `docs/specs/managed-provider-session-contract.md`
- `docs/specs/provider-release-proof.md`

## Problem

OpenCode support currently treats the OpenCode SQLite database as one mostly
linear transcript source. That was enough for import, launch, reattach, tool
call parsing, and basic live-send proof. It is not enough for the newer
OpenCode model where a session can contain:

- primary-agent switches
- primary-agent-to-subagent delegation
- foreground and background subagent tasks
- child-session navigation in the TUI
- explicit session forks
- async prompts and no-reply prompts
- server-driven clients over a local OpenAPI surface

The temptation is to add OpenCode-specific session species. We should not do
that. The existing Longhouse identity kernel already has the right nouns:
session, thread, run, connection, aliases. The work is to generalize the
Claude workflow/subagent path into provider-neutral lineage and then map
OpenCode evidence into that kernel.

## Research Snapshot

Researched on 2026-06-20 against:

- OpenCode docs:
  - `https://opencode.ai/docs/agents/`
  - `https://opencode.ai/docs/cli/`
  - `https://opencode.ai/docs/server/`
- Upstream source clone:
  - repo: `https://github.com/sst/opencode`
  - commit: `009f3799cd6d28cad5a3e1b3902a80f60f93122e`
  - files:
    - `packages/opencode/src/tool/task.ts`
    - `packages/opencode/src/session/session.ts`
    - `packages/opencode/src/session/prompt.ts`
    - `packages/core/src/v1/session.ts`
    - `packages/sdk/openapi.json`

High-confidence OpenCode facts:

- OpenCode exposes two agent modes as user concepts: primary agents and
  subagents. Built-in primary agents include `build` and `plan`; built-in
  subagents include `general`, `explore`, and `scout`.
- `opencode agent create` supports `--mode all|primary|subagent`,
  `--permissions`, and `--model`.
- The TUI has explicit parent/child navigation for subagent sessions.
- The CLI supports `--continue`, `--session`, and `--fork` on session-oriented
  commands.
- The server publishes an OpenAPI 3.1 surface, and the TUI itself talks to the
  server as a client.
- The OpenAPI surface includes:
  - `GET /session/{sessionID}/children`
  - `POST /session/{sessionID}/fork`
  - `POST /session/{sessionID}/prompt_async`
  - message/prompt request bodies that can include `agent`, `noReply`,
    `AgentPartInput`, and `SubtaskPartInput`
- The source `TaskTool` creates a child session with `parentID: ctx.sessionID`,
  records task metadata on the parent tool call, and optionally runs the child
  in background mode behind `OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS=true`.
- The OpenCode session table has `parent_id`, and the current Longhouse parser
  already preserves that as `SessionMetadata.forked_from_session_id`.

## Key Distinction

OpenCode `parentID` / `parent_id` is lineage evidence, not a complete semantic
classification.

At least two different things can produce a child relationship:

- `TaskTool` subagent delegation creates child sessions under the parent.
- `session.fork` creates a new independent session copy with a forked title.

Longhouse must not classify every OpenCode parent id as `subagent`. The correct
first abstraction is provider-neutral lineage:

```text
parent provider session id
child provider session id
parent event/message id when known
lineage source: task | fork | unknown
agent name/mode when known
background/foreground when known
```

Only after that evidence is normalized should the server decide whether the
child belongs under the parent session as a non-primary thread, or appears as a
separate top-level Longhouse session linked back to its parent.

## Longhouse Target Model

### Sessions

A Longhouse `AgentSession` remains the product row that appears in timeline,
wall, iOS, search, and recall.

OpenCode subagent tasks should not become top-level sessions when they can be
resolved to their parent. They should become child `SessionThread` rows under
the parent session.

OpenCode forks are different. A fork is user-visible alternate work, not just
worker output. It should normally become a top-level session with durable
lineage back to the parent, unless the UI later adds an explicit branch tree
view that can make forked sessions visible without cluttering the timeline.

### Threads

Extend the branch-kind vocabulary:

```text
root | subagent | continuation | fork
```

`fork` does not require a schema migration because `branch_kind` is a string.
The implementation choice is whether forks get:

1. a top-level `AgentSession` whose primary thread has `branch_kind='fork'` and
   aliases pointing back to the parent, or
2. a non-primary child thread under the parent session.

For launch, prefer option 1 for OpenCode forks. It preserves timeline
discoverability and matches the user's mental model that a fork is a new path
they may continue. We can add a grouped branch UI later without moving durable
event rows again.

OpenCode subagents should use the existing child-thread path:

```text
session_threads(
  session_id = <parent Longhouse session>,
  provider = 'opencode',
  parent_thread_id = <parent primary thread>,
  branch_kind = 'subagent',
  is_primary = 0
)
```

### Aliases

Provider-neutral aliases should be preferred for new code:

- `provider_session_id`
- `parent_provider_session_id`
- `forked_from_provider_session_id`
- `source_path`
- `subagent_id`
- `subagent_prompt_id`
- `subagent_tool_use_id`
- `provider_agent_name`
- `provider_agent_mode`
- `workflow_run_id`
- `workflow_attribution_agent`
- `workflow_attribution_skill`

Existing Claude-specific aliases remain compatibility evidence:

- `claude_agent_id`
- `claude_prompt_id`
- `claude_tool_use_id`

OpenCode-specific aliases are allowed when they carry evidence that should not
be normalized away:

- `opencode_session_id`
- `opencode_parent_session_id`
- `opencode_part_id`
- `opencode_message_id`
- `opencode_task_id`
- `opencode_background_job_id`

Do not use a shared parent alias as a child identity key. Sibling subagents all
share the same parent and often the same workflow/run attribution.

## OpenCode Evidence Mapping

### Root Session

Current parser behavior:

- `provider_session_id = OpenCode session.id`
- Longhouse deterministic id = UUIDv5 over `opencode:<session.id>`
- `cwd`, project label, version, and start time are parsed from the SQLite row
- `forked_from_session_id = session.parent_id`

Implemented additions:

- Record root-thread alias `provider_session_id=<opencode session.id>`.
- Preserve `session.agent` in the parser fingerprint so agent changes are
  re-shipped when OpenCode stores the column.

Future additions:

- Record provider-specific alias `opencode_session_id=<opencode session.id>`
  if a provider-specific alias namespace becomes useful.
- Preserve `session.agent` as `provider_agent_name` once the ingest model has a
  provider-neutral metadata field for it.
- Preserve agent mode when available from config/server evidence as
  `provider_agent_mode`.

### Subagent Task Child

Primary evidence:

- OpenCode session row `parent_id`.
- Tool part metadata from `TaskTool`, especially child session id, parent
  session id, selected agent, model, and background/job id when present.
- `SubtaskPart` / `SubtaskPartInput` has prompt, description, agent, model,
  and optional command.

Mapping:

- `is_sidechain = true`
- `parent_provider_session_id = parent session id`
- `provider_session_id = child session id`
- `subagent_id = child session id` only if no better task id exists
- `attribution_agent = OpenCode agent name` when known
- `subagent_tool_use_id = parent tool-call id` when known
- `branch_kind = subagent`
- `provider_agent_name = selected subagent name`
- `provider_agent_mode = subagent`
- `workflow_run_id` only when OpenCode later exposes a durable workflow/batch id

Foreground subagents should appear as child lanes whose result is also visible
through the parent tool-result text. Background subagents need runtime state of
their own so users can tell that the parent has moved on while the child is
still active.

### Forked Session

Primary evidence:

- CLI `--fork` path.
- Server `POST /session/{sessionID}/fork`.
- OpenCode title mutation to `(... fork #N)`.
- New session created by `Session.fork`.

Mapping:

- `is_sidechain = false`
- `parent_provider_session_id = original session id`
- root/primary thread with `branch_kind = fork`
- alias `forked_from_provider_session_id = original session id`
- parent message id if forked from a specific message

Forks should stay visible in the default timeline for now. The UI can annotate
them as "forked from ..." once the session detail branch view exists.

### Primary Agent Switch

Primary evidence:

- OpenCode assistant messages include agent/mode fields.
- Server event schema includes agent-switched events.
- CLI/TUI lets users switch primary agents during a session.

Mapping:

- Do not create a new Longhouse session or thread just because a primary agent
  changes.
- Preserve `provider_agent_name` and switch events in event metadata or
  observations.
- Later UI can show primary-agent changes as inline milestones.

### Async / No-Reply Prompt

Primary evidence:

- Server API exposes `prompt_async`.
- Prompt request body includes `noReply`.

Mapping:

- This is a run/control-path behavior, not session identity.
- Managed OpenCode send should keep the existing capability distinction:
  OpenCode supports send/abort/reattach proofs; do not advertise active-turn
  steer until semantics are proven.
- Store async prompt/request ids as event metadata when available.

## Implementation Status

Completed in `epic/opencode-lineage-support`:

- OpenCode `session.parent_id` remains lineage evidence, not automatic
  subagent classification.
- Parent task-tool evidence now classifies child sessions as subagent threads.
- Plain parentage without task evidence now projects as a visible top-level
  linked session unless fork evidence is present.
- Title-proven OpenCode forks project as visible top-level `fork` threads.
- `session_edges` now records semantic `task_child`, `fork`, and `unknown`
  lineage evidence alongside compatibility aliases.
- Subagent aliases are now provider-neutral (`subagent_id`,
  `subagent_prompt_id`, `subagent_tool_use_id`) while Claude aliases remain
  compatibility evidence for Claude only.
- Unresolved OpenCode task children are hidden from the default timeline and
  relinked under the parent when it arrives later.
- Workflow lookup no longer defaults to Claude-only aliases.
- The universal provider-release harness has an
  `opencode_lineage_projection` scenario that proves resolved children,
  orphan relink, fork visibility, and generic aliases through a real Longhouse
  SQLite ingest path.

Still future work:

- Dedicated UI badges for forked sessions and child-lane affordances.
- Machine API endpoints that expose children/forks directly under
  `/api/agents/*`.
- Search/recall result shaping that returns parent sessions by default for
  subagent matches while preserving child-thread context.
- Provider-agent metadata fields such as `provider_agent_name` and
  `provider_agent_mode`.
- Background subagent runtime state once OpenCode persists enough restart-safe
  state to prove it.
- Async/no-reply prompt semantics and active-turn steer proofs.

## Release Proof

Add provider-release scenarios:

- `opencode_lineage_projection` (implemented)
  - proves a task-tool child attaches under the parent thread.
  - proves an unresolved child relinks when the parent arrives later.
  - proves a plain parented session stays visible as a `fork`.
  - proves provider-neutral aliases survive ingest.
- `opencode_background_subagent` (future)
  - proves background task state and parent result injection do not flatten the
    child transcript.
- `opencode_primary_agent_switch` (future)
  - proves agent switch metadata is retained without creating bogus sessions.
- `opencode_prompt_async_no_reply` (future)
  - proves async/no-reply sends are archived and do not falsely project active
    turn steer.

## Design Decisions

### Do not redesign the database wholesale

The current identity-kernel refactor was the right move. The session/thread/run
split is exactly the abstraction that modern agent harnesses need. The next
step is not a new "workflow" object under every provider; it is provider-neutral
lineage evidence mapped into the kernel.

### Treat workflows as projections first

Claude dynamic workflows are currently represented through `workflow_run_id`
aliases and query helpers. That is fine for now. OpenCode does not appear to
expose a durable workflow-run id equivalent yet; its concrete durable primitive
is child sessions. We should not invent synthetic workflow ids until a provider
surface or Longhouse-managed launch flow gives us one.

### Forks are not subagents

Subagents are worker lanes under a task. Forks are alternate user-visible paths.
Both have parentage, but they should not share default timeline behavior.

### Provider adapters should emit evidence, not product decisions

The Rust provider parser should not need to know whether the web timeline hides
something. It should emit raw, normalized evidence:

- parent id
- child id
- source message/tool id
- agent name/mode
- background status
- fork/task hint

The server resolver owns product classification because it can see existing
Longhouse sessions, aliases, runs, and unresolved orphan rows.

## Open Questions

- Does OpenCode persist enough metadata in the SQLite DB to distinguish
  `TaskTool` children from `session.fork` children without hitting the server?
- Should OpenCode forks be grouped under the parent in the UI while still
  remaining top-level search/timeline rows?
- Does background subagent state survive process restart through the OpenCode
  database, or only through in-memory background-job state?
- Should Longhouse managed OpenCode launch enable
  `OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS` by default, or only observe it
  when the user/provider already enabled it?
- How should OpenCode `AgentPart` mentions be displayed: inline event metadata,
  child-thread hints, or both?

## Non-Goals

- Add a generic workflow product surface.
- Advertise OpenCode active-turn steer.
- Flatten child transcripts into parent primary-thread event order.
- Treat every provider feature named "agent" as the same product capability.
