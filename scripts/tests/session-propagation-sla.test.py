#!/usr/bin/env python3
"""Deterministic checks for the session-promotion qualification boundary."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OPS = ROOT / "scripts" / "ops"
sys.path.insert(0, str(OPS))

spec = importlib.util.spec_from_file_location(
    "profile_managed_session_propagation",
    OPS / "profile-managed-session-propagation.py",
)
if spec is None or spec.loader is None:
    raise RuntimeError("could not load managed-session profiler")
profiler = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = profiler
spec.loader.exec_module(profiler)


def catalog(*, hidden: int, user: int = 0, assistant: int = 0, tools: int = 0) -> dict:
    return {
        "live_session_catalog": {
            "session_id": "session-1",
            "hidden_from_default_timeline": hidden,
            "user_messages": user,
            "assistant_messages": assistant,
            "tool_calls": tools,
        }
    }


def storage(*, hidden: int, user: int = 0, assistant: int = 0, tools: int = 0) -> dict:
    return {
        "storage_session": {
            "session_id": "session-1",
            "hidden_from_default_timeline": hidden,
            "user_messages": user,
            "assistant_messages": assistant,
            "tool_calls": tools,
        }
    }


def test_empty_shell_and_promotion_boundary() -> None:
    empty = catalog(hidden=1)
    assert profiler.hosted_empty_shell(empty)
    assert not profiler.hosted_content_published(empty)

    user_content = catalog(hidden=0, user=1)
    assert profiler.hosted_content_published(user_content)
    assert not profiler.hosted_empty_shell(user_content)

    assistant_content = catalog(hidden=0, assistant=1)
    assert profiler.hosted_content_published(assistant_content)

    tool_content = catalog(hidden=0, tools=1)
    assert profiler.hosted_content_published(tool_content)

    still_hidden = catalog(hidden=1, user=1, assistant=1)
    assert not profiler.hosted_content_published(still_hidden)

    served_storage = {**catalog(hidden=1), **storage(hidden=0, assistant=1)}
    assert profiler.hosted_content_published(served_storage)
    assert profiler.hosted_content_counts(served_storage)["assistant_messages"] == 1
    assert profiler.hosted_served_row(served_storage)[0] == "sessions"


def test_empty_projection_proof_and_failed_empty_launch() -> None:
    proof = profiler.empty_shell_projection_proof(
        catalog(hidden=1),
        {"listing_status": 200, "matches": []},
    )
    assert proof["proven"] is True
    assert proof["default_listing_contains_session"] is False

    failed = {**catalog(hidden=1), "session": {"ended_at": "2026-08-02T00:00:00+00:00"}}
    assert profiler.lifecycle_closed(failed)
    assert not profiler.hosted_content_published(failed)

    visible = profiler.empty_shell_projection_proof(
        catalog(hidden=0, assistant=1),
        {"listing_status": 200, "matches": [{"thread_id": "session-1"}]},
    )
    assert visible["proven"] is False
    assert visible["default_listing_contains_session"] is True

    visible_storage = profiler.empty_shell_projection_proof(
        {**catalog(hidden=1), **storage(hidden=0, assistant=1)},
        {"listing_status": 200, "matches": [{"thread_id": "session-1"}]},
    )
    assert visible_storage["catalog_source"] == "sessions"
    assert visible_storage["default_listing_contains_session"] is True

    promotion = profiler.content_promotion_projection_proof(
        {**catalog(hidden=1), **storage(hidden=0, assistant=1)},
        {"listing_status": 200, "matches": [{"thread_id": "session-1"}]},
    )
    assert promotion["proven"] is True


def test_hosted_assistant_proof_accepts_storage_count_when_preview_is_replaced() -> None:
    data = {
        "storage_session": {
            "first_user_message_preview": "Reply with exactly LH_PROBE",
            "last_visible_text_preview": "provider shell text",
            "user_messages": 1,
            "assistant_messages": 1,
        }
    }
    assert profiler.hosted_assistant_events_contain(data, "LH_PROBE") is True

    data["storage_session"]["assistant_messages"] = 0
    assert profiler.hosted_assistant_events_contain(data, "LH_PROBE") is False


def test_promotion_delta_rejects_out_of_order_observation() -> None:
    instance = profiler.Profiler.__new__(profiler.Profiler)
    instance.observations = [
        {
            "case_id": "B1",
            "session_id": "session-1",
            "event": "empty_shell_observed",
            "observed_at_monotonic_ms": 100,
            "payload": {},
        },
        {
            "case_id": "B1",
            "session_id": "session-1",
            "event": "content_durable_published",
            "observed_at_monotonic_ms": 400,
            "payload": {"observation_interval_ms": 500},
        },
        {
            "case_id": "B1",
            "session_id": "session-1",
            "event": "browser_timeline_card_painted",
            "observed_at_monotonic_ms": 850,
            "payload": {},
        },
    ]
    assert instance.event_delta_any_order_ms(
        "B1", "session-1", "content_durable_published", "browser_timeline_card_painted"
    ) == 450
    assert instance.event_payload_int(
        "B1", "session-1", "content_durable_published", "observation_interval_ms"
    ) == 500

    instance.observations[-1]["observed_at_monotonic_ms"] = 300
    raw = instance.event_delta_any_order_ms(
        "B1", "session-1", "content_durable_published", "browser_timeline_card_painted"
    )
    assert raw == -100
    assert profiler.valid_monotonic_delta_ms(raw) is None
    assert profiler.valid_monotonic_delta_ms(0) == 0


def test_promotion_uses_requested_managed_ownership_when_archive_omits_it() -> None:
    assert profiler.qualification_ownership({}, "managed") == "managed"
    assert (
        profiler.qualification_ownership({"execution_home": "remote_runner"}, "managed")
        == "remote_runner"
    )


def test_cursor_stop_accepts_already_detached_session() -> None:
    result = profiler.CommandResult(
        cmd=["longhouse-engine", "cursor-helm", "stop"],
        returncode=1,
        stdout="",
        stderr="Error: session_not_attached: cursor helm state file not found",
    )
    assert profiler.cursor_helm_stop_already_complete(result)

    result.stderr = "Error: command_failed: coordination channel rejected stop"
    assert not profiler.cursor_helm_stop_already_complete(result)


def test_batch_preflight_accepts_current_healthy_transport_schema() -> None:
    assert profiler.local_transport_is_currently_healthy(
        {
            "health_state": "degraded",
            "reasons": ["engine_evidence_stale"],
            "transport": {"status": "healthy"},
            "spool": {"pending_count": 0},
        }
    )
    assert not profiler.local_transport_is_currently_healthy(
        {
            "transport": {"status": "degraded"},
            "spool": {"pending_count": 0},
        }
    )


def test_manifest_moves_legacy_metric_out_of_hard_targeting() -> None:
    manifest = profiler.sla_manifest()
    assert profiler.metric_is_diagnostic(manifest, "warm_session_created_to_card_paint_ms")
    assert profiler.target_for_metric("warm_session_created_to_card_paint_ms") is None
    assert profiler.target_for_metric("content_durable_to_timeline_card_paint_ms") == 500
    case = profiler.case_by_id(manifest, "managed_codex_created_session_card_promotion")
    assert case is not None
    assert case["metrics"] == ["content_durable_to_timeline_card_paint_ms"]


def test_batch_clean_metrics_exclude_classified_failures() -> None:
    cases = [
        {
            "verdict": "pass",
            "failure_classification": None,
            "provider_timeout": False,
            "content_durable_to_timeline_card_paint_ms": 100,
        },
        {
            "verdict": "slow",
            "failure_classification": None,
            "provider_timeout": False,
            "content_durable_to_timeline_card_paint_ms": 300,
        },
        {
            "verdict": "pass",
            "failure_classification": "hosted_transport_degraded",
            "provider_timeout": False,
            "content_durable_to_timeline_card_paint_ms": 12_000,
        },
        {
            "verdict": "provider_timeout",
            "failure_classification": None,
            "provider_timeout": True,
            "content_durable_to_timeline_card_paint_ms": 200,
        },
    ]
    aggregate = profiler.aggregate_batch_cases(cases)
    assert aggregate["clean_observation_count"] == 2
    clean = aggregate["clean_metrics"]["content_durable_to_timeline_card_paint_ms"]
    assert clean["count"] == 2
    assert clean["p50"] == 100
    assert clean["p95"] == 300


def test_http_protocol_browser_error_is_transport_contamination() -> None:
    instance = profiler.Profiler.__new__(profiler.Profiler)
    instance.observations = [
        {
            "case_id": "D1",
            "session_id": "session-1",
            "event": "browser_ui_console",
            "source": "browser_ui",
            "payload": {"text": "Failed to load resource: net::ERR_HTTP2_PROTOCOL_ERROR"},
        }
    ]
    assert instance.transport_failure_classification("D1", "session-1", None) == "hosted_transport_degraded"


if __name__ == "__main__":
    for test in (
        test_empty_shell_and_promotion_boundary,
        test_empty_projection_proof_and_failed_empty_launch,
        test_promotion_delta_rejects_out_of_order_observation,
        test_manifest_moves_legacy_metric_out_of_hard_targeting,
        test_batch_clean_metrics_exclude_classified_failures,
        test_http_protocol_browser_error_is_transport_contamination,
    ):
        test()
    print("session propagation SLA tests passed")
