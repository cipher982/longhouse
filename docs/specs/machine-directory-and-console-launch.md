# Machine Directory and Console Launch

Status: Proposed epic
Owner: Runtime Host machine surface + web/iOS launch UX
Created: 2026-07-14
Related:
- `VISION.md`
- `docs/specs/agents-machine-surface.md`
- `docs/specs/machine-control-truth.md`
- `docs/specs/human-launch-provenance.md`
- `docs/specs/renderable-session-launch-pipeline.md`

## Outcome

Longhouse should present one honest machine directory and one Console launch
contract across web and iOS.

A user opening **New Session** should immediately understand:

- which real machines belong to them;
- which machines can accept a Console launch now;
- which provider and execution modes are available on each machine;
- why a machine is unavailable without it disappearing;
- what action, if any, restores availability.

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

## Product Decisions

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

### 3. Status Is Explicit and Not Color-Only

Human presentation maps canonical state to:

| State | Visual | Compact copy |
| --- | --- | --- |
| ready | green filled dot | Ready |
| offline | gray hollow dot | Offline · last seen … |
| connected but unsupported | amber info symbol | Update or configure |
| policy restricted | lock or amber info symbol | Restricted |
| auth/runtime failure | red warning symbol | Connection needs repair |

Red is reserved for a fault that needs repair. A sleeping laptop or stopped VPS
is gray, not a red failure. Text accompanies every color.

`engine_build` is operator detail and does not appear in the compact launch
picker. It remains available in machine details, health, and diagnostics.

### 4. Console Prefers One-Shot by Default

Web/iOS launches are **Console** launches. The default execution lifetime is
`one_shot` when the target advertises it, matching the canonical Console
definition: the provider runs one turn under the Machine Agent and the
Longhouse UI is the user interface. A machine with no one-shot-capable provider
falls back honestly to an advertised `live_control` option rather than becoming
artificially unavailable.

When a provider also supports `live_control`, both clients may offer the same
advanced **Keep runtime open** choice. That option is explicit and must not be
silently selected because a client lacks a first-message field.

iOS therefore needs a first-message field and the same execution-lifetime
choice as web before it switches to the canonical default. Until that UI lands,
its existing explicit `live_control` request remains a known compatibility
exception rather than being redefined as Console truth.

### 5. Provider Defaults Are Product Policy

The Runtime Host returns the default provider and execution lifetime. Policy
selects a mode first: prefer `one_shot` when any provider supports it, otherwise
fall back to `live_control`. Within providers supporting that selected mode,
prefer Codex, otherwise use stable provider order. This cannot return a default
provider/mode pair the machine does not advertise.

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
  "supports": ["codex.launch", "codex.run_once", "claude.launch"],
  "control_operations_by_provider": {
    "claude": ["launch"],
    "codex": ["launch", "run_once"]
  },
  "launch": {
    "blocked_by": null,
    "providers": [
      {
        "provider": "claude",
        "execution_lifetimes": ["live_control"]
      },
      {
        "provider": "codex",
        "execution_lifetimes": ["one_shot", "live_control"]
      }
    ],
    "default_provider": "codex",
    "default_execution_lifetime": "one_shot"
  }
}
```

An offline enrollment remains present:

```json
{
  "device_id": "cube-canary",
  "machine_name": "cube-canary",
  "control_channel_status": "disconnected",
  "last_seen_at": "2026-07-12T17:30:30Z",
  "supports": [],
  "control_operations_by_provider": {},
  "launch": {
    "blocked_by": "control_down",
    "providers": [],
    "default_provider": null,
    "default_execution_lifetime": null
  }
}
```

### Contract Rules

- A machine is ready iff `launch.providers` contains at least one provider with
  at least one supported execution lifetime now.
- `launch.blocked_by` is null when providers are present and required when they
  are empty. A redundant launch-status enum is intentionally omitted.
- `launch.providers` is derived once from
  `control_operations_by_provider`; clients do not merge raw `supports` or
  legacy convenience fields.
- `launchable_providers` means providers supporting live-control `launch`; it
  does not include run-once-only providers.
- `default_provider` must name an entry in `launch.providers`.
- `default_execution_lifetime` must be present in that provider's
  `execution_lifetimes`.
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
| `ios/Sources/LonghouseApp/LaunchSessionSheet.swift` | capability/default inference and live-control-only form | native grouped UI plus canonical mode parity |
| `scripts/ops/remote-launch-smoke.py` | Codex compatibility fallback | canonical launch-option assertion |
| `server/zerg/cli/local_health.py` and dogfood scripts | operator diagnostics over raw truth | continue using raw operations, not human projection copy |
| `web/src/pages/DevicesPage.tsx` | credential-centric management | deferred machine-management epic |

## Human UX Contract

Web and iOS should produce the same information hierarchy with native controls:

```text
Machine

