# Managed Turn Observability MVP

Status: Draft
Last updated: 2026-04-23

## Goal

Add a small, durable observability layer for managed sessions so Longhouse can answer:

- where a managed turn spent time inside Longhouse
- whether `/api/agents/ingest` is getting slower as sessions grow
- whether the slow part is backend overhead vs provider/session behavior

This builds on [session-timing-model.md](./session-timing-model.md). That doc defines lifecycle truth. This doc defines the first tracing contract on top of it.

## Scope

This MVP covers the runtime-host server only:

- managed-local turn dispatch on the fast-ack path
- managed-local active/terminal watcher tasks
- `/api/agents/ingest`

This MVP does not yet cover:

- engine OTLP export
- browser/UI timing
- prompt/response payload capture
- blanket auto-instrumentation of every HTTP route

## Export Contract

- Use OpenTelemetry manual spans only.
- OTLP export is opt-in.
- Export activates only when `OTEL_EXPORTER_OTLP_ENDPOINT` or `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` is set.
- If no OTLP endpoint env var is set, Longhouse still creates spans in-process but installs no exporter and emits no retry traffic.

## Resource Attributes

All server spans should include these resource attrs:

- `service.name=longhouse-runtime`
- `service.version=<release version>`
- `service.instance.id=<hostname>`
- `deployment.environment.name=<settings.environment or app_mode>`
- `longhouse.app_mode=<dev|demo|production>`
- `longhouse.build.channel=<dev|release>`
- `longhouse.build.commit=<short sha>`
- `longhouse.build.dirty=<bool>`
- `longhouse.build.qualified_version=<display build identity>`

## Span Taxonomy

Root spans:

- `longhouse.turn`
- `longhouse.ingest`

Managed-turn child spans:

- `longhouse.turn.baseline`
- `longhouse.turn.persist_create`
- `longhouse.turn.provider_dispatch`
- `longhouse.turn.persist_send_result`
- `longhouse.turn.wait_active`
- `longhouse.turn.persist_active`
- `longhouse.turn.wait_terminal`
- `longhouse.turn.persist_terminal`
- `longhouse.turn.lock_release`

Ingest child spans:

- `longhouse.ingest.decode`
- `longhouse.ingest.validate`
- `longhouse.ingest.write`

## Custom Attributes

Use `longhouse.*` attrs for product-specific detail:

- `longhouse.provider`
- `longhouse.managed`
- `longhouse.session.id`
- `longhouse.turn.request_id`
- `longhouse.turn.control_path`
- `longhouse.turn.outcome`
- `longhouse.turn.error_code`
- `longhouse.turn.baseline_event_id`
- `longhouse.turn.baseline_observation_id`
- `longhouse.turn.user_submitted_at`
- `longhouse.turn.send_observed_at`
- `longhouse.turn.active_phase_observed_at`
- `longhouse.turn.terminal_at`
- `longhouse.turn.phase_ms.*`
- `longhouse.ingest.auth_kind`
- `longhouse.ingest.content_encoding`
- `longhouse.ingest.body_bytes_wire`
- `longhouse.ingest.body_bytes_decoded`
- `longhouse.ingest.event_count`
- `longhouse.ingest.events_inserted`
- `longhouse.ingest.events_skipped`
- `longhouse.ingest.session_created`

## Privacy Rules

Do not export:

- message text
- assistant text
- tool input/output bodies
- raw transcript payloads

Export only IDs, booleans, counts, byte sizes, phases, and durations.

## Questions This MVP Answers

- Is managed turn latency dominated by Longhouse persistence/coordination before send acceptance?
- Are active or terminal watcher waits timing out disproportionately?
- Is `/api/agents/ingest` decode or write time growing with larger payloads?

## Next Steps

Once this slice is live and useful:

1. Add engine-side spans for detect-delta, prepare-ship, and export.
2. Propagate trace context from managed turn dispatch into engine ship/ingest.
3. Add dashboards for `p50/p95` turn send-accept time, watcher timeout rate, ingest decode time, and ingest write time.
