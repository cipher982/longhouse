# Session Capability Consistency

Status: In progress
Owner: session product surface
Spec: `docs/specs/launch-runtime-simplification.md`
Last updated: 2026-04-03

## Goal

Keep Longhouse on one session model everywhere the user touches it:

- timeline is the home
- a session is always just a session
- Longhouse-first launch adds control capability to that same session
- capability changes state, not layout or ontology

## Done when

- Timeline, session detail, README, and launch docs all describe one session object in one timeline.
- Session capability is visible as state (`Live control`, `Reattach on host`, `Web continue`, `History only`) without implying different session classes.
- Core session surfaces stay structurally consistent across capability states.
- When Longhouse cannot drive a session, the action stays visible but disabled or explained in place instead of disappearing.
- Focused tests cover the main capability states in both timeline and session detail.

## Current status

- [x] Public copy and docs now describe one session model instead of separate session types.
- [x] Timeline cards surface capability state directly.
- [x] Session detail surfaces capability state directly.
- [x] Session detail dock stays visible for searchable-only sessions and explains why continuation is disabled.
- [x] Session detail now exposes one stable primary action row (`Continue here`) and disables it with in-place explanation when control is unavailable.
- [ ] Normalize remaining session-facing CTAs and notices so capability changes state, not structure.
- [ ] Audit timeline/detail/context surfaces for any remaining type-driven wording or hidden actions.
- [ ] Add focused regression coverage for the capability rule where it is still implicit.

## Execution order

### Phase 1: Written contract

Keep the docs and marketing surfaces aligned.

- README
- launch runtime spec
- landing spec
- public docs page

### Phase 2: Detail consistency

Make session detail obey the rule completely.

- keep the dock present
- disable or explain unsupported control paths
- avoid special-case layouts that imply a different object type

### Phase 3: Timeline consistency

Keep timeline cards and click-through affordances honest.

- capability stays visible on cards
- card structure stays fixed
- labels and affordances should not imply a separate class of session

### Phase 4: Regression coverage

Lock the rule in with tests.

- unit coverage for capability mapping
- session-detail behavior coverage
- targeted E2E for timeline/detail capability states

## Notes

- This task is about the product surface, not the transport layer.
- Coordination, messaging, and machine-surface work continue under `session-kernel-public-primitives.md`.
- Launch-story copy stays under `launch-runtime-simplification.md`.
- The immediate next slice is to audit and normalize the remaining session-facing CTAs outside the detail dock.
