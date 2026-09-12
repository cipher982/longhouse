from __future__ import annotations

import hashlib
import io
import json
import urllib.error
from pathlib import Path

import pytest

from zerg.qa import omp_helm_lifecycle
from zerg.qa import provider_console_lifecycle as lifecycle
from zerg.qa.live_session_toolkit import new_qualification_isolation_root
from zerg.qa.omp_console_producer import _PROFILE as CONSOLE_PROFILE
from zerg.qa.omp_console_producer import _VARIANT as CONSOLE_VARIANT
from zerg.qa.omp_console_producer import ASSERTION_ID as CONSOLE_ASSERTION
from zerg.qa.omp_console_producer import REGISTRATION as CONSOLE_REGISTRATION
from zerg.qa.omp_console_producer import _is_terminal_agent_end
from zerg.qa.omp_console_producer import _native_settlement as omp_console_settlement
from zerg.qa.omp_console_producer import omp_console_assertions
from zerg.qa.omp_console_producer import omp_native_model_evidence
from zerg.qa.omp_helm_lifecycle import _PROFILE as HELM_PROFILE
from zerg.qa.omp_helm_lifecycle import _VARIANTS
from zerg.qa.omp_helm_lifecycle import ASSERTIONS as HELM_ASSERTIONS
from zerg.qa.omp_helm_lifecycle import REGISTRATION as HELM_REGISTRATION
from zerg.qa.omp_helm_lifecycle import _assertion_result_status
from zerg.qa.omp_helm_lifecycle import _channel_command_evidence
from zerg.qa.omp_helm_lifecycle import _cleanup_receipt
from zerg.qa.omp_helm_lifecycle import _events_page_metadata
from zerg.qa.omp_helm_lifecycle import _exact_session_retirement
from zerg.qa.omp_helm_lifecycle import _flush_receipt_complete
from zerg.qa.omp_helm_lifecycle import _helm_cleanup_ready
from zerg.qa.omp_helm_lifecycle import _helm_result_status
from zerg.qa.omp_helm_lifecycle import _manifest_is_stable
from zerg.qa.omp_helm_lifecycle import _native_settlement
from zerg.qa.omp_helm_lifecycle import _redacted_state_snapshot
from zerg.qa.omp_helm_lifecycle import _register_native_source
from zerg.qa.omp_helm_lifecycle import _remove_isolation_after_source_retention
from zerg.qa.omp_helm_lifecycle import _runtime_control_identity_is_complete
from zerg.qa.omp_helm_lifecycle import _runtime_convergence
from zerg.qa.omp_helm_lifecycle import _runtime_events_snapshot
from zerg.qa.omp_helm_lifecycle import _served_control_identity
from zerg.qa.omp_helm_lifecycle import _served_projection_evidence
from zerg.qa.omp_helm_lifecycle import _wait_cleanup_receipt
from zerg.qa.omp_helm_lifecycle import _wait_native_marker
from zerg.qa.omp_helm_lifecycle import _wait_runtime_control_identity
from zerg.qa.omp_helm_lifecycle import _wait_served_run_retirement
from zerg.qa.omp_helm_lifecycle import omp_helm_lifecycle_assertions
from zerg.qa.provider_console_lifecycle import _omp_continuation_prompt
from zerg.qa.provider_qualification import _PROFILES


def _identity_receipt(
    subject_key: str,
    *,
    session_id: str = "session-1",
    owner_identity: list[object] | None = None,
) -> dict[str, object]:
    if owner_identity is None:
        owner_identity = [session_id, f"native-{subject_key}", f"/tmp/{subject_key}.jsonl", 1, "launcher", 2, "provider"]
    return {
        "session_id": session_id,
        "expected_subject_key": subject_key,
        "served_path": "canonical_session_detail",
        "control_subject_key": subject_key,
        "owner_identity": owner_identity,
        "actions": {
            "send_input": "available",
            "interrupt": "available",
            "terminate": "available",
        },
    }


def _omp_state(
    connection_id: str,
    lease_generation: str,
    updated_at: str,
    *,
    ready: bool = True,
    provider_pid: int = 12,
) -> dict[str, object]:
    return {
        "ready": ready,
        "session_id": "session-1",
        "native_session_id": "native-1",
        "session_file": "/tmp/session-1.jsonl",
        "launcher_pid": 11,
        "launcher_process_start_time": "launcher-start",
        "provider_pid": provider_pid,
        "provider_process_start_time": "provider-start",
        "connection_id": connection_id,
        "lease_generation": lease_generation,
        "updated_at": updated_at,
    }


def test_omp_qualification_producers_are_registered_on_their_own_contracts() -> None:
    assert CONSOLE_REGISTRATION.producer_id == "omp.console_lifecycle.v1"
    assert CONSOLE_REGISTRATION.producer_revision == 8
    assert CONSOLE_REGISTRATION.providers == ("omp",)
    assert CONSOLE_REGISTRATION.scenario_id == "omp_console_lifecycle"
    assert CONSOLE_REGISTRATION.scenario_revision == 8
    assert "console_continuation_receipt" in CONSOLE_REGISTRATION.required_artifacts
    assert HELM_REGISTRATION.producer_id == "omp.helm_lifecycle.v1"
    assert HELM_REGISTRATION.producer_revision == 8
    assert HELM_REGISTRATION.scenario_revision == 8
    assert HELM_REGISTRATION.providers == ("omp",)
    assert HELM_REGISTRATION.scenario_id == "omp_helm_lifecycle"
    assert "transcript_flush_receipt" in HELM_REGISTRATION.required_artifacts
    assert "transcript_shipper_receipt" in HELM_REGISTRATION.required_artifacts
    assert "runtime_convergence_receipt" in HELM_REGISTRATION.required_artifacts
    assert ("omp", "omp_print_v1") in _PROFILES
    assert ("omp", "omp_helm_v1") in _PROFILES


def test_omp_stock_version_line_is_prefixed_for_both_release_profiles() -> None:
    assert CONSOLE_PROFILE.version_line.fullmatch("omp/18.1.18")
    assert HELM_PROFILE.version_line.fullmatch("omp/18.1.18")
    assert CONSOLE_PROFILE.version_line.fullmatch("18.1.18") is None
    assert HELM_PROFILE.version_line.fullmatch("18.1.18") is None


def test_qualification_isolation_root_uses_short_sandbox_alias(monkeypatch, tmp_path) -> None:
    sandbox_home = tmp_path / "sandbox-home"
    sandbox_home.mkdir()
    monkeypatch.setenv("LONGHOUSE_QUALIFICATION_HOME", str(sandbox_home))

    first = new_qualification_isolation_root("omp-helm")
    second = new_qualification_isolation_root("pi-helm")

    assert first.parent == sandbox_home
    assert second.parent == sandbox_home
    assert first != second
    assert str(first / "home" / ".longhouse" / "agent" / "transcript-wake.sock").startswith(str(sandbox_home))
    first.rmdir()
    second.rmdir()


def test_qualification_isolation_root_keeps_unique_tmp_behavior_outside_sandbox(monkeypatch) -> None:
    monkeypatch.delenv("LONGHOUSE_QUALIFICATION_HOME", raising=False)

    isolation = new_qualification_isolation_root("pi-helm")

    try:
        assert isolation.parent == Path("/tmp")
    finally:
        isolation.rmdir()


@pytest.mark.parametrize(
    ("event", "expected"),
    (
        ({"type": "agent_end"}, True),
        ({"type": "agent_end", "willContinue": True}, False),
        ({"type": "agent_end", "isTerminal": False, "willContinue": False}, False),
        ({"type": "agent_end", "isTerminal": "true"}, False),
        ({"type": "agent_end", "willContinue": "false"}, False),
        ({"type": "agent_end", "isTerminal": True, "willContinue": "false"}, False),
    ),
)
def test_omp_agent_end_lifecycle_fields_are_strict(event: dict[str, object], expected: bool) -> None:
    assert _is_terminal_agent_end(event) is expected


