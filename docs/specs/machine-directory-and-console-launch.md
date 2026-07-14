# Machine Directory and Console Launch

Status: Active epic; launch UI revision required before further implementation
Owner: Runtime Host machine surface + web/iOS launch UX
Created: 2026-07-14
Related:
- `VISION.md`
- `docs/specs/agents-machine-surface.md`
- `docs/specs/machine-control-truth.md`
- `docs/specs/human-launch-provenance.md`
- `docs/specs/renderable-session-launch-pipeline.md`
- `docs/specs/turn-scoped-console-execution.md`

> **Execution-model correction (2026-07-14):**
> `turn-scoped-console-execution.md` supersedes this document's first-message,
> `one_shot`/`live_control`, and **Keep session open** decisions. This document
> remains canonical for the machine directory, naming, ordering, availability,
> and visual picker contract. Console now creates an empty thread; the normal
> composer starts turn-scoped provider invocations.

## Outcome

Longhouse should present one honest machine directory and one Console launch
contract across web and iOS.

A user opening **New Session** should immediately understand:

- which real machines belong to them;
- which machines can accept a Console launch now;
- which providers can start and resume Console turns on each machine;
- why a machine is unavailable without it disappearing;
- where to inspect the machine when repair is needed.

The Runtime Host owns those semantic decisions. SwiftUI and React own native
presentation, local form state, and platform interaction. Neither client should
reconstruct launch capability from raw Machine Agent support strings.

## Why This Epic Exists

The backend already has the right architectural seam: the shared machine
directory joins durable enrollment with the live Machine Agent control-channel
registry, and `/api/agents/machines` plus `/api/timeline/machines` expose the
same response model.

The response currently exposes several overlapping representations of launch
truth:

- `supports`
- `control_operations_by_provider`
- `launchable_providers`
- `can_launch_codex`
- `launch_blocked_by`
- `online` and `control_channel_status`

Web and iOS then reconcile those fields independently. The implementations
already disagree:

- web treats `launch` or `run_once` as launchable;
- iOS requires live-control `launch`;
- web defaults to `one_shot` and asks for the first prompt;
- iOS always submits `live_control` with no first prompt;
- web hides unavailable machines whenever any machine is launchable;
- iOS includes unavailable machines in one flat native picker;
- both clients duplicate provider defaults, blocked-reason handling, and
  compatibility fallbacks.

This is product drift encoded as client logic. Adding matching helpers in two
languages would preserve the underlying problem.

The incident that exposed the UX also revealed a separate lifecycle failure:
ephemeral benchmark credentials were represented as durable machines because
their producer skipped token revocation. That producer bug is fixed in
`38f05cdc2`, and the leaked credentials were revoked. The durable lesson is
that legitimate offline machines and disposable automation identities must be
fixed at enrollment lifecycle boundaries, never hidden by client name filters.

The first iOS implementation also exposed a specification failure. The spec
allowed a native `Menu` without defining its closed state, open state,
typography, tint, wrapping, or display-name behavior. SwiftUI consequently
rendered the selected value in accent blue, reduced `Ready` to caption size,
and concatenated the routing id and technical block reason into one wrapping
menu label. Passing API and snapshot tests did not make that interaction
acceptable. The revised contract below replaces the menu and makes visual
states part of acceptance.

## Product Decisions

### Provider Scope

Cursor is a supported Console target through the turn-scoped
`cursor-agent acp` adapter. It must appear in both launchers because ACP proves
fresh `session/new` and resumed `session/load` turns. Cursor Helm remains the
separate terminal-owned `longhouse cursor` path.

Antigravity is not a launch target yet. Its proven Machine Agent surface is
`antigravity.send`; it has neither remote launch nor run-once evidence. Adding
it requires a provider execution path, lifecycle/binding contract, and live
proof—not merely exposing another picker row.

### 1. Offline Machines Remain Visible

