"""Prometheus metrics for Longhouse runtime surfaces.

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

    database_migrations_failed_total = Counter(
        "database_migrations_failed_total",
        "Total startup SQLite migrations that failed. Label is the migration name.",
        labelnames=("migration_name",),
    )

    # ------------------------------------------------------------------
    # Gauges (current state) -------------------------------------------
    # ------------------------------------------------------------------

    from prometheus_client import Gauge  # type: ignore  # noqa: WPS433

    # ------------------------------------------------------------------
    # Histograms (latency) ---------------------------------------------
    # ------------------------------------------------------------------
    from prometheus_client import Histogram  # type: ignore  # noqa: WPS433

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

    dashboard_snapshot_runs_returned = Histogram(
        "dashboard_snapshot_runs_returned",
        "Number of runs included across all automations in dashboard snapshots",
        buckets=(0, 5, 10, 25, 50, 100, 200, 500, 1000, 2000),
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
        labelnames=("provider", "state"),
    )

    agents_heartbeat_snapshot_skipped_total = Counter(
        "agents_heartbeat_snapshot_skipped_total",
        "Agent heartbeat session snapshots skipped because the canonical digest was unchanged",
        labelnames=("reason",),
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

    # Product read-path telemetry. Labels are deliberately coarse and bounded:
    # never add request paths, session ids, owners, queries, or object keys.
    product_read_requests_total = Counter(
        "longhouse_product_read_requests_total",
        "Product read requests by stable route class, status family, and outcome",
        labelnames=("route_class", "status_family", "outcome"),
    )

    product_read_request_seconds = Histogram(
        "longhouse_product_read_request_seconds",
        "End-to-end product read request latency by stable route class and outcome",
        labelnames=("route_class", "status_family", "outcome"),
        buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 15, 60),
    )

    product_read_stage_seconds = Histogram(
        "longhouse_product_read_stage_seconds",
        "Independently timed product read stage latency; stages may be nested and are not additive",
        labelnames=("surface", "stage"),
        buckets=(0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
    )

    product_read_bytes = Histogram(
        "longhouse_product_read_bytes",
        "Compressed immutable object bytes read per product request",
        labelnames=("surface", "object_kind"),
        buckets=(0, 1_024, 4_096, 16_384, 65_536, 262_144, 1_048_576, 4_194_304, 16_777_216),
    )

    product_read_objects = Histogram(
        "longhouse_product_read_objects",
        "Immutable objects read per product request",
        labelnames=("surface", "object_kind"),
        buckets=(0, 1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1_000),
    )

    build_identity_info = Gauge(
        "longhouse_build_info",
        "Runtime build identity for correlating retained telemetry with deployments",
        labelnames=("version", "commit", "channel", "dirty"),
    )

    # ------------------------------------------------------------------
    # God-view gauges: current operational state, refreshed at scrape time
    # (see routers/metrics.py::_refresh_dynamic_gauges). These turn the
    # point-in-time /health write-serializer view and the latest-per-device
    # heartbeat snapshots into retained Prometheus time series so a single
    # dashboard can show where time goes across devices + instance.
    # ------------------------------------------------------------------

    # Server-side WriteSerializer current state (from get_metrics()).
    write_serializer_queue_depth = Gauge(
        "longhouse_write_serializer_queue_depth",
        "WriteSerializer pending queue depth at scrape time",
    )

    write_serializer_writer_active = Gauge(
        "longhouse_write_serializer_writer_active",
        "Whether a WriteSerializer writer currently holds the slot (1/0)",
    )

    write_serializer_active_age_ms = Gauge(
        "longhouse_write_serializer_active_age_ms",
        "Age of the currently active WriteSerializer write in milliseconds",
    )

    write_serializer_idle_queue_stalled = Gauge(
        "longhouse_write_serializer_idle_queue_stalled",
        "Whether the WriteSerializer queue is nonempty but no writer is active (1/0)",
    )

    # Rolling per-label queue-wait / exec percentiles (p50/p95/p99).
    write_serializer_queue_wait_ms = Gauge(
        "longhouse_write_serializer_queue_wait_ms",
        "Rolling WriteSerializer queue-wait latency per label and quantile (ms)",
        labelnames=("label", "quantile"),
    )

    write_serializer_exec_ms = Gauge(
        "longhouse_write_serializer_exec_ms",
        "Rolling WriteSerializer exec latency per label and quantile (ms)",
        labelnames=("label", "quantile"),
    )

    # SQLite WAL pressure: cheapest leading indicator of write backpressure.
    sqlite_wal_bytes = Gauge(
        "longhouse_sqlite_wal_bytes",
        "Current SQLite WAL file size in bytes",
    )

    live_write_serializer_queue_depth = Gauge(
        "longhouse_live_write_serializer_queue_depth",
        "Live Store WriteSerializer pending queue depth at scrape time",
    )

    live_write_serializer_writer_active = Gauge(
        "longhouse_live_write_serializer_writer_active",
        "Whether the Live Store WriteSerializer writer currently holds the slot (1/0)",
    )

    live_write_serializer_active_age_ms = Gauge(
        "longhouse_live_write_serializer_active_age_ms",
        "Age of the currently active Live Store WriteSerializer write in milliseconds",
    )

    live_write_serializer_idle_queue_stalled = Gauge(
        "longhouse_live_write_serializer_idle_queue_stalled",
        "Whether the Live Store WriteSerializer queue is nonempty but no writer is active (1/0)",
    )

    live_write_serializer_queue_wait_ms = Gauge(
        "longhouse_live_write_serializer_queue_wait_ms",
        "Rolling Live Store WriteSerializer queue-wait latency per label and quantile (ms)",
        labelnames=("label", "quantile"),
    )

    live_write_serializer_exec_ms = Gauge(
        "longhouse_live_write_serializer_exec_ms",
        "Rolling Live Store WriteSerializer exec latency per label and quantile (ms)",
        labelnames=("label", "quantile"),
    )

    live_sqlite_wal_bytes = Gauge(
        "longhouse_live_sqlite_wal_bytes",
        "Current Live Store SQLite WAL file size in bytes",
    )

    live_archive_outbox_pending = Gauge(
        "longhouse_live_archive_outbox_pending",
        "Live Store archive outbox rows not yet drained",
    )

    live_archive_outbox_failed = Gauge(
        "longhouse_live_archive_outbox_failed",
        "Live Store archive outbox pending rows with a recorded drain error",
    )

    live_archive_outbox_oldest_pending_age_seconds = Gauge(
        "longhouse_live_archive_outbox_oldest_pending_age_seconds",
        "Age of the oldest pending Live Store archive outbox row in seconds",
    )

    live_archive_outbox_last_drained_age_seconds = Gauge(
        "longhouse_live_archive_outbox_last_drained_age_seconds",
        "Age of the most recently drained Live Store archive outbox row in seconds",
    )

    live_archive_outbox_max_attempts = Gauge(
        "longhouse_live_archive_outbox_max_attempts",
        "Maximum drain attempt count across retained Live Store archive outbox rows",
    )

    live_store_table_bytes = Gauge(
        "longhouse_live_store_table_bytes",
        "Approximate Live Store SQLite bytes by table from dbstat",
        labelnames=("table",),
    )

    # Per-device shipping state from the latest heartbeat per device. Device
    # cardinality is low (a handful of user-owned machines). Age/offline are
    # derived in PromQL from the last-heartbeat timestamp to avoid emitting
    # stale gauge children for devices that stop reporting.
    device_last_heartbeat_timestamp_seconds = Gauge(
        "longhouse_device_last_heartbeat_timestamp_seconds",
        "Unix timestamp of the latest heartbeat received per device",
        labelnames=("device",),
    )

    device_ship_latency_ms = Gauge(
        "longhouse_device_ship_latency_ms",
        "Per-device ship latency from the latest heartbeat (ms)",
        labelnames=("device", "quantile"),
    )

    device_spool_pending = Gauge(
        "longhouse_device_spool_pending",
        "Per-device spool pending count from the latest heartbeat",
        labelnames=("device",),
    )

    device_spool_dead = Gauge(
        "longhouse_device_spool_dead",
        "Per-device spool dead-letter count from the latest heartbeat",
        labelnames=("device",),
    )

    device_consecutive_ship_failures = Gauge(
        "longhouse_device_consecutive_ship_failures",
        "Per-device consecutive ship failures from the latest heartbeat",
        labelnames=("device",),
    )

    device_parse_errors_1h = Gauge(
        "longhouse_device_parse_errors_1h",
        "Per-device parse errors in the last hour from the latest heartbeat",
        labelnames=("device",),
    )

    device_disk_free_bytes = Gauge(
        "longhouse_device_disk_free_bytes",
        "Per-device free disk bytes from the latest heartbeat",
        labelnames=("device",),
    )

    # The heartbeat's own self-reported offline flag. This is NOT the staleness
    # overlay — a device that went silent without reporting still shows 0 here.
    # Derive true offline from device_last_heartbeat_timestamp_seconds in PromQL.
    device_reported_offline = Gauge(
        "longhouse_device_reported_offline",
        "Per-device self-reported offline flag from the latest heartbeat (1/0)",
        labelnames=("device",),
    )

    device_archive_backlog_pending_bytes = Gauge(
        "longhouse_device_archive_backlog_pending_bytes",
        "Per-device archive backlog pending bytes from the latest heartbeat",
        labelnames=("device",),
    )

    device_archive_backlog_pending_ranges = Gauge(
        "longhouse_device_archive_backlog_pending_ranges",
        "Per-device archive backlog pending ranges from the latest heartbeat",
        labelnames=("device",),
    )

except ModuleNotFoundError:  # pragma: no cover – metrics disabled when lib absent

    class _NoopCounter:  # noqa: D401 – tiny helper
        def inc(self, _value: int | float = 1):  # noqa: D401 – mimic prometheus
            return None

        def labels(self, *args, **kwargs):  # type: ignore
            return self

    database_migrations_failed_total = _NoopCounter()  # type: ignore[assignment]
    dashboard_snapshot_requests_total = _NoopCounter()  # type: ignore[assignment]
    managed_turn_requests_total = _NoopCounter()  # type: ignore[assignment]
    managed_turn_wait_total = _NoopCounter()  # type: ignore[assignment]
    agents_ingest_requests_total = _NoopCounter()  # type: ignore[assignment]
    agents_ingest_events_total = _NoopCounter()  # type: ignore[assignment]
    agents_heartbeat_requests_total = _NoopCounter()  # type: ignore[assignment]
    managed_session_heartbeat_lease_rows_total = _NoopCounter()  # type: ignore[assignment]
    agents_heartbeat_snapshot_skipped_total = _NoopCounter()  # type: ignore[assignment]
    managed_codex_runtime_observations_total = _NoopCounter()  # type: ignore[assignment]
    managed_codex_bridge_freshness_total = _NoopCounter()  # type: ignore[assignment]
    session_input_attachments_total = _NoopCounter()  # type: ignore[assignment]
    session_input_attachment_blob_fetches_total = _NoopCounter()  # type: ignore[assignment]
    product_read_requests_total = _NoopCounter()  # type: ignore[assignment]

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

    # Provide *noop* Histogram so code can call ``observe`` without importing
    # the optional dependency in minimal CI images.

    class _NoopHistogram:  # noqa: D401 – tiny helper
        def observe(self, _value: float):  # noqa: D401 – mimic prometheus
            return None

        def labels(self, *args, **kwargs):  # type: ignore
            return self

    dashboard_snapshot_latency_seconds = _NoopHistogram()  # type: ignore[assignment]
    dashboard_snapshot_runs_returned = _NoopHistogram()  # type: ignore[assignment]
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

    # God-view gauges (see the configured branch above).
    write_serializer_queue_depth = _NoopGauge()  # type: ignore[assignment]
    write_serializer_writer_active = _NoopGauge()  # type: ignore[assignment]
    write_serializer_active_age_ms = _NoopGauge()  # type: ignore[assignment]
    write_serializer_idle_queue_stalled = _NoopGauge()  # type: ignore[assignment]
    write_serializer_queue_wait_ms = _NoopGauge()  # type: ignore[assignment]
    write_serializer_exec_ms = _NoopGauge()  # type: ignore[assignment]
    sqlite_wal_bytes = _NoopGauge()  # type: ignore[assignment]
    live_write_serializer_queue_depth = _NoopGauge()  # type: ignore[assignment]
    live_write_serializer_writer_active = _NoopGauge()  # type: ignore[assignment]
    live_write_serializer_active_age_ms = _NoopGauge()  # type: ignore[assignment]
    live_write_serializer_idle_queue_stalled = _NoopGauge()  # type: ignore[assignment]
    live_write_serializer_queue_wait_ms = _NoopGauge()  # type: ignore[assignment]
    live_write_serializer_exec_ms = _NoopGauge()  # type: ignore[assignment]
    live_sqlite_wal_bytes = _NoopGauge()  # type: ignore[assignment]
    live_archive_outbox_pending = _NoopGauge()  # type: ignore[assignment]
    live_archive_outbox_failed = _NoopGauge()  # type: ignore[assignment]
    live_archive_outbox_oldest_pending_age_seconds = _NoopGauge()  # type: ignore[assignment]
    live_archive_outbox_last_drained_age_seconds = _NoopGauge()  # type: ignore[assignment]
    live_archive_outbox_max_attempts = _NoopGauge()  # type: ignore[assignment]
    live_store_table_bytes = _NoopGauge()  # type: ignore[assignment]
    device_last_heartbeat_timestamp_seconds = _NoopGauge()  # type: ignore[assignment]
    device_ship_latency_ms = _NoopGauge()  # type: ignore[assignment]
    device_spool_pending = _NoopGauge()  # type: ignore[assignment]
    device_spool_dead = _NoopGauge()  # type: ignore[assignment]
    device_consecutive_ship_failures = _NoopGauge()  # type: ignore[assignment]
    device_parse_errors_1h = _NoopGauge()  # type: ignore[assignment]
    device_disk_free_bytes = _NoopGauge()  # type: ignore[assignment]
    device_reported_offline = _NoopGauge()  # type: ignore[assignment]
    device_archive_backlog_pending_bytes = _NoopGauge()  # type: ignore[assignment]
    device_archive_backlog_pending_ranges = _NoopGauge()  # type: ignore[assignment]
    build_identity_info = _NoopGauge()  # type: ignore[assignment]
    product_read_request_seconds = _NoopHistogram()  # type: ignore[assignment]
    product_read_stage_seconds = _NoopHistogram()  # type: ignore[assignment]
    product_read_bytes = _NoopHistogram()  # type: ignore[assignment]
    product_read_objects = _NoopHistogram()  # type: ignore[assignment]
