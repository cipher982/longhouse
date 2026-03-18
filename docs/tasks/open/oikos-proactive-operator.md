# Proactive Oikos Operator Mode

Status: In progress
Spec: `docs/specs/oikos-proactive-operator.md`
Last updated: 2026-03-17

## Goal

Make Oikos feel like a bounded technical deputy that can notice meaningful changes, inspect them, and either act or escalate without turning into a sprawling automation engine.

## Done when

- The product principles are locked and reflected in the runtime.
- Wakeups and policy state stay thin and Oikos-owned.
- One bounded autonomy slice can inspect a session, continue it, or escalate back to the user.

## Checklist

- [x] Write a principles-first spec and rollout plan
- [x] Dogfood the tiny wakeup set around coding-session transitions plus a periodic sweep fallback
- [x] Add the thinnest possible Oikos-owned trigger history / policy state
- [ ] Ship one bounded autonomy slice that exercises inspect / continue / escalate behavior

## Notes

- Keep this deputy-like, not scheduler-like.
- The next important seam is a reusable `invoke_oikos()` transport boundary so proactive wakeups do not hardcode the web surface.
