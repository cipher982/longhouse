"""Tests for the agent heartbeat ingest endpoint.

``POST /agents/heartbeat`` is one catalogd RPC. The route validates the wire
payload, derives managed control leases from the engine's resolved sessions,
and hands ``machine.heartbeat.apply.v2`` a heartbeat stamp; it never opens the
archive database. So every effect these tests observe, they observe in the live
catalog the fixtures provision -- ``live_heartbeat_stamps`` for retention and
telemetry, ``live_control_leases`` for managed control.

What happens *behind* that RPC -- replay identity, retention semantics,
snapshot reconciliation, the shadow reducers -- belongs to catalogd and is
covered in ``test_catalogd_heartbeat``. What these tests own is the route: what
it rejects, what it retains, and what it hands the catalog.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from tests_lite.live_catalog_harness import live_catalog  # noqa: E402, F401
from tests_lite.live_catalog_harness import live_catalog_client  # noqa: E402, F401
from zerg.catalogd.schema import create_catalog_engine  # noqa: E402
from zerg.machine_evidence import canonical_evidence_hash  # noqa: E402
from zerg.machine_evidence import validate_machine_evidence_identities  # noqa: E402
from zerg.models.live_store import LiveControlLease  # noqa: E402
from zerg.models.live_store import LiveHeartbeatStamp  # noqa: E402
from zerg.models.live_store import LiveSessionCatalog  # noqa: E402
from zerg.models.live_store import LiveSessionRun  # noqa: E402
from zerg.models.live_store import LiveSessionThread  # noqa: E402
from zerg.services.catalogd_supervisor import catalogd_paths  # noqa: E402

OWNER_EMAIL = "owner@heartbeat.test"
DEVICE_ID = "cinder"

# ---------------------------------------------------------------------------
# Enrollment and catalog reads
# ---------------------------------------------------------------------------


def _enroll(live_catalog, *device_ids: str) -> dict[str, dict[str, str]]:
    """One owner, one device token per device: the heartbeat's real identity."""

    owner_id = live_catalog.create_user(OWNER_EMAIL)
    return {
        device_id: {"X-Agents-Token": live_catalog.create_device_token(owner_id=owner_id, device_id=device_id)} for device_id in device_ids
    }


def _headers(live_catalog, device_id: str = DEVICE_ID) -> dict[str, str]:
    return _enroll(live_catalog, device_id)[device_id]


def _catalog_rows(table) -> list[dict[str, Any]]:
    """Rows the daemon committed, read from the live catalog database itself."""

    engine = create_catalog_engine(catalogd_paths()[0])
    try:
        with engine.connect() as connection:
            return [dict(row) for row in connection.execute(table.select()).mappings()]
    finally:
        engine.dispose()


def _stamps(device_id: str | None = None) -> list[dict[str, Any]]:
    rows = _catalog_rows(LiveHeartbeatStamp.__table__)
    if device_id is not None:
        rows = [row for row in rows if row["device_id"] == device_id]
    return sorted(rows, key=lambda row: row["id"])


def _one_stamp(device_id: str | None = None) -> dict[str, Any]:
    rows = _stamps(device_id)
    assert len(rows) == 1, f"expected exactly one heartbeat stamp, got {len(rows)}"
    return rows[0]


def _leases() -> list[dict[str, Any]]:
    return sorted(_catalog_rows(LiveControlLease.__table__), key=lambda row: row["id"])


def _lease_payload(row: dict[str, Any]) -> dict[str, Any]:
    return json.loads(row["payload_json"] or "{}")


