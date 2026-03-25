# Managed-Local Loop Profiling

Status: Complete
Owner: Codex
Spec: `docs/specs/managed-local-loop-profiling.md`
Last updated: 2026-03-25

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
- [x] Re-stabilize hosted managed-local continuation and live QA so the latency pass has a trustworthy prod baseline
- [x] Persist review-local latency timestamps on the backend
- [x] Expose derived latency metrics via `/api/oikos/turn-reviews`
- [x] Surface the latest latency breakdown in Session Detail
- [x] Run targeted tests
- [x] Run a fresh managed-local smoke/profile pass and record the measured timings

## Notes

- Keep this narrow. The current question is latency visibility, not architecture.
- `session_turn_reviews.created_at` already serves as review/card creation time; do not duplicate it unless the data model forces it.
- Defer notification/UI-observed timing until the persisted review trail proves the backend hot path is no longer the bottleneck.
- Managed-local continuation correctness and hosted live QA are green again. The current Codex lane is latency decomposition and visibility, not more `/sessions/{id}/chat` correctness churn.
- Verified on `david010` with session `1e2741e5-dcbb-460e-89c8-449680a65b9d`: `pre_enqueue_latency_ms=870`, `claim_latency_ms=404`, `controller_latency_ms=899`, `worker_latency_ms=917`, `review_latency_ms=2189`, `processing_latency_ms=1321`.
- Local verification on 2026-03-25: `make test` (`1161 passed`) and `MINIMAL=1 make test-frontend-unit` (`276 passed`, `4 skipped`).
