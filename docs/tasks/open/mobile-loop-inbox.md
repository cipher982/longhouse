# Mobile Loop Inbox

Status: In progress
Spec: `docs/specs/mobile-loop-inbox.md`
Last updated: 2026-03-19

## Goal

Ship a tiny phone-first Loop Inbox so away-from-keyboard session follow-up does not require the desktop UI, VNC, or terminal text entry.

## Done when

- There is a dedicated thin backend contract for mobile loop inbox + action card data.
- Same-session follow-up actions can be triggered without the desktop workspace UI.
- A separate lightweight mobile shell can consume that contract cleanly.

## Checklist

- [x] Write the product spec and rollout plan
- [x] Add a dedicated loop inbox read API for sessions needing attention
- [x] Add a dedicated action-card read API for one session
- [x] Add focused tests for inbox filtering, ordering, and payload shape
- [x] Add bounded action endpoints for the common mobile cases
- [x] Build the first tiny mobile shell for inbox + action card
- [x] Add notification delivery for attention-worthy finished turns
  Initial slice: Telegram deep link into `/loop/:sessionId`
- [ ] Dogfood the traveling / away-from-keyboard flow end to end

## Notes

- Keep the phone surface derived and action-oriented.
- Do not turn the mobile work into “responsive desktop Longhouse.”
- Buttons first, freeform input second.
- Automated guardrail now covers: completed turn -> loop inbox item -> approve action -> same-session resume job.
