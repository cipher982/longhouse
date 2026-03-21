# Timeline Realtime Desktop Control View

Status: In progress
Spec: `docs/specs/timeline-realtime-action-center.md`
Last updated: 2026-03-21

## Goal

Make Timeline the primary desktop runtime/control view for agent sessions. Keep one page and one scrolling list, but make each timeline card trustworthy about what is happening right now so users can stop juggling multiple terminal tabs for local Claude and Codex work.

## Done when

- The existing Timeline cards show live runtime state directly on the main page without a separate live subpanel.
- The main session list can surface older-but-active sessions based on real activity, not just `started_at`.
- The first implementation slice works with current Claude presence and transcript-derived fallbacks without making the list jitter.
- The phase-2 runtime architecture is specified with exact storage, reducer, collector, and API contracts.
- The follow-on plan for process/PID liveness and richer Codex runtime signals is documented and staged.
- Backend and frontend tests cover the new row-overlay contract and the main regressions called out in the spec.
- Timeline surfaces runtime truth without defining continuation transport or follow-up execution semantics.

## Checklist

- [x] Write and review the realtime timeline spec
- [x] Fix main timeline selection/order/pagination to use a recent-activity anchor instead of raw `started_at`
- [x] Add runtime overlay fields to the main timeline query/response
- [x] Fold live state into the existing Timeline cards and retire the separate live panel from the main flow
- [x] Define the post-slice-1 liveness plan for local Claude/Codex sessions
- [x] Add targeted backend and frontend tests
- [x] Write the concrete phase-2 runtime architecture spec
- [ ] Implement `session_runtime_state` and `session_runtime_events`
- [ ] Add runtime event ingest endpoint and reducer service
- [ ] Mirror Claude hook signals into runtime events
- [ ] Emit transcript progress and binding signals into runtime state
- [ ] Add timeline SSE patch stream and client integration
- [ ] Add managed Codex runtime adapter
- [ ] Verify the timeline manually with multiple concurrent sessions and long-running silent turns

## Notes

- Product direction is one Timeline page, not a separate desktop live destination.
- Avoid introducing new user-facing bucket concepts in the first slice; the main value is “what is happening now” on the existing recency-driven list.
- Keep list ordering stable. Fast runtime updates should update card chrome, not cause constant reordering.
- Slice 1 does not depend on PID/process supervision.
- Phase 2 should keep the UI contract simple: one row, one runtime overlay, one SSE patch stream.
- Timeline is a desktop runtime/control view. Loop or follow-up cards can own mobile actions and continuation semantics.
- `needs_user` is a runtime phase, not a promise about which actions are or are not available.
