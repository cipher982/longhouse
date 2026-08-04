from __future__ import annotations

from zerg.services.local_health.classifier import _health_classification_context
from zerg.services.local_health.classifier import _health_flags
from zerg.services.local_health.classifier import _suggested_action_ids


def _health_flags_for(*, unresolved: int) -> tuple[bool, bool]:
    return _health_flags(
        launch_state="ready",
        service_status="running",
        engine_error=None,
        engine_exists=True,
        engine_age=1,
        transport_assessment=None,
        disk_free_bytes=None,
        outbox_count=0,
        outbox_oldest=None,
        spool_pending=0,
        archive_pending_ranges=0,
        archive_pending_bytes=0,
        archive_state="idle",
        archive_mode="idle",
        archive_dead_ranges=0,
        archive_dead_bytes=0,
        storage_blocked_sources=2,
        storage_unresolved_blocked_sources=unresolved,
        storage_outbox_error=None,
        orphan_bridge_count=0,
        managed_degraded=0,
        managed_detached=0,
        unknown_managed_phase_count=0,
        canonical_sessions_missing=False,
        canonical_sessions_invalid=False,
    )


def test_reconcilable_storage_sources_are_degraded_not_broken():
    assert _health_flags_for(unresolved=0) == (False, True)


def test_unresolved_storage_sources_promote_repair_severity():
    assert _health_flags_for(unresolved=1) == (True, True)


def test_legacy_storage_payload_falls_back_to_latest_block_kind():
    context = _health_classification_context(
        service={"status": "running"},
        engine_status={
            "exists": True,
            "age_seconds": 1,
            "payload": {
                "storage_v2_outbox": {
                    "blocked_source_count": 1,
                    "latest_block_kind": "source_epoch_conflict_unresolved",
                }
            },
        },
        transport_sample=None,
        outbox={"file_count": 0},
        launch_readiness={"state": "ready", "reasons": [], "suggested_actions": []},
        archive_repair={},
        managed_summary={},
        managed_sessions=[],
    )

    assert context.storage_unresolved_blocked_sources == 1


def test_storage_reasons_use_a_stable_source_inspection_action_id():
    assert _suggested_action_ids(
        ["storage_v2_sources_blocked", "storage_v2_sources_unresolved"]
    ) == ["inspect_storage_source"]
