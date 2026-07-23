# Shadow Automation Isolation and Timeline Curation

Status: Proposed
Owner: Longhouse session core
Created: 2026-07-23
Related:

- `VISION.md`
- `docs/specs/human-launch-provenance.md`
- `docs/specs/agents-machine-surface.md`
- `docs/specs/provider-release-proof.md`

## Decision

Keep Shadow permissive: a native provider transcript discovered on a user's
machine is a real session and remains eligible for the default timeline even
when Longhouse did not launch it. Do not attempt to infer whether it was
started by a human, CI, or a test from transcript text, timing, process shape,
or provider-specific heuristics.

Instead, automation that invokes a real provider must establish its test/CI
boundary **before the provider creates a native transcript**. A provider test
must either write only to an isolated provider state root outside Machine Agent
discovery, or register an exact, durable source exclusion before launch. The
test boundary, never the transcript contents, supplies `test_or_canary`
provenance if a record is intentionally imported.

Separately, users can hide a session from their own default timeline. This is a
reversible preference; it neither deletes history nor reclassifies its origin.

## Why

Shadow is a launch-product promise: someone can type `claude`, `codex`, `agy`,
or another supported provider directly into a terminal and later find that
native transcript in Longhouse. A real-provider proof using stock `agy --print`
is indistinguishable from that user action once both write the same upstream
transcript store. Default-hidden unknown Shadow sessions would therefore make
Longhouse lose legitimate user work.

The July 22 Antigravity leak demonstrated the other half of this rule. The
provider-release proof ran stock `agy`, which wrote to the ordinary discovered
Antigravity brain directory. The Machine Agent truthfully imported it as a
native Shadow transcript. It had no test provenance, so it was visible. The
failure was producer-boundary isolation, not Shadow ingest or timeline
admission.

## First Principles

1. **Native transcript existence is sufficient for Shadow import.** It is not
   evidence of who initiated a process, and Longhouse must not pretend it is.
2. **The initiator owns automation classification.** A harness, CI job, agent
   task, or provider factory knows it is automation before spawning a provider;
   an archive reader usually cannot know later.
3. **Provider behavior is tested honestly.** Isolation may change a provider's
   documented config/state root, but may not replace the provider binary,
   fabricate transcripts, or suppress failures merely to keep the timeline
   clean.
4. **Discovery remains lossless for user work.** No global ignore rule for
   prompt markers, temporary-looking paths, `--print`, process ancestry, or
   unknown Shadow sessions.
5. **Visibility has distinct owners.** Producer provenance answers why a known
   automation record is hidden. A user preference answers what that user wants
   to see. Neither mutates raw history.
6. **A hidden record stays inspectable.** Direct URLs, search with an explicit
   include-hidden control, raw export, and operator diagnostics remain possible
   subject to normal owner authorization.

## Producer Boundary Contract

Every automation path that can launch a provider capable of writing native
transcripts must declare one of these modes before process launch:

| Mode | Use | Requirement |
| --- | --- | --- |
| `isolated_provider_state` | Real-provider proof where the provider supports an explicit state/profile/home root | Point that root inside the run directory, verify the created transcript is outside every discovered root, then remove it with normal run cleanup. |
| `unwatched_worker` | A provider cannot isolate state | Run the proof on a machine without a connected Machine Agent. This is a deliberate infrastructure choice, not a silent fallback. |
| `registered_exact_exclusion` (compatibility only) | A provider has no supported state-root override and cannot use an unwatched worker | Register one run-scoped canonical source identity with the local Machine Agent before spawn. This is last-resort compatibility work, not a general discovery-ignore plane. |

The default is `isolated_provider_state`, followed by `unwatched_worker` when
isolation is unavailable. A provider adapter must explicitly declare its mode;
absence of a declaration fails the proof before the provider is started. Do not
add a best-effort fallback from one mode to another.

### Isolated provider state

Each adapter owns the provider-specific invocation details and must prove all
of the following in its artifact:

- the upstream binary path and version actually run;
- the exact documented provider configuration/state override used;
- the run-local state root and workspace;
- the transcript path(s) created by the invocation;
- that none is under a Machine Agent discovery root; and
- cleanup outcome without suppressing provider/test failures.

For Antigravity, first establish the supported CLI configuration or state-root
mechanism with an upstream contract test. `LONGHOUSE_HOME` is not an
Antigravity transcript-root override and must not be represented as one. Until
that supported mechanism exists, the Antigravity real-send proof runs on an
unwatched worker. Do not add a broad Machine Agent exclusion API merely to keep
this proof on a watched machine.

### Registered exact exclusion

This is a Machine Agent capability, not a Runtime Host guess. Its request must
contain a generated run id and one exact run-scoped canonical source identity;
it may not accept directory prefixes, globs, prompt text, or a generic `ignore
tests` switch. The registration key must be impossible to collide with normal
user work, carry a small maximum TTL, be single-use where possible, and fail the
proof if the source created by the provider does not match it.

The Machine Agent persists the exclusion locally before spawn and applies it at
discovery/ship time. A release/control proof does **not ship** a matching source.
Only a harness whose explicit purpose is to prove ingest/provenance may instead
ship a record with immutable `origin_kind=test_or_canary`; that exceptional
behavior is a required artifact field, not an adapter choice made ad hoc. An
exclusion is removed in a `finally` path. Expiry fails closed to normal Shadow
import; it must never hide unrelated user sessions.

