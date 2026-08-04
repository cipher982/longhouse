from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone

from zerg.services.local_health.classifier import _classify_health
from zerg.services.local_health.classifier import _collect_health_reasons
from zerg.services.local_health.classifier import _collect_managed_launch_recovery
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


def test_legacy_storage_payload_does_not_infer_risk_from_latest_block_kind():
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

    assert context.storage_unresolved_blocked_sources == 0
    assert context.storage_block_proof_unknown is True


def test_legacy_safe_storage_payload_is_attention_not_false_green():
    context = _health_classification_context(
        service={"status": "running"},
        engine_status={
            "exists": True,
            "age_seconds": 1,
            "payload": {
                "storage_v2_outbox": {
                    "blocked_source_count": 2,
                    "latest_block_kind": "source_epoch_conflict",
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

    assert context.storage_block_proof_unknown is True
    assert _health_flags_for(unresolved=0) == (False, True)


def test_storage_reasons_use_a_stable_source_inspection_action_id():
    assert _suggested_action_ids(["storage_v2_sources_blocked", "storage_v2_sources_proof_unknown"]) == ["inspect_storage_source"]


def test_unknown_managed_and_dead_letter_reasons_have_explicit_actions():
    assert _suggested_action_ids(["managed_unknown_phase", "spool_dead"]) == [
        "inspect_managed_session",
        "inspect_shipping",
    ]


def test_orphaned_managed_bridge_uses_the_exact_stop_action():
    assert _suggested_action_ids(["orphaned_managed_bridge"]) == ["stop_managed_bridge"]


def test_storage_reasons_scope_source_inspection_to_latest_epoch():
    context = _health_classification_context(
        service={"status": "running"},
        engine_status={
            "exists": True,
            "age_seconds": 1,
            "payload": {
                "storage_v2_outbox": {
                    "blocked_source_count": 1,
                    "unresolved_blocked_source_count": 1,
                    "latest_block_source_epoch": "01234567-89ab-cdef-0123-456789abcdef",
                    "latest_unresolved_block_source_epoch": "fedcba98-7654-3210-fedc-ba9876543210",
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

    _reasons, actions = _collect_health_reasons(context, transport_assessment=None)

    assert any(
        "longhouse shipping inspect --source-epoch fedcba98-7654-3210-fedc-ba9876543210 --json" in action
        for action in actions
    )


def test_managed_launch_recovery_scan_surfaces_exhausted_intents(tmp_path):
    home = tmp_path / ".longhouse"
    retry_dir = home / "agent" / "managed-local" / "registration-retries"
    retry_dir.mkdir(parents=True)
    (retry_dir / "session.json").write_text('{"recovery_exhausted": true}')
    (retry_dir / "active.json").write_text('{"recovery_exhausted": false}')

    recovery = _collect_managed_launch_recovery(
        {"path": str(home / "agent" / "engine-status.json")}
    )

    assert recovery == {"exhausted_count": 1, "active_count": 1, "scan_error": False}


def test_managed_launch_recovery_scan_uses_default_agent_path_without_engine_status():
    recovery = _collect_managed_launch_recovery({})

    assert set(recovery) == {"exhausted_count", "active_count", "scan_error"}


def test_outcome_receipts_only_surface_after_exhaustion(tmp_path):
    home = tmp_path / ".longhouse"
    retry_dir = home / "agent" / "managed-local" / "outcome-retries"
    retry_dir.mkdir(parents=True)
    pending = retry_dir / "pending.json"
    created_at = (datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat()
    pending.write_text(f'{{"recovery_exhausted": false, "created_at": "{created_at}"}}')
    # A fresh outcome receipt is normal post-launch convergence and should not
    # make every healthy launch amber. An older receipt is active recovery.
    (retry_dir / "exhausted.json").write_text('{"recovery_exhausted": true}')

    recovery = _collect_managed_launch_recovery(
        {"path": str(home / "agent" / "engine-status.json")}
    )

    assert recovery == {"exhausted_count": 1, "active_count": 1, "scan_error": False}


def test_exhausted_managed_launch_recovery_is_broken_and_actionable(tmp_path):
    context = _health_classification_context(
        service={"status": "running"},
        engine_status={
            "exists": True,
            "age_seconds": 1,
            "path": str(tmp_path / ".longhouse" / "agent" / "engine-status.json"),
            "payload": {},
        },
        transport_sample=None,
        outbox={"file_count": 0},
        launch_readiness={"state": "ready", "reasons": [], "suggested_actions": []},
        archive_repair={},
        managed_summary={},
        managed_sessions=[],
        managed_launch_recovery={"exhausted_count": 1, "scan_error": False},
    )
    reasons, _actions = _collect_health_reasons(context, transport_assessment=None)

    assert "managed_launch_recovery_exhausted" in reasons
    assert _health_flags(
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
        storage_blocked_sources=0,
        storage_unresolved_blocked_sources=0,
        storage_outbox_error=None,
        orphan_bridge_count=0,
        managed_degraded=0,
        managed_detached=0,
        unknown_managed_phase_count=0,
        canonical_sessions_missing=False,
        canonical_sessions_invalid=False,
        managed_recovery_exhausted_count=context.managed_recovery_exhausted_count,
        managed_recovery_scan_error=context.managed_recovery_scan_error,
    ) == (True, False)


def test_active_managed_launch_recovery_is_amber_and_actionable():
    context = _health_classification_context(
        service={"status": "running"},
        engine_status={"exists": True, "age_seconds": 1, "payload": {}},
        transport_sample=None,
        outbox={"file_count": 0},
        launch_readiness={"state": "ready", "reasons": [], "suggested_actions": []},
        archive_repair={},
        managed_summary={},
        managed_sessions=[],
        managed_launch_recovery={"exhausted_count": 0, "active_count": 1, "scan_error": False},
    )
    reasons, actions = _collect_health_reasons(context, transport_assessment=None)

    assert "managed_launch_recovery_active" in reasons
    assert "inspect_managed_session" in _suggested_action_ids(reasons)
    assert any("Runtime Host launch recovery continues" in action for action in actions)
    classification = _classify_health(
        service={"status": "running"},
        engine_status={"exists": True, "age_seconds": 1, "payload": {}},
        transport_sample=None,
        transport_assessment=None,
        outbox={"file_count": 0},
        launch_readiness={"state": "ready", "reasons": [], "suggested_actions": []},
        archive_repair={},
        managed_summary={},
        managed_sessions=[],
        managed_launch_recovery={"exhausted_count": 0, "active_count": 1, "scan_error": False},
    )
    assert classification[:2] == ("degraded", "yellow")
    assert _health_flags(
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
        storage_blocked_sources=0,
        storage_unresolved_blocked_sources=0,
        storage_outbox_error=None,
        orphan_bridge_count=0,
        managed_degraded=0,
        managed_detached=0,
        unknown_managed_phase_count=0,
        canonical_sessions_missing=False,
        canonical_sessions_invalid=False,
        managed_recovery_exhausted_count=0,
        managed_recovery_active_count=1,
        managed_recovery_scan_error=False,
    ) == (False, True)
