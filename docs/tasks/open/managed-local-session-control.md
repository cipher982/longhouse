# Managed Local Session Control

Status: In progress
Spec: `docs/specs/managed-local-session-control.md`
Last updated: 2026-03-21

## Goal

Ship the first trustworthy away-from-keyboard product for laptop-first Claude usage:

- Longhouse-managed local sessions run on the user's Mac
- phone can continue or reply into that exact session
- no implicit cloud takeover

## Done when

- Managed local sessions have explicit execution-home metadata.
- Longhouse can launch a managed local Claude session under tmux on a runner it controls.
- `/sessions/{id}/chat` can send text into that exact managed local session.
- Loop cards for managed local sessions show `On this Mac`.
- Loop offers `Continue`, `Reply`, and `Not now` for managed local cards.
- No managed-local action silently changes execution venue.

## Checklist

- [x] Create persistent managed-local spec with decisions and phased plan
- [x] Add managed-local execution-home + tmux metadata to sessions
- [x] Add managed-local tmux command builder/service with focused tests
- [x] Expose execution-home metadata in session APIs
- [x] Add managed-local launcher on a connected runner
- [x] Route `/sessions/{id}/chat` to tmux-backed local sessions
- [ ] Add Loop `Reply` as a first-class action for managed-local cards
- [ ] Show `On this Mac` clearly in Loop/session surfaces
- [ ] Dogfood a real managed local session from laptop + phone

## Notes

- V1 intentionally excludes local-to-cloud switching.
- V1 intentionally excludes attaching to arbitrary already-open naked local Claude sessions.
- Reuse the existing session chat route and streaming model where possible.
- Prefer tmux-backed launch over a custom terminal wrapper in Phase 1/2.
