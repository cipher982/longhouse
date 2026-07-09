# Hatch Automation Session Origin

Status: Implemented V1
Owner: Longhouse session core
Created: 2026-07-08
Related:
- `VISION.md`
- `docs/specs/session-identity-kernel.md`
- `docs/specs/subagent-thread-ingest.md`
- `docs/specs/agents-machine-surface.md`

## Problem

Hatch one-shot agent runs are automation artifacts, not user-facing Longhouse
sessions. They still have valuable transcripts, but they should not fill the
main timeline, iOS timeline, wall, or default recall view as top-level work.

The current system leaks Hatch-launched OpenCode runs into the main timeline.
Dogfood evidence on hosted `david010` showed dozens of recent `opencode`
sessions with one user prompt such as "Final code review..." or "Quick phase
review...", all materialized as primary `branch_kind='root'` threads. Those
sessions are real archive rows, but they are not real user-started tasks.

This is not an iOS problem. iOS is only one client of `/api/timeline/sessions`.
The root issue is that Hatch automation and provider-native subagents are both
trying to flow through the old "sidechain" word.

## Current Failure Mode

Hatch currently does the right thing only at process-launch time:

```text
hatch non-interactive run
  -> sets LONGHOUSE_IS_SIDECHAIN=1
  -> launches provider CLI, often `opencode run --pure ...`
  -> Machine Agent ingests provider transcript
```

The engine compressor turns `LONGHOUSE_IS_SIDECHAIN=1` into
`SessionIngest.is_sidechain=true`. That flag is useful for legacy compatibility,
but it is not enough durable identity evidence.

After the session identity cleanup, `AgentSession.is_sidechain` no longer
exists. The server now hides unresolved subagent rows only when it sees durable
child evidence, such as:

- a primary `SessionThread.branch_kind='subagent'`;
- a `/subagents/` source path;
- provider parentage such as `parent_provider_session_id`;
- provider child aliases such as `subagent_id`.

Hatch OpenCode one-shots usually have none of that. A sampled leaked row had
only:

```text
session.provider = opencode
session.project = longhouse
primary_thread.branch_kind = root
alias provider_session_id = ses_...
source_path = ~/.local/share/opencode/opencode.db#opencode:ses_...
```

The transient Hatch automation bit was not persisted as a durable session graph
classification. From the Longhouse DB alone, many historical Hatch rows can be
recognized only by heuristic shape, not by exact source truth.

## Vocabulary

Do not use "sidechain" for Hatch.

| Term | Created By | Product Meaning | Timeline Default |
| --- | --- | --- | --- |
| Provider root session | User or Longhouse launching a provider | Real user-visible task | Show |
| Provider subagent / sidechain | Claude, Codex, or OpenCode internally spawning child work | Child transcript under a parent provider session | Hide as top-level; attach under parent |
| Provider fork | Provider/user creating a forked continuation | Separate timeline-visible branch when product-visible | Usually show |
| Hatch automation run | Hatch launching a provider CLI for delegated work | Automation child artifact with its own transcript | Hide from main timeline |
| Canary/test/provider proof | Test harness or support automation | Operational evidence | Hide unless explicitly requested |

## Goals

1. Preserve Hatch child transcripts as first-class archive artifacts.
2. Hide Hatch automation from the default main timeline, iOS timeline, wall,
   and default recall/search.
3. Link Hatch child runs back to the calling Longhouse session when that
   context is available.
4. Keep provider-native subagent handling separate from Hatch automation.
5. Make the classification durable enough to survive restarts, archive
   rebuilds, and backfills.
6. Enforce the hide behavior on the hot list projections, especially
   `TimelineCard`, not only on deep graph rows.
7. Keep V1 small: no transcript merging, no rich child-run UI required.

## Non-Goals

- Do not merge Hatch child transcript events into the parent transcript.
- Do not pretend every historical Hatch row can be perfectly recovered from
  Longhouse data alone.
- Do not reintroduce `AgentSession.is_sidechain`.
- Do not make iOS, web, or recall special-case Hatch prompts.
- Do not require Hatch to be running inside a managed parent session; orphan
  Hatch automation still needs a durable hidden classification.