An enrolled machine is part of the user's machine directory even when its
control channel is disconnected. Offline is a normal availability state, not
an error and not permission to erase the machine from the picker.

Ephemeral QA, CI, benchmark, and smoke identities must revoke their enrollment
credentials when work finishes. The launch UI must not contain prefix-based
filters for automation names.

### 2. Availability Groups Before Name Ordering

The canonical directory order is:

1. ready machines;
2. connected but blocked machines;
3. offline machines.

Within each group, sort case-insensitively by display name and then `device_id`
for stability. Do not sort offline machines by last-seen recency; a picker
should not reshuffle merely because one machine briefly reconnects.

Each client may remember the last selected machine per Runtime Host identity
and choose it initially if it is still ready. That preference should not
reorder the visible directory.

### 2a. Routing Identity Is Not Display Identity

`device_id` is an opaque, stable routing and credential identity. It is never
humanized, suffix-stripped, title-cased, or otherwise transformed by a client.
Names such as `cube-canary` may legitimately encode an environment or
enrollment lane, but that does not make them appropriate primary UI labels.

`machine_name` is the durable user-facing label. The Runtime Host must preserve
it while the machine is offline instead of falling back to `device_id` merely
because the live hello frame is gone. Enrollment should carry a display name,
and a connected Machine Agent hello may refresh that name. Persisting the name
must not persist stale launch capabilities.

For the current dogfood machines the intended labels are `cinder` and `cube`.
`cube-canary` remains available in machine details as the technical routing id
until that enrollment is renamed or replaced. A client must not infer `cube`
from the `-canary` suffix.

### 3. Status Is Explicit and Not Color-Only

Human presentation maps canonical state to:

| State | Visual | Compact copy |
| --- | --- | --- |
| ready | green filled dot | Ready |
| offline | gray hollow dot | Offline · last seen … |
| connected but unsupported | amber info symbol | Console launch unavailable |
| engine too old | amber info symbol | Update required |
| policy restricted | lock or amber info symbol | Restricted |
| auth/runtime failure | red warning symbol | Needs repair |

Red is reserved for a fault that needs repair. A sleeping laptop or stopped VPS
is gray, not a red failure. Text accompanies every color.

`engine_build` is operator detail and does not appear in the compact launch
picker. It remains available in machine details, health, and diagnostics.

### 4. Console Uses Turn-Scoped Execution

Web/iOS launches are **Console** launches. They create an empty durable thread
without starting a provider process. The normal session composer starts the
first and later turns. Each turn acquires a provider invocation and releases it
after the provider's terminal turn outcome.

Neither client asks for a first message in the launcher or exposes
`one_shot`, `live_control`, **Run once**, or **Keep session open**. Those names
describe current implementation paths, not product choices.

### 5. Provider Defaults Are Product Policy

The Runtime Host returns the default provider. Prefer Codex when it can start a
turn on the selected machine; otherwise use stable provider order. The backend
must not return a provider that cannot create and later resume a turn-scoped
Console invocation.

Clients render the returned choice and may preserve an explicit user selection
while it remains valid.

This is deterministic product policy, not model judgment and not a
platform-specific decision.

### 6. Hosted Plan Restrictions Stay Separate From Machine Health

The public Runtime Host does not invent billing tiers. If hosted entitlements
later restrict a launch option, the Control Plane supplies a distinct policy
restriction through its explicit service boundary. A restricted option is not
reported as offline, unsupported, or unhealthy.

No hosted tier field is added in the first implementation slice.

## Canonical Contract

`GET /api/agents/machines` remains the canonical machine route.
`GET /api/timeline/machines` remains the user-auth/browser veneer over the same
service projection and response model.

Raw evidence remains available for agents and operators. A new nested `launch`
projection becomes the only human-client input for launch eligibility and
defaults:

