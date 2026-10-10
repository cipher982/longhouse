"""Failure conformance for the Claude background live oracle.

These tests exercise the oracle's evidence predicates and final verdicts with
captured records/results.  They do not launch Claude or call a provider.
"""

from __future__ import annotations

import json
from argparse import Namespace
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from tests_lite._factory_envelope import assert_result_conforms
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


def test_stop_only_nonregistry_callbacks_do_not_invalidate_the_parent_task_pair() -> None:
    records = _native_records()
    records.insert(2, _record("SubagentStop", {"agent_id": "native-stop-only-actor", "agent_type": ""}))

    source = oracle._source_observation(records, "native-1")

    assert source["native_callbacks_matched"] is True
    assert source["active_registry_kinds"] == ["shell", "subagent"]


def test_callback_pairs_for_another_actor_cannot_certify_the_parent_registry_task() -> None:
    records = _native_records()
    records[1]["payload"]["background_tasks"][0]["id"] = "unobserved-parent-task"

    source = oracle._source_observation(records, "native-1")

    assert source["active_registry_observed"] is True
    assert source["native_callbacks_matched"] is False


def test_stop_only_parent_registry_task_cannot_certify_a_missing_start() -> None:
    records = _native_records()[1:]

    source = oracle._source_observation(records, "native-1")

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


def _capture_scenario(
    monkeypatch: Any,
    tmp_path: Path,
    *,
    final_state: dict[str, Any] | None = None,
    parent_start: str = "2026-09-30T11:59:59+00:00",
    fault_fired: bool = False,
) -> dict[str, Any]:
    parent_marker = "LONGHOUSE_CLAUDE_BG_PARENT_beef"
    prompt = oracle._background_prompt(parent_marker, "LONGHOUSE_CLAUDE_BG_CHILD_beef", "LONGHOUSE_CLAUDE_BG_SHELL_beef")
    transcript = [
        {"type": "user", "message": {"content": prompt}, "timestamp": parent_start},
        {"type": "system", "subtype": "turn_duration", "timestamp": "2026-09-30T12:00:02+00:00"},
    ]
    empty = {"delegation": {"state": "none", "count": 0, "items": []}}
    active = {"delegation": {"state": "pending", "count": 2}}
    if fault_fired:
        (tmp_path / "qa-fault-receipt.jsonl").write_text(
            json.dumps({"schema_version": 1, "fault": oracle.FAULT, "session_id": "managed-1", "fired_at": "now", "detail": {}}) + "\n"
        )
        unknown = {"delegation": {"state": "unknown"}}
        states = iter([unknown, unknown])
    else:
        states = iter([active, final_state if final_state is not None else empty])
    monkeypatch.setattr(oracle.helm, "_transcript_rows", lambda *_args: transcript)
    monkeypatch.setattr(oracle.helm, "_hosted_assistant_texts", lambda *_args: [parent_marker])
    monkeypatch.setattr(oracle.helm, "_served_state", lambda *_args: next(states, final_state if final_state is not None else empty))
    monkeypatch.setattr(oracle, "_capture_records", lambda *_args: _native_records())

    def wait_until(predicate: Any, **_kwargs: Any) -> None:
        assert predicate()

    clock = [0.0]

    def poll(seconds: float) -> None:
        if fault_fired:
            raise AssertionError("fired writer fault must not consume another polling deadline")
        clock[0] += seconds

    monkeypatch.setattr(oracle.helm, "wait_until", wait_until)
    monkeypatch.setattr(oracle.time, "sleep", poll)
    monkeypatch.setattr(oracle.time, "monotonic", lambda: clock[0])
    return oracle._capture_background(
        args=Namespace(
            api_url="http://127.0.0.1",
            agents_token="fixture",
            response_timeout_secs=1,
            negative_control=oracle.FAULT if fault_fired else None,
        ),
        session=None,
        session_id="managed-1",
        provider_session_id="native-1",
        lookup_id="managed-1",
        home=tmp_path,
        root=tmp_path,
        environment={},
        hook_capture_dir=tmp_path,
        prompt=prompt,
        initial_parent_read={"captured_at": "2026-09-30T11:59:58+00:00", "served_path": "canonical_session_detail", "state": empty},
    )


@pytest.mark.parametrize("field", ["activity", "active_tool"])
def test_child_callback_replacement_rejects_parent_even_at_final_empty_read(monkeypatch: Any, tmp_path: Path, field: str) -> None:
    result = _capture_scenario(
        monkeypatch,
        tmp_path,
        final_state={"delegation": {"state": "none", "count": 0}, field: {"tool_id": "agent-1"}},
    )

    assert result["assertions"]["claude_background_registry_served"] is True
    assert result["child_callback_replaced_parent_tool"] is True
    assert result["assertions"]["claude_background_callbacks_scoped"] is False
    assert result["status"] == "fail"


def test_active_stop_before_requested_parent_turn_cannot_certify_its_boundary(monkeypatch: Any, tmp_path: Path) -> None:
    result = _capture_scenario(monkeypatch, tmp_path, parent_start="2026-09-30T12:00:01+00:00")

    assert result["assertions"]["claude_background_registry_served"] is True
    assert result["assertions"]["claude_background_parent_turn_boundary"] is False
    assert result["status"] == "fail"


