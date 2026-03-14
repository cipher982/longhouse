# Run Stream Refactor

Date: 2026-03-14

## Goal

Refactor `apps/zerg/backend/zerg/routers/stream.py` so the router becomes thin HTTP glue and the replay/live stream lifecycle logic lives in one service module with focused tests.

This is a cleanup refactor, not a behavior change.

## Current Problem

`_replay_and_stream()` currently owns too many responsibilities at once:

1. Historical replay loading
2. Test-only `test_commis` DB routing
3. Continuation run lookup and aliasing
4. Event-bus subscription and queue overflow handling
5. Stream lifecycle bookkeeping
6. SSE payload encoding

That logic is service-grade code living in a router module, and it has almost no direct characterization tests today.

## Constraints

- Preserve current stream behavior for replay, live events, continuation aliasing, `stream_control`, and overflow.
- Keep test-only `test_commis` routing intact during the refactor.
- Do not change producer behavior in `oikos_service`, `commis_inbox_trigger`, or lifecycle emitters as part of this refactor.
- Avoid a large manager/framework abstraction. One service module is enough.

## Target Shape

Create one module:

- `apps/zerg/backend/zerg/services/run_stream.py`

Move these concerns into it:

- `HistoricalRunEvent`
- `with_test_commis_routing(...)`
- `load_historical_run_events(...)`
- `StreamLifecycleState`
- `ContinuationAliasResolver`
- `filter_stream_event(...)`
- `RunEventSubscription`
- `encode_*_sse(...)`
- `stream_run_events(...)`

Keep `apps/zerg/backend/zerg/routers/stream.py` responsible for:

- ownership lookup
- `Last-Event-ID` parsing
- `EventSourceResponse(...)`
- compatibility wrapper exports used by other routers

## Implementation Order

### Slice 1: Characterization Tests

Add `apps/zerg/backend/tests_lite/test_run_stream_service.py` to pin the current behavior through the existing public wrappers.

Initial tests:

- `test_stream_run_events_live_emits_connected_then_replay`
- `test_stream_run_events_replays_before_live_and_skips_duplicate_live_event_ids`
- `test_stream_run_events_closes_after_replay_close_marker`
- `test_stream_run_replay_last_event_id_header_overrides_query_param`

### Slice 2: Replay Loader + Test Routing

Extract the existing `_load_historical_events()` and repeated test-routing context handling into the service module.

Scope:

- `HistoricalRunEvent`
- `with_test_commis_routing(...)`
- `load_historical_run_events(...)`

Tests:

- `test_load_historical_run_events_uses_test_commis_id_context`
- `test_stream_run_events_live_defaults_to_context_test_commis_id`
- `test_load_historical_run_events_returns_serializable_records_after_session_closes`

### Slice 3: Lifecycle State Machine

Extract the mutable lifecycle bookkeeping from `_replay_and_stream()` into `StreamLifecycleState`.

State to move:

- `pending_commiss`
- `oikos_done`
- `saw_oikos_complete`
- `continuation_active`
- `awaiting_continuation_until`
- `close_event_id`
- `stream_lease_until`

Core methods:

- `apply(event_type, event, *, from_replay, now_monotonic)`
- `should_close_after_replay(last_sent_event_id, status)`
- `next_timeout(now_monotonic)`
- `should_close_on_timeout(now_monotonic)`
- `should_close_after_live_event(event_id, now_monotonic)`

Tests:

- `test_lifecycle_keep_open_extends_lease_only_for_live_events`
- `test_lifecycle_close_waits_until_close_marker_is_streamed`
- `test_lifecycle_starts_grace_window_after_oikos_complete_and_last_commis`
- `test_lifecycle_oikos_deferred_respects_close_stream_flag`

### Slice 4: Continuation Filtering + Aliasing

Move event filtering and continuation aliasing out of the inline event handler.

Scope:

- `ContinuationAliasResolver`
- `filter_stream_event(...)`

Tests:

- `test_filter_stream_event_drops_wrong_owner`
- `test_filter_stream_event_drops_tool_event_missing_run_id`
- `test_filter_stream_event_aliases_direct_continuation_run_id_to_root`
- `test_filter_stream_event_aliases_chained_continuation_via_root_run_id`
- `test_filter_stream_event_fails_closed_when_continuation_lookup_errors`
- `test_continuation_alias_resolver_uses_test_commis_id_context`

### Slice 5: Subscription + Overflow

Move event subscription and bounded-queue overflow handling into a small helper/context manager.

Scope:

- `STREAM_EVENT_TYPES`
- `TOOL_EVENTS_REQUIRING_RUN_ID`
- `RunEventSubscription`

Tests:

- `test_run_event_subscription_subscribes_and_unsubscribes_all_stream_event_types`
- `test_run_event_subscription_sets_overflow_when_queue_is_full`
- `test_run_event_subscription_drops_new_events_after_overflow`
- `test_stream_run_events_emits_overflow_and_returns`

### Slice 6: Orchestration + Router Thinning

Move SSE encoding and the combined replay/live orchestration into `stream_run_events(...)`.

Scope:

- `encode_connected_sse(...)`
- `encode_replay_sse(...)`
- `encode_live_sse(...)`
- `encode_heartbeat_sse(...)`
- `encode_overflow_sse(...)`
- `stream_run_events(...)`

Router end state:

- thin wrappers only
- existing imports continue working
- no large mutable closure remains in the router

Final tests:

- `test_stream_run_replay_returns_404_for_unowned_run`
- `test_stream_run_replay_invalid_last_event_id_falls_back_to_query_param`
- `test_stream_run_replay_does_not_alias_continuations_by_default`
- `test_stream_run_events_live_does_alias_continuations`

## Done Conditions

- `stream.py` no longer contains the large lifecycle state closure
- replay/live lifecycle behavior is covered by focused tests
- continuation aliasing and overflow behavior are explicitly tested
- router remains behavior-compatible for existing callers
- `make test` passes after the final slice