def test_omp_helm_channel_terminal_evidence_preserves_lifecycle_field_presence(monkeypatch, tmp_path) -> None:
    state = {
        "ready": True,
        "native_session_id": "native-1",
        "session_file": str(tmp_path / "omp.jsonl"),
        "live_turn_seq": 3,
        "agent_end_observed": True,
        "agent_end_is_terminal": True,
        "agent_end_will_continue": None,
        "agent_end_is_terminal_present": False,
        "agent_end_will_continue_present": False,
    }
    monkeypatch.setattr(omp_helm_lifecycle, "_read_state", lambda _path: state)
    monkeypatch.setattr(
        omp_helm_lifecycle,
        "_wait",
        lambda observe, *, timeout, description: observe(),
    )

    event, _ = omp_helm_lifecycle._wait_channel_terminal(
        tmp_path,
        session_id="session-1",
        native_session_id="native-1",
        session_file=tmp_path / "omp.jsonl",
        minimum_turn_seq=3,
    )

    assert event == {"type": "agent_end", "source": "omp_helm_extension_channel"}
    assert _is_terminal_agent_end(event) is True

    state.update(
        {
            "agent_end_is_terminal_present": True,
            "agent_end_will_continue_present": True,
            "agent_end_will_continue": False,
        }
    )
    event, _ = omp_helm_lifecycle._wait_channel_terminal(
        tmp_path,
        session_id="session-1",
        native_session_id="native-1",
        session_file=tmp_path / "omp.jsonl",
        minimum_turn_seq=3,
    )
    assert event["isTerminal"] is True
    assert event["willContinue"] is False
    state["live_turn_seq"] = 2
    assert (
        omp_helm_lifecycle._wait_channel_terminal(
            tmp_path,
            session_id="session-1",
            native_session_id="native-1",
            session_file=tmp_path / "omp.jsonl",
            minimum_turn_seq=3,
        )
        is None
    )