def test_parent_stop_in_requested_turn_can_certify_background_lifecycle(monkeypatch: Any, tmp_path: Path) -> None:
    result = _capture_scenario(monkeypatch, tmp_path)

    assert result["assertions"] == dict.fromkeys(oracle.ASSERTIONS, True)
    assert result["child_callback_replaced_parent_tool"] is False
    assert result["status"] == "pass"


def test_fired_writer_fault_rejects_registry_without_waiting_for_impossible_empty_state(monkeypatch: Any, tmp_path: Path) -> None:
    result = _capture_scenario(monkeypatch, tmp_path, fault_fired=True)

    assert result["assertions"]["claude_background_registry_served"] is False
    assert all(value for name, value in result["assertions"].items() if name != "claude_background_registry_served")
    assert result["status"] == "fail"
    assert "error" not in result


def test_native_empty_source_cannot_certify_unknown_served_registry(monkeypatch: Any, tmp_path: Path) -> None:
    result = _capture_scenario(monkeypatch, tmp_path, final_state={"delegation": {"state": "unknown", "items": [], "count": 0}})

    assert result["assertions"]["claude_background_explicit_empty"] is True
    assert result["assertions"]["claude_background_registry_served"] is False
    assert result["status"] == "fail"


@pytest.mark.parametrize("negative_control", [None, oracle.FAULT])
def test_a_setup_failure_reports_the_authored_variant_and_keeps_the_execution_key_apart(
    monkeypatch: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str], negative_control: str | None
) -> None:
    """The factory compares ``variant`` with the cell's authored variant (none); a typed setup failure must
    not be refused as a malformed result before anyone reads its failure code."""

    engine = tmp_path / "longhouse-engine"
    claude = tmp_path / "claude"
    for path in (engine, claude):
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        path.chmod(0o700)

    def boom(_args: Any) -> dict[str, Any]:
        raise RuntimeError("setup exploded")

    monkeypatch.setattr(oracle, "require_disposable_runtime", lambda _url: None)
    monkeypatch.setattr(oracle, "_run_direct", boom)
    variant = oracle._VARIANTS[0]

    code = oracle.main(
        [
            "--variant",
            variant,
            "--evidence-root",
            str(tmp_path / "evidence"),
            "--repo-root",
            str(tmp_path),
            "--engine",
            str(engine),
            "--provider-bin",
            str(claude),
            "--api-url",
            "http://127.0.0.1:1",
            "--agents-token",
            "token",
            *(["--negative-control", negative_control] if negative_control else []),
        ]
    )

    assert code == 1
    result = json.loads(capsys.readouterr().out)
    assert result["failure_code"] == "claude_background_setup_failed"
    assert result["variant"] is None
    assert result["execution_variant"] == variant
    assert_result_conforms(oracle, result, variant=variant)
    assert result["status"] == "fail"
    assert result["error"] == "RuntimeError: setup exploded"
    assert "observation" not in result
    assert "assertions" not in result
    assert datetime.fromisoformat(result["generated_at"].replace("Z", "+00:00")).tzinfo is not None
    evidence_root = tmp_path / "evidence"
    manifest_paths = {item["path"] for item in result["artifact_manifest"]}
    assert manifest_paths == {
        path.relative_to(evidence_root).as_posix() for path in evidence_root.rglob("*") if path.is_file() and path.name != "result.json"
    }
    failures = [
        json.loads((evidence_root / item["path"]).read_text()) for item in result["artifact_manifest"] if item["path"].endswith(".json")
    ]
    assert any(isinstance(item, dict) and item.get("error") == "RuntimeError: setup exploded" for item in failures)
    if negative_control:
        assert result["negative_control"]["status"] == "inconclusive"


def _run_cell(monkeypatch: Any, tmp_path: Path, result: dict[str, Any]) -> dict[str, Any]:
    # The real Helm lifecycle result is scenario-scoped (claude_helm_lifecycle).
    result = {**result, "observation_scope": "scenario"}
    monkeypatch.setattr(oracle.helm, "run_lifecycle", lambda *_args, **_kwargs: result)
    return oracle._run_direct(Namespace(evidence_root=tmp_path, variant=oracle._VARIANTS[0], negative_control=None))


def test_cleanup_failure_after_every_assertion_held_leaves_status_to_the_assertions(monkeypatch: Any, tmp_path: Path) -> None:
    """The factory judges cleanup from the cleanup receipt; status follows the assertion map."""

    held = _scenario(target=True)
    result = _run_cell(monkeypatch, tmp_path, _lifecycle_result(tmp_path, scenario=held, cleanup_ok=False, status="pass"))

    assert result["status"] == "pass"
    assert all(result["assertions"].values())
    assert "failure_code" not in result


def test_a_failed_run_whose_assertions_all_held_is_a_typed_harness_failure(monkeypatch: Any, tmp_path: Path) -> None:
    """status fail with an all-true map is a contradiction the factory refuses; it reached no failing verdict."""

    held = _scenario(target=True)
    result = _run_cell(monkeypatch, tmp_path, _lifecycle_result(tmp_path, scenario=held, status="fail"))

    assert result["status"] == "fail"
    assert "assertions" not in result and "observation" not in result
    assert isinstance(result["error"], str) and result["error"]
    assert result["partial_observation"]["lifecycle"]["scenario"]["assertions"] == held["assertions"]
    assert_result_conforms(oracle, result, variant=oracle._VARIANTS[0])
