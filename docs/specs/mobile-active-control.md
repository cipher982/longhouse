# Mobile Active Control

Longhouse mobile should feel like a pocket cockpit for real agent sessions:
the user can inspect the full transcript, understand the current decision
point, accept or edit a suggested reply, and set the desired autonomy policy
for the session.

## Product Decisions

- **The full transcript remains canonical.** Mobile should not replace chat with
  a semantic inbox. The user needs the raw session log to trust a suggestion,
  debug the agent, and scroll back for context.
- **The semantic layer sits above the transcript.** A compact session cockpit
  may summarize current state and expose controls, but it must never hide the
  transcript tail or make the transcript feel secondary.
- **Managed is the expected default for Longhouse-launched sessions.** For a
  properly installed user launching through `longhouse claude`, `longhouse
  codex`, or equivalent wrappers, sessions should normally be steerable.
  Imported history and bare CLI runs remain read-only. Primary UX should treat
  live control as the normal case and reserve `unmanaged`, `read-only`, and
  `control offline` copy for degraded/imported sessions.
- **Autonomy is a policy, not a mystery mode.** `assist` and `autopilot` must be
  visible session policies with clear boundaries. They do not make sense unless
  the user can still read the transcript, edit generated text, and stop or
  downgrade the policy.

## Control States

- **Assist**: the default mode. In the current product, Longhouse drafts a
  suggested next user message from the latest transcript tail for review. The
  user edits or sends it.
- **Autopilot**: Longhouse may send bounded follow-up messages without another
  tap, only after a server-side policy runner exists with durable limits,
  escalation rules, and a visible kill switch.

Legacy `manual` rows are normalized to `assist` on read. `loop_mode` must not be
treated as an active autopilot controller until a server-side loop actually consumes it.

## Current State

Completed:

- iOS session details open at the transcript tail.
- iOS session detail polls while visible so the in-app transcript stays
  near-realtime.
- `POST /api/sessions/{session_id}/draft-reply` exists for browser/mobile
  clients.
- `POST /api/agents/sessions/{session_id}/draft-reply` exists for machine/API
  parity.
- Drafting is gated to sessions that support live send.
- Drafting is read-only; it does not take the live send lock or block the user
  from pre-staging a reply while the agent is working.
- The endpoint returns a draft only. It never sends to the provider session.
- iOS has a composer-level draft control.

## Target Shape

Session detail should be structured as:

```text
Session detail
  Session cockpit
    Current state
    Control health, only when degraded
    Assist / Autopilot policy control
    Suggested reply action
  Full transcript tail
    Complete chat/tool context
    Scrollback remains available
  Composer
    Draft prefill
    Explicit send
```

## Phase 1: iOS Session Cockpit

- Add a compact cockpit panel above the transcript.
- Show the current phase (`Needs you`, `Working`, `Blocked`, `Idle`) from the
  same phase/source fields already used by session detail. Do not add a new
  summarization dependency in this phase.
- Show the best available session title/project metadata.
- Show degraded control state only when control is unavailable or read-only.
- Make `Draft reply` a clear text action in the cockpit while retaining the
  composer icon shortcut.
- Decode and display `loop_mode` on iOS session detail.
- Let iOS update `loop_mode` through `/api/timeline/sessions/{id}/loop-mode`.
- Keep `Assist` and `Autopilot` honest:
  - `Assist` means client-triggered drafts in this phase.
  - `Autopilot` is displayed as a preview/policy-only mode until a runner
    exists.
- Keep visible polling while the detail screen is open; this is the current
  realtime-ish strategy for cockpit phase updates.

Acceptance:

- Opening a managed session shows the cockpit, full transcript tail, and
  composer.
- Opening an unmanaged/imported session shows the transcript and a read-only
  degraded control state.
- Tapping `Draft reply` fills the composer without sending.
- Changing Assist/Autopilot updates the server and refreshes local state.
- Draft generation failure (`401`, `409`, `502`, `503`) produces clear copy and
  keeps the transcript/composer intact.
- Capability downgrades while the screen is visible update the cockpit on the
  next poll.
- iOS tests or build validation cover the new model/API shape.

## Phase 2: Web Draft Parity And Copy Alignment

- Add the same draft-reply affordance to the web live-send dock for managed
  sessions.
- Do not overwrite typed text.
- Disable or hide draft generation when the composer already contains text.
- Keep lock semantics aligned with iOS: draft generation does not take the
  live-send lock.
- Reuse the existing draft endpoint rather than adding a browser-only route.
- Keep copy honest: `Autopilot` is policy-only/preview until a runner exists.

Acceptance:

- Managed web sessions can prefill a suggested reply.
- Existing manual send behavior is unchanged.
- Web tests cover the empty-composer and non-overwrite paths.

## Phase 3: Autopilot Display Foundation

Autopilot should not become active in this phase unless the server-side runner
is implemented. The foundation can safely include:

- Policy display on iOS and web.
- Clear downgrade/stop affordance.
- Server-side tests proving `loop_mode` is persisted and returned on session
  detail.

Non-goal for this phase:

- A hidden loop that sends messages automatically without a durable policy
  runner and visible stop control.

## Future: Real Autopilot Runner

The real runner is a separate project, not a UI phase. It needs its own design
and acceptance criteria before implementation:

- turn budget
- max wall-clock duration
- escalation on destructive, external, cost-bearing, or ambiguous actions
- durable audit rows for every automatic send
- kill switch that works from web, iOS, and the machine surface
- rate limits and cooldowns for draft/auto-send generation
- telemetry for draft tap -> send conversion and autopilot stop/downgrade
  behavior
