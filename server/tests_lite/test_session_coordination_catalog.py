from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from uuid import UUID

import pytest

from zerg.services.session_coordination import project_storage_v2_wall

NOW = datetime(2026, 7, 13, 18, 0, tzinfo=timezone.utc)
SESSION_ID = "019f5bf4-6a4a-7c43-9459-15a2639ce680"
THREAD_ID = "019f5bf4-6a4a-7c43-9459-15a2639ce681"
RUN_ID = "019f5bf4-6a4a-7c43-9459-15a2639ce682"


def _facts(*, managed: bool = True) -> dict:
    facts = {
        "catalog": {
            "session_id": SESSION_ID,
            "provider": "codex",
            "environment": "production",
            "project": "longhouse",
            "device_id": "shipper-clifford",
            "device_name": None,
            "cwd": "/workspace/longhouse",
            "git_repo": "cipher982/longhouse",
            "git_branch": "main",
            "started_at": (NOW - timedelta(minutes=10)).isoformat(),
            "last_activity_at": (NOW - timedelta(seconds=15)).isoformat(),
            "user_messages": 1,
            "assistant_messages": 1,
            "tool_calls": 1,
            "summary_title": "Catalog wall",
        },
        "card": {
            "session_id": SESSION_ID,
            "provider": "codex",
            "environment": "production",
            "project": "longhouse",
            "device_id": "shipper-clifford",
            "cwd": "/workspace/longhouse",
            "started_at": (NOW - timedelta(minutes=10)).isoformat(),
            "last_activity_at": (NOW - timedelta(seconds=5)).isoformat(),
            "summary_title": "Storage-v2 wall",
            "user_messages": 4,
            "assistant_messages": 5,
            "tool_calls": 6,
            "archive_state": "current",
            "parser_revision": "test",
        },
        "runtime": {
            "runtime_key": "codex:test",
            "session_id": SESSION_ID,
            "provider": "codex",
            "device_id": "shipper-clifford",
            "phase": "needs_user",
            "phase_source": "semantic",
            "last_runtime_signal_at": (NOW - timedelta(seconds=5)).isoformat(),
            "last_live_at": (NOW - timedelta(seconds=5)).isoformat(),
            "timeline_anchor_at": (NOW - timedelta(seconds=5)).isoformat(),
            "freshness_expires_at": (NOW + timedelta(minutes=1)).isoformat(),
            "runtime_version": 2,
            "updated_at": (NOW - timedelta(seconds=5)).isoformat(),
        },
        "primary_thread": None,
        "latest_run": None,
        "connections": [],
    }
    if managed:
        facts.update(
            {
                "primary_thread": {
                    "id": THREAD_ID,
                    "session_id": SESSION_ID,
                    "provider": "codex",
                    "branch_kind": "root",
                    "is_primary": 1,
                    "created_at": (NOW - timedelta(minutes=10)).isoformat(),
                    "updated_at": NOW.isoformat(),
                },
                "latest_run": {
                    "id": RUN_ID,
                    "thread_id": THREAD_ID,
                    "provider": "codex",
                    "launch_origin": "longhouse_spawned",
                    "started_at": (NOW - timedelta(minutes=10)).isoformat(),
                },
                "connections": [
                    {
                        "id": 7,
                        "run_id": RUN_ID,
                        "control_plane": "codex_app_server",
                        "acquisition_kind": "spawned_control",
                        "state": "attached",
                        "can_send_input": 1,
                        "can_interrupt": 1,
                        "can_terminate": 1,
                        "can_tail_output": 1,
                        "can_resume": 1,
                        "acquired_at": (NOW - timedelta(minutes=10)).isoformat(),
                        "last_health_at": (NOW - timedelta(seconds=5)).isoformat(),
                    }
                ],
            }
        )
    return facts


def test_project_storage_v2_wall_uses_bounded_facts_only():
    snapshot = {
        "observed_at": NOW.isoformat(),
        "rows": [{"thread_id": THREAD_ID, "facts": _facts()}],
        "total": 1,
    }

    [item] = project_storage_v2_wall(
        snapshot,
        pending_counts={UUID(SESSION_ID): 3},
    )

    assert item.session_id == SESSION_ID
    assert item.device_name == "clifford"
    assert item.summary_title == "Storage-v2 wall"
    assert item.last_event_at == NOW - timedelta(seconds=5)
    assert item.last_user_message_at is None
    assert item.last_tool_call_at is None
    assert item.has_live_presence is True
    assert item.presence_state == "needs_user"
    assert item.kernel_control_label == "live"
    assert item.kernel_live_control_available is True
    assert item.kernel_host_reattach_available is True
    assert item.kernel_observe_only is False
    assert item.kernel_search_only is False
    assert item.kernel_staleness_reason is None
    assert item.pending_inbound_messages == 3
    assert (item.user_messages, item.assistant_messages, item.tool_calls) == (4, 5, 6)


def test_project_storage_v2_wall_preserves_imported_and_default_pending_count():
    facts = _facts(managed=False)
    facts["runtime"] = None

    [item] = project_storage_v2_wall(
        {
            "observed_at": NOW.isoformat(),
            "rows": [{"thread_id": SESSION_ID, "facts": facts}],
        }
    )

    assert item.has_live_presence is False
    assert item.presence_state is None
    assert item.kernel_control_label == "imported"
    assert item.kernel_live_control_available is False
    assert item.kernel_host_reattach_available is False
    assert item.kernel_observe_only is False
    assert item.kernel_search_only is True
    assert item.kernel_staleness_reason == "imported_only"
    assert item.pending_inbound_messages == 0


def test_project_storage_v2_wall_applies_repo_filter_and_limit_after_snapshot():
    row = {"thread_id": THREAD_ID, "facts": _facts()}
    snapshot = {"observed_at": NOW.isoformat(), "rows": [row, row]}

    assert project_storage_v2_wall(snapshot, repo="unrelated", limit=1) == []
    items = project_storage_v2_wall(snapshot, repo="LONGHOUSE", limit=1)

    assert len(items) == 1
    assert items[0].session_id == SESSION_ID


@pytest.mark.parametrize(
    "snapshot, message",
    [
        ({"rows": []}, "missing observed_at"),
        ({"observed_at": NOW.isoformat(), "rows": [{}]}, "row is missing facts"),
        (
            {"observed_at": NOW.isoformat(), "rows": [{"facts": {"catalog": None}}]},
            "facts are missing catalog",
        ),
    ],
)
def test_project_storage_v2_wall_rejects_incomplete_facts(snapshot, message):
    with pytest.raises(ValueError, match=message):
        project_storage_v2_wall(snapshot)
