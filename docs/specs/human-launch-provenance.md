# Human Launch Provenance

Status: Implemented
Owner: Longhouse session core
Created: 2026-07-09
Related:
- `VISION.md`
- `docs/specs/renderable-session-launch-pipeline.md`
- `docs/specs/agents-machine-surface.md`

## Problem

Longhouse currently protects the main timeline mostly by identifying things that
should be hidden after the fact: provider subagents, delegated automation, and
test/canary runs. That works for known classes, but it is the wrong long-term
polarity. Agents can call agents for review, testing, benchmarks, CI proofs, or
their own helper workflows. Trying to name every non-user launch shape creates a
taxonomy chase and leaves the default human timeline vulnerable to the next new
automation path.

The dogfood failure that motivated this spec had two leak classes:

- Automated review/audit sessions such as "Final code review..." showing as normal
  root sessions.
- Automated integration-test sessions such as "What is 5+5? Reply with just
  the number." and "What is the largest planet? Reply with just the planet
  name." showing as searchable user work.

Both are real archive artifacts, but neither is a session the user intentionally
started as his own work item. The better root fix is to positively label the
sessions the user did start: typing `longhouse codex` / `longhouse claude` in a
terminal, or launching from Longhouse web/iOS. Automation can still be archived
and searchable, but user-facing default surfaces should eventually prefer
positive human-intent provenance over negative heuristics.

## Current State

The current implemented V1 hidden-origin model persists:

```text
origin_kind:
  hatch_automation
  test_or_canary

hidden_from_default_timeline:
  true when origin_kind is hidden
```

That is necessary but incomplete. It answers "why should this row be hidden?"
It does not answer "why should this row be visible?" A bare provider transcript
imported from disk and a provider transcript launched by `longhouse codex` can
both look like normal provider root sessions unless the launch path preserves
the user's action.

Longhouse already has strong positive evidence in some paths:

- managed-local wrappers (`longhouse claude`, `longhouse codex`, `longhouse
  opencode`, `longhouse agy`) call `/api/sessions/managed-local/this-device`
  before starting the provider process;
- Console creation from web/iOS creates an empty durable session and thread
  before the composer dispatches a turn-scoped invocation to the Machine Agent;

The missing step is to persist a small, explicit launch-provenance fact on those
rows and keep it sticky when archive ingest later refreshes the same session.

## Vocabulary

| Field | Meaning | Examples |
| --- | --- | --- |
| `launch_actor` | Who intentionally initiated this Longhouse session from the product's point of view | `human_shell`, `human_ui`, `automation` |
| `launch_surface` | Where that initiation happened | `terminal`, `web`, `ios`, `api`, `hatch`, `test`, `ci` |
| `origin_kind` | Why a transcript is default-hidden | `hatch_automation`, `test_or_canary` |
| `execution_home` | Where/how the provider process is controlled | `managed_local`, `unmanaged_local`, `managed_hosted` |

`launch_actor` is not a provider lineage field. Provider subagents still use
`branch_kind`, thread aliases, and `SessionEdge`. `launch_actor` is also not
control capability. A session can be `launch_actor=human_shell` and currently
search-only if the control channel is gone.

`launch_actor` is product provenance, not an integrity boundary. In V1 the
managed-local terminal stamp is asserted by the Longhouse CLI request under a
TTY/no-automation guard; the server normalizes it but does not cryptographically
prove the local foreground shell.

## V1 Goals

1. Persist positive human-launch provenance for Longhouse-owned launch paths.
2. Keep the provenance sticky across later archive ingest and projection
   rebuilds.
3. Denormalize the provenance onto `TimelineCard` so hot-list/debug surfaces can
   reason about it without deep joins.
4. Do not hide `launch_actor=unknown` by default yet. Unknown includes existing
   Shadow history and older rows. Tightening default visibility is a later,
   measured rollout after we have enough labeled live data.
5. Preserve hidden-origin handling for automation/test rows. Positive human
   launch proof should not unhide explicit `origin_kind=hatch_automation` or
   `test_or_canary`.

## Non-Goals

- Do not infer every bare CLI launch from process ancestry in V1.
- Do not require shell integration before Longhouse remains useful.
- Do not reclassify historical unknown rows automatically.
- Do not add a broad user-facing launch taxonomy to UI copy.
- Do not merge or delete automation transcripts.

## Data Model

Add nullable, indexed launch-provenance columns to the session graph and hot
projection:

```text
sessions.launch_actor     text nullable
sessions.launch_surface   text nullable

timeline_cards.launch_actor
timeline_cards.launch_surface
```

Allowed actor values:

```text
human_shell
human_ui
automation
```

Allowed surface values:

```text
terminal
web
ios
api
hatch
test
ci
provider_subprocess
```

Unknown or invalid values normalize to `NULL`. Store `NULL` for "not yet
known"; do not store a string value such as `unknown`.

`SessionRun.launch_origin` remains the per-run execution lifecycle label
(`longhouse_spawned`, `external_adopted`). It is not a
human-intent label: QA harnesses and automation can create
`longhouse_spawned` runs. `launch_actor` is the durable human/automation
provenance for default product visibility. `execution_home` remains the control
and location axis.

## Launch Contracts

### Managed Local Terminal

`longhouse <provider>` wrappers should create sessions with:

```text
launch_actor = human_shell
launch_surface = terminal
```

