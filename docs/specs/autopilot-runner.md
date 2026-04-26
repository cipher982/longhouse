# Autopilot Runner

Status: Future design

Longhouse `loop_mode=autopilot` is currently policy metadata. It must not send
messages automatically until this runner exists.

## Goal

Allow a managed Longhouse session to continue bounded, low-risk work without a
tap for every turn while preserving user trust, transcript visibility, and a
hard stop path from every control surface.

## Non-Goals

- Hidden auto-send behavior from a UI-only mode toggle.
- Automatic control of imported or unmanaged sessions.
- Cross-machine takeover. The session still runs where its execution owner
  lives.
- Replacing the transcript with summaries or semantic cards.

## Required Contracts

- **Policy**: persisted per session, including mode, turn budget, wall-clock
  limit, escalation categories, and stop/downgrade state.
- **Eligibility**: autopilot can act only when the session has live control and
  the previous turn is terminal or explicitly waiting for user input.
- **Escalation**: destructive actions, external side effects, secrets, payments,
  production deploys, ambiguous product decisions, and cost-bearing model calls
  must ask the user instead of continuing.
- **Audit**: every automatic send records who/what generated it, source event
  ids, policy version, prompt text, and result.
- **Kill switch**: web, iOS, and the machine surface can immediately downgrade
  to assist.
- **Rate limits**: generation and auto-send are limited per session and per
  owner to prevent runaway loops.

## Open Design Questions

- Whether Assist should remain client-triggered only or add server-triggered
  draft generation on `needs_user` transitions.
- Whether autopilot should use the same draft-reply prompt with stricter
  policy gates or a separate evaluator/generator pair.
- How to surface automatic-send provenance in the transcript without adding
  noisy system messages.
- How long a policy grant should last before the user must reaffirm it.
