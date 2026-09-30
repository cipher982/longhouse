from __future__ import annotations

import json

from zerg.qa.background_fidelity import combine_real_replay_results
from zerg.qa.omp_background_producer import omp_background_assertions


def test_child_navigation_gap_is_blocked_not_provider_pass() -> None:
    result = combine_real_replay_results(
        parser_result={
            "status": "pass",
            "assertions": {"engine_facts_produced": True},
        },
        catalog_result={
            "status": "blocked",
            "failure_code": "background_child_navigation_unproven",
        },
    )

    assert result["status"] == "blocked"
    assert result["failure_code"] == "background_child_navigation_unproven"
    assert result["assertions"]["catalog_served_path_passed"] is False


def test_parser_crash_or_digest_failure_remains_failure() -> None:
    result = combine_real_replay_results(
        parser_result={
            "status": "fail",
            "failure_code": "background_engine_parse_failed",
            "assertions": {"engine_facts_produced": False},
        },
        catalog_result={"status": "blocked", "failure_code": "background_registry_missing"},
    )

    assert result["status"] == "fail"
    assert result["failure_code"] == "background_engine_parse_failed"
    assert result["operation_evidence"]["background_fidelity"]["status"] == "fail"


def test_omp_background_assertions_require_terminal_job_status(tmp_path) -> None:
    frame = {
        "session_id": "session-1",
        "run_id": "run-1",
        "native_session_id": "native-1",
        "session_file": "/tmp/session-1.jsonl",
        "connection_id": "connection-1",
        "lease_generation": "lease-1",
        "event": {
            "async_running_complete": True,
            "async_jobs": [{"id": "job-1", "status": "running"}],
            "task_progress": [{"agent_id": "agent-1", "job_id": "job-1", "status": "running"}],
        },
    }
    capture = tmp_path / "extension.jsonl"
    capture.write_text(json.dumps(frame) + "\n", encoding="utf-8")
    served_receipt = tmp_path / "omp-background-served-receipt.json"
    served_receipt.write_text(
        json.dumps(
            {
                "managed_transport": "omp_helm_channel",
                "state": {
                    "provider": "omp",
                    "machine_name": "machine-1",
                    "session_id": "session-1",
                    "run_id": "run-1",
                    "native_session_id": "native-1",
                    "session_file": "/tmp/session-1.jsonl",
                    "connection_id": "connection-1",
                    "lease_generation": "lease-1",
                },
                "control_identity": {
                    "session_id": "session-1",
                    "expected_subject_key": "run:run-1",
                    "control_subject_key": "run:run-1",
                    "served_path": "canonical_session_detail",
                    "owner_identity": ["session-1", "native-1", "/tmp/session-1.jsonl", 10, "launcher-birth", 11, "provider-birth"],
                    "actions": {"send_input": "available", "interrupt": "available", "terminate": "available"},
                },
                "detail": {"id": "session-1", "session_state": {"delegation": {"count": 1, "recent_items": []}}},
            }
        ),
        encoding="utf-8",
    )
    result = {"provider": "omp"}

    assertions = omp_background_assertions(tmp_path, result)

    assert assertions["omp_background_registry_owner_scoped"] is True
    assert assertions["omp_background_partial_progress_child_scoped"] is True
    assert assertions["omp_background_terminal_status_preserved"] is False

    frame["event"]["task_progress"][0]["status"] = "completed"
    capture.write_text(json.dumps(frame) + "\n", encoding="utf-8")
    assert omp_background_assertions(tmp_path, result)["omp_background_terminal_status_preserved"] is False
    receipt = json.loads(served_receipt.read_text())
    receipt["detail"]["session_state"]["delegation"]["recent_items"] = [{"id": "job-1", "kind": "subagent", "status": "completed"}]
    served_receipt.write_text(json.dumps(receipt), encoding="utf-8")
    assert omp_background_assertions(tmp_path, result)["omp_background_terminal_status_preserved"] is True
    frame["event"]["task_progress"][0]["status"] = "failed"
    capture.write_text(json.dumps(frame) + "\n", encoding="utf-8")
    assert omp_background_assertions(tmp_path, result)["omp_background_terminal_status_preserved"] is False
    receipt["detail"]["session_state"]["delegation"]["recent_items"][0]["status"] = "failed"
    served_receipt.write_text(json.dumps(receipt), encoding="utf-8")
    assert omp_background_assertions(tmp_path, result)["omp_background_terminal_status_preserved"] is True


def test_omp_background_harness_identity_without_managed_receipt_is_not_proof(tmp_path) -> None:
    frame = {
        "session_id": "harness-session",
        "run_id": "harness-run",
        "native_session_id": "harness-native",
        "session_file": "/tmp/harness.jsonl",
        "connection_id": "harness-connection",
        "lease_generation": "harness-lease",
        "event": {
            "async_running_complete": True,
            "async_jobs": [{"id": "job-1", "status": "running"}],
            "task_progress": [{"agent_id": "agent-1", "job_id": "job-1", "status": "completed"}],
        },
    }
    (tmp_path / "capture.ndjson").write_text(json.dumps(frame) + "\n", encoding="utf-8")

    assertions = omp_background_assertions(tmp_path, {"provider": "omp"})

    assert assertions == {
        "omp_background_registry_owner_scoped": False,
        "omp_background_partial_progress_child_scoped": False,
        "omp_background_terminal_status_preserved": False,
    }