The wrapper should also pass the same metadata to the provider process through
environment variables so archive ingest can reinforce the server-created row:

```text
LONGHOUSE_LAUNCH_ACTOR=human_shell
LONGHOUSE_LAUNCH_SURFACE=terminal
```

The wrapper should only stamp that environment when it is actually serving an
interactive human terminal and no automation marker is present:

```text
stdin/stdout are TTY
AND LONGHOUSE_ORIGIN_KIND is absent
AND LONGHOUSE_IS_SIDECHAIN is not true
```

Nested automation must be able to override or unset these variables. If a
payload has `origin_kind=hatch_automation` or `test_or_canary`, or the payload
is a provider sidechain/subagent, that automation signal wins and human env
provenance from inheritance is ignored. Bare `codex` or `claude` Shadow ingest
remains unlabeled until a separate shell-integration or process-lineage proof
lands.

### Web / iOS / API Console Creation

Console session shells should record provenance from the authenticated
principal, not merely from the route or shared service:

```text
browser/native authenticated user -> launch_actor = human_ui
agents-token / machine authenticated caller -> launch_actor = automation
launch_surface = api
```

If web/iOS later send an explicit client surface, the server can store `web` or
`ios`; V1 may use `api` for all browser/native Console creation because the load
bearing distinction is `human_ui` versus automation.

### Automation

Delegated automation and explicit probes should keep using hidden origins:

```text
origin_kind = hatch_automation | test_or_canary
hidden_from_default_timeline = true
```

They may also set:

```text
launch_actor = automation
launch_surface = hatch | test | ci
```

V1 does not require this for hiding because hidden origin already gates default
visibility. It is useful debug metadata when available.

## Precedence

Use this ordering when multiple signals are present:

1. `origin_kind in (hatch_automation, test_or_canary)` sets
   `hidden_from_default_timeline=true` and prevents storing a human
   `launch_actor` from the same payload.
2. Authenticated launch shell creation is authoritative: browser/native user
   launches stamp `human_ui`; managed terminal wrappers stamp `human_shell`;
   agents-token launch routes stamp `automation`.
3. Env or transcript metadata may fill missing launch provenance, but only when
   no hidden origin or sidechain signal is present.
4. `NULL` means unlabeled legacy/Shadow history and remains visible in V1 unless
   some other existing filter hides it.

## Read Behavior

V1 does not flip the timeline to positive-only visibility. Default lists still
hide rows using `hidden_from_default_timeline`. The new provenance fields are
visible to backend projections and debug/API consumers, and future tightening
can be expressed as:

```text
show by default when:
  hidden_from_default_timeline = false
  AND (
    launch_actor in (human_shell, human_ui)
    OR legacy_unknown_visibility_rollout_enabled
  )
```

This avoids losing unlabeled Shadow history while giving Longhouse a durable
path to protect the main page once managed and shell-integrated launch paths are
fully labeled.

## Sticky Rules

1. Ingest may fill `launch_actor`/`launch_surface` only when the existing value
   is `NULL`.
2. Ingest must never overwrite an existing non-null launch provenance value.
   Corrections are explicit operator repairs, not side effects of later archive
   refresh.
3. Incoming `NULL` must never clear existing launch provenance.
4. Explicit hidden origin always keeps the row hidden and blocks storing a
   human actor from the same inherited-env payload.
5. If a later ingest changes the row into a hidden origin, an existing human
   launch actor is cleared. This handles classification sidecars that arrive
   after a first archive ingest.
6. Provider sidechain/subagent ingest blocks inherited human actors the same way
   hidden origins do.

## Implementation Plan

1. Add model columns to `AgentSession` and `TimelineCard`.
2. Add `launch_actor` and `launch_surface` to `SessionIngest`.
3. Normalize/persist the fields in `AgentsStore.ingest_session`, including
   existing-session refresh and timeline card updates.
4. Set managed-local browser-auth launch shells to `human_shell` / `terminal`.
5. Set browser/iOS Console session shells to `human_ui` / `api`, accepting a
   validated `web`/`ios` client surface hint when present; agents-token Console
   routes stamp `automation` / `api`.
6. Pass `LONGHOUSE_LAUNCH_ACTOR` and `LONGHOUSE_LAUNCH_SURFACE` from managed
   local provider wrappers only under the interactive/no-automation guard.
7. Extend engine compressor metadata/env propagation.
8. Add focused tests for:
   - managed-local shell rows carry human-shell provenance;
   - browser/native Console rows carry human-ui provenance;
   - agents-token Console rows do not get human-ui provenance;
   - ingest persists provenance and does not clear it on later unlabeled ingest;
   - ingest does not overwrite existing conflicting provenance;
   - hidden origins still hide and clear inherited human provenance when they
     arrive after a first ingest;
   - sidechains/subagents suppress inherited human provenance;
   - engine compressor ignores inherited human provenance when hidden origin is
     or sidechain evidence is present and ships clean env-derived launch
     provenance otherwise.

## Open Questions

1. Should client launches eventually distinguish `web` versus `ios`, or is
   `human_ui` enough for the visibility decision?
2. Should a future shell integration stamp bare provider launches from Warp or
   Terminal as `human_shell`, or should Longhouse reserve positive human
   provenance only for `longhouse <provider>` wrappers?
3. When positive-only timeline filtering rolls out, should unknown Shadow
   history remain visible by age/grace period, by provider, or only behind a
   user setting?
