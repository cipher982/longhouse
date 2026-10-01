"""Failure conformance for the Claude background live oracle.

These tests exercise the oracle's evidence predicates and final verdicts with
captured records/results.  They do not launch Claude or call a provider.
"""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path
from typing import Any

from zerg.qa import claude_background_producer as oracle


def _record(event: str, payload: dict[str, Any], *, session_id: str = "native-1", digest_ok: bool = True) -> dict[str, Any]:
    return {
        "path": f"{event}.stdin",
        "metadata_path": f"{event}.stdin.meta.json",
        "payload": payload,
        "event": event,
        "native_session_id": session_id,
        "bytes": 10,
        "sha256": "sha256:captured",
        "metadata": {"sha256": "sha256:captured"},
        "metadata_digest_matches": digest_ok,
        "captured_at": "2026-09-30T12:00:00+00:00",
        "captured_mtime_ns": 1,
    }


def _native_records(*, stop_callbacks: bool = True, digest_ok: bool = True, session_id: str = "native-1") -> list[dict[str, Any]]:
    records = [
        _record("SubagentStart", {"agent_id": "agent-1"}, session_id=session_id, digest_ok=digest_ok),
        _record(
            "Stop",
            {
                "background_tasks": [
                    {"id": "agent-1", "type": "subagent", "status": "running"},
                    {"id": "shell-1", "type": "shell", "status": "running"},
                ]
            },
            session_id=session_id,
            digest_ok=digest_ok,
        ),
    ]
    if stop_callbacks:
        records.append(_record("SubagentStop", {"agent_id": "agent-1"}, session_id=session_id, digest_ok=digest_ok))
    records.append(_record("Stop", {"background_tasks": []}, session_id=session_id, digest_ok=digest_ok))
    return records


def test_source_requires_authoritative_identity_digests_callbacks_and_empty_stop() -> None:
    source = oracle._source_observation(_native_records(), "native-1")

    assert source["identity_ok"] is True
    assert source["digests_ok"] is True
    assert source["active_registry_observed"] is True
    assert source["active_registry_kinds"] == ["shell", "subagent"]
    assert source["native_callbacks_matched"] is True
    assert source["explicit_empty_observed"] is True


def test_source_rejects_unmatched_native_callback_even_with_a_registry() -> None:
    source = oracle._source_observation(_native_records(stop_callbacks=False), "native-1")

    assert source["active_registry_observed"] is True
    assert source["native_callbacks_available"] is True
    assert source["native_callbacks_matched"] is False


def test_source_rejects_identity_or_digest_drift() -> None:
    source = oracle._source_observation(_native_records(digest_ok=False, session_id="other-native"), "native-1")

    assert source["identity_ok"] is False
    assert source["digests_ok"] is False
    assert source["active_registry_observed"] is True


def _scenario(*, target: bool = False, native_source: bool = True, explicit_empty: bool = True) -> dict[str, Any]:
    assertions = {name: True for name in oracle.ASSERTIONS}
    assertions["claude_background_registry_served"] = target
    assertions["claude_background_native_source"] = native_source
    assertions["claude_background_explicit_empty"] = explicit_empty
    return {
        "status": "pass" if target and native_source and explicit_empty else "fail",
        "source": {"active_registry_observed": True, "explicit_empty_observed": explicit_empty},
        "parent_turn": {"assistant_marker_archived": True},
        "assertions": assertions,
    }


def _lifecycle_result(tmp_path: Path, *, scenario: dict[str, Any], cleanup_ok: bool = True, status: str = "fail") -> dict[str, Any]:
    return {
        "status": status,
        "session_id": "managed-1",
        "claude_helm_process_exited": cleanup_ok,
        "observation": {
            "claude_helm_process_exited": cleanup_ok,
            "lifecycle": {
                "launch_registration": {"passed": True},
                "scenario": scenario,
            },
        },
    }


def _run(monkeypatch: Any, tmp_path: Path, result: dict[str, Any], *, receipt: dict[str, Any] | None = None) -> dict[str, Any]:
    monkeypatch.setattr(oracle.helm, "run_lifecycle", lambda *_args, **_kwargs: result)
    if receipt is not None:
        (tmp_path / "qa-fault-receipt.jsonl").write_text(json.dumps(receipt) + "\n", encoding="utf-8")
    return oracle._run_direct(
        Namespace(
            evidence_root=tmp_path,
            variant=None,
            negative_control=oracle.FAULT,
        )
    )


def test_negative_control_requires_exact_receipt_and_healthy_unrelated_paths(monkeypatch: Any, tmp_path: Path) -> None:
    result = _run(
        monkeypatch,
        tmp_path,
        _lifecycle_result(tmp_path, scenario=_scenario(target=False)),
        receipt={"schema_version": 1, "fault": oracle.FAULT, "session_id": "managed-1", "fired_at": "now", "detail": {}},
    )

    assert result["status"] == "pass"
    assert result["negative_control"]["fault_fired"] is True
    assert result["negative_control"]["target_rejected"] is True
    assert result["negative_control"]["observed_code"] == "background_registry_missing"
    assert result["negative_control"]["expected_code"] == "background_registry_missing"
    assert result["negative_control"]["preconditions_held"] is True


def test_negative_control_without_receipt_is_inconclusive_not_pass(monkeypatch: Any, tmp_path: Path) -> None:
    result = _run(monkeypatch, tmp_path, _lifecycle_result(tmp_path, scenario=_scenario(target=False)))

    assert result["status"] == "inconclusive"
    assert result["failure_code"] == "claude_background_negative_control_inconclusive"


def test_negative_control_with_wrong_receipt_identity_is_inconclusive(monkeypatch: Any, tmp_path: Path) -> None:
    result = _run(
        monkeypatch,
        tmp_path,
        _lifecycle_result(tmp_path, scenario=_scenario(target=False)),
        receipt={"schema_version": 1, "fault": oracle.FAULT, "session_id": "other-session", "fired_at": "now", "detail": {}},
    )

    assert result["status"] == "inconclusive"
    assert result["negative_control"]["fault_fired"] is False


def test_negative_control_without_raw_empty_source_is_inconclusive(monkeypatch: Any, tmp_path: Path) -> None:
    result = _run(
        monkeypatch,
        tmp_path,
        _lifecycle_result(tmp_path, scenario=_scenario(target=False, explicit_empty=False)),
        receipt={"schema_version": 1, "fault": oracle.FAULT, "session_id": "managed-1", "fired_at": "now", "detail": {}},
    )

    assert result["status"] == "inconclusive"
    assert result["negative_control"]["preconditions_held"] is False


def test_negative_control_without_native_identity_is_inconclusive(monkeypatch: Any, tmp_path: Path) -> None:
    result = _run(
        monkeypatch,
        tmp_path,
        _lifecycle_result(tmp_path, scenario=_scenario(target=False, native_source=False)),
        receipt={"schema_version": 1, "fault": oracle.FAULT, "session_id": "managed-1", "fired_at": "now", "detail": {}},
    )

    assert result["status"] == "inconclusive"
    assert result["negative_control"]["preconditions_held"] is False


def test_negative_control_cleanup_failure_is_inconclusive(monkeypatch: Any, tmp_path: Path) -> None:
    result = _run(
        monkeypatch,
        tmp_path,
        _lifecycle_result(tmp_path, scenario=_scenario(target=False), cleanup_ok=False),
        receipt={"schema_version": 1, "fault": oracle.FAULT, "session_id": "managed-1", "fired_at": "now", "detail": {}},
    )

    assert result["status"] == "inconclusive"
    assert result["failure_code"] == "claude_background_negative_control_inconclusive"
