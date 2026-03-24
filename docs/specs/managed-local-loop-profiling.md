# Managed-Local Loop Profiling

Status: in progress
Last updated: 2026-03-23

## Goal

Make the managed-local Loop hot path measurable from one durable product artifact:

- local assistant turn finishes
- Longhouse ingests the tail
- `turn_loop` runs
- a review/card exists

The product already proved that `/loop` SSE is fast once a card exists. The remaining problem is backend latency visibility and dogfood confidence.

## Current State

Longhouse already records the decision artifact that matters:

- `session_turn_reviews`

That row is the thing the product renders in Session Detail and Loop Inbox. Today it tells us what Longhouse decided, but not how long the hot path took to get there.

The queue already has partial timing truth:

- assistant-turn source timestamp exists in transcript events
- `session_tasks.created_at` tells us when `turn_loop` was enqueued
- `session_turn_reviews.created_at` tells us when the card/review was recorded

What is missing is a review-local latency trail that survives later queue churn and can be inspected without manual DB archaeology.

## Decision

Persist the latency trail on `session_turn_reviews`, not in a separate tracing system.

Rationale:

- the review row is already the stable product artifact
- the hot path is narrow and deterministic enough that we do not need a generic telemetry framework
- dogfooding should work from the existing Session Detail / Loop surfaces and API

## Review Timing Model

Store these timestamps on each review:

- `assistant_turn_finished_at`
  - source of truth: timestamp on the completed assistant event extracted from the transcript
- `turn_loop_enqueued_at`
  - source of truth: the `SessionTask.created_at` that triggered the successful `turn_loop` processing attempt
- `turn_loop_completed_at`
  - source of truth: the time `maybe_process_session_turn_loop()` finishes the record/execute/enqueue path for that review
- `created_at`
  - existing field; remains the review/card creation timestamp

Expose these derived metrics in the API:

- `queue_latency_ms`
  - `turn_loop_enqueued_at - assistant_turn_finished_at`
- `review_latency_ms`
  - `created_at - assistant_turn_finished_at`
- `processing_latency_ms`
  - `turn_loop_completed_at - turn_loop_enqueued_at`

## Why This Slice

This is enough to answer the current product question:

- did the local ship/ingest path lag before `turn_loop` even started?
- did the worker/controller path lag after enqueue?
- how long until a Loop card became real?

## Explicitly Deferred

Do not expand this slice into:

- a generic distributed tracing system
- `ui_first_seen_at`
- per-SSE delivery instrumentation
- per-notification-channel timing (`push_sent_at`, Telegram timing)
- cross-table trace IDs for every turn review

Those can come later if the persisted review trail shows they are actually needed.

## Success Criteria

- New reviews persist the managed-local latency trail.
- `/api/oikos/turn-reviews` exposes the timestamps plus derived latency numbers.
- Session Detail shows the latency breakdown for dogfooding/debugging.
- Tests cover the persistence and API contract.
- A real managed-local smoke run produces post-fix timing numbers without manual DB spelunking.
