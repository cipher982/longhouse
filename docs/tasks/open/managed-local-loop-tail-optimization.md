# Managed-Local Loop Tail Optimization

Status: In progress
Owner: Codex
Last updated: 2026-03-25

## Goal

Use the now-persisted managed-local Loop timing trail to shave the remaining hosted review tail without reopening the correctness work that is already green.

Current focus:

- reduce the remaining pre-enqueue latency from assistant-finished to `turn_loop` enqueue
- reduce the remaining claim/worker tail once the task is enqueued
- keep the work narrow and measurement-driven

## Done when

- Fresh hosted smoke runs show `review_latency_ms` reliably in low single-digit seconds.
- The dominant remaining tail is identified with real before/after numbers.
- At least one bounded backend change lands with targeted tests.
- The task notes record the latest hosted timings on `david010`.

## Checklist

- [x] Close the stale profiling task and capture a fresh prod baseline
- [ ] Run a fresh multi-turn smoke and compare first-turn vs steady-state timings
- [ ] Pick the biggest remaining latency bucket and implement one bounded optimization slice
- [ ] Add or update targeted tests around the chosen latency path
- [ ] Re-run hosted smoke and record before/after timings

## Notes

- Correctness is green; do not reopen `/sessions/{id}/chat` completion semantics unless new timings force it.
- Current `david010` baseline from session `1e2741e5-dcbb-460e-89c8-449680a65b9d`: `pre_enqueue_latency_ms=870`, `claim_latency_ms=404`, `controller_latency_ms=899`, `worker_latency_ms=917`, `review_latency_ms=2189`, `processing_latency_ms=1321`.
- Prefer one-variable-at-a-time changes. The point of this slice is to avoid another blind reliability thrash.