Available
  ● cinder                         Ready

Unavailable
  ○ cube-canary                    Offline · last seen 2d ago
```

### Interaction

- Ready rows select the launch target.
- Unavailable rows remain visible and expose a short reason/remediation, but
  cannot submit a launch.
- When no machine is ready, keep the directory visible and lead with “No
  machines ready to launch.”
- When there are no enrollments, show the distinct install/connect empty state.
- A large unavailable section may collapse behind `Unavailable (N)`, but the
  machines remain discoverable in the same sheet.
- Selecting another ready machine clears machine-scoped workspace and provider
  state, then applies backend defaults.
- Workspace suggestions are fetched only for a ready selected machine.
- Both clients display provider names through their normal provider-label
  presentation, not raw lowercase identifiers.
- Build hashes do not appear in picker labels.

SwiftUI may use a `Menu`/custom selection sheet if native `Picker` sections
cannot express status detail cleanly. React may use a small listbox instead of
an HTML `<select>`. Pixel parity is not a goal; semantic parity is.

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
- Move default provider/lifetime policy into that projection.
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
- Retain one-shot first-message and advanced live-control behavior.
- Persist last selected ready machine per Runtime Host identity.

### D. iOS Launch Surface

- Replace the flat picker with availability-grouped native presentation.
- Consume only `machine.launch` for eligibility, provider options, and defaults.
- Use gray offline status and reserve red for repairable faults.
- Add first message plus the same runtime choice as web.
- Move iOS to canonical one-shot Console default after that form exists.
- Persist last selected ready machine per Runtime Host identity.
- Keep preview coverage for mixed ready/offline/blocked lists and large names.

### E. Diagnostics and Agent Consumers

- Keep `supports` and `control_operations_by_provider` available to
  `local-health`, provider proof, dogfood checks, and scripts.
- Migrate `remote-launch-smoke.py` away from `can_launch_codex` compatibility
  once `launch.providers` is available.
- Do not make diagnostic tools consume human status copy or icons.
- Keep shipping health, control reachability, and provider launch readiness as
  separate axes.

## Delivery Sequence

1. Backend projection, sorting, schema tests, and both route veneers.
2. Generated TypeScript/Swift DTO coverage.
3. Web grouped picker migrated to `launch`.
4. iOS grouped picker migrated to `launch` without changing its runtime mode.
5. iOS first-message/runtime parity, then canonical one-shot default.
6. Remove client-side legacy inference and compatibility helpers.
7. Stop. Machine management, producer-wide cleanup, generator expansion, and
   legacy field deletion are separate follow-ons.

Each slice should be independently shippable. Do not combine API migration,
both UI rewrites, and machine-management mutations in one change.

## Acceptance Criteria

- Web and iOS show the same enrolled machines grouped as available or
  unavailable.
- A legitimate offline machine remains visible with last-seen context.
- An ephemeral automation credential disappears after its owning workflow
  completes because it is revoked, not because a client recognizes its name.
- Both clients receive identical provider options and defaults from the
  Runtime Host.
- Neither client scans `supports`, merges provider collections, or checks
  `can_launch_codex` to decide launch eligibility.
- Both clients distinguish no enrollment from no ready machine.
- Both clients use gray for ordinary offline state and explicit text alongside
  status color.
- Console one-shot is the default on web and iOS whenever the selected machine
  advertises it; live-control is the explicit option or the honest fallback
  when no one-shot provider exists.
- `/api/agents/machines` and `/api/timeline/machines` remain response-shape
  equivalent.
- Raw machine capabilities remain available to agents and diagnostics.
- Web type checking consumes the generated OpenAPI machine schema; iOS decode
  tests pin its intentionally hand-owned DTO.

## Test Strategy

- Backend matrix: offline, connected/no-launch, live-control-only,
  run-once-only, dual-mode, multi-provider, duplicate credential, stable sort.
- Route parity: agent-auth and browser-auth machine responses share one model.
- Web: grouped rendering, status copy, unavailable interaction, backend
  defaults, last selection, execution-lifetime gating.
- iOS: DTO decoding, grouped previews, selection reset, offline explanation,
  backend defaults, one-shot/live-control payloads.
- Automation: retain the focused benchmark cleanup regression from
  `38f05cdc2`; broader producer coverage is deferred.
- Visual QA: compact and long-name machine lists in light/dark mode and normal
  accessibility text sizes.

## Non-Goals

- No shared Swift/TypeScript UI framework.
- No second machine registry or persistent last-known capability cache.
- No offline launch queue or wake-on-demand behavior.
- No provider binary installation or update management.
- No name-prefix filtering for CI/test machines.
- No broad health-dashboard redesign.
- No speculative hosted billing model in the public Runtime Host.