## Identity Model

The missing model is not another session type and not a broad new taxonomy. V1
needs one durable fact:

```text
origin_kind:
  hatch_automation
```

Absent `origin_kind` means "normal product session" for this spec. Existing
axes continue to own their domains:

- provider lineage uses `branch_kind`, `SessionEdge`, and provider aliases;
- test/provider-proof sessions use existing environment/canary filters;
- managed/user control state uses the session kernel capability projection.

Default timeline visibility is derived from `origin_kind=hatch_automation`.
There is no separate V1 `relationship_kind` or `timeline_visibility` enum.
Parent linkage uses the existing `SessionEdge.edge_kind`.

Storage must be explicit enough for the hot list path. Do not make aliases the
only source of truth for hiding. V1 should persist the origin on the primary
thread/session graph and denormalize the default-hide decision into
`TimelineCard` or whatever compact list projection replaces it.

```text
session_threads or compact session graph projection
  origin_kind = hatch_automation

timeline_cards or equivalent hot-list projection
  origin_kind = hatch_automation
  hidden_from_default_timeline = true

session_edges
  edge_kind = automation_child
  visibility = hidden
  parent_longhouse_session_id = <optional parent session UUID>
  hatch_run_id = <optional hatch-generated run UUID>
```

Fail-visible rule: unknown or ambiguous rows stay visible. Only explicit Hatch
origin, or an approved high-confidence repair, may hide a row.

Sticky refresh rule: once a session/thread is marked `hatch_automation`, later
provider re-ingest without Hatch env must not clear the mark.

Precedence rule: provider-native subagent evidence wins provider lineage. If a
future payload somehow has both provider-subagent evidence and Hatch origin,
attach it as the provider child transcript and preserve Hatch origin as a label;
do not create a second top-level Hatch child session.

## Hatch Contract

When Hatch launches a provider process, it should set explicit Longhouse
automation metadata instead of relying on the legacy sidechain bit:

```text
LONGHOUSE_ORIGIN_KIND=hatch_automation
LONGHOUSE_HATCH_RUN_ID=<uuid>
LONGHOUSE_PARENT_SESSION_ID=<current Longhouse session id, if known>
LONGHOUSE_PARENT_THREAD_ID=<current Longhouse thread id, if known>
LONGHOUSE_PARENT_PROVIDER_SESSION_ID=<current provider session id, if known>
```

`LONGHOUSE_IS_SIDECHAIN=1` may stay temporarily as a compatibility signal, but
new code must not depend on it for Hatch.

Hatch should source the parent session from the normal Longhouse managed-session
environment when present. If no parent is present, Hatch still sets
`origin_kind=hatch_automation`; the run becomes a hidden orphan automation
artifact by derived default-list behavior.

## Engine Contract

The engine ingest payload should carry automation origin separately from
provider lineage:

```text
origin_kind: str | None
hatch_run_id: str | None
parent_longhouse_session_id: str | None
parent_thread_id: str | None
parent_provider_session_id: str | None
```

Provider-native fields remain provider-native:

```text
is_sidechain
lineage_kind
parent_provider_session_id
subagent_id
subagent_tool_use_id
workflow_run_id
```

The engine must not convert Hatch automation into provider subagent lineage.
For OpenCode in particular, `opencode run --pure` may create a perfectly normal
provider root transcript. The fact that Hatch launched it is Longhouse origin
metadata, not OpenCode lineage metadata.

## Server Ingest Contract

On ingest:

1. Normalize provider lineage exactly as today for Claude/Codex/OpenCode child
   transcripts.
2. Normalize Longhouse origin metadata independently.
3. If provider lineage resolves as a subagent, attach to the provider parent
   child thread and hide top-level rows as already specified in
   `subagent-thread-ingest`.
4. If `origin_kind=hatch_automation`, create or update a normal archive
   session/thread, persist sticky Hatch origin, denormalize the default-hide
   decision onto `TimelineCard` or the hot list projection, and record a
   `SessionEdge` to the parent when available.
5. Do not merge Hatch child events into the parent primary thread.
6. Do not let runtime/control state on a Hatch child promote the parent or child
   into a live-control affordance.