```json
{
  "device_id": "cinder",
  "machine_name": "cinder",
  "control_channel_status": "connected",
  "last_seen_at": "2026-07-14T17:41:07Z",
  "engine_build": "30381fc7",
  "supports": ["codex.run_once", "codex.resume_run_once"],
  "control_operations_by_provider": {
    "codex": ["run_once", "resume_run_once"]
  },
  "launch": {
    "blocked_by": null,
    "providers": [
      {"provider": "codex"}
    ],
    "default_provider": "codex"
  }
}
```

An offline enrollment remains present:

```json
{
  "device_id": "cube-canary",
  "machine_name": "cube",
  "control_channel_status": "disconnected",
  "last_seen_at": "2026-07-12T17:30:30Z",
  "supports": [],
  "control_operations_by_provider": {},
  "launch": {
    "blocked_by": "control_down",
    "providers": [],
    "default_provider": null
  }
}
```

### Contract Rules

- A machine is ready iff `launch.providers` contains at least one provider that
  can create a fresh Console turn and later resume the same thread.
- `launch.blocked_by` is null when providers are present and required when they
  are empty. A redundant launch-status enum is intentionally omitted.
- `launch.providers` is derived once from
  `control_operations_by_provider`; clients do not merge raw `supports` or
  legacy convenience fields.
- Legacy `launchable_providers` is diagnostic compatibility data and does not
  define Console eligibility.
- `default_provider` must name an entry in `launch.providers`.
- Offline entries do not claim last-known provider support. Current capability
  remains live truth.
- `supports` and `control_operations_by_provider` remain raw/mechanical
  evidence for agent and diagnostic consumers.
- `online`, `can_launch_codex`, `launchable_providers`, and
  `launch_blocked_by` remain compatibility and diagnostic fields. Human clients
  stop reading them for launch decisions; deleting them is not part of this
  epic.

The first implementation should use the existing blocked-reason vocabulary.
Do not add speculative status variants. `policy_restricted` lands only with a
real Control Plane entitlement input.

## Impact Map

| Surface | Current responsibility or drift | Epic destination |
| --- | --- | --- |
| `server/zerg/services/machines_directory.py` | joins enrollment/live registry and derives overlapping fields | owns canonical `launch` projection and stable ordering |
| `server/zerg/schemas/machines.py` | flat raw plus compatibility schema | typed nested launch options/defaults |
| `server/zerg/routers/agents_machines.py` | canonical machine-auth route | unchanged route over expanded shared model |
| `server/zerg/routers/timeline.py` | user-auth veneer | unchanged veneer over the same projection |
| `server/zerg/services/managed_provider_contracts.py` | canonical provider-operation mechanics | remains the input to launch projection |
| `web/src/services/api/launch.ts` | hand-maintained DTOs | generated machine DTO consumption |
| `web/src/components/LaunchSessionModal.tsx` | capability merging, defaults, hidden offline machines | native web rendering of backend launch semantics |
| `ios/Sources/Shared/LonghouseAPI.swift` | hand-maintained DTO plus capability inference | hand-owned nested DTO plus transport only |
| `ios/Sources/LonghouseApp/LaunchSessionSheet.swift` | capability/default inference and process-lifetime form | native grouped UI plus turn-scoped Console creation |
| `scripts/ops/remote-launch-smoke.py` | Codex compatibility fallback | canonical launch-option assertion |
| `server/zerg/cli/local_health.py` and dogfood scripts | operator diagnostics over raw truth | continue using raw operations, not human projection copy |
| `web/src/pages/DevicesPage.tsx` | credential-centric management | deferred machine-management epic |

## Human Launch Experience

The launch form is a short session-configuration screen, not a directory, a
prompt composer, or a diagnostics dashboard. It has three primary inputs:

1. where the work runs: machine and workspace;
2. which coding agent runs it;
3. optional provider settings.

Machine is a compact summary row under **Machine**. Agent and workspace are
compact summary rows under **Session**; they are configuration, not additional
places where the work runs. **Create session** is the single primary action.
It creates an empty thread and opens the normal conversation, whose composer
owns the first and later messages. No process-lifetime setting appears.