This is a compatibility bridge, not the preferred long-term provider contract:
source identity cannot always be known before a provider creates it.

### Required changes to automation callers

- `provider-control-e2e-canary.py` declares and records its producer-boundary
  mode for every real-provider branch.
- `provider-release-proof.py`, release automation, provider-factory work, CI,
  and agent-run provider proofs pass a generated run id and reject an
  adapter that lacks a mode declaration.
- The launch environment explicitly carries `LONGHOUSE_ORIGIN_KIND=test_or_canary`,
  `LONGHOUSE_LAUNCH_ACTOR=automation`, and a surface (`test`, `ci`, or `hatch`)
  for Longhouse-owned paths. These values are supplemental evidence, never a
  replacement for state isolation or exact exclusion.
- Proof artifacts surface `producer_boundary` as a required green/failed
  component, so a provider proof cannot be accepted while leaking into a
  watched user's normal transcript root.

## Timeline Curation: User Hide

### Product behavior

Add **Hide from timeline** to a session's overflow menu and detail view.
It immediately removes that session from the owner's default timeline while
preserving the transcript, its source provenance, search index, and direct
link. The action offers Undo. A companion **Hidden sessions** filter lets the
owner inspect and restore hidden sessions. Initial scope is one session only;
there is no provider-wide, project-wide, or content-rule filter UI.

This is intentionally different from the existing task-state action `archive`:
archiving describes work organization, while hiding says “I do not want this
record in my normal timeline.” A user may choose either or both.

### Canonical data model

Keep machine/origin truth separate from a user decision:

```text
sessions.user_hidden_from_timeline boolean not null default false
sessions.user_hidden_at            timestamp nullable
sessions.user_hidden_by_owner_id   owner identifier nullable
```

Mirror the effective field needed for hot catalog reads on the canonical
catalog/session-card projection. `hidden_from_default_timeline` remains the
producer/origin-driven classification (`test_or_canary`, agent automation).
Do not overload it for an interactive user action.

Effective default visibility is:

```text
NOT hidden_from_default_timeline
AND NOT user_hidden_from_timeline
AND user_state NOT IN (archived, snoozed, deleted)
```

The session remains returned only when a caller explicitly asks to include the
relevant hidden class; a direct owner-authorized detail/read remains available
regardless of either hide field. `include_user_hidden` and
`include_origin_hidden` are separate machine query controls—user curation must
not accidentally surface tests/canaries, nor must an operator test view erase
the user's own curation. Search and recall default to the same effective filter.
Machine archive/export enumeration does not lose raw data because of this
preference.

### API and authorization

Extend the existing canonical owner-scoped session-preferences mutation and
catalog transaction with `user_hidden_from_timeline`; then expose the browser
veneer. Do not create a parallel preferences store. The HTTP shape can remain a
narrow visibility endpoint if that is clearer than the existing action routes,
but it must call the same `update_session_preferences` service and projection
path:

```text
PATCH /api/agents/sessions/{session_id}/timeline-visibility
{ "hidden": true | false }

PATCH /api/timeline/sessions/{session_id}/timeline-visibility
{ "hidden": true | false }
```

The endpoint is idempotent, owner-scoped, and emits a normal catalog/timeline
invalidation. It records an audit-quality preference timestamp and owner id;
it must not alter `origin_kind`, launch provenance, raw objects, or `user_state`.
A client cannot hide another owner's session and an ingest refresh cannot clear
a user's hide choice. Hiding causes a removal/omission on the default timeline
stream; restoring causes it to reappear. Search and recall follow the same
default and explicit include controls.

### UI contract

- Timeline card overflow: `Hide from timeline`.
- Session detail overflow: the same action, plus `Unhide` when opened through a
  direct link or the Hidden filter.
- Default timeline excludes user-hidden rows without a toast-like permanent
  “filtering” surprise; the action itself confirms and exposes Undo.
- A `Hidden` view maps only to `include_user_hidden`; an operator/test view may
  separately request origin-hidden records. Ship either view only after the
  basic hide action is proven. Do not start with Gmail-like rule creation.
- Copy says “Hide from timeline,” never “Delete” or “Ignore.”

## Rollout and Verification

1. Add a failing regression that runs the Antigravity real-send canary with the
   local Machine Agent watching its normal provider root; prove the selected
   boundary produces no shipped session for an ordinary release/control proof.
2. Implement the provider-specific isolation contract (or an explicitly
   approved compatibility exclusion) and make the proof artifact require it.
3. Ship the user-hide data/API projection with unit coverage for owner scope,
   idempotence, ingest non-overwrite, default exclusion, direct detail, and
   include-hidden search/recall.
4. Add the minimal hide/unhide UI and fixture-backed capture. Use it to clean
   up already-imported leaks; do not automatically reclassify or delete them.
5. On dogfood, run one real native provider session and one real provider proof:
   the former appears as Shadow; the latter has no default-timeline card. Hide
   and restore the real session without changing its archive/export.

## Non-goals

- Do not make Shadow opt-in.
- Do not classify automation from transcript contents or model markers.
- Do not delete leaked historical records as part of this work.
- Do not introduce user-authored content filters, inbox rules, or bulk cleanup.
- Do not make a background suppression mechanism silently fall back to a
  different machine, credential, provider binary, or test mode.