Recommended edge:

```text
session_edges(
  edge_kind = automation_child,
  visibility = hidden,
  source_session_id = <parent Longhouse session, optional>,
  source_thread_id = <parent thread, optional>,
  target_session_id = <hatch child session>,
  target_thread_id = <hatch child primary thread>,
  provider_edge_id = <hatch_run_id or child provider_session_id>,
  metadata_json = {
    "origin_kind": "hatch_automation",
    "hatch_run_id": "...",
    "launcher": "hatch",
    "provider": "opencode"
  }
)
```

If the parent session id arrives before the parent thread is materialized,
record the parent id alias and let a small reconciliation pass fill the thread
edge later.

## Read Behavior

Default human-facing session lists must filter hidden automation:

- `/api/timeline/sessions`
- timeline SSE stream
- iOS timeline through the same route
- `/api/agents/sessions/wall`
- default recall/search contexts

Machine/debug reads should be able to include it explicitly:

```text
include_automation=true
origin_kind=hatch_automation
```

V1 should add at most one public include flag. Do not add separate
`relationship_kind` query filters until there is a real user/debug workflow.

Parent session detail can later expose a compact "Automation runs" drawer, but
V1 only needs the data model and default filters.

## Backfill

There are two different backfills:

### Provider Subagent Backfill

This remains source-truth backfill. Use durable provider evidence:

- Claude `/subagents/` source paths and raw `isSidechain:true`;
- Codex `source.subagent` or `forked_from`;
- OpenCode `parent_id` plus parent `task` part metadata;
- existing `SessionThreadAlias` and `SessionEdge` rows.

Confidence is high when that evidence exists.

### Hatch Automation Backfill

Historical Hatch classification is only partially recoverable from Longhouse DB
alone because the launch-time `LONGHOUSE_IS_SIDECHAIN=1` bit was not persisted
as durable origin metadata.

Use conservative heuristics only for rows whose shape strongly matches Hatch:

- provider is `opencode` or other Hatch-backed provider;
- primary thread is `branch_kind='root'`;
- exactly one distinct user prompt or a small repeated duplicate set from
  archive replay;
- prompt uses known Hatch review/delegation patterns;
- source path is provider-native one-shot storage such as
  `opencode.db#opencode:ses_...`;
- no managed Longhouse control path;
- no evidence of a user TUI session.

Prompt/shape heuristics are report-only by default. They must not auto-hide
real user one-shot sessions such as a manually started OpenCode "review this"
run. Auto-apply should require exact Hatch local artifacts or a reviewed ID
list.

If Hatch local result artifacts contain `provider_session_id` or session ids,
prefer those over heuristics. The backfill should emit a dry-run report with
confidence buckets before applying:

```text
high_confidence_hatch_rows
medium_confidence_hatch_rows
skipped_ambiguous_rows
provider_subagents_relinked
```

Only exact/high-confidence artifact-backed rows should be hidden automatically.
Medium confidence rows should be report-only unless explicitly approved.

## Current Code Issues To Address

1. Hatch only sets `LONGHOUSE_IS_SIDECHAIN=1`, which is too lossy for durable
   session origin.
2. The engine compressor still treats the env override as `is_sidechain`, which
   conflates Hatch automation with provider subagents.
3. `classify_lineage_kind()` returns `none` for `is_sidechain=true` when no
   parent or `/subagents/` source path exists. That is correct for provider
   lineage, but it means Hatch automation is not classified anywhere else.
4. Timeline filters only exclude primary `branch_kind='subagent'` unresolved
   children. Hatch one-shots are root primary threads, so they pass.
5. Historical sampled rows have empty slim `source_lines.raw_json` and empty
   transcript observation payloads; the ingest envelope is not available for a
   perfect historical reconstruction.
6. `TimelineCard` is a denormalized hot path. If it is not updated, hidden graph
   metadata can still leak through the real timeline UX.

## Implementation Plan

### Phase 1: Contract and Storage

- Add Longhouse origin fields to `SessionIngest`.
- Add engine metadata extraction from `LONGHOUSE_ORIGIN_KIND`,
  `LONGHOUSE_HATCH_RUN_ID`, and parent session/thread env vars.
