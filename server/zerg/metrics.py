"""Prometheus metrics for trigger subsystem and Gmail integration.

The module bundles all counters in one place so importing side-effects
(metric registration) happen exactly once per process.  Routers and
services can simply ``from zerg.metrics import …`` and increment.
"""

from __future__ import annotations

# ``prometheus_client`` is an optional dependency pulled in via
# ``backend/pyproject.toml``.  Import lazily so unit-tests that filter
# deps via *–no-deps* still succeed.


try:
    from prometheus_client import Counter  # type: ignore

    trigger_fired_total = Counter(
        "trigger_fired_total",
        "Total number of triggers that fired (all types)",
    )

    gmail_watch_renew_total = Counter(
        "gmail_watch_renew_total",
        "Total number of Gmail watch renewals performed",
    )

    gmail_api_error_total = Counter(
        "gmail_api_error_total",
        "Number of errors when interacting with the Gmail API",
    )

    gmail_webhook_error_total = Counter(
        "gmail_webhook_error_total",
        "Number of errors in Gmail webhook background processing",
    )

    external_api_retry_total = Counter(
        "external_api_retry_total",
        "Total retries executed against external providers",
        labelnames=("provider", "function"),
    )

    database_migrations_failed_total = Counter(
        "database_migrations_failed_total",
        "Total startup SQLite migrations that failed. Label is the migration name.",
        labelnames=("migration_name",),
    )

    # ------------------------------------------------------------------
    # Gauges (current state) -------------------------------------------
    # ------------------------------------------------------------------

    from prometheus_client import Gauge  # type: ignore  # noqa: WPS433

    gmail_connector_history_id = Gauge(
        "gmail_connector_history_id",
        "Current history_id for each Gmail connector",
        labelnames=("connector_id", "owner_id"),
    )

    gmail_connector_watch_expiry = Gauge(
        "gmail_connector_watch_expiry_seconds",
        "Unix timestamp when Gmail watch expires",
        labelnames=("connector_id", "owner_id"),
    )

    pubsub_webhook_processing = Gauge(
        "pubsub_webhook_processing_total",
        "Number of Pub/Sub webhooks currently being processed",
    )

    # ------------------------------------------------------------------
    # Histograms (latency) ---------------------------------------------
    # ------------------------------------------------------------------

    from prometheus_client import Histogram  # type: ignore  # noqa: WPS433

    gmail_http_latency_seconds = Histogram(
        "gmail_http_latency_seconds",
        "Latency of Gmail HTTP requests (seconds)",
        buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    )

    trigger_processing_seconds = Histogram(
        "trigger_processing_seconds",
        "End-to-end processing time of a single trigger (seconds)",
        buckets=(0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
    )

    dashboard_snapshot_requests_total = Counter(
        "dashboard_snapshot_requests_total",
        "Total number of dashboard snapshot requests served",
        labelnames=("scope", "status"),
    )

    dashboard_snapshot_latency_seconds = Histogram(
        "dashboard_snapshot_latency_seconds",
        "Latency of dashboard snapshot responses (seconds)",
        buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
    )

    dashboard_snapshot_fiches_returned = Histogram(
        "dashboard_snapshot_fiches_returned",
        "Number of fiches included in dashboard snapshot responses",
        buckets=(0, 1, 5, 10, 25, 50, 100, 200, 500, 1000),
    )

    dashboard_snapshot_runs_returned = Histogram(
        "dashboard_snapshot_runs_returned",
        "Number of runs included across all fiches in dashboard snapshots",
        buckets=(0, 5, 10, 25, 50, 100, 200, 500, 1000, 2000),
    )

    websocket_run_updates_total = Counter(
        "websocket_run_updates_total",
        "Total run_update events broadcast to WebSocket clients",
        labelnames=("status", "source_event"),
    )

    websocket_run_update_latency_seconds = Histogram(
        "websocket_run_update_latency_seconds",
        "Elapsed time between run start and run_update broadcast (seconds)",
        buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120),
    )

    managed_turn_requests_total = Counter(
        "managed_turn_requests_total",
        "Managed turn requests by provider and outcome",
        labelnames=("provider", "outcome"),
    )

    managed_turn_dispatch_seconds = Histogram(
        "managed_turn_dispatch_seconds",
        "Managed turn send-accept latency by provider (seconds)",
        labelnames=("provider",),
        buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30),
    )

    managed_turn_phase_seconds = Histogram(
        "managed_turn_phase_seconds",
        "Managed turn phase durations before send acceptance (seconds)",
        labelnames=("provider", "phase"),
        buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
    )

    managed_turn_wait_seconds = Histogram(
        "managed_turn_wait_seconds",
        "Managed turn watcher wait durations by milestone and outcome (seconds)",
        labelnames=("provider", "milestone", "outcome"),
        buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600),
    )

    managed_turn_wait_total = Counter(
        "managed_turn_wait_total",
        "Managed turn watcher outcomes by milestone",
        labelnames=("provider", "milestone", "outcome"),
    )

    agents_ingest_requests_total = Counter(
        "agents_ingest_requests_total",
        "Agent ingest requests by auth kind, provider, and status",
        labelnames=("auth_kind", "provider", "status"),
    )

    agents_ingest_decode_seconds = Histogram(
        "agents_ingest_decode_seconds",
        "Decode and decompression time for agent ingest requests (seconds)",
        labelnames=("content_encoding",),
        buckets=(0.0005, 0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
    )

    agents_ingest_write_seconds = Histogram(
        "agents_ingest_write_seconds",
        "Write-serializer backed ingest write latency (seconds)",
        labelnames=("provider",),
        buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
    )

    agents_ingest_payload_bytes = Histogram(
        "agents_ingest_payload_bytes",
        "Payload byte sizes for agent ingest requests",
        labelnames=("content_encoding", "kind"),
        buckets=(256, 1_024, 4_096, 16_384, 65_536, 262_144, 1_048_576, 4_194_304, 16_777_216),
    )

    agents_ingest_events_total = Counter(
        "agents_ingest_events_total",
        "Event counts observed during agent ingest",
        labelnames=("provider", "kind"),
    )

    # Event age at ingest: emitted_at (provider/engine) -> server receive.
    # Upper bound on engine→server hop; drives the SLA budget for realtime UI.
    event_age_at_ingest_seconds = Histogram(
        "event_age_at_ingest_seconds",
        "Age of an event when the server first sees it (emitted_at → ingest receive) in seconds",
        labelnames=("surface", "provider", "managed"),
        buckets=(0.01, 0.025, 0.05, 0.1, 0.15, 0.25, 0.5, 1, 2, 5, 15, 60),
    )

    # Total end-to-end latency: provider emitted_at -> UI rendered (beacon from client).
    # The user-facing SLA metric. Labels let us track web vs ios, managed vs unmanaged.
    event_end_to_end_latency_seconds = Histogram(
        "event_end_to_end_latency_seconds",
        "Total provider-emit to client-render latency (seconds)",
        labelnames=("surface", "managed"),
        buckets=(0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.75, 1, 2, 5, 15),
    )

    event_render_beacons_total = Counter(
        "event_render_beacons_total",
        "Client render beacons received (for end-to-end latency tracking)",
        labelnames=("surface", "outcome"),
    )

    # Canary pipeline: always-on synthetic probe measuring emit -> observer
    # round-trip. Separate from user-facing SLA histograms so canary volume
    # doesn't pollute real-user percentiles.
    canary_latency_seconds = Histogram(
        "canary_latency_seconds",
        "Synthetic canary hop latency (seconds). Hop: ingest|sse|render.",
        labelnames=("hop", "surface"),
        buckets=(0.01, 0.025, 0.05, 0.1, 0.15, 0.25, 0.5, 1, 2, 5, 15, 60),
    )

    canary_observations_total = Counter(
        "canary_observations_total",
        "Canary observations accepted/rejected by hop and outcome.",
        labelnames=("hop", "outcome"),
    )

    canary_seq_last_seen = Gauge(
        "canary_seq_last_seen",
        "Most recent canary_seq seen per hop; gaps indicate dropped pipeline events.",
        labelnames=("hop",),
    )

    agents_heartbeat_requests_total = Counter(
        "agents_heartbeat_requests_total",
        "Agent heartbeat requests by auth kind and status",
        labelnames=("auth_kind", "status"),
    )

    agents_heartbeat_write_seconds = Histogram(
        "agents_heartbeat_write_seconds",
        "Write-serializer enqueue plus heartbeat write latency (seconds)",
        buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
    )

    agents_heartbeat_payload_bytes = Histogram(
        "agents_heartbeat_payload_bytes",
        "Wire payload byte sizes for agent heartbeat requests",
        buckets=(128, 256, 512, 768, 1_024, 2_048, 4_096, 8_192, 16_384),
    )

    managed_session_heartbeat_lease_rows_total = Counter(
        "managed_session_heartbeat_lease_rows_total",
        "Managed session lease rows observed in agent heartbeat payloads before observation dedupe",
        labelnames=("provider", "state", "phase"),
    )

    managed_codex_runtime_observations_total = Counter(
        "managed_codex_runtime_observations_total",
        "Managed Codex runtime observations by source, kind, and reducer outcome",
        labelnames=("source", "kind", "outcome"),
    )

    managed_codex_bridge_freshness_total = Counter(
        "managed_codex_bridge_freshness_total",
        "Managed Codex bridge phase freshness budget choices",
        labelnames=("outcome",),
    )

    managed_codex_liveness_invariant_sessions = Gauge(
        "managed_codex_liveness_invariant_sessions",
        "Managed Codex sessions currently violating liveness invariants",
        labelnames=("invariant",),
    )

    session_input_attachments_total = Counter(
        "session_input_attachments_total",
        "Image-attach multipart submissions by client and outcome",
        labelnames=("client", "outcome"),
    )

    session_input_attachment_bytes = Histogram(
        "session_input_attachment_bytes",
        "Bytes per stored attachment after server validation",
        buckets=(64_000, 128_000, 256_000, 512_000, 1_024_000, 2_097_152),
    )

    session_input_attachment_blob_fetches_total = Counter(
        "session_input_attachment_blob_fetches_total",
        "Engine blob fetches against /api/agents/.../attachments/.../blob by outcome",
        labelnames=("outcome",),
    )

except ModuleNotFoundError:  # pragma: no cover – metrics disabled when lib absent

    class _NoopCounter:  # noqa: D401 – tiny helper
        def inc(self, _value: int | float = 1):  # noqa: D401 – mimic prometheus
            return None

        def labels(self, *args, **kwargs):  # type: ignore
            return self

    trigger_fired_total = _NoopCounter()  # type: ignore[assignment]
    gmail_watch_renew_total = _NoopCounter()  # type: ignore[assignment]
    gmail_api_error_total = _NoopCounter()  # type: ignore[assignment]
    gmail_webhook_error_total = _NoopCounter()  # type: ignore[assignment]
    external_api_retry_total = _NoopCounter()  # type: ignore[assignment]
    database_migrations_failed_total = _NoopCounter()  # type: ignore[assignment]
    dashboard_snapshot_requests_total = _NoopCounter()  # type: ignore[assignment]
    websocket_run_updates_total = _NoopCounter()  # type: ignore[assignment]
    managed_turn_requests_total = _NoopCounter()  # type: ignore[assignment]
    managed_turn_wait_total = _NoopCounter()  # type: ignore[assignment]
    agents_ingest_requests_total = _NoopCounter()  # type: ignore[assignment]
    agents_ingest_events_total = _NoopCounter()  # type: ignore[assignment]
    agents_heartbeat_requests_total = _NoopCounter()  # type: ignore[assignment]
    managed_session_heartbeat_lease_rows_total = _NoopCounter()  # type: ignore[assignment]
    managed_codex_runtime_observations_total = _NoopCounter()  # type: ignore[assignment]
    managed_codex_bridge_freshness_total = _NoopCounter()  # type: ignore[assignment]
    session_input_attachments_total = _NoopCounter()  # type: ignore[assignment]
    session_input_attachment_blob_fetches_total = _NoopCounter()  # type: ignore[assignment]

    # Provide *noop* Gauge so code can call ``set`` without importing
    # the optional dependency in minimal CI images.

    class _NoopGauge:  # noqa: D401 – tiny helper
        def set(self, _value: float):  # noqa: D401 – mimic prometheus
            return None

        def inc(self, _value: float = 1):  # noqa: D401 – mimic prometheus
            return None

        def dec(self, _value: float = 1):  # noqa: D401 – mimic prometheus
            return None

        def labels(self, *args, **kwargs):  # type: ignore
            return self

    gmail_connector_history_id = _NoopGauge()  # type: ignore[assignment]
    gmail_connector_watch_expiry = _NoopGauge()  # type: ignore[assignment]
    pubsub_webhook_processing = _NoopGauge()  # type: ignore[assignment]
    managed_codex_liveness_invariant_sessions = _NoopGauge()  # type: ignore[assignment]

    # Provide *noop* Histogram so code can call ``observe`` without importing
    # the optional dependency in minimal CI images.

    class _NoopHistogram:  # noqa: D401 – tiny helper
        def observe(self, _value: float):  # noqa: D401 – mimic prometheus
            return None

        def labels(self, *args, **kwargs):  # type: ignore
            return self

    gmail_http_latency_seconds = _NoopHistogram()  # type: ignore[assignment]
    trigger_processing_seconds = _NoopHistogram()  # type: ignore[assignment]
    dashboard_snapshot_latency_seconds = _NoopHistogram()  # type: ignore[assignment]
    dashboard_snapshot_fiches_returned = _NoopHistogram()  # type: ignore[assignment]
    dashboard_snapshot_runs_returned = _NoopHistogram()  # type: ignore[assignment]
    websocket_run_update_latency_seconds = _NoopHistogram()  # type: ignore[assignment]
    managed_turn_dispatch_seconds = _NoopHistogram()  # type: ignore[assignment]
    managed_turn_phase_seconds = _NoopHistogram()  # type: ignore[assignment]
    managed_turn_wait_seconds = _NoopHistogram()  # type: ignore[assignment]
    agents_ingest_decode_seconds = _NoopHistogram()  # type: ignore[assignment]
    agents_ingest_write_seconds = _NoopHistogram()  # type: ignore[assignment]
    agents_ingest_payload_bytes = _NoopHistogram()  # type: ignore[assignment]
    agents_heartbeat_write_seconds = _NoopHistogram()  # type: ignore[assignment]
    agents_heartbeat_payload_bytes = _NoopHistogram()  # type: ignore[assignment]
    session_input_attachment_bytes = _NoopHistogram()  # type: ignore[assignment]
    event_age_at_ingest_seconds = _NoopHistogram()  # type: ignore[assignment]
    event_end_to_end_latency_seconds = _NoopHistogram()  # type: ignore[assignment]
    event_render_beacons_total = _NoopCounter()  # type: ignore[assignment]
    canary_latency_seconds = _NoopHistogram()  # type: ignore[assignment]
    canary_observations_total = _NoopCounter()  # type: ignore[assignment]
    canary_seq_last_seen = _NoopGauge()  # type: ignore[assignment]