```text
New Session

MACHINE
  cinder                         >
  ● Ready

SESSION
  Codex                          >
  Coding agent
  zerg                           >
  Workspace · ~/git/zerg

Advanced options                >

[ Create session ]
```

### iOS Selection Pattern

Do not use `Menu`, a wheel picker, or an inline dropdown for machines. Machine
rows contain status detail and need a scalable open state. Tapping the summary
row pushes a **Choose Machine** destination inside the launch sheet's existing
`NavigationStack`:

```text
Choose Machine

AVAILABLE
  ● cinder
    Ready                         ✓

UNAVAILABLE
  ○ cube
    Offline · Last seen 2 days ago
```

Use the same summary-row plus destination pattern for agent and workspace when
there is more than one choice. With one agent, the row is static and has no
chevron. Workspace selection owns search and the potentially long recent list;
the launch form itself never embeds the whole workspace list.

### Web Selection Pattern

Web uses the same summary card and information hierarchy. Its machine row may
open a popover/listbox rather than navigating to another page, provided the
open surface has enough width for two-line rows and supports keyboard and
screen-reader listbox behavior. It must not fall back to a native `<select>`
because native options cannot express grouped availability and secondary copy
consistently.

Pixel parity is not required. Row anatomy, copy, ordering, defaults, and state
transitions are shared product behavior.

### Machine Row Anatomy

Every machine row has separate fields, never a constructed sentence:

- status symbol;
- `machine_name` as the primary label;
- concise human status as secondary text;
- selected checkmark for a ready selected row;
- optional navigation affordance in the closed summary row.

Never concatenate `machine_name`, an em dash, and `launch.blocked_by`. The
technical reason `control_down` maps to the human state **Offline**. Detailed
copy such as **Control channel disconnected** belongs in machine details or a
repair flow, not the chooser. Last-seen context is shown when known.

Unavailable rows remain visible and cannot be selected. In the first slice they
are readable, non-button rows; VoiceOver announces the name, status, and **Not
available**. Machine details and repair navigation are out of the chooser until
there is an explicit trailing details control. A disabled launch row must not
secretly behave as a details link. Collapse **Unavailable (N)** only after more
than three rows, and only when real list size justifies building that behavior.

### Typography and Color

- Primary row labels use the platform body style, regular or semibold.
- Secondary status and path text use subheadline, not caption.
- Caption is reserved for explanatory footnotes below a group.
- Primary labels use the normal foreground color. A selectable container must
  not tint all of its label content with the app accent color.
- Accent color is reserved for the selected checkmark, focused control, and
  primary action.
- Ready uses a green filled dot plus the word **Ready**.
- Ordinary offline uses a gray ring plus **Offline** and is never red.
- Connected without provider launch support uses amber **Console launch
  unavailable**; `engine_too_old` uses amber **Update required**; auth/runtime
  faults use red **Needs repair**. These remain rows within the single
  **Unavailable** group; clients map backend reasons to this pinned copy and do
  not invent another availability enum.
- Dynamic Type may wrap the secondary line, but a machine name and its status
  never share one wrapping text node.

### State and Interaction Rules

- Ready rows select the launch target.
- Selecting another ready machine clears machine-scoped workspace, provider,
  and error state, then applies backend defaults.
- Workspace suggestions are fetched only for the selected ready machine.
- When no machine is ready, show the directory destination with all enrolled
  machines and lead with **No machines ready to launch**. Do not render an empty
  form with a disabled button.
- When there are no enrollments, show the distinct install/connect empty state.
- Both clients display provider names through canonical presentation labels,
  never raw lowercase identifiers.
- Build hashes and routing ids do not appear in compact launch rows.
- The primary action remains reachable without scrolling through a recent
  workspace list. On iOS it should sit after the compact form or in a safe-area
  action inset; it must not be buried beneath optional metadata.