- Persist `origin_kind=hatch_automation` durably on the primary thread/session
  graph.
- Denormalize `origin_kind` or `hidden_from_default_timeline` onto
  `TimelineCard` or the hot-list projection.
- Record a hidden `SessionEdge(edge_kind='automation_child')` when a parent is
  known.
- Keep `LONGHOUSE_IS_SIDECHAIN=1` compatibility during rollout.

### Phase 2: Hatch Emission

- Update Hatch to emit explicit origin/visibility env vars for every
  automation launch.
- When Hatch runs inside a Longhouse managed session, pass through the current
  parent session/thread/provider context.
- Add Hatch tests proving non-interactive runs set both JSON/automation defaults
  and explicit Longhouse origin metadata.

### Phase 3: Timeline and Search Filters

- Update timeline list and timeline stream filters to exclude Hatch automation
  from the shared list-filter seam.
- Add a single explicit `include_automation=true` or `include_hidden=true` flag
  for debug/archive reads.
- Extend wall/default recall filters to exclude hidden automation unless
  requested.
- Add API tests that a Hatch child archive row is preserved but absent from the
  default timeline.

### Phase 4: Backfill

- Extend the existing kernel backfill command or add a focused command:
  `longhouse db classify-automation --dry-run --json`.
- First pass: provider subagents from source truth.
- Second pass: exact Hatch artifact-backed automation classifier.
- Prompt/shape heuristic classifier reports candidates only.
- Produce a report before applying.
- Apply only exact/high-confidence Hatch rows by default.
- Run as an explicit operator command, not a startup migration.

### Phase 5: Parent Detail Surface

- Optional V1.5: expose linked automation child runs on the parent session
  detail page through a compact debug drawer.
- Do not block timeline cleanup on this UI.

## Test Plan

- Hatch unit: non-TTY surfaced OpenCode run sets explicit origin env vars.
- Engine unit: origin env vars serialize into ingest JSON independently of
  `is_sidechain`.
- Server ingest unit: `origin_kind=hatch_automation` creates a root archive
  session with sticky Hatch origin, hidden hot-list projection, and optional
  parent edge.
- Server ingest unit: provider subagent with parent evidence still attaches as
  child `branch_kind='subagent'`, not as Hatch automation.
- Timeline API unit: hidden Hatch automation is absent by default and present
  with an explicit include flag.
- Timeline stream unit: hidden Hatch automation is not emitted by default.
- Wall/recall tests: hidden automation does not pollute default results.
- Backfill dry-run test: high-confidence Hatch-shaped rows are reported, not
  blindly applied.
- Sticky refresh test: re-ingest without Hatch env does not clear Hatch origin.
- Negative control: normal interactive OpenCode "review this" session stays
  visible.
- Hatch negative control: interactive or explicitly non-automation Hatch paths
  do not set the hide origin.
- Attention/title test: hidden Hatch automation does not trigger default
  attention/title waste.

## Open Questions

1. Should V1 store `origin_kind` as an indexed column on `session_threads`, on
   `timeline_cards`, or in a compact graph projection table? Alias-only storage
   is not sufficient for the hot list path.
2. Can Hatch local artifacts provide provider session ids for historical
   backfill? If yes, the historical classifier can be much more precise.
3. Should `LONGHOUSE_IS_SIDECHAIN=1` be deprecated immediately after explicit
   origin metadata ships, or retained for third-party agent-mesh callers?

## Review Refinements Adopted

A first-principles review by a Hatch-spawned Cursor/Grok agent recommended
keeping V1 thinner than the original draft:

- use one durable `origin_kind=hatch_automation` fact;
- derive default hide behavior instead of adding a parallel visibility enum;
- use `SessionEdge.edge_kind='automation_child'` for linkage;
- denormalize into `TimelineCard` or the hot list projection;
- make the mark sticky across later re-ingest;
- keep heuristic historical backfill report-only unless exact Hatch artifacts
  or reviewed IDs are available.
- persist Hatch OpenCode origin through a provider-session sidecar because
  daemon-side `opencode.db` shipping cannot see the provider process env.
