# Realtime Propagation Observability

## Purpose

Longhouse only earns trust when an already-open web or iOS session feels like
the terminal: local changes appear quickly, stale states are explained, and a
slow update can be traced to the exact layer that held it.

This spec defines the product-facing telemetry path for one question:

> A provider event changed locally. How long did it take to become visible to a
> user, and which layer owned the delay?

## Product Contract

Realtime propagation and durable archive are related but separate lanes.

- **Realtime lane:** managed session truth should reach warm clients with p95
  below 500ms on nominal network, and alarm above 1000ms.
- **Durable lane:** canonical transcript/archive rows should be correct and
  searchable, but they should not be the only mechanism that lets warm clients
  show current truth.
- **Forensics lane:** any specific slow update should produce a per-session
  waterfall without reading engine logs by hand.

## Stage Vocabulary

Use these names consistently in endpoints, dashboards, logs, and future OTel
exports.

```text
provider_event
  -> engine_observed
  -> engine_enqueued
  -> engine_job_started
  -> engine_http_send_started
  -> server_handler_entered
  -> server_store_returned
  -> server_fanout
  -> client_received
  -> client_rendered
```

The first endpoint ships with existing durable/event evidence plus lightweight
fanout and client-receive stamps. Older sessions may still miss
`server_fanout` and `client_received` because those probes were not persisted
before this slice.

## Existing Evidence

The initial read-only report stitches these sources:

- `events`: durable transcript projection rows with provider timestamp,
  source path, source offset, role, and event id.
- `session_observations(kind=provider_event)`: transcript observation rows with
  provider timestamp and Runtime Host receive time.
- `session_observations(source=agents_ingest_trace)`: persisted engine
  `ship_pipeline_trace` plus server ingest trace.
- `session_observations(kind=server_fanout)`: server pubsub fanout timestamp
  and per-topic sequence metadata.
- `session_observations(kind=client_render)`: web/iOS render beacons.

Client render beacons may describe the projection event that caused a refresh
instead of the exact transcript item that became visible. For session-detail
rendering, match by either `event:{event.id}` or WebKit `latest_item_id`
(`user:{id}`, `assistant:{id}`, and so on).

## Initial Endpoint

`GET /api/observability/sessions/{session_id}/latency`

Query parameters:

- `event_limit`: recent durable transcript events to inspect. Default 20, max
  100.
- `surface`: optional client surface filter such as `web` or `ios`.

Response shape:

- session metadata
- recent events with a latency waterfall
- provider observation reference when present
- ship trace reference when present
- first matching client render for the requested `surface`, or first across all
  surfaces when `surface` is omitted
- measured stages with source and confidence
- `measured_total_ms` and `unaccounted_ms` so clock skew or missing probes are
  visible instead of hidden inside the waterfall
- `client_clock_skew_ms` when a render beacon supplied client clock correction
- largest measured segment as the current bottleneck
- explicit gaps for missing probes
- `known_unimplemented_probes` for system-wide missing probes that should not
  be confused with per-event data loss

The endpoint must not include transcript text. It may expose event ids, roles,
timestamps, source offsets, byte counts, and timing metadata.

Any stage that spans clocks from different machines/processes must be marked
`derived`. That includes provider-to-engine and engine HTTP send-to-server
handler. These numbers are still useful for locating a stall, but they are not
NTP-proof wall-clock truth.

## Known Gaps

- Engine scheduler stalls are visible as `enqueue_to_job_ms` or
  `local_join_delay_ms`, but the current persisted trace does not explain what
  blocked the local runtime.
- Clients only persist `client_received_at` when they also send a render beacon.
  A dedicated first-receive beacon is still needed for failed/never-rendered
  refreshes.
- Omitting `surface` intentionally collapses web and iOS renders; product
  debugging should pass `surface=ios` or `surface=web`.

## Build Order

1. Ship the read-only per-session latency endpoint from existing data.
2. Add engine scheduler stall telemetry: ready queue depth, in-flight counts,
   local event-loop stall samples, and join-delay reason labels.
3. Emit dedicated first-client-receive telemetry for cases where render never
   completes; the current slice piggybacks receive stamps on render beacons.
4. Build a realtime dashboard over the same report model: p50/p95/p99 by
   provider, surface, device, and culprit segment.