- Changing the machine resets the agent to that machine's backend default.

## Deferred Follow-Ons

The directory currently treats non-revoked device credentials grouped by
`device_id` as enrollment. That is sufficient for this epic and correctly
collapses duplicate credentials for one real machine.

The later management epic should make that model understandable:

- evolve the web **Device Tokens** page toward **Machines & credentials**;
- show one logical machine row with its credential count and last-seen state;
- provide an explicit, confirmed **Forget machine** action that revokes all
  credentials for one `device_id`;
- keep per-credential revoke available as an advanced/security action;
- do not auto-expire legitimate offline machines based only on age;
- require QA/CI producers to own and verify cleanup of ephemeral credentials.

That work must use existing revocation mechanics rather than adding a second
machine registry. It is not a blocker for the launch-contract and picker work.

A producer-wide credential cleanup audit is also deferred. This epic preserves
the benchmark cleanup regression introduced in `38f05cdc2` and requires no
name-based UI hiding; other producers should be fixed when concrete leakage is
observed or in a dedicated automation-hygiene sweep.

## Implementation Workstreams

### A. Canonical Backend Projection

- Add typed `MachineLaunchProjection` and `MachineLaunchProviderOption` schemas.
- Derive them in `services/machines_directory.py` from the existing managed
  provider contract projection.
- Move default-provider policy into that projection.
- Change canonical sorting to state group, display name, `device_id`.
- Keep `/api/agents/machines` and `/api/timeline/machines` byte-shape aligned.
- Add `GET /api/agents/machines` to the machine-surface canon.

### B. Contract Types and Compatibility

- Generate TypeScript machine DTOs from OpenAPI instead of maintaining
  `web/src/services/api/launch.ts` copies.
- Add the nested launch DTO to the existing hand-owned iOS machine-directory
  model. Expanding the session-focused iOS generator is not a blocker.
- Keep legacy response fields for an additive migration window.
- Remove client compatibility inference; backend field deletion is deferred.
- Add schema invariants proving defaults always reference supported options.

### C. Web Launch Surface

- Replace the launchable-only HTML select with availability-grouped rows.
- Consume only `machine.launch` for eligibility, provider options, and defaults.
- Preserve offline rows and render typed reasons.
- Remove build hashes from labels.
- Remove the launch-form prompt and all process-lifetime controls after the
  empty-thread plus composer-first-turn vertical slice works end to end.
- Persist last selected ready machine per Runtime Host identity.

### D. iOS Launch Surface

- Replace the shipped machine `Menu` with a summary row and grouped navigation
  destination.
- Consume only `machine.launch` for eligibility, provider options, and defaults.
- Use gray offline status and reserve red for repairable faults.
- Remove the launch-form prompt and all process-lifetime controls after the
  empty-thread plus composer-first-turn vertical slice works end to end.
- Move agent and workspace selection out of the long inline `Form` and into
  compact summary rows/destinations.
- Persist last selected ready machine per Runtime Host identity.
- Keep preview coverage for the closed form and every chooser state.

### E. Durable Machine Names

- Add a durable machine display-name source to enrollment-backed directory
  entries; do not introduce another machine registry.
- Refresh it from explicit enrollment/rename input or a connected hello frame.
- Return the last durable `machine_name` while offline while keeping current
  capabilities empty.
- Expose `device_id` only in machine details and agent/diagnostic responses.
- Set the dogfood VPS display name to `cube` through the canonical rename or
  enrollment path, never through a client alias table.

### F. Diagnostics and Agent Consumers

- Keep `supports` and `control_operations_by_provider` available to
  `local-health`, provider proof, dogfood checks, and scripts.
- Migrate `remote-launch-smoke.py` away from `can_launch_codex` compatibility
  once `launch.providers` is available.
- Do not make diagnostic tools consume human status copy or icons.
- Keep shipping health, control reachability, and provider launch readiness as
  separate axes.

## Delivery Sequence

