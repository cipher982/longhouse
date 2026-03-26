# Timeline Realtime Desktop Control View

Status: Done
Spec: `docs/specs/timeline-realtime-action-center.md`
Last updated: 2026-03-25

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
- [x] Phase 2: add execution-home metadata to session APIs and Timeline rows
- [x] Phase 3: treat managed-local as a stronger runtime class than transcript-only legacy sessions
- [x] Collapse the main Timeline off the secondary `/sessions/active` overlay path
- [x] Reduce ad hoc runtime fallback paths on the Timeline read path
- [x] Replace the backend 1-second full-list SSE polling loop with a cheaper change detector
- [x] Verify the timeline manually with multiple concurrent sessions and long-running silent turns

## Notes

- Product direction is one Timeline page, not a separate desktop live destination.
- Keep list ordering stable. Fast runtime updates should update card chrome, not cause constant reordering.
- The current SSE protocol should stay row-level (`session_upsert` / `session_remove`) unless simpler backend optimizations prove insufficient.
- `/sessions/active` is migration residue for the main Timeline flow, not the target architecture.
- Timeline is a desktop runtime/control view. Loop or follow-up cards can own mobile actions and continuation semantics.
- `needs_user` is a runtime phase, not a promise about which actions are or are not available.
- `managed_local` is the golden path for exact/control-capable local sessions.
- Unmanaged local should remain honest fallback observability, not the long-term exact/actionable path.
- The shared `execution_home` contract now matches the managed-local branch enum and derives from existing session metadata when the dedicated session columns are not present yet.
- `/sessions/active` overlay is fully removed from the timeline router (agents router machine API remains).
- Legacy `web/src/legacy/forum/` (ForumCanvas, ForumPage, 2.3k LOC) deleted — no remaining consumers.
- The only non-thread path on the timeline read surface is query/hybrid search, which returns raw sessions that the frontend reshapes via `buildCompatibilityTimelineCards()`. This is intentional until thread-aware search ranking is built.
- `build_fallback_runtime_view()` is the structural fallback for sessions without materialized `SessionRuntimeState` rows — it tags confidence explicitly ("live", "inferred", "ended"), not an ad hoc path.
- Phase 3 (managed runtime class) is satisfied by the existing confidence/phase_source system: `semantic` phase_source gets `live` confidence → rich display ("Running bash", "Needs you"); `progress` phase_source gets `inferred` confidence → weak display ("Recent progress"). Verified on david010 with real managed-local and transcript-only sessions.
