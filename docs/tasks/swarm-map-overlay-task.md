# Swarm Map Overlay Task

## Goal
Build a demo-ready, fully playable iso-2D Swarm/RTS overlay that integrates with worker/task/Jarvis events, supports deterministic replays, and stays robust under real data.

## Non-Goals
- Shipping production-grade networking, matchmaking, or multi-user sync.
- Building a full RTS engine (combat/pathfinding/AI); we only need a decision-driven overlay.
- Replacing existing Jarvis or dashboard UX outside the overlay surface.

## Milestones
1. **Foundations: Data contracts + deterministic replay**
   - Success: deterministic generator yields identical frames for a given seed; core types for rooms/nodes/units/tasks/alerts defined; replay hydration loads into UI state without backend.
   - Status: Done (2026-01-26) - Added `apps/zerg/frontend-web/src/swarm/*` with types, layout transforms, replay generator, hydration, and tests.
2. **Playable map surface**
   - Success: iso-2D map renders at 60fps on desktop; pan/zoom + selection + hover tooltips work; mobile layout supports drag + pinch.
   - Status: Done (2026-01-26) - Added Swarm map overlay page, canvas renderer, pan/zoom/selection, and replay-driven playback.
3. **Decision loop UI**
   - Success: task list -> action -> visible map effect (marker/route/status change); selected entity shows actionable controls.
   - Status: Done (2026-01-26) - Added task list selection, Drop-In actions, and nudge marker/status updates.
4. **Integration hooks**
   - Success: adapter maps real worker/task events to map entities; mock and real data share the same contract; swap between live and replay via config.
   - Status: Done (2026-01-26) - Added live/replay toggle and event bus mapping for supervisor + worker events.
5. **Polish + tests**
   - Success: alerts/markers/legend are legible; unit/integration tests cover mapping + replay; E2E smoke renders map and plays one replay.
   - Status: In progress (2026-01-26) - Added legend UI, marker expiry rendering, alert/marker state tests, live mapping coverage, and an E2E smoke for map render + live toggle.

## Architecture Notes
- **Data model**: Map state normalized by `roomId`, `entityId`, `taskId`, `workerId`. Overlay-specific schema for layout (iso grid, anchor points, layers). Replay events are append-only and idempotent.
- **API**: read-only endpoints (or SSE/WS events) provide task/worker updates; adapter converts to overlay events. Mock replay feeds the same event shape.
- **UI layers**:
  1) Map canvas (iso tiles + entities + markers),
  2) Command list / decision loop panel,
  3) Drop-in details panel (entity + task + alerts).

## Mocking + Replay Strategy
- Deterministic seed drives map generation + event stream.
- Replay file format: `{seed, config, events[]}` with timestamps; hydration replays into state store.
- Fast-forward + pause + scrub supported for debugging.

## Test Plan
- **Unit**: layout transforms (grid->iso), selection hit-testing, alert classification.
- **Integration**: replay hydration produces expected state snapshots; adapter maps tasks/workers to entities.
- **E2E**: smoke test that loads overlay, starts replay, and asserts visible markers + task list entry.

## Risks / Open Questions
- Spec file `docs/specs/swarm-map-rts-ui.md` exists but is a placeholder; replace with full handoff when available.
- Performance risks on low-end GPUs; must keep draw calls minimal and avoid layout thrash.
- Event ordering: live task/worker events may arrive out of order; replay must be idempotent.
- Mobile UX: pinch-zoom + selection overlays need careful hit targets.
- Original repo is `~/git/zerg`; it may contain missing context (e.g., `.env` vars). Avoid editing that repo because main has ongoing work.

## Next Action
Decide whether to add a lightweight E2E smoke for the Swarm map overlay.