The launch projection, DTO migration, grouped web picker, and first iOS form
slice have landed. The screenshots that triggered this revision invalidate the
iOS interaction. The turn-scoped correction also invalidates execution
lifetimes in the backend projection.

Before more implementation, approve the closed-form and open-chooser visual
contract in this document. Then deliver narrow slices:

1. Ship empty-thread creation, `can_start_turn`, and composer first-turn
   dispatch as one vertical slice.
2. Remove execution lifetime and first-message fields from the backend launch
   projection and both clients.
3. Durable machine display names, including the canonical `cube` dogfood label.
4. iOS compact form and machine chooser with open-state preview coverage.
5. iOS agent/workspace destinations and advanced-options cleanup.
6. Web compact summary card and accessible machine listbox/popover.
7. Cross-surface state, accessibility, and visual matrix verification.
8. Remove remaining client compatibility inference and helpers.
9. Stop. Machine credential management, producer-wide cleanup, generator
   expansion, and legacy field deletion remain separate follow-ons.

Each slice should be independently shippable. Do not combine API migration,
both UI rewrites, and machine-management mutations in one change.

## Acceptance Criteria

- Web and iOS show the same enrolled machines grouped as available or
  unavailable.
- The launch form shows compact machine/agent/workspace summaries rather than
  embedding full option lists.
- A legitimate offline machine remains visible with last-seen context.
- Offline machines retain their durable human display name; `device_id` is not
  substituted into compact UI when the control channel disconnects.
- An ephemeral automation credential disappears after its owning workflow
  completes because it is revoked, not because a client recognizes its name.
- Both clients receive identical provider options and defaults from the
  Runtime Host.
- Neither client scans `supports`, merges provider collections, or checks
  `can_launch_codex` to decide launch eligibility.
- Both clients distinguish no enrollment from no ready machine.
- Both clients use gray for ordinary offline state and explicit text alongside
  status color.
- Machine names remain normal foreground text in closed and open selection
  states; platform accent tint does not recolor entire row labels.
- Name, status, and remediation are separate semantic fields and accessibility
  elements, never one concatenated wrapping string.
- Console creation starts no provider process. The conversation composer starts
  fresh and resumed turn-scoped invocations through the same backend contract.
- `/api/agents/machines` and `/api/timeline/machines` remain response-shape
  equivalent.
- Raw machine capabilities remain available to agents and diagnostics.
- Web type checking consumes the generated OpenAPI machine schema; iOS decode
  tests pin its intentionally hand-owned DTO.

## Test Strategy

- Backend matrix: offline, connected/no-turn-adapter, fresh-only,
  fresh-plus-resume, multi-provider, duplicate credential, stable sort.
- Route parity: agent-auth and browser-auth machine responses share one model.
- Web: grouped rendering, status copy, unavailable interaction, backend
  defaults, last selection, empty-thread navigation, composer first turn.
- iOS: DTO decoding, closed-form preview, pushed chooser previews, selection
  reset, offline explanation, backend defaults, empty-thread navigation,
  composer first turn.
- Automation: retain the focused benchmark cleanup regression from
  `38f05cdc2`; broader producer coverage is deferred.
- Visual QA matrix: closed form, chooser open, one/many/no ready machines,
  long names, unknown last-seen, light/dark mode, and default plus accessibility
  Dynamic Type. Capture the actual open selector state; a closed-form snapshot
  cannot approve its popup or destination.
- Device/simulator interaction QA: tap every summary row, select and return,
  verify VoiceOver label/order, verify disabled rows, and verify the primary
  action remains reachable with the keyboard shown.

## Non-Goals

- No shared Swift/TypeScript UI framework.
- No second machine registry or persistent last-known capability cache.
- No offline launch queue or wake-on-demand behavior.
- No provider binary installation or update management.
- No name-prefix filtering for CI/test machines.
- No broad health-dashboard redesign.
- No speculative hosted billing model in the public Runtime Host.
