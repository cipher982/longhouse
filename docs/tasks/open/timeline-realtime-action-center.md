# Timeline Realtime Desktop Control View

Status: In progress
Spec: `docs/specs/timeline-realtime-action-center.md`
Last updated: 2026-03-21

## Goal

Make Timeline the primary desktop runtime/control view for agent sessions. Keep one page and one scrolling list, but make each timeline card trustworthy about what is happening right now so users can stop juggling multiple terminal tabs for local Claude and Codex work.

## Done when

- Inferred unmanaged rows no longer render as plain `Active`.
- Timeline rows expose a canonical execution-home field without taking over action semantics.
- Managed-local sessions can surface a stronger runtime class than transcript-only legacy sessions.
- The main Timeline cards take their runtime truth from `/timeline/sessions` rows, not an optional `/sessions/active` overlay merge.
- Materialized runtime state is the normal Timeline truth path and remaining ad hoc fallbacks are clearly migration-only.
- Timeline keeps the current row-level SSE model, but the backend no longer polls the full filtered list every second per client.
- Backend and frontend tests cover the Timeline row runtime contract and stream behavior.
- Timeline surfaces runtime truth without defining continuation transport or follow-up execution semantics.

## Checklist

- [x] Write and review the realtime timeline spec
- [x] Fix main timeline selection/order/pagination to use a recent-activity anchor instead of raw `started_at`
- [x] Add runtime overlay fields to the main timeline query/response
- [x] Fold live state into the existing Timeline cards and retire the separate live panel from the main flow
- [x] Define the post-slice-1 liveness plan for local Claude/Codex sessions
- [x] Add targeted backend and frontend tests
- [x] Ship materialized runtime state and runtime events for Timeline
- [x] Ship Timeline SSE row updates with a slow reconciliation poll
- [x] Refresh the spec/task docs to match shipped reality
- [x] Phase 1: weaken inferred user-facing labels and keep confidence explicit
- [ ] Phase 2: add execution-home metadata to session APIs and Timeline rows
- [ ] Phase 3: treat managed-local as a stronger runtime class than transcript-only legacy sessions
- [ ] Collapse the main Timeline off the secondary `/sessions/active` overlay path
- [ ] Reduce ad hoc runtime fallback paths on the Timeline read path
- [ ] Replace the backend 1-second full-list SSE polling loop with a cheaper change detector
- [ ] Verify the timeline manually with multiple concurrent sessions and long-running silent turns

## Notes

- Product direction is one Timeline page, not a separate desktop live destination.
- Keep list ordering stable. Fast runtime updates should update card chrome, not cause constant reordering.
- The current SSE protocol should stay row-level (`session_upsert` / `session_remove`) unless simpler backend optimizations prove insufficient.
- `/sessions/active` is migration residue for the main Timeline flow, not the target architecture.
- Timeline is a desktop runtime/control view. Loop or follow-up cards can own mobile actions and continuation semantics.
- `needs_user` is a runtime phase, not a promise about which actions are or are not available.
- `managed_local` is the golden path for exact/control-capable local sessions.
- Unmanaged local should remain honest fallback observability, not the long-term exact/actionable path.
