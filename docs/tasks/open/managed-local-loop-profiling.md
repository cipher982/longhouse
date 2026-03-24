# Managed-Local Loop Profiling

Status: In progress
Spec: `docs/specs/managed-local-loop-profiling.md`
Last updated: 2026-03-23

## Goal

Make the managed-local Loop hot path measurable enough to answer one question with live evidence:

- how long from completed local assistant turn to Loop review/card?

## Done when

- Turn reviews persist assistant-finished, turn-loop-enqueued, and turn-loop-completed timestamps.
- The Oikos turn-review API exposes those timestamps and a small latency breakdown.
- Session Detail renders the latency breakdown for the latest review.
- Targeted backend/frontend tests cover the new contract.
- A fresh managed-local smoke run produces real post-fix latency numbers.

## Checklist

- [x] Write the focused profiling spec/task slice
- [ ] Persist review-local latency timestamps on the backend
- [ ] Expose derived latency metrics via `/api/oikos/turn-reviews`
- [ ] Surface the latest latency breakdown in Session Detail
- [ ] Run targeted tests
- [ ] Run a fresh managed-local smoke/profile pass and record the measured timings

## Notes

- Keep this narrow. The current question is latency visibility, not architecture.
- `session_turn_reviews.created_at` already serves as review/card creation time; do not duplicate it unless the data model forces it.
- Defer notification/UI-observed timing until the persisted review trail proves the backend hot path is no longer the bottleneck.