def test_omp_native_model_evidence_binds_provider_event_to_retained_source(tmp_path) -> None:
    source = tmp_path / "omp-native.jsonl"
    source.write_text(
        "\n".join(
            json.dumps(event)
            for event in [
                {"type": "session", "id": "native-1"},
                {
                    "type": "message",
                    "model": "openrouter/fixture-model",
                    "message": {
                        "role": "assistant",
                        "model": "openrouter/fixture-model",
                        "stopReason": "toolUse",
                        "content": [{"type": "toolCall", "id": "call-1"}],
                        "usage": {"input": 5, "output": 2, "cost": {"total": 0.0001}},
                    },
                },
                {
                    "type": "message",
                    "model": "openrouter/fixture-model",
                    "message": {
                        "role": "assistant",
                        "model": "openrouter/fixture-model",
                        "stopReason": "stop",
                        "content": [{"type": "text", "text": "OMP_MODEL_MARKER"}],
                        "usage": {
                            "input": 11,
                            "output": 7,
                            "cost": {"input": 0.0001, "output": 0.0002, "total": 0.0003},
                        },
                    },
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    source_digest = f"sha256:{hashlib.sha256(source.read_bytes()).hexdigest()}"
    (tmp_path / "provider-source-retention.json").write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "source": str(source),
                        "path": source.relative_to(tmp_path).as_posix(),
                        "kind": "source_path",
                        "retained": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "console-continuation-receipt.json").write_text(
        json.dumps(
            {
                "first_turn_evidence": {
                    "source_kind": "source_path",
                    "retained_path": source.relative_to(tmp_path).as_posix(),
                    "retained_source_path": str(source),
                    "source_sha256": source_digest,
                    "source_start_offset": 0,
                    "source_end_offset": source.stat().st_size,
                    "provider_thread_id": "native-1",
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "adapter-dispatch-receipt.json").write_text(json.dumps({"provider_thread_id": "native-1"}), encoding="utf-8")
    (tmp_path / "provider-response-binding-receipt.json").write_text(
        json.dumps({"provider_thread_id": "native-1"}),
        encoding="utf-8",
    )

    evidence = omp_native_model_evidence(
        tmp_path,
        source_canary="omp_console_lifecycle",
        qualification_model="openrouter/fixture-model",
        api_key_configured=True,
        first_turn_only=True,
    )

    assert evidence is not None
    assert evidence["operation_evidence"]["model_call"] == {"status": "pass", "level": "live_token"}
    assert evidence["model"] == "openrouter/fixture-model"
    assert evidence["result_event"]["provider"] is None
    assert evidence["result_event"]["usage"]["input"] == 16
    assert evidence["result_event"]["usage"]["output"] == 9
    assert evidence["result_event"]["total_cost_usd"] == 0.0004
    artifact = evidence["source_artifacts"][0]
    assert artifact["path"] == source.relative_to(tmp_path).as_posix()
    assert artifact["sha256"].startswith("sha256:") and len(artifact["sha256"]) == 71
    assert artifact["event_sha256"].startswith("sha256:") and len(artifact["event_sha256"]) == 71
    assert artifact["native_event_sha256"].startswith("sha256:") and len(artifact["native_event_sha256"]) == 71
    assert evidence["result_event"]["model_source"] == "provider_event"

    full_evidence = omp_native_model_evidence(
        tmp_path,
        source_canary="omp_console_lifecycle",
        qualification_model="openrouter/fixture-model",
        api_key_configured=True,
    )
    assert full_evidence is not None
    assert full_evidence["result_event"]["usage"]["output"] == 9
    assert "event_window" not in full_evidence["source_artifacts"][0]


def test_omp_native_model_evidence_rejects_stream_stdout_without_native_usage(tmp_path) -> None:
    _write_omp_console_settlement_fixture(
        tmp_path,
        first_turn_events=[{"type": "session", "id": "native-1"}],
    )
    stdout_source = tmp_path / "provider-sources" / "stdout.jsonl"
    stdout_source.write_text(
        json.dumps({"type": "assistant", "text": "stream-shaped output"}) + "\n" + json.dumps({"type": "agent_end"}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "console-continuation-receipt.json").unlink()
    (tmp_path / "provider-response-binding-receipt.json").write_text(
        json.dumps(
            {
                "provider_thread_id": "native-1",
                "provider_response_source_kind": "stdout_path",
                "provider_response_source_path": "/stdout/provider.jsonl",
                "provider_response_source_sha256": f"sha256:{hashlib.sha256(stdout_source.read_bytes()).hexdigest()}",
            }
        ),
        encoding="utf-8",
    )

    evidence = omp_native_model_evidence(
        tmp_path,
        source_canary="omp_console_lifecycle",
        qualification_model="openrouter/fixture-model",
        api_key_configured=True,
        first_turn_only=True,
    )

    assert evidence is None


def test_omp_native_model_evidence_uses_bound_retained_native_source(tmp_path) -> None:
    native_source, _ = _write_omp_console_settlement_fixture(
        tmp_path,
        first_turn_events=[
            {"type": "session", "id": "native-1"},
            {
                "type": "message",
                "model": "openrouter/fixture-model",
                "message": {
                    "role": "assistant",
                    "model": "openrouter/fixture-model",
                    "stopReason": "stop",
                    "usage": {"input": 3, "output": 2, "cost": {"total": 0.0001}},
                },
            },
        ],
    )
    (tmp_path / "console-continuation-receipt.json").unlink()
    (tmp_path / "provider-response-binding-receipt.json").write_text(
        json.dumps(
            {
                "provider_thread_id": "native-1",
                "provider_response_source_kind": "source_path",
                "provider_response_source_path": "/native/session.jsonl",
                "provider_response_source_sha256": f"sha256:{hashlib.sha256(native_source.read_bytes()).hexdigest()}",
            }
        ),
        encoding="utf-8",
    )

    evidence = omp_native_model_evidence(
        tmp_path,
        source_canary="omp_console_lifecycle",
        qualification_model="openrouter/fixture-model",
        api_key_configured=True,
        first_turn_only=True,
    )

    assert evidence is not None
    assert evidence["model"] == "openrouter/fixture-model"
    assert evidence["result_event"]["usage"]["output"] == 2


def _write_omp_console_settlement_fixture(tmp_path, *, first_turn_events, later_events=(), malformed_suffix=b""):
    native_source = tmp_path / "provider-sources" / "native.jsonl"
    native_source.parent.mkdir()
    first_turn_bytes = ("\n".join(json.dumps(event) for event in first_turn_events) + "\n").encode()
    later_bytes = ("\n".join(json.dumps(event) for event in later_events) + "\n").encode() if later_events else b""
    native_source.write_bytes(first_turn_bytes + later_bytes + malformed_suffix)

    stdout_source = tmp_path / "provider-sources" / "stdout.jsonl"
    stdout_source.write_text(json.dumps({"type": "agent_end"}) + "\n", encoding="utf-8")
    native_relative = native_source.relative_to(tmp_path).as_posix()
    stdout_relative = stdout_source.relative_to(tmp_path).as_posix()
    (tmp_path / "provider-source-retention.json").write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "source": "/native/session.jsonl",
                        "kind": "source_path",
                        "path": native_relative,
                        "retained": True,
                    },
                    {
                        "source": "/stdout/provider.jsonl",
                        "kind": "stdout_path",
                        "path": stdout_relative,
                        "retained": True,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "console-continuation-receipt.json").write_text(
        json.dumps(
            {
                "first_turn_evidence": {
                    "source_kind": "source_path",
                    "retained_path": native_relative,
                    "retained_source_path": "/native/session.jsonl",
                    "source_sha256": f"sha256:{hashlib.sha256(native_source.read_bytes()).hexdigest()}",
                    "source_start_offset": 0,
                    "source_end_offset": len(first_turn_bytes),
                    "provider_thread_id": "native-1",
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "adapter-dispatch-receipt.json").write_text(json.dumps({"provider_thread_id": "native-1"}), encoding="utf-8")
    (tmp_path / "provider-response-binding-receipt.json").write_text(
        json.dumps(
            {
                "provider_thread_id": "native-1",
                "marker": "OMP_FIRST_TURN_MARKER",
                "tool_marker": "OMP_TOOL_OUTPUT",
                "provider_response_source_kind": "stdout_path",
                "provider_response_source_path": "/stdout/provider.jsonl",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "console-boundary-receipt.json").write_text(
        json.dumps({"claim_state": "terminal", "claim_terminal_state": "run_completed"}),
        encoding="utf-8",
    )
    (tmp_path / "transcript-flush-receipt.json").write_text(
        json.dumps({"status": "pass", "exit_code": 0}),
        encoding="utf-8",
    )
    return native_source, first_turn_bytes


def test_omp_native_settlement_binds_native_evidence_to_first_turn_and_keeps_stdout_terminal_separate(tmp_path) -> None:
    marker = "OMP_FIRST_TURN_MARKER"
    tool_marker = "OMP_TOOL_OUTPUT"
    first_turn = [
        {"type": "session", "id": "native-1"},
        {"type": "message", "message": {"role": "assistant", "content": [{"type": "toolCall", "id": "call-1"}]}},
        {"type": "message", "message": {"role": "toolResult", "toolCallId": "call-1", "content": tool_marker}},
        {"type": "message", "message": {"role": "assistant", "content": [{"type": "text", "text": marker}]}},
    ]
    later_events = [
        {"type": "message", "message": {"role": "assistant", "content": [{"type": "text", "text": marker}]}},
    ]
    _write_omp_console_settlement_fixture(tmp_path, first_turn_events=first_turn, later_events=later_events)

    settlement = omp_console_settlement(tmp_path)

    assert settlement["status"] == "pass"
    assert settlement["agent_end_terminal"] is True
    assert settlement["agent_end_evidence_source"] == "omp_print_projection"
    assert settlement["provider_response_source_kind"] == "stdout_path"
    assert settlement["native_archive_bound"] is True
    assert settlement["native_marker_count"] == 1
    assert settlement["native_tool_call_ids"] == ["call-1"]
    assert settlement["native_tool_result_ids"] == ["call-1"]
    assert settlement["native_tool_evidence_complete"] is True
    assert settlement["malformed_source"] is False


@pytest.mark.parametrize("failure", ("missing", "mismatched", "duplicate", "bad", "mid-line", "past-eof"))
def test_omp_native_settlement_rejects_missing_mismatched_duplicate_or_invalid_first_turn_window(tmp_path, failure) -> None:
    marker = "OMP_FIRST_TURN_MARKER"
    source, first_turn_bytes = _write_omp_console_settlement_fixture(
        tmp_path,
        first_turn_events=[
            {"type": "session", "id": "native-1"},
            {"type": "message", "message": {"role": "assistant", "content": [{"type": "text", "text": marker}]}},
        ],
    )
    continuation_path = tmp_path / "console-continuation-receipt.json"
    continuation = json.loads(continuation_path.read_text(encoding="utf-8"))
    if failure == "missing":
        continuation.pop("first_turn_evidence")
    elif failure == "mismatched":
        continuation["first_turn_evidence"]["retained_source_path"] = "/other/session.jsonl"
    elif failure == "duplicate":
        retention_path = tmp_path / "provider-source-retention.json"
        retention = json.loads(retention_path.read_text(encoding="utf-8"))
        retention["sources"].append(dict(retention["sources"][0]))
        retention_path.write_text(json.dumps(retention), encoding="utf-8")
    elif failure == "bad":
        continuation["first_turn_evidence"]["source_end_offset"] = 0
    elif failure == "mid-line":
        continuation["first_turn_evidence"]["source_end_offset"] = len(first_turn_bytes) - 1
    elif failure == "past-eof":
        continuation["first_turn_evidence"]["source_end_offset"] = source.stat().st_size + 1
    continuation_path.write_text(json.dumps(continuation), encoding="utf-8")

    settlement = omp_console_settlement(tmp_path)

    assert settlement["status"] == "fail"
    assert settlement["native_archive_bound"] is False
    assert settlement["native_tool_evidence_complete"] is False


def test_omp_native_settlement_rejects_malformed_records_outside_first_turn_window(tmp_path) -> None:
    _write_omp_console_settlement_fixture(
        tmp_path,
        first_turn_events=[{"type": "session", "id": "native-1"}],
        malformed_suffix=b"not-json\n",
    )

    settlement = omp_console_settlement(tmp_path)

    assert settlement["status"] == "fail"
    assert settlement["malformed_source"] is True
    assert settlement["native_archive_bound"] is False


def test_omp_native_settlement_ignores_tool_and_marker_evidence_outside_first_turn_window(tmp_path) -> None:
    _write_omp_console_settlement_fixture(
        tmp_path,
        first_turn_events=[{"type": "session", "id": "native-1"}],
        later_events=[
            {"type": "message", "message": {"role": "assistant", "content": [{"type": "toolCall", "id": "late-call"}]}},
            {"type": "message", "message": {"role": "toolResult", "toolCallId": "late-call", "content": "OMP_TOOL_OUTPUT"}},
            {"type": "message", "message": {"role": "assistant", "content": [{"type": "text", "text": "OMP_FIRST_TURN_MARKER"}]}},
        ],
    )

    settlement = omp_console_settlement(tmp_path)

    assert settlement["status"] == "fail"
    assert settlement["native_marker_count"] == 0
    assert settlement["native_tool_call_ids"] == []
    assert settlement["native_tool_result_ids"] == []
    assert settlement["native_tool_evidence_complete"] is False


def test_omp_native_model_evidence_rejects_successful_model_event_outside_first_turn_window(tmp_path) -> None:
    _write_omp_console_settlement_fixture(
        tmp_path,
        first_turn_events=[{"type": "session", "id": "native-1"}],
        later_events=[
            {
                "type": "message",
                "model": "openrouter/fixture-model",
                "message": {
                    "role": "assistant",
                    "model": "openrouter/fixture-model",
                    "stopReason": "stop",
                    "content": [{"type": "text", "text": "late model"}],
                    "usage": {"input": 1, "output": 1},
                },
            }
        ],
    )

    assert (
        omp_native_model_evidence(
            tmp_path,
            source_canary="omp_console_lifecycle",
            qualification_model="openrouter/fixture-model",
            api_key_configured=True,
            first_turn_only=True,
        )
        is None
    )


def test_omp_native_model_evidence_publishes_the_first_turn_event_window(tmp_path) -> None:
    first_turn = [
        {"type": "session", "id": "native-1"},
        {
            "type": "message",
            "model": "openrouter/fixture-model",
            "message": {
                "role": "assistant",
                "model": "openrouter/fixture-model",
                "stopReason": "stop",
                "content": [{"type": "text", "text": "OMP_WINDOW_MARKER"}],
                "usage": {"input": 3, "output": 2, "cost": {"total": 0.0001}},
            },
        },
    ]
    later = [
        {
            "type": "message",
            "model": "openrouter/fixture-model",
            "message": {
                "role": "assistant",
                "model": "openrouter/fixture-model",
                "stopReason": "stop",
                "usage": {"input": 9, "output": 8, "cost": {"total": 0.0002}},
            },
        }
    ]
    _write_omp_console_settlement_fixture(tmp_path, first_turn_events=first_turn, later_events=later)

    evidence = omp_native_model_evidence(
        tmp_path,
        source_canary="omp_console_lifecycle",
        qualification_model="openrouter/fixture-model",
        api_key_configured=True,
        first_turn_only=True,
    )

    assert evidence is not None
    assert evidence["result_event"]["usage"]["output"] == 2
    assert evidence["source_artifacts"][0]["event_window"] == {
        "start_offset": 0,
        "end_offset": len(("\n".join(json.dumps(event) for event in first_turn) + "\n").encode()),
    }


def test_omp_continuation_prompt_names_the_earlier_context_label_without_tool_or_marker_replay() -> None:
    prompt = _omp_continuation_prompt("OMP_RESUME_MARKER")

    assert '"Remember this context phrase:"' in prompt
    assert "earlier user message" in prompt
    assert "OMP_RESUME_MARKER" in prompt
    assert "entire visible answer" in prompt
    assert "Do not quote, mention, or reuse any earlier assistant answer" in prompt
    assert "No explanation" in prompt


def test_omp_helm_marker_prompts_preserve_setup_instructions() -> None:
    marker = "OMP_HELM_MARKER"

    exact = omp_helm_lifecycle._exact_marker_prompt(marker)
    assert "new turn" in exact
    assert "entire visible answer" in exact
    assert "Do not quote, mention, or reuse any earlier answer" in exact
    assert marker in exact

    setup = omp_helm_lifecycle._setup_marker_prompt(
        marker,
        setup="Use the bash tool to run `sleep 8`, then",
    )
    assert "`sleep 8`" in setup
    assert setup.endswith("No explanation.")
    assert marker in setup
    assert "entire visible answer" in setup

    context = omp_helm_lifecycle._setup_marker_prompt(
        marker,
        setup="Remember this context phrase: OMP_HELM_CONTEXT. Then",
    )
    assert "Remember this context phrase: OMP_HELM_CONTEXT." in context
    assert marker in context
    assert "Do not quote, mention, or reuse any earlier answer" in context


def test_omp_helm_controls_use_runtime_agents_api(monkeypatch, tmp_path) -> None:
    from zerg.qa.omp_helm_lifecycle import _run_engine

    session_id = "session-1"
    state_dir = tmp_path / "home" / "managed-local" / "omp-helm"
    state_dir.mkdir(parents=True)
    (state_dir / f"{session_id}.json").write_text(
        json.dumps({"native_session_id": "native-1", "phase": "running"}),
        encoding="utf-8",
    )
    seen: dict[str, object] = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"outcome": "sent", "client_request_id": "fixture-request"}).encode("utf-8")

    def _urlopen(request, timeout):
        seen["request"] = request
        seen["timeout"] = timeout
        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    result = _run_engine(
        Path("/unused/longhouse-engine"),
        "steer",
        session_id,
        {
            "LONGHOUSE_OMP_HELM_URL": "https://runtime.test",
            "LONGHOUSE_OMP_HELM_TOKEN": "agent-token",
            "LONGHOUSE_HOME": str(tmp_path / "home"),
        },
        text="redirect now",
    )

    request = seen["request"]
    assert request.get_header("X-agents-token") == "agent-token"
    body = json.loads(request.data)
    assert body["text"] == "redirect now"
    assert body["intent"] == "steer"
    assert body["client_request_id"].startswith("omp-helm-steer-")
    assert "native_session_id" not in result["payload"]
    assert "status" not in result["payload"]
    assert result["transport"] == "runtime_host_agents_api"
    assert result["path"] == f"/api/agents/sessions/{session_id}/input"
    assert result["request"]["method"] == "POST"
    assert result["request"]["path"] == result["path"]
    assert result["request"]["payload"]["text"] == "redirect now"
    assert result["request"]["payload"]["intent"] == "steer"


def test_omp_runtime_control_retries_machine_agent_reconnect(monkeypatch) -> None:
    attempts = 0

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"outcome": "sent"}'

    def _urlopen(_request, timeout):
        nonlocal attempts
        assert timeout == 20
        attempts += 1
        if attempts < 3:
            raise urllib.error.HTTPError(
                "https://runtime.test/api/agents/sessions/session-1/input",
                409,
                "conflict",
                {},
                io.BytesIO(b'{"detail":"no live Longhouse control channel"}'),
            )
        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    monkeypatch.setattr(omp_helm_lifecycle.time, "sleep", lambda _seconds: None)

    response = omp_helm_lifecycle._runtime_post(
        "https://runtime.test",
        "agent-token",
        "/api/agents/sessions/session-1/input",
        {"text": "hello"},
    )

    assert response == {"outcome": "sent"}
    assert attempts == 3


def test_omp_channel_ack_binds_real_runtime_input_response_to_channel_state() -> None:
    state = {
        "ready": True,
        "session_id": "session-1",
        "native_session_id": "native-1",
        "phase": "running",
    }
    command = {
        "accepted": True,
        "payload": {"outcome": "sent", "client_request_id": "request-1"},
        "request": {
            "method": "POST",
            "path": "/api/agents/sessions/session-1/input",
            "payload": {"text": "redirect now", "intent": "steer", "client_request_id": "request-1"},
        },
    }

    evidence = _channel_command_evidence(command, state)

    assert evidence["channel_ack_bound"] is True
    assert evidence["native_session_id"] == "native-1"
    assert evidence["status"] == "running"
    assert "argv" not in command
    command["payload"]["client_request_id"] = "other-request"
    assert _channel_command_evidence(command, state)["channel_ack_bound"] is False


def test_omp_helm_active_state_evidence_redacts_secret_fields() -> None:
    snapshot = _redacted_state_snapshot(
        {
            "provider": "omp",
            "session_id": "session-1",
            "native_session_id": "native-1",
            "model": "fixture-model",
            "channel_token": "<fixture-channel-token>",
            "nested": {"client_secret": "<fixture-client-secret>", "owner": "fixture-owner"},
        }
    )

    assert snapshot["model"] == "fixture-model"
    assert snapshot["session_id"] == "session-1"
    assert snapshot["channel_token"] == "<redacted>"
    assert snapshot["nested"] == {"client_secret": "<redacted>", "owner": "fixture-owner"}


def test_omp_helm_settlement_requires_terminal_channel_evidence(tmp_path) -> None:
    session_file = tmp_path / "omp.jsonl"
    session_file.write_text(
        "\n".join(
            [
                json.dumps({"type": "session", "id": "native-1"}),
                json.dumps(
                    {
                        "type": "message",
                        "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    state = {
        "status": "ready",
        "phase": "running",
        "native_session_id": "native-1",
        "session_file": str(session_file),
        "agent_end_observed": True,
        "agent_end_is_terminal": True,
        "agent_end_will_continue": False,
        "updated_at": "2026-09-10T00:00:00Z",
    }
    assert _native_settlement(session_file, channel_state=state, native_session_id="native-1")["status"] == "fail"
    state["phase"] = "idle"
    assert _native_settlement(session_file, channel_state=state, native_session_id="native-1")["status"] == "pass"


def test_omp_helm_zero_delta_flush_is_accepted_only_before_independent_marker_proof(monkeypatch) -> None:
    marker = "OMP_HELM_INITIAL_0123456789abcdef"
    flush = {
        "status": "pass",
        "exit_code": 0,
        "daemon_paused": True,
        "daemon_restarted": True,
        "events_shipped": 0,
    }
    assert _flush_receipt_complete(flush)
    assert not _flush_receipt_complete({**flush, "events_shipped": -1})
    assert not _flush_receipt_complete({key: value for key, value in flush.items() if key != "events_shipped"})

    monkeypatch.setattr(
        "zerg.qa.omp_helm_lifecycle._runtime_snapshot",
        lambda *_args: {
            "detail": {"id": "session-1", "provider": "omp", "provider_session_id": "native-1"},
            "thread": {
                "root_session_id": "session-1",
                "head_session_id": "session-1",
                "sessions": [{"id": "session-1"}],
            },
            "events": {
                "events": [{"id": "event-1", "role": "assistant", "content_text": marker}],
                "total": 1,
                "generation_id": "generation-1",
                "branch_mode": "head",
                "next_cursor": None,
                "has_more": False,
            },
            "diagnostic": {"session_id": "session-1", "served_path": "canonical_session_detail"},
        },
    )

    convergence = _runtime_convergence(
        "https://runtime.example",
        "token",
        session_id="session-1",
        native_session_id="native-1",
        marker=marker,
        flush=flush,
        native_source_path="/native/session.jsonl",
        timeout=1,
    )

    assert convergence["status"] == "pass"
    event = convergence["served_projection"]["events"][0]
    assert event["role"] == "assistant"
    assert event["marker_occurrences"] == 1
    assert "session_id" not in event
    assert convergence["flush"]["events_shipped"] == 0


def test_omp_served_control_identity_requires_exact_subject_and_actions() -> None:
    diagnostic = {
        "served_path": "canonical_session_detail",
        "shadow": {
            "fact_sources": {"control": {"subject_key": "connection:conn-1:lease-7"}},
            "control": {
                "actions": {
                    "send_input": {"state": "available"},
                    "interrupt": {"state": "available"},
                    "terminate": {"state": "available"},
                }
            },
        },
    }

    assert _served_control_identity(diagnostic, expected_subject_key="connection:conn-1:lease-7")
    assert not _served_control_identity(diagnostic, expected_subject_key="connection:conn-2:lease-7")
    diagnostic["shadow"]["control"]["actions"]["terminate"]["state"] = "unavailable"
    assert not _served_control_identity(diagnostic, expected_subject_key="connection:conn-1:lease-7")
    diagnostic["shadow"]["control"]["actions"]["interrupt"]["state"] = "unavailable"
    assert not _served_control_identity(diagnostic, expected_subject_key="connection:conn-1:lease-7")
    diagnostic["shadow"]["control"]["actions"]["interrupt"]["state"] = "available"
    diagnostic["shadow"]["control"]["actions"].pop("terminate")
    assert not _served_control_identity(diagnostic, expected_subject_key="connection:conn-1:lease-7")


def test_omp_wait_runtime_control_identity_retries_until_projection_matches(monkeypatch) -> None:
    diagnostics = iter(
        [
            {"served_path": "canonical_session_detail", "shadow": {}},
            {
                "served_path": "canonical_session_detail",
                "shadow": {
                    "fact_sources": {"control": {"subject_key": "connection:conn-1:lease-7"}},
                    "control": {
                        "actions": {
                            "send_input": {"state": "available"},
                            "interrupt": {"state": "available"},
                            "terminate": {"state": "available"},
                        }
                    },
                },
            },
        ]
    )
    monkeypatch.setattr(
        "zerg.qa.omp_helm_lifecycle._runtime_get",
        lambda *_args: next(diagnostics),
    )

    result = _wait_runtime_control_identity(
        "https://runtime.example",
        "token",
        session_id="session-1",
        state={"connection_id": "conn-1", "lease_generation": "lease-7"},
        timeout=1,
    )

    assert result["session_id"] == "session-1"
    assert result["expected_subject_key"] == "connection:conn-1:lease-7"


def test_omp_wait_runtime_control_identity_retries_transient_reads(monkeypatch) -> None:
    diagnostics = iter(
        [
            RuntimeError("Runtime Host HTTP 503"),
            {
                "served_path": "canonical_session_detail",
                "shadow": {
                    "fact_sources": {"control": {"subject_key": "connection:conn-1:lease-7"}},
                    "control": {
                        "actions": {
                            "send_input": {"state": "available"},
                            "interrupt": {"state": "available"},
                            "terminate": {"state": "available"},
                        }
                    },
                },
            },
        ]
    )

    def runtime_get(*_args):
        value = next(diagnostics)
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(
        "zerg.qa.omp_helm_lifecycle._runtime_get",
        runtime_get,
    )

    result = _wait_runtime_control_identity(
        "https://runtime.example",
        "token",
        session_id="session-1",
        state={"connection_id": "conn-1", "lease_generation": "lease-7"},
        timeout=1,
    )

    assert result["transient_errors"] == ["Runtime Host HTTP 503"]


def test_omp_wait_runtime_control_identity_waits_through_reconnect_lease_rotation(monkeypatch, tmp_path) -> None:
    state_dir = tmp_path / "managed-local" / "omp-helm"
    state_dir.mkdir(parents=True)
    state_path = state_dir / "session-1.json"
    baseline = _omp_state("conn-old", "lease-old", "2026-01-01T00:00:00Z")
    rotated = _omp_state("conn-new", "lease-new", "2026-01-01T00:00:01Z")
    state_path.write_text(json.dumps(baseline), encoding="utf-8")
    diagnostic = {
        "served_path": "canonical_session_detail",
        "shadow": {
            "fact_sources": {"control": {"subject_key": "connection:conn-new:lease-new"}},
            "control": {
                "actions": {
                    "send_input": {"state": "available"},
                    "interrupt": {"state": "available"},
                    "terminate": {"state": "available"},
                }
            },
        },
    }
    calls = 0

    def runtime_get(*_args):
        nonlocal calls
        calls += 1
        if calls == 1:
            state_path.write_text(json.dumps({**rotated, "ready": False}), encoding="utf-8")
        return diagnostic

    def advance_reconnect(_seconds):
        state_path.write_text(json.dumps(rotated), encoding="utf-8")

    monkeypatch.setattr("zerg.qa.omp_helm_lifecycle._runtime_get", runtime_get)
    monkeypatch.setattr("zerg.qa.omp_helm_lifecycle.time.sleep", advance_reconnect)

    result = _wait_runtime_control_identity(
        "https://runtime.example",
        "token",
        home=tmp_path,
        session_id="session-1",
        state=baseline,
        timeout=1,
    )

    assert calls == 2
    assert result["expected_subject_key"] == "connection:conn-new:lease-new"
    assert result["local_state"]["connection_id"] == "conn-new"


def test_omp_wait_runtime_control_identity_rejects_owner_rotation(monkeypatch, tmp_path) -> None:
    state_dir = tmp_path / "managed-local" / "omp-helm"
    state_dir.mkdir(parents=True)
    state_path = state_dir / "session-1.json"
    baseline = _omp_state("conn-old", "lease-old", "2026-01-01T00:00:00Z")
    state_path.write_text(json.dumps(baseline), encoding="utf-8")
    diagnostic = {
        "served_path": "canonical_session_detail",
        "shadow": {
            "fact_sources": {"control": {"subject_key": "connection:conn-new:lease-new"}},
            "control": {
                "actions": {
                    "send_input": {"state": "available"},
                    "interrupt": {"state": "available"},
                    "terminate": {"state": "available"},
                }
            },
        },
    }

    def runtime_get(*_args):
        state_path.write_text(
            json.dumps(
                {
                    **baseline,
                    "connection_id": "conn-new",
                    "lease_generation": "lease-new",
                    "provider_pid": 99,
                }
            ),
            encoding="utf-8",
        )
        return diagnostic

    monkeypatch.setattr("zerg.qa.omp_helm_lifecycle._runtime_get", runtime_get)

    with pytest.raises(RuntimeError) as error:
        _wait_runtime_control_identity(
            "https://runtime.example",
            "token",
            home=tmp_path,
            session_id="session-1",
            state=baseline,
            timeout=1,
        )

    message = str(error.value)
    assert "execution_owner_changed_during_read" in message
    assert "provider_pid" in message


def test_omp_wait_runtime_control_identity_retains_timeout_observation(monkeypatch, tmp_path) -> None:
    state_dir = tmp_path / "managed-local" / "omp-helm"
    state_dir.mkdir(parents=True)
    baseline = _omp_state("conn-1", "lease-1", "2026-01-01T00:00:00Z")
    (state_dir / "session-1.json").write_text(json.dumps(baseline), encoding="utf-8")
    diagnostic = {
        "served_path": "canonical_session_detail",
        "shadow": {
            "fact_sources": {"control": {"subject_key": "connection:wrong:lease"}},
            "control": {
                "actions": {
                    "send_input": {"state": "available"},
                    "interrupt": {"state": "available"},
                    "terminate": {"state": "available"},
                }
            },
        },
    }
    monotonic_values = iter((0.0, 0.0, 1.0))
    monkeypatch.setattr("zerg.qa.omp_helm_lifecycle._runtime_get", lambda *_args: diagnostic)
    monkeypatch.setattr("zerg.qa.omp_helm_lifecycle.time.monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr("zerg.qa.omp_helm_lifecycle.time.sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError) as error:
        _wait_runtime_control_identity(
            "https://runtime.example",
            "token",
            home=tmp_path,
            session_id="session-1",
            state=baseline,
            timeout=1,
        )

    message = str(error.value)
    assert "last_observation=" in message
    assert "projection_not_bound" in message
    assert "connection:wrong:lease" in message


def test_omp_runtime_convergence_does_not_prove_an_incomplete_events_page() -> None:
    assert _events_page_metadata({"events": [], "total": 1, "has_more": True, "next_cursor": "cursor-1"})["complete"] is False
    assert (
        _events_page_metadata(
            {
                "events": [],
                "total": 0,
                "generation_id": "generation-1",
                "branch_mode": "head",
                "has_more": False,
                "next_cursor": None,
            }
        )["complete"]
        is True
    )
    assert (
        _events_page_metadata(
            {
                "events": [{"id": "event-1"}, {"id": "event-2"}],
                "total": 6,
                "generation_id": "generation-1",
                "branch_mode": "head",
                "has_more": False,
                "next_cursor": None,
            }
        )["complete"]
        is True
    )


def test_omp_runtime_events_snapshot_exhausts_pages_under_one_generation(monkeypatch) -> None:
    pages = iter(
        [
            {
                "session_id": "session-1",
                "events": [{"id": "event-1"}],
                "total": 2,
                "generation_id": "generation-1",
                "branch_mode": "head",
                "has_more": True,
                "next_cursor": "cursor/2",
            },
            {
                "session_id": "session-1",
                "events": [{"id": "event-2"}],
                "total": 2,
                "generation_id": "generation-1",
                "branch_mode": "head",
                "has_more": False,
                "next_cursor": None,
            },
        ]
    )
    requests: list[str] = []

    def runtime_get(_api_url: str, _token: str, path: str) -> dict[str, object]:
        requests.append(path)
        return next(pages)

    monkeypatch.setattr(omp_helm_lifecycle, "_runtime_get", runtime_get)

    snapshot = _runtime_events_snapshot("https://runtime.example", "token", "session-1")

    assert [event["id"] for event in snapshot["events"]] == ["event-1", "event-2"]
    assert snapshot["pagination"] == {
        "pages_read": 2,
        "exhausted": True,
        "generation_id": "generation-1",
    }
    assert "cursor=cursor%2F2" in requests[1]


def test_omp_runtime_events_snapshot_rejects_generation_change(monkeypatch) -> None:
    pages = iter(
        [
            {
                "session_id": "session-1",
                "events": [],
                "total": 0,
                "generation_id": "generation-1",
                "branch_mode": "head",
                "has_more": True,
                "next_cursor": "cursor-2",
            },
            {
                "session_id": "session-1",
                "events": [],
                "total": 0,
                "generation_id": "generation-2",
                "branch_mode": "head",
                "has_more": False,
                "next_cursor": None,
            },
        ]
    )
    monkeypatch.setattr(omp_helm_lifecycle, "_runtime_get", lambda *_args: next(pages))

    with pytest.raises(RuntimeError, match="changed the events generation"):
        _runtime_events_snapshot("https://runtime.example", "token", "session-1")


def test_omp_wait_native_marker_ignores_tool_arguments(monkeypatch, tmp_path) -> None:
    marker = "OMP_MARKER"
    session_file = tmp_path / "session.jsonl"
    session_file.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "message",
                        "id": "tool-argument",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "toolCall", "arguments": {"command": f"echo {marker}"}}],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "message",
                        "id": "assistant-text",
                        "message": {"role": "assistant", "content": [{"type": "text", "text": marker}]},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(omp_helm_lifecycle, "_wait", lambda observe, **_kwargs: observe())

    row = _wait_native_marker(session_file, marker)

    assert row["id"] == "assistant-text"


def test_omp_wait_served_run_retirement_waits_for_terminal_fact(monkeypatch) -> None:
    observations = iter(
        [
            {"retired": False, "active_run_count": None},
            {"retired": True, "session_id": "session-1", "active_run_count": 0},
        ]
    )
    monkeypatch.setattr(
        omp_helm_lifecycle.lifecycle,
        "_served_run_inventory_evidence",
        lambda *_args: next(observations),
    )

    result = _wait_served_run_retirement(
        "https://runtime.example",
        "token",
        "session-1",
        [{"session_id": "session-1", "run_id": "run-1", "state": "terminal"}],
        timeout=1,
    )

    assert result["retired"] is True
    assert result["retirement_wait_status"] == "pass"


def test_omp_runtime_convergence_retains_unproven_page_metadata(monkeypatch) -> None:
    marker = "OMP_INCOMPLETE_PAGE"
    monkeypatch.setattr(
        "zerg.qa.omp_helm_lifecycle._runtime_snapshot",
        lambda *_args: {
            "detail": {},
            "thread": {},
            "events": {
                "events": [{"id": "event-1", "role": "assistant", "content_text": marker}],
                "total": 2,
                "generation_id": "generation-1",
                "branch_mode": "head",
                "next_cursor": "cursor-2",
                "has_more": True,
            },
            "diagnostic": {"served_path": "canonical_session_detail"},
        },
    )

    convergence = _runtime_convergence(
        "https://runtime.example",
        "token",
        session_id="session-1",
        native_session_id="native-1",
        marker=marker,
        flush={"status": "pass"},
        native_source_path="/native/session.jsonl",
        timeout=1,
    )

    assert convergence["status"] == "unproven"
    assert convergence["events_page"]["has_more"] is True
    assert convergence["events_page"]["next_cursor"] == "cursor-2"


def test_omp_registers_each_native_source_before_later_cleanup() -> None:
    claims = []
    _register_native_source(
        claims,
        label="initial",
        source_path="/omp/initial.jsonl",
        session_id="session-1",
        native_session_id="native-1",
    )
    _register_native_source(
        claims,
        label="replacement",
        source_path="/omp/replacement.jsonl",
        session_id="session-1",
        native_session_id="native-2",
    )
    _register_native_source(
        claims,
        label="duplicate",
        source_path="/omp/initial.jsonl",
        session_id="session-1",
        native_session_id="native-1",
    )

    assert [item["source_path"] for item in claims] == ["/omp/initial.jsonl", "/omp/replacement.jsonl"]
    assert claims[1]["native_session_id"] == "native-2"


def test_omp_cleanup_retains_generation_owner_birth_and_dead_evidence(monkeypatch) -> None:
    monkeypatch.setattr("zerg.qa.omp_helm_lifecycle._pid_dead", lambda _pid: True)
    monkeypatch.setattr("zerg.qa.omp_helm_lifecycle._pgid_dead", lambda _pgid: True)
    records = [
        {
            "owner": owner,
            "label": label,
            "pid": 100 if label == "launcher" else 101,
            "process_group_id": 200 if label == "launcher" else 201,
            "birth": f"{owner}-{label}",
            "expected_birth": f"{owner}-{label}",
            "birth_matches": True,
        }
        for owner in ("initial", "replacement", "cold_resume")
        for label in ("launcher", "provider")
    ]

    cleanup = _cleanup_receipt(records)

    assert cleanup["status"] == "pass"
    assert cleanup["owned_process_count"] == 6
    assert all(
        item["owner"] in {"initial", "replacement", "cold_resume"}
        and item["process_group_id"] > 0
        and item["pid_positive"] is True
        and item["process_group_positive"] is True
        and item["pid_dead"] is True
        and item["process_group_dead"] is True
        for item in cleanup["owned_processes"]
    )


def test_omp_cleanup_waits_for_async_owner_exit(monkeypatch) -> None:
    records = [{"label": "provider", "pid": 101, "process_group_id": 201, "birth_matches": True}]
    receipts = iter(
        [
            {"status": "fail", "birth_identities_verified": True},
            {"status": "pass", "birth_identities_verified": True},
        ]
    )
    monkeypatch.setattr(omp_helm_lifecycle, "_cleanup_receipt", lambda _records: next(receipts))
    monkeypatch.setattr(omp_helm_lifecycle.time, "sleep", lambda _seconds: None)

    assert _wait_cleanup_receipt(records, timeout=1) == {"status": "pass", "birth_identities_verified": True}


def test_omp_keeps_isolation_when_complete_source_retention_fails(tmp_path) -> None:
    isolation = tmp_path / "isolation"
    isolation.mkdir()
    source = isolation / "session.jsonl"
    source.write_bytes(b"complete native bytes\n")
    retained = lifecycle._retain_claim_sources(
        tmp_path,
        [{"run_id": "initial", "source_path": str(source)}],
        {},
        complete=True,
    )
    cleanup: dict[str, object] = {}

    assert retained[0]["complete"] is True
    assert (tmp_path / str(retained[0]["path"])).read_bytes() == source.read_bytes()
    assert (
        _remove_isolation_after_source_retention(
            isolation,
            source_retention_verified=False,
            cleanup=cleanup,
        )
        is False
    )
    assert isolation.exists()
    assert cleanup["authoritative_source_evidence_retained"] is True


def test_omp_selected_assertion_status_ignores_unrelated_sibling_failures() -> None:
    selected = _VARIANTS[0]
    assertions = {assertion: assertion == HELM_ASSERTIONS[0] for assertion in HELM_ASSERTIONS}

    assert _assertion_result_status(assertions, selected) == "pass"
    assert _assertion_result_status(assertions, "not-a-cell") == "fail"


def test_omp_console_settlement_and_context_recall_are_required() -> None:
    observation = {
        "runtime_host_turn_dispatch": True,
        "exact_session_thread_run_binding": True,
        "transcript_converged_exactly_once": True,
        "omp_continuation_context_recalled": True,
        "live_model_evidence": {
            "model": "openrouter/fixture-model",
            "source_artifacts": [{"path": "provider-sources/native.raw"}],
        },
        "omp_settlement": {
            "agent_end_terminal": True,
            "agent_end_evidence_shape": False,
            "provider_response_source_bound": True,
            "provider_response_source_kind": "stdout_path",
            "stream_drained": True,
            "native_archive_bound": True,
            "native_session_id_bound": True,
            "native_terminal_after_assistant": True,
            "native_marker_count": 1,
            "native_tool_evidence_complete": True,
            "malformed_source": False,
        },
        "no_orphan_provider_processes": True,
        "served_run_retired": True,
        "interrupt_contract_preserved": True,
        "post_interrupt_sendable": True,
        "canary_session_hidden": True,
    }

    assert omp_console_assertions(observation) == {CONSOLE_ASSERTION: True}
    observation["served_run_retired"] = False
    assert omp_console_assertions(observation)[CONSOLE_ASSERTION] is False
    observation["served_run_retired"] = True
    observation["omp_continuation_context_recalled"] = False
    assert omp_console_assertions(observation)[CONSOLE_ASSERTION] is False
    observation["omp_continuation_context_recalled"] = True
    observation["omp_settlement"]["agent_end_terminal"] = False
    assert omp_console_assertions(observation)[CONSOLE_ASSERTION] is False


def test_omp_helm_assertions_do_not_use_agent_settled_as_completion() -> None:
    observation = {
        "observation_scope": "scenario",
        "omp_native_extension_channel_bound": True,
        "omp_agent_end_settlement_observed": True,
        "omp_native_archive_bound": True,
        "omp_transcript_shipper_started": True,
        "omp_transcript_flush_completed": True,
        "omp_runtime_transcript_converged": True,
        "runtime_agents_api_controls": True,
        "runtime_control_identity_complete": True,
        "runtime_control_identity": {
            "initial": _identity_receipt("connection:initial:lease-1"),
            "replacement": _identity_receipt("connection:replacement:lease-2"),
            "cold_resume": _identity_receipt("connection:resume:lease-3"),
            "final": _identity_receipt("connection:resume:lease-3"),
        },
        "send_idle": True,
        "follow_up_native": True,
        "steer_active": True,
        "abort_native": True,
        "terminate_owned": True,
        "cold_resume_exact_file": True,
        "stale_owner_refused": True,
        "native_replacement_bound": True,
        "settlement": {
            "status": "pass",
            "agent_end_terminal": True,
            "agent_end_evidence_shape": True,
            "native_session_header_count": 1,
            "native_terminal_after_assistant": True,
            "malformed_source": False,
            "native_archive_bound": True,
            "agent_settled_is_not_completion_contract": True,
        },
        "cleanup": {
            "status": "pass",
            "provider_process_dead": True,
            "process_group_dead": True,
            "orphan_count": 0,
            "shipper_stop_verified": True,
            "canary_session_hidden": True,
            "served_run_retired": True,
            "source_retention_verified": True,
            "isolation_removed": True,
        },
        "channel_binding": {
            "ready": True,
            "session_id_present": True,
            "native_session_id_present": True,
            "connection_id_present": True,
            "lease_generation_present": True,
            "session_file_present": True,
        },
        "send_evidence": {"native_source_bound": True, "marker_count": 1, "channel_ack_bound": True},
        "follow_up_evidence": {"native_source_bound": True, "marker_count": 1, "channel_ack_bound": True},
        "steer_evidence": {
            "native_source_bound": True,
            "marker_count": 1,
            "channel_ack_bound": True,
            "active_command_bound": True,
            "steer_command_bound": True,
            "active_state": {"phase": "thinking", "native_session_id": "native-1"},
            "native_session_id": "native-1",
        },
        "cold_resume_evidence": {
            "native_source_bound": True,
            "channel_terminal_bound": True,
            "marker_count": 1,
            "context_recalled": True,
            "context_marker_count": 1,
            "terminal": True,
            "exact_file": True,
        },
        "replacement_evidence": {"native_source_bound": True, "marker_count": 1, "channel_ack_bound": True},
        "stale_owner_evidence": {"error_code": "stale_channel"},
        "abort_evidence": {
            "channel_source_bound": True,
            "terminal": True,
            "channel_ack_bound": True,
        },
    }

    assert set(omp_helm_lifecycle_assertions(observation)) == set(HELM_ASSERTIONS)
    assert all(omp_helm_lifecycle_assertions(observation).values())
    observation["cold_resume_evidence"]["context_marker_count"] = 2
    assert omp_helm_lifecycle_assertions(observation)["omp_helm_cold_resume_exact_file"] is False
    observation["cold_resume_evidence"]["context_marker_count"] = 1
    cold_owner = list(observation["runtime_control_identity"]["cold_resume"]["owner_identity"])
    observation["runtime_control_identity"]["final"] = _identity_receipt(
        "connection:resume:lease-4",
        owner_identity=cold_owner,
    )
    assert all(omp_helm_lifecycle_assertions(observation).values())
    observation["runtime_control_identity"]["final"]["owner_identity"][1] = "native-other"
    assert omp_helm_lifecycle_assertions(observation)["omp_helm_launch_registration"] is False
    observation["runtime_control_identity"]["replacement"]["control_subject_key"] = "connection:wrong:lease-2"
    assert omp_helm_lifecycle_assertions(observation)["omp_helm_launch_registration"] is False
    observation["runtime_control_identity"] = {}
    assert omp_helm_lifecycle_assertions(observation)["omp_helm_launch_registration"] is False
    observation["abort_evidence"]["terminal"] = False
    assert omp_helm_lifecycle_assertions(observation)["omp_helm_abort_native"] is False


def test_omp_runtime_control_identity_rejects_missing_owner_fields() -> None:
    labels = ("initial", "replacement", "cold_resume", "final")
    cold_owner = _identity_receipt("connection:cold_resume:lease-1")["owner_identity"]
    valid = {
        label: _identity_receipt(
            f"connection:{label}:lease-1",
            owner_identity=list(cold_owner) if label == "final" else None,
        )
        for label in labels
    }
    assert _runtime_control_identity_is_complete(valid)

    invalid = {
        label: _identity_receipt(
            f"connection:{label}:lease-1",
            owner_identity=[None] * 7,
        )
        for label in labels
    }
    assert not _runtime_control_identity_is_complete(invalid)


def test_omp_cleanup_gate_is_required_for_every_selected_assertion() -> None:
    cleanup = {
        "status": "pass",
        "provider_process_dead": True,
        "process_group_dead": True,
        "orphan_count": 0,
        "shipper_stop_verified": True,
        "canary_session_hidden": True,
        "served_run_retired": True,
        "source_retention_verified": True,
        "isolation_removed": True,
    }

    assert _helm_cleanup_ready(cleanup)
    cleanup["source_retention_verified"] = False
    assert not _helm_cleanup_ready(cleanup)


def test_omp_result_status_cannot_pass_on_a_selected_assertion_without_final_evidence() -> None:
    assertions = {assertion: assertion == HELM_ASSERTIONS[0] for assertion in HELM_ASSERTIONS}

    assert _helm_result_status(assertions, _VARIANTS[0], cleanup_ready=False, manifest_stable=True) == "fail"
    assert _helm_result_status(assertions, _VARIANTS[0], cleanup_ready=True, manifest_stable=False) == "fail"
    assert _helm_result_status(assertions, _VARIANTS[0], cleanup_ready=True, manifest_stable=True) == "fail"


def test_omp_served_projection_requires_exact_runtime_identity_and_marker_event() -> None:
    marker = "OMP_EXACT_MARKER"
    snapshot = {
        "detail": {"id": "session-1", "provider": "omp", "provider_session_id": "native-1"},
        "thread": {
            "root_session_id": "session-1",
            "head_session_id": "session-1",
            "sessions": [{"id": "session-1"}],
        },
        "events": {
            "events": [{"id": "event-1", "role": "assistant", "content_text": marker}],
            "total": 1,
            "generation_id": "generation-1",
            "branch_mode": "head",
            "next_cursor": None,
            "has_more": False,
        },
        "diagnostic": {"session_id": "session-1", "served_path": "canonical_session_detail"},
    }

    projection = _served_projection_evidence(
        snapshot,
        session_id="session-1",
        native_session_id="native-1",
        marker=marker,
    )

    assert projection["detail"] == {
        "id": "session-1",
        "provider": "omp",
        "provider_session_id": "native-1",
    }
    assert projection["marker_event_id"] == "event-1"
    assert projection["marker_events"][0]["content_text"] == marker
    assert projection["marker_event_durable"] is True
    assert projection["marker_event_identity_bound"] is True

    snapshot["events"]["events"][0].pop("id")
    projection = _served_projection_evidence(
        snapshot,
        session_id="session-1",
        native_session_id="native-1",
        marker=marker,
    )
    assert projection["marker_event_identity_bound"] is False


def test_omp_runtime_convergence_rejects_wrong_served_provider(monkeypatch) -> None:
    marker = "OMP_IDENTITY_MARKER"
    monkeypatch.setattr(
        "zerg.qa.omp_helm_lifecycle._runtime_snapshot",
        lambda *_args: {
            "detail": {"id": "session-1", "provider": "codex", "provider_session_id": "native-1"},
            "thread": {
                "root_session_id": "session-1",
                "head_session_id": "session-1",
                "sessions": [{"id": "session-1"}],
            },
            "events": {
                "events": [{"id": "event-1", "role": "assistant", "content_text": marker}],
                "total": 1,
                "generation_id": "generation-1",
                "branch_mode": "head",
                "next_cursor": None,
                "has_more": False,
            },
            "diagnostic": {"session_id": "session-1", "served_path": "canonical_session_detail"},
        },
    )

    with pytest.raises(RuntimeError, match="timed out waiting for Runtime Host convergence"):
        _runtime_convergence(
            "https://runtime.example",
            "token",
            session_id="session-1",
            native_session_id="native-1",
            marker=marker,
            flush={"status": "pass"},
            native_source_path="/native/session.jsonl",
            timeout=0.01,
        )


def test_omp_manifest_stability_detects_post_manifest_mutation(tmp_path) -> None:
    evidence = tmp_path / "cleanup-receipt.json"
    evidence.write_text("{}\n", encoding="utf-8")
    from zerg.qa.provider_release_identity import artifact_manifest

    manifest = artifact_manifest(tmp_path)
    assert {entry["path"] for entry in manifest} == {"cleanup-receipt.json"}
    (tmp_path / "result.json").write_text(json.dumps({"artifact_manifest": manifest}), encoding="utf-8")
    assert artifact_manifest(tmp_path) == manifest
    assert _manifest_is_stable(tmp_path, manifest)
    evidence.write_text('{"status":"fail"}\n', encoding="utf-8")
    assert not _manifest_is_stable(tmp_path, manifest)


@pytest.mark.parametrize(
    ("module_name", "runner_name", "variant"),
    (
        ("zerg.qa.omp_console_producer", "run_omp_console", CONSOLE_VARIANT),
        ("zerg.qa.omp_helm_lifecycle", "run_omp_helm", _VARIANTS[0]),
    ),
)
def test_omp_main_failure_retains_partial_artifact_manifest(monkeypatch, tmp_path, module_name, runner_name, variant, capsys) -> None:
    module = __import__(module_name, fromlist=["main"])

    def fail(args):
        args.evidence_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        (args.evidence_root / "partial-receipt.json").write_text("{}\n", encoding="utf-8")
        raise RuntimeError("synthetic qualification failure")

    monkeypatch.setattr(module, runner_name, fail)

    arguments = ["--variant", variant, "--evidence-root", str(tmp_path / "evidence")]
    if module_name == "zerg.qa.omp_console_producer":
        arguments.extend(["--model", "fixture-model"])
    result = module.main(arguments)

    assert result == 1
    payload = json.loads((tmp_path / "evidence" / "result.json").read_text(encoding="utf-8"))
    assert payload["observation_scope"] == "scenario"
    assert payload["failure_code"].endswith("_lifecycle_failed")
    assert payload["error"] == "RuntimeError: synthetic qualification failure"
    assert [entry["path"] for entry in payload["artifact_manifest"]] == ["partial-receipt.json"]
    assert "synthetic qualification failure" in capsys.readouterr().out


def test_omp_semantic_entrypoint_uses_validated_request_and_runtime_token(tmp_path, monkeypatch) -> None:
    from zerg.qa import omp_helm_lifecycle as helm

    request_path = tmp_path / "request.json"
    request_path.write_text("{}\n", encoding="utf-8")
    output_root = tmp_path / "output"
    request = {"provider_bin": "/validated/omp", "expected_provider_version": "1.2.3"}
    captured: dict[str, object] = {}
    monkeypatch.setenv("LONGHOUSE_RUNTIME_AGENTS_TOKEN", "runtime-token")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(helm.identity, "load_request", lambda *_args, **_kwargs: request)

    def fake_run(args):
        captured.update(
            {
                "provider_bin": args.provider_bin,
                "provider_version": args.provider_version,
                "agents_token": args.agents_token,
                "variant": args.variant,
            }
        )
        return {"status": "pass", "observation": {}}

    def fake_semantic(_request_path, _output_root, **kwargs):
        captured["scenario_revision"] = kwargs["scenario_revision"]
        observation, assertions, secrets = kwargs["executor"](Path("/factory/omp"), tmp_path / "semantic-evidence")
        captured["secrets"] = secrets
        captured["assertion_count"] = len(assertions)
        return {"observation": observation}

    monkeypatch.setattr(helm, "run_omp_helm", fake_run)
    monkeypatch.setattr(helm.semantic, "run_semantic_profile", fake_semantic)

    result = helm.run(request_path, output_root)

    assert result == {"observation": {}}
    assert captured == {
        "provider_bin": Path("/factory/omp"),
        "provider_version": "1.2.3",
        "agents_token": "runtime-token",
        "variant": None,
        "scenario_revision": 8,
        "secrets": ("runtime-token",),
        "assertion_count": len(HELM_ASSERTIONS),
    }


def test_omp_helm_oracle_rejects_archive_only_or_unbound_evidence() -> None:
    observation = {
        "omp_native_extension_channel_bound": True,
        "send_idle": True,
        "steer_active": True,
        "abort_native": True,
        "terminate_owned": True,
        "cold_resume_exact_file": True,
        "stale_owner_refused": True,
        "native_replacement_bound": True,
        "settlement": {"agent_end_terminal": True, "native_archive_bound": True},
        "cleanup": {
            "status": "pass",
            "provider_process_dead": True,
            "process_group_dead": True,
            "orphan_count": 0,
            "canary_session_hidden": False,
        },
    }

    assert all(value is False for value in omp_helm_lifecycle_assertions(observation).values())


def test_omp_console_oracle_requires_exact_native_settlement_and_retirement() -> None:
    observation = {
        "runtime_host_turn_dispatch": True,
        "omp_continuation_context_recalled": True,
        "omp_settlement": {
            "agent_end_terminal": True,
            "stream_drained": True,
            "native_archive_bound": True,
            "native_session_id_bound": False,
            "native_terminal_after_assistant": False,
            "native_marker_count": 0,
        },
        "no_orphan_provider_processes": True,
        "canary_session_hidden": False,
    }

    assert omp_console_assertions(observation)[CONSOLE_ASSERTION] is False


def test_omp_cleanup_retirement_is_bound_to_the_exact_hidden_archived_session() -> None:
    assert not _exact_session_retirement({"status": "pass", "hidden": True}, "session-1")
    assert not _exact_session_retirement(
        {
            "status": "pass",
            "session_id": "other-session",
            "hidden": True,
            "archived": True,
            "present_in_served_inventory": False,
        },
        "session-1",
    )
    assert _exact_session_retirement(
        {
            "status": "pass",
            "session_id": "session-1",
            "hidden": True,
            "archived": True,
            "present_in_served_inventory": False,
        },
        "session-1",
    )