def _naive(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


_STAMP_FIELD_DEFAULTS: dict[str, Any] = {
    "version": "seeded",
    "last_ship_at": None,
    "last_ship_attempt_at": None,
    "last_ship_result": None,
    "last_ship_latency_ms": None,
    "last_ship_http_status": None,
    "spool_pending": 0,
    "spool_dead": 0,
    "parse_errors_1h": 0,
    "consecutive_failures": 0,
    "ship_attempts_1h": 0,
    "ship_successes_1h": 0,
    "ship_rate_limited_1h": 0,
    "ship_server_errors_1h": 0,
    "ship_payload_rejections_1h": 0,
    "ship_payload_too_large_1h": 0,
    "ship_retryable_client_errors_1h": 0,
    "ship_connect_errors_1h": 0,
    "ship_latency_p50_ms_1h": None,
    "ship_latency_p95_ms_1h": None,
    "disk_free_bytes": 0,
    "is_offline": 0,
    "raw_json": "{}",
    "sessions_digest": None,
    "sessions_sequence": None,
}


def _seed_stamp(live_catalog, *, device_id: str, received_at: datetime, **overrides: Any) -> None:
    """Apply one heartbeat straight through the RPC the route calls."""

    live_catalog.rpc(
        "machine.heartbeat.apply.v2",
        {
            "heartbeat": {
                "device_id": device_id,
                "received_at": received_at.isoformat(),
                **_STAMP_FIELD_DEFAULTS,
                **overrides,
            },
            "managed_leases": [],
            "managed_leases_present": False,
            "owner_id": None,
        },
    )


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------


def _machine_evidence_payload() -> dict[str, object]:
    observed_at = "2026-05-08T12:00:00Z"
    process = [
        {
            "provider": provider,
            "session_id": f"{provider}-session" if provider != "antigravity" else None,
            "provider_session_id": f"{provider}-provider-session",
            "role": "provider",
            "pid": 100 + index,
            "process_start_time": "Thu May  8 11:59:00 2026",
            "boot_id": "macos:1777970400:0",
            "cwd": f"/tmp/{provider}",
            "alive": True,
            "source": "provider_process_scan",
            "observed_at": observed_at,
        }
        for index, provider in enumerate(("codex", "claude", "opencode", "cursor", "antigravity"))
    ]
    control = [
        {
            "provider": provider,
            "session_id": f"{provider}-session",
            "provider_session_id": f"{provider}-provider-session",
            "ownership": "managed",
            "state": "attached",
            "bridge_status": "ready",
            "lease_ttl_ms": 900_000,
            "source": "provider_control_scan",
            "observed_at": observed_at,
        }
        for provider in ("codex", "claude", "opencode", "cursor")
    ]
    transcript = [
        {
            "provider": provider,
            "session_id": None if provider == "antigravity" else f"{provider}-session",
            "provider_session_id": f"{provider}-provider-session",
            "source_path": f"/tmp/{provider}.jsonl",
            "source_offset": 12,
            "source": "provider_transcript_scan",
            "observed_at": observed_at,
        }
        for provider in ("codex", "claude", "opencode", "antigravity")
    ]
    return {
        "schema_version": 1,
        "observed_at": observed_at,
        "process": process,
        "activity": [
            {
                "provider": "codex",
                "session_id": "codex-session",
                "kind": "running",
                "raw_kind": "running",
                "tool_name": "Shell",
                "source": "codex_bridge",
                "observed_at": observed_at,
                "valid_until": "2026-05-08T12:10:00Z",
            }
        ],
        "control": control,
        "transcript": transcript,
        "process_snapshot_scopes": [
            {
                "scope": "managed_state_files",
                "complete": True,
                "captured_at": observed_at,
                "machine_boot_id": "macos:1777970400:0",
                "source": "managed_provider_scan",
            },
            {
                "scope": "unmanaged_provider_processes",
                "complete": True,
                "captured_at": observed_at,
                "machine_boot_id": "macos:1777970400:0",
                "source": "unmanaged_process_scan",
            },
        ],
        "readiness": [
            {
                "provider": "antigravity",
                "session_id": "antigravity-session",
                "operation": "send_input",
                "hook_installed": True,
                "recent_hook_observed": True,
                "claim_observed": True,
                "response_observed": True,
                "continuation_observed": False,
                "hook_event": "PreInvocation",
                "hook_observed_at": observed_at,
                "claim_message_id": "message-1",
                "claimed_at": observed_at,
                "response_event": "PreInvocation",
                "response_at": observed_at,
                "response_status": "ok",
                "observed_at": observed_at,
                "valid_until": "2026-05-08T12:02:00Z",
                "source": "antigravity_hook_state",
                "raw_locator": "/tmp/antigravity-session.json",
                "reason_codes": [],
            }
        ],
    }


def _process_identity(evidence: dict[str, object]) -> dict[str, object]:
    process = evidence["process"]
    assert isinstance(process, list)
    fact = process[0]
    provider = str(fact["provider"])
    pid = int(fact["pid"])
    process_start = str(fact["process_start_time"])
    generation = hashlib.sha256(f"{provider}:{pid}:{process_start}".encode()).hexdigest()
    boot = hashlib.sha256(str(fact["boot_id"]).encode()).hexdigest()
    return {
        "fact_family": "process",
        "fact_index": 0,
        "subject_key": f"process:{'0' * 64}:{provider}:{boot}:{pid}:{generation}",
        "source": fact["source"],
        "source_epoch": generation,
        "source_seq": None,
        "sequenced": False,
        "dedupe_key": "a" * 64,
        "evidence_hash": canonical_evidence_hash(fact),
    }


def _resolved_managed_session(
    session_id,
    *,
    provider: str = "codex",
    state: str = "attached",
    bridge_status: str = "ready",
    thread_subscription_status: str | None = "subscribed",
    phase: str = "idle",
) -> dict[str, object]:
    bridge: dict[str, object] = {
        "bridge_pid": 4202,
        "app_server_pid": 4203,
        "heartbeat_at": "2026-05-05T11:59:58Z",
        "status": bridge_status,
    }
    if thread_subscription_status is not None:
        bridge["thread_subscription_status"] = thread_subscription_status
    return {
        "session_id": str(session_id),
        "provider": provider,
        "provider_session_id": f"thread-{provider}",
        "control_path": "managed",
        "presentation_state": "managed_attached",
        "state": state,
        "phase": phase,
        "phase_observed_at": "2026-05-05T11:59:58Z",
        "last_activity_at": "2026-05-05T11:59:58Z",
        "workspace": {"cwd": "/Users/test/git/zerg", "label": "zerg"},
        "process": {
            "pid": 4201,
            "process_start_time": "Mon May  5 11:20:00 2026",
            "boot_id": "macos:1777970400:0",
            "started_at": "2026-05-05T11:20:00Z",
        },
        "bridge": bridge,
        "evidence": {"process_observed": True, "transcript_observed": True},
        "reason_codes": [] if state == "attached" else [state],
    }


def _seed_open_run(session_id, *, provider: str = "codex") -> tuple[str, str]:
    """A session whose run is still open, as while the provider is running."""

    started_at = datetime.now(UTC) - timedelta(minutes=5)
    thread_id = str(uuid4())
    run_id = str(uuid4())
    engine = create_catalog_engine(catalogd_paths()[0])
    try:
        with engine.begin() as connection:
            connection.execute(
                LiveSessionCatalog.__table__.insert().values(
                    session_id=str(session_id),
                    provider=provider,
                    environment="development",
                    device_id=DEVICE_ID,
                    started_at=started_at,
                    primary_thread_id=thread_id,
                    created_at=started_at,
                    updated_at=started_at,
                )
            )
            connection.execute(
                LiveSessionThread.__table__.insert().values(
                    id=thread_id,
                    session_id=str(session_id),
                    provider=provider,
                    branch_kind="root",
                    is_primary=1,
                    created_at=started_at,
                    updated_at=started_at,
                )
            )
            connection.execute(
                LiveSessionRun.__table__.insert().values(
                    id=run_id,
                    thread_id=thread_id,
                    provider=provider,
                    host_id=DEVICE_ID,
                    launch_origin="longhouse_spawned",
                    started_at=started_at,
                )
            )
    finally:
        engine.dispose()
    return thread_id, run_id


def _legacy_lease(session_id, *, provider: str, machine_id: str) -> dict[str, object]:
    return {
        "session_id": str(session_id),
        "provider": provider,
        "machine_id": machine_id,
        "sequence": 42,
        "state": "attached",
        "phase": "idle",
        "bridge_status": "ready",
        "observed_at": "2026-05-05T11:59:58Z",
        "lease_ttl_ms": 900_000,
    }


# ---------------------------------------------------------------------------
# Stamp retention and telemetry
# ---------------------------------------------------------------------------


def test_heartbeat_endpoint_creates_row(live_catalog, live_catalog_client):
    """POST /agents/heartbeat commits one heartbeat stamp to the live catalog."""

    response = live_catalog_client.post(
        "/agents/heartbeat",
        headers=_headers(live_catalog),
        json={
            "version": "0.5.0",
            "daemon_pid": 12345,
            "spool_pending_count": 3,
            "parse_error_count_1h": 0,
            "consecutive_ship_failures": 0,
            "disk_free_bytes": 50_000_000_000,
            "is_offline": False,
        },
    )
    assert response.status_code == 204, response.text

    stamp = _one_stamp()
    assert stamp["device_id"] == DEVICE_ID
    assert stamp["version"] == "0.5.0"
    assert stamp["last_ship_attempt_at"] is None
    assert stamp["last_ship_result"] is None
    assert stamp["last_ship_latency_ms"] is None
    assert stamp["last_ship_http_status"] is None
    assert stamp["spool_pending"] == 3
    assert stamp["spool_dead"] == 0
    assert stamp["ship_attempts_1h"] == 0
    assert stamp["ship_successes_1h"] == 0
    assert stamp["ship_rate_limited_1h"] == 0
    assert stamp["ship_server_errors_1h"] == 0
    assert stamp["ship_payload_rejections_1h"] == 0
    assert stamp["ship_payload_too_large_1h"] == 0
    assert stamp["ship_retryable_client_errors_1h"] == 0
    assert stamp["ship_connect_errors_1h"] == 0
    assert stamp["ship_latency_p50_ms_1h"] is None
    assert stamp["ship_latency_p95_ms_1h"] is None
    assert stamp["disk_free_bytes"] == 50_000_000_000
    assert stamp["is_offline"] == 0


def test_heartbeat_endpoint_appends_history_rows(live_catalog, live_catalog_client):
    """Two POSTs append two stamps: a heartbeat is history, not an upsert."""

    headers = _headers(live_catalog)
    for pending in range(2):
        response = live_catalog_client.post(
            "/agents/heartbeat",
            headers=headers,
            json={
                "version": "0.5.0",
                "daemon_pid": 99,
                "spool_pending_count": pending,
                "parse_error_count_1h": 0,
                "consecutive_ship_failures": 0,
                "disk_free_bytes": 1_000_000,
                "is_offline": False,
            },
        )
        assert response.status_code == 204, response.text

    assert [stamp["spool_pending"] for stamp in _stamps(DEVICE_ID)] == [0, 1]


def test_heartbeat_prunes_old_rows(live_catalog, live_catalog_client):
    """Stamps older than the retention window go away on the next heartbeat."""

    _seed_stamp(
        live_catalog,
        device_id=DEVICE_ID,
        received_at=datetime.now(UTC) - timedelta(days=31),
        version="0.4.0",
    )
    assert len(_stamps(DEVICE_ID)) == 1

    response = live_catalog_client.post(
        "/agents/heartbeat",
        headers=_headers(live_catalog),
        json={
            "version": "0.5.0",
            "daemon_pid": 1,
            "spool_pending_count": 0,
            "parse_error_count_1h": 0,
            "consecutive_ship_failures": 0,
            "disk_free_bytes": 0,
            "is_offline": False,
        },
    )
    assert response.status_code == 204, response.text

    assert [stamp["version"] for stamp in _stamps(DEVICE_ID)] == ["0.5.0"]


def test_heartbeat_endpoint_persists_transport_summary_fields(live_catalog, live_catalog_client):
    """The stamp preserves the engine ship telemetry payload, minus local paths."""

    response = live_catalog_client.post(
        "/agents/heartbeat",
        headers=_headers(live_catalog),
        json={
            "version": "0.5.0",
            "daemon_pid": 42,
            "last_ship_attempt_at": "2026-04-23T20:00:03Z",
            "last_ship_result": "rate_limited",
            "last_ship_latency_ms": 187,
            "last_ship_http_status": 429,
            "last_ship_error_kind": "rate_limited",
            "last_ship_error_message": "429: rate limited",
            "spool_pending_count": 7,
            "spool_dead_count": 2,
            "managed_launch_recovery": {
                "active_count": 1,
                "exhausted_count": 2,
            },
            "parse_error_count_1h": 2,
            "consecutive_ship_failures": 1,
            "ship_attempts_1h": 12,
            "ship_successes_1h": 8,
            "ship_rate_limited_1h": 3,
            "ship_server_errors_1h": 1,
            "ship_payload_rejections_1h": 0,
            "ship_payload_too_large_1h": 0,
            "ship_retryable_client_errors_1h": 0,
            "ship_connect_errors_1h": 0,
            "ship_latency_p50_ms_1h": 140,
            "ship_latency_p95_ms_1h": 260,
            "ship_lanes": {
                "archive": {
                    "attempts_1h": 4,
                    "successes_1h": 3,
                    "backpressure_1h": 1,
                    "events_1h": 120,
                    "bytes_1h": 524288,
                    "events_per_sec_ewma_10s": 42.5,
                    "bytes_per_sec_ewma_10s": 131072.0,
                }
            },
            "adaptive_backlog_limiter": {"historical_cap": 3, "state": "steady"},
            "ship_scheduler": {"ready_scan": 7, "in_flight_scan": 1},
            "history_import": {
                "state": "inventory_ready",
                "inventory": {
                    "schema_version": 1,
                    "generation": 4,
                    "content_sha256": "a" * 64,
                    "observed_at": "2026-04-23T20:00:02Z",
                    "scan_duration_ms": 81,
                    "scan_error_count": 0,
                    "source_count": 2,
                    "source_bytes": 300,
                    "wal_bytes": 0,
                    "footprint_bytes": 300,
                    "providers": [
                        {
                            "provider": "claude",
                            "source_count": 2,
                            "source_bytes": 300,
                            "wal_bytes": 0,
                            "footprint_bytes": 300,
                            "oldest_modified_at_ms": 10,
                            "newest_modified_at_ms": 20,
                            "path": "/must/not/survive",
                        }
                    ],
                },
            },
            "events_per_sec_ewma_10s": 12.5,
            "bytes_per_sec_ewma_10s": 65536.0,
            "disk_free_bytes": 50_000_000,
            "is_offline": False,
        },
    )
    assert response.status_code == 204, response.text

    stamp = _one_stamp()
    raw = json.loads(stamp["raw_json"])
    assert stamp["last_ship_attempt_at"] is not None
    # SQLite drops timezone info on round-trip; Postgres preserves UTC.
    assert _naive(stamp["last_ship_attempt_at"]) == datetime(2026, 4, 23, 20, 0, 3)
    assert stamp["last_ship_result"] == "rate_limited"
    assert stamp["last_ship_latency_ms"] == 187
    assert stamp["last_ship_http_status"] == 429
    assert stamp["spool_dead"] == 2
    assert stamp["ship_attempts_1h"] == 12
    assert stamp["ship_successes_1h"] == 8
    assert stamp["ship_rate_limited_1h"] == 3
    assert stamp["ship_server_errors_1h"] == 1
    assert stamp["ship_payload_rejections_1h"] == 0
    assert stamp["ship_payload_too_large_1h"] == 0
    assert stamp["ship_retryable_client_errors_1h"] == 0
    assert stamp["ship_connect_errors_1h"] == 0
    assert stamp["ship_latency_p50_ms_1h"] == 140
    assert stamp["ship_latency_p95_ms_1h"] == 260
    assert raw["last_ship_attempt_at"] == "2026-04-23T20:00:03Z"
    assert raw["last_ship_result"] == "rate_limited"
    assert raw["adaptive_backlog_limiter"]["historical_cap"] == 3
    assert raw["ship_scheduler"]["ready_scan"] == 7
    assert raw["history_import"]["state"] == "inventory_ready"
    assert raw["managed_launch_recovery"] == {
        "active_count": 1,
        "exhausted_count": 2,
    }
    assert raw["history_import"]["inventory"]["generation"] == 4
    assert "path" not in raw["history_import"]["inventory"]["providers"][0]
    assert raw["last_ship_latency_ms"] == 187
    assert raw["last_ship_http_status"] == 429
    assert raw["last_ship_error_kind"] == "rate_limited"
    assert raw["last_ship_error_message"] == "429: rate limited"
    assert raw["spool_dead_count"] == 2
    assert raw["ship_attempts_1h"] == 12
    assert raw["ship_successes_1h"] == 8
    assert raw["ship_rate_limited_1h"] == 3
    assert raw["ship_latency_p50_ms_1h"] == 140
    assert raw["ship_latency_p95_ms_1h"] == 260
    assert raw["ship_lanes"]["archive"]["attempts_1h"] == 4
    assert raw["ship_lanes"]["archive"]["backpressure_1h"] == 1
    assert raw["ship_lanes"]["archive"]["bytes_per_sec_ewma_10s"] == 131072.0
    assert raw["events_per_sec_ewma_10s"] == 12.5
    assert raw["bytes_per_sec_ewma_10s"] == 65536.0


def test_heartbeat_legacy_omission_is_unavailable_and_invalid_inventory_soft_fails(live_catalog, live_catalog_client):
    from zerg.routers.heartbeat import HeartbeatIn
    from zerg.services.agent_heartbeat_health import build_machine_transport_health_summary

    headers = _headers(live_catalog)
    response = live_catalog_client.post(
        "/agents/heartbeat",
        headers=headers,
        json={"version": "legacy", "disk_free_bytes": 100, "is_offline": False},
    )
    assert response.status_code == 204, response.text

    stamp = _one_stamp()
    assert "history_import" not in json.loads(stamp["raw_json"])
    summary = build_machine_transport_health_summary(
        SimpleNamespace(**stamp),
        stale_after_seconds=3600,
        now=stamp["received_at"].replace(tzinfo=UTC),
    )
    assert summary.history_import.state == "unavailable"

    explicit = HeartbeatIn.model_validate({"history_import": {"state": "discovering"}})
    assert explicit.history_import is not None
    assert explicit.history_import.state == "discovering"

    malformed = HeartbeatIn.model_validate(
        {
            "history_import": {
                "state": "inventory_ready",
                "inventory": {
                    "schema_version": 1,
                    "generation": 1,
                    "content_sha256": "a" * 64,
                    "observed_at": "2026-04-23T20:00:02Z",
                    "scan_duration_ms": 1,
                    "scan_error_count": 0,
                    "source_count": 2,
                    "source_bytes": 10,
                    "wal_bytes": 0,
                    "footprint_bytes": 10,
                    "providers": [
                        {
                            "provider": "claude",
                            "source_count": 1,
                            "source_bytes": 10,
                            "wal_bytes": 0,
                            "footprint_bytes": 10,
                        }
                    ],
                },
            }
        }
    )
    assert malformed.history_import is not None
    assert malformed.history_import.state == "unavailable"

    malformed_response = live_catalog_client.post(
        "/agents/heartbeat",
        headers=headers,
        json={
            "version": "current",
            "history_import": {
                "state": "inventory_ready",
                "inventory": {
                    "schema_version": 1,
                    "generation": 1,
                    "content_sha256": "a" * 64,
                    "observed_at": "2026-04-23T20:00:02Z",
                    "scan_duration_ms": 1,
                    "scan_error_count": 0,
                    "source_count": 2,
                    "source_bytes": 10,
                    "wal_bytes": 0,
                    "footprint_bytes": 10,
                    "providers": [],
                },
            },
        },
    )
    assert malformed_response.status_code == 204, malformed_response.text
    latest = _stamps(DEVICE_ID)[-1]
    assert json.loads(latest["raw_json"])["history_import"]["state"] == "unavailable"


def test_heartbeat_mixed_unit_history_progress_is_typed_and_path_free():
    from zerg.routers.heartbeat import HeartbeatIn

    history_import = {
        "state": "importing",
        "inventory": {
            "schema_version": 1,
            "generation": 1,
            "content_sha256": "a" * 64,
            "observed_at": "2026-07-21T10:00:00Z",
            "scan_duration_ms": 10,
            "scan_error_count": 0,
            "source_count": 2,
            "source_bytes": 6_000,
            "wal_bytes": 0,
            "footprint_bytes": 6_000,
            "providers": [
                {
                    "provider": "codex",
                    "source_count": 1,
                    "source_bytes": 1_000,
                    "wal_bytes": 0,
                    "footprint_bytes": 1_000,
                },
                {
                    "provider": "opencode",
                    "source_count": 1,
                    "source_bytes": 5_000,
                    "wal_bytes": 0,
                    "footprint_bytes": 5_000,
                },
            ],
        },
        "progress": {
            "acknowledged_source_bytes": 600,
            "remaining_source_bytes": 600,
            "acknowledged_records": 27,
            "remaining_records": 3,
            "pending_outbox_count": 1,
            "pending_outbox_bytes": 100,
            "blocked_source_count": 0,
            "blocked_bytes": 0,
            "percent_complete": 99,
            "providers": [
                {
                    "provider": "codex",
                    "unit": "bytes",
                    "inventory_source_count": 1,
                    "inventory_source_bytes": 1_000,
                    "tracked_source_count": 1,
                    "complete_source_count": 0,
                    "observed_units": 1_200,
                    "acknowledged_units": 600,
                    "remaining_units": 600,
                    "exact_total": False,
                    "inventory_coverage_complete": False,
                    "path": "/must/not/survive",
                },
                {
                    "provider": "opencode",
                    "unit": "records",
                    "inventory_source_count": 1,
                    "inventory_source_bytes": 5_000,
                    "tracked_source_count": 2,
                    "complete_source_count": 1,
                    "observed_units": 30,
                    "acknowledged_units": 27,
                    "remaining_units": 3,
                    "exact_total": False,
                    "inventory_coverage_complete": False,
                },
            ],
        },
    }

    parsed = HeartbeatIn.model_validate({"history_import": history_import})
    assert parsed.history_import is not None
    assert parsed.history_import.state == "importing"
    assert parsed.history_import.progress is not None
    assert parsed.history_import.progress.remaining_source_bytes == 600
    assert parsed.history_import.progress.remaining_records == 3
    encoded = parsed.history_import.model_dump(mode="json", exclude_none=True)
    assert "percent_complete" not in encoded["progress"]
    assert "path" not in encoded["progress"]["providers"][0]
    invalid_current = HeartbeatIn.model_validate({"history_import": {**history_import, "state": "current"}})
    assert invalid_current.history_import is not None
    assert invalid_current.history_import.state == "unavailable"

    current_progress = {
        **history_import["progress"],
        "acknowledged_source_bytes": 1_000,
        "remaining_source_bytes": 0,
        "acknowledged_records": 30,
        "remaining_records": 0,
        "pending_outbox_count": 0,
        "pending_outbox_bytes": 0,
        "providers": [
            {
                **history_import["progress"]["providers"][0],
                "observed_units": 1_000,
                "acknowledged_units": 1_000,
                "remaining_units": 0,
                "complete_source_count": 1,
                "exact_total": True,
                "inventory_coverage_complete": True,
            },
            {
                **history_import["progress"]["providers"][1],
                "acknowledged_units": 30,
                "remaining_units": 0,
                "complete_source_count": 2,
                "inventory_coverage_complete": True,
            },
        ],
    }
    accepted_current = HeartbeatIn.model_validate(
        {
            "history_import": {
                **history_import,
                "state": "current",
                "progress": current_progress,
            }
        }
    )
    assert accepted_current.history_import is not None
    assert accepted_current.history_import.state == "current"


# ---------------------------------------------------------------------------
# Typed machine evidence
# ---------------------------------------------------------------------------


def test_heartbeat_accepts_and_retains_typed_machine_evidence_without_reducing_it(live_catalog, live_catalog_client):
    evidence = _machine_evidence_payload()

    response = live_catalog_client.post(
        "/agents/heartbeat",
        headers=_headers(live_catalog),
        json={"version": "phase-2", "daemon_pid": 42, "machine_evidence": evidence},
    )
    assert response.status_code == 204, response.text

    retained = json.loads(_one_stamp()["raw_json"])["machine_evidence"]
    assert retained["schema_version"] == 1
    assert {fact["provider"] for fact in retained["process"]} == {
        "codex",
        "claude",
        "opencode",
        "cursor",
        "antigravity",
    }
    assert retained["process"][0]["process_start_time"] == "Thu May  8 11:59:00 2026"
    # Typed control evidence is validation-only. It must not silently become a
    # second lifecycle/control reducer.
    assert _leases() == []


def test_heartbeat_accepts_reducer_grade_identity_without_promoting_authority(live_catalog, live_catalog_client):
    evidence = _machine_evidence_payload()
    evidence["schema_version"] = 2
    process = evidence["process"]
    assert isinstance(process, list)
    process[0]["observed_at"] = "2026-05-08T12:00:00+00:00"
    evidence["identities"] = [_process_identity(evidence)]

    response = live_catalog_client.post(
        "/agents/heartbeat",
        headers=_headers(live_catalog),
        json={"version": "phase-2.5", "daemon_pid": 42, "machine_evidence": evidence},
    )
    assert response.status_code == 204, response.text

    retained = json.loads(_one_stamp()["raw_json"])["machine_evidence"]
    assert retained["schema_version"] == 2
    assert retained["identities"][0]["subject_key"].startswith("process:")
    assert len(validate_machine_evidence_identities(retained)) == 1
    assert _leases() == []


def test_heartbeat_machine_evidence_rejects_invalid_and_unbounded_claims(live_catalog, live_catalog_client):
    headers = _headers(live_catalog)
    invalid_evidence = []

    unknown_schema = _machine_evidence_payload()
    unknown_schema["schema_version"] = 4
    invalid_evidence.append(unknown_schema)

    mismatched_identity = _machine_evidence_payload()
    mismatched_identity["schema_version"] = 2
    mismatched_identity["identities"] = [
        {
            "fact_family": "process",
            "fact_index": 0,
            "subject_key": "process:codex:boot:101:start",
            "source": "wrong_source",
            "sequenced": False,
            "dedupe_key": "a" * 64,
            "evidence_hash": "b" * 64,
        }
    ]
    invalid_evidence.append(mismatched_identity)

    forged_hash = _machine_evidence_payload()
    forged_hash["schema_version"] = 2
    forged_identity = _process_identity(forged_hash)
    forged_identity["evidence_hash"] = "b" * 64
    forged_hash["identities"] = [forged_identity]
    invalid_evidence.append(forged_hash)

    mismatched_subject = _machine_evidence_payload()
    mismatched_subject["schema_version"] = 2
    subject_identity = _process_identity(mismatched_subject)
    subject_identity["subject_key"] = "process:" + "0" * 64 + ":codex:" + "0" * 64 + ":100:" + "0" * 64
    mismatched_subject["identities"] = [subject_identity]
    invalid_evidence.append(mismatched_subject)

    invalid_pid = _machine_evidence_payload()
    process = invalid_pid["process"]
    assert isinstance(process, list)
    process[0]["pid"] = 0
    invalid_evidence.append(invalid_pid)

    # An Antigravity control claim used to be rejected here because the
    # Machine Agent had no scanner that could make one. It has since
    # 2026-08-20: the Helm launcher seeds a control identity and the hook
    # scanner emits the fact. What must still never happen is a Shadow
    # session producing one, and that is guarded where it belongs -- in the
    # engine, by a_shadow_antigravity_session_produces_no_control_fact_at_all.

    oversized = _machine_evidence_payload()
    oversized["process"] = [process[1]] * 2_049
    invalid_evidence.append(oversized)

    for evidence in invalid_evidence:
        response = live_catalog_client.post(
            "/agents/heartbeat",
            headers=headers,
            json={"version": "phase-2", "daemon_pid": 42, "machine_evidence": evidence},
        )
        assert response.status_code == 422

    # A rejected payload never reaches the catalog.
    assert _stamps() == []


def test_heartbeat_rejects_null_resolved_sessions(live_catalog, live_catalog_client):
    response = live_catalog_client.post(
        "/agents/heartbeat",
        headers=_headers(live_catalog),
        json={
            "version": "0.7.0",
            "daemon_pid": 42,
            "sessions": None,
        },
    )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# The catalogd call itself
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_catalog_heartbeat_uses_one_rpc_without_opening_sqlite(monkeypatch):
    """The whole route is one ``machine.heartbeat.apply.v2`` over a null session."""

    import zerg.routers.heartbeat as heartbeat_router

    calls: list[tuple[str, dict, float]] = []

    class CatalogClient:
        async def call(self, method, params, *, timeout_seconds):
            calls.append((method, params, timeout_seconds))
            return {"previous_sessions_digest": "digest-0", "commit_seq": "42", "exact_replay": False}

    class FakeRequest:
        client = SimpleNamespace(host="127.0.0.1")

        async def body(self):
            return b"{}"

    monkeypatch.setattr(heartbeat_router, "get_catalogd_client", lambda: CatalogClient())

    session_id = uuid4()
    payload = heartbeat_router.HeartbeatIn(
        version="catalog-test",
        sessions_digest="digest-1",
        sessions_sequence=8,
        managed_sessions=[
            heartbeat_router.ManagedSessionLeaseIn(
                session_id=session_id,
                provider="codex",
                machine_id="cinder",
                sequence=8,
                state="attached",
                phase="idle",
            )
        ],
    )
    response = await heartbeat_router.ingest_heartbeat(
        payload,
        FakeRequest(),
        None,
        SimpleNamespace(id="token-1", device_id="cinder", owner_id=7),
    )

    assert response.status_code == 204
    assert len(calls) == 1
    method, params, timeout = calls[0]
    assert method == "machine.heartbeat.apply.v2"
    assert timeout == heartbeat_router._HOT_HEARTBEAT_QUEUE_TIMEOUT_SECONDS
    assert params["heartbeat"]["device_id"] == "cinder"
    assert params["heartbeat"]["sessions_digest"] == "digest-1"
    assert params["managed_leases_present"] is True
    assert params["managed_leases"][0]["session_id"] == str(session_id)
    assert params["owner_id"] == 7


# ---------------------------------------------------------------------------
# Managed control leases
# ---------------------------------------------------------------------------


def test_heartbeat_resolved_sessions_materialize_managed_control(live_catalog, live_catalog_client):
    session_id = uuid4()

    response = live_catalog_client.post(
        "/agents/heartbeat",
        headers=_headers(live_catalog),
        json={
            "version": "0.7.0",
            "daemon_pid": 42,
            "sessions": [_resolved_managed_session(session_id, phase="thinking")],
        },
    )
    assert response.status_code == 204, response.text

    leases = _leases()
    assert len(leases) == 1
    lease = leases[0]
    assert lease["session_id"] == str(session_id)
    assert lease["provider"] == "codex"
    assert lease["state"] == "attached"
    assert lease["device_id"] == DEVICE_ID
    assert lease["machine_id"] == DEVICE_ID
    assert _lease_payload(lease)["control_state"] == "online"

    raw = json.loads(_one_stamp()["raw_json"])
    assert raw["sessions"][0]["control_path"] == "managed"
    assert raw["sessions"][0]["process"]["process_start_time"] == "Mon May  5 11:20:00 2026"
    assert raw["sessions"][0]["process"]["boot_id"] == "macos:1777970400:0"
    assert raw["managed_sessions"] == []


def test_heartbeat_resolved_sessions_ignore_legacy_session_identity(live_catalog, live_catalog_client):
    resolved_session_id = uuid4()
    legacy_session_id = uuid4()

    response = live_catalog_client.post(
        "/agents/heartbeat",
        headers=_headers(live_catalog),
        json={
            "version": "0.7.0",
            "daemon_pid": 42,
            "sessions": [_resolved_managed_session(resolved_session_id)],
            "managed_sessions": [_legacy_lease(legacy_session_id, provider="claude", machine_id="legacy-machine")],
        },
    )
    assert response.status_code == 204, response.text

    assert [(lease["session_id"], lease["provider"], lease["device_id"]) for lease in _leases()] == [
        (str(resolved_session_id), "codex", DEVICE_ID)
    ]


def test_heartbeat_resolved_managed_unknown_state_does_not_attach(live_catalog, live_catalog_client):
    session_id = uuid4()

    response = live_catalog_client.post(
        "/agents/heartbeat",
        headers=_headers(live_catalog),
        json={
            "version": "0.7.0",
            "daemon_pid": 42,
            "sessions": [_resolved_managed_session(session_id, state="future_state")],
        },
    )
    assert response.status_code == 204, response.text

    leases = _leases()
    assert len(leases) == 1
    assert leases[0]["state"] == "future_state"
    payload = _lease_payload(leases[0])
    assert payload["control_state"] == "unknown"
    assert payload["reason"] == "unknown_lease_state"


def test_heartbeat_legacy_managed_sessions_still_materialize_control(live_catalog, live_catalog_client):
    session_id = uuid4()

    response = live_catalog_client.post(
        "/agents/heartbeat",
        headers=_headers(live_catalog),
        json={
            "version": "0.6.0",
            "daemon_pid": 42,
            "managed_sessions": [_legacy_lease(session_id, provider="claude", machine_id=DEVICE_ID)],
        },
    )
    assert response.status_code == 204, response.text

    leases = _leases()
    assert len(leases) == 1
    assert leases[0]["session_id"] == str(session_id)
    assert leases[0]["provider"] == "claude"
    assert leases[0]["state"] == "attached"
    assert leases[0]["device_id"] == DEVICE_ID
    assert _lease_payload(leases[0])["control_state"] == "online"

    retained_lease = json.loads(_one_stamp()["raw_json"])["managed_sessions"][0]
    assert "phase" not in retained_lease
    assert "tool_name" not in retained_lease


def test_heartbeat_empty_resolved_sessions_detaches_missing_managed_control(live_catalog, live_catalog_client):
    session_id = uuid4()
    headers = _headers(live_catalog)
    _thread_id, run_id = _seed_open_run(session_id)

    attach = live_catalog_client.post(
        "/agents/heartbeat",
        headers=headers,
        json={"version": "0.7.0", "daemon_pid": 42, "sessions": [_resolved_managed_session(session_id)]},
    )
    assert attach.status_code == 204, attach.text
    assert _leases()[0]["state"] == "attached"

    empty = live_catalog_client.post(
        "/agents/heartbeat",
        headers=headers,
        json={"version": "0.7.0", "daemon_pid": 42, "sessions": []},
    )
    assert empty.status_code == 204, empty.text

    leases = _leases()
    assert len(leases) == 1
    assert leases[0]["state"] == "missing"
    payload = _lease_payload(leases[0])
    assert payload["control_state"] == "offline"
    assert payload["reason"] == "missing_from_snapshot"

    # Detaching control is not ending the run. A snapshot that omits a session
    # is evidence about the control path only -- the Machine Agent stopped
    # reporting it. Treating that silence as process exit is exactly the
    # "missing evidence invents a terminal state" failure, and it is what makes
    # a late phase signal look like a reopen. The run stays open, unjudged.
    runs = _catalog_rows(LiveSessionRun.__table__)
    assert [row["id"] for row in runs] == [str(run_id)]
    assert runs[0]["ended_at"] is None
    assert runs[0]["exit_status"] is None


def test_heartbeat_empty_resolved_sessions_does_not_detach_other_device_control(live_catalog, live_catalog_client):
    """An empty snapshot speaks only for the device that sent it."""

    mine = uuid4()
    theirs = uuid4()
    tokens = _enroll(live_catalog, DEVICE_ID, "other-device")

    for device_id, session_id in ((DEVICE_ID, mine), ("other-device", theirs)):
        attach = live_catalog_client.post(
            "/agents/heartbeat",
            headers=tokens[device_id],
            json={"version": "0.7.0", "daemon_pid": 42, "sessions": [_resolved_managed_session(session_id)]},
        )
        assert attach.status_code == 204, attach.text

    empty = live_catalog_client.post(
        "/agents/heartbeat",
        headers=tokens[DEVICE_ID],
        json={"version": "0.7.0", "daemon_pid": 42, "sessions": []},
    )
    assert empty.status_code == 204, empty.text

    assert {lease["device_id"]: lease["state"] for lease in _leases()} == {
        DEVICE_ID: "missing",
        "other-device": "attached",
    }


def test_heartbeat_missing_managed_detach_can_be_disabled(live_catalog, live_catalog_client, monkeypatch):
    session_id = uuid4()
    headers = _headers(live_catalog)

    attach = live_catalog_client.post(
        "/agents/heartbeat",
        headers=headers,
        json={"version": "0.7.0", "daemon_pid": 42, "sessions": [_resolved_managed_session(session_id)]},
    )
    assert attach.status_code == 204, attach.text

    monkeypatch.setenv("LONGHOUSE_DISABLE_MISSING_MANAGED_LEASE_DETACH", "1")
    empty = live_catalog_client.post(
        "/agents/heartbeat",
        headers=headers,
        json={"version": "0.7.0", "daemon_pid": 42, "sessions": []},
    )
    assert empty.status_code == 204, empty.text

    leases = _leases()
    assert len(leases) == 1
    assert leases[0]["state"] == "attached"
    assert leases[0]["device_id"] == DEVICE_ID
