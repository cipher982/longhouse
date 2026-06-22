# Session Graph Invariants

Status: active design guardrail
Owner: Longhouse session core
Created: 2026-06-21
Related:
- `docs/specs/session-identity-kernel.md`
- `docs/specs/session-graph-cleanup-goal.md`
- `docs/specs/subagent-thread-ingest.md`
- `docs/specs/opencode-lineage-orchestration-support.md`
- `docs/specs/provider-release-proof.md`

Longhouse stores provider transcripts as a session graph. Provider adapters emit
observed evidence; the shared resolver decides how that evidence projects into
sessions, threads, aliases, search, recall, and control.

## Timeline

Default timeline rows are user-visible work paths.

- Root sessions appear.
- Forked sessions appear as top-level rows with parent lineage attached.
- Continuations appear according to the existing thread-root projection rules.
- Resolved subagent/task children do not appear as top-level rows; they appear
  as child threads under the parent session.
- Unresolved task children are hidden while they are waiting for a parent.
- Unknown lineage stays visible unless evidence proves it is worker-only.

## Hidden Rows

A row may be hidden from the default timeline only when there is durable evidence
that it is worker-only or transient support state.

- Task/subagent evidence may hide the child.
- Workflow journals and control ledgers may be dropped or hidden.
- Plain parentage is not enough to hide a session.
- Missing parent evidence should fail visible, not disappear.

## Relink

Relink is allowed when an orphan row later gains a resolvable parent.

- Only hidden task/subagent orphans are relinked into parent child threads.
- Forks are not relinked into subagent lanes.
- Relink preserves child events, source lines, provider aliases, subagent
  attribution, and workflow attribution.
- Relink removes the orphan top-level session only after the child thread and
  event ownership are durable.
- Archive export/reclaim derive child archive owners from the session graph
  projection, not from route-local alias queries.

## Search And Recall

Search and recall should return the unit the user can act on.

- Matches inside resolved subagent threads return the parent session, with
  child-thread context preserved.
- Matches inside visible forks return the fork session, with parent lineage
  available as context.
- Matches inside unresolved hidden orphans should not create standalone timeline
  clutter, but the archived evidence remains queryable for diagnostics.
- Raw source lines remain the lossless evidence layer; projections must not be
  the only way to recover provider facts.

## Remote Control

Control follows the execution owner, not the visual projection.

- Only managed sessions/runs with a proven provider control path are steerable.
- Child-thread visibility does not imply independent send/abort/reattach.
- Fork visibility does not imply provider-level fork creation support.
- Async or no-reply prompts are run/control transactions until provider proof
  shows they create steerable active turns.
- Capability labels must distinguish supported, read-only, unsupported, and
  unknown behavior.

Provider action coverage is derived by
`server/zerg/services/provider_action_coverage.py`. Humans author action
questions and proof requirements there; support states are computed from
managed-provider contracts and executable harness/release-proof artifacts.

## Semantic Resolver Cases

The resolver must be covered by provider-neutral table tests:

- Parent plus task evidence projects as a child `subagent` thread.
- Parent plus fork evidence projects as a visible `fork` session.
- Parent-only or unknown lineage projects as a visible linked session.
- Orphan task child projects as a hidden `subagent` primary thread and relinks
  when the parent arrives.
- Agent switches project as inline event/actor metadata only.
- Async prompts project as run/control state only.

## Code Boundaries

- Provider adapters emit evidence, not product decisions.
- The resolver owns classification.
- Graph write code owns primary threads, child threads, aliases, and durable
  edges. The first narrow writer module is
  `server/zerg/services/agents/session_graph_writes.py`; `AgentsStore` should
  orchestrate it rather than own graph mechanics directly.
- Projection code owns presentation shape. The first shared projection module is
  `server/zerg/services/session_graph_projection.py`; routes and stores should
  call it instead of interpreting raw aliases directly.
- Capability code owns "can Longhouse safely do this now?"
- Aliases are lookup aids and compatibility evidence, not the semantic source of
  truth.

## API Surfaces

- `/agents/sessions/{session_id}/graph` is the machine/API graph surface for
  child, fork, and linked edges.
- `/timeline/sessions/{session_id}/graph` is the browser-auth mirror.
- Existing workflow endpoints are compatibility projections over the same graph
  module; they keep their response shape while the UI migrates toward graph
  primitives.
