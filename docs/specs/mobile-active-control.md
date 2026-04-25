# Mobile Active Control

Longhouse mobile should help a user steer managed sessions without pretending every imported session is controllable.

## Control States

- **Manual**: the user types a message and explicitly sends it into a managed session.
- **Draft**: Longhouse generates a suggested next user message from the latest transcript tail, fills the composer, and waits for the user to edit or send.
- **Autopilot**: Longhouse decides and sends follow-up messages without another tap, within a bounded policy. This is not active yet.

The existing `loop_mode` value is intent metadata. It must not be treated as an active autopilot controller until a server-side loop actually consumes it.

## Phase 1

- Open iOS session details at the transcript tail.
- Add `POST /api/sessions/{session_id}/draft-reply` for browser/mobile clients.
- Add `POST /api/agents/sessions/{session_id}/draft-reply` for machine/API parity.
- Gate drafting to sessions that already support live send.
- Reuse the session lock so draft generation does not race a live send.
- Return a draft only. The endpoint never sends to the provider session.
- Add an iOS composer control that requests a draft and fills the text field.

## Later

- Show the same draft affordance on web.
- Persist draft audit rows if users start relying on generated suggestions.
- Add server-side assist mode when a session enters `needs_user`.
- Add autopilot only after there is a durable policy, bounded turn budget, and visible kill switch.
