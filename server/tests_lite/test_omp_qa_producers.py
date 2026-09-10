from __future__ import annotations

import json
from pathlib import Path

import pytest

from zerg.qa import provider_console_lifecycle as lifecycle
from zerg.qa.omp_console_producer import ASSERTION_ID as CONSOLE_ASSERTION
from zerg.qa.omp_console_producer import REGISTRATION as CONSOLE_REGISTRATION
from zerg.qa.omp_console_producer import omp_console_assertions
from zerg.qa.omp_console_producer import omp_native_model_evidence
from zerg.qa.omp_helm_lifecycle import _VARIANTS
from zerg.qa.omp_helm_lifecycle import ASSERTIONS as HELM_ASSERTIONS
from zerg.qa.omp_helm_lifecycle import REGISTRATION as HELM_REGISTRATION
from zerg.qa.omp_helm_lifecycle import _assertion_result_status
from zerg.qa.omp_helm_lifecycle import _cleanup_receipt
from zerg.qa.omp_helm_lifecycle import _events_page_metadata
from zerg.qa.omp_helm_lifecycle import _exact_session_retirement
from zerg.qa.omp_helm_lifecycle import _flush_receipt_complete
from zerg.qa.omp_helm_lifecycle import _helm_cleanup_ready
from zerg.qa.omp_helm_lifecycle import _helm_result_status
from zerg.qa.omp_helm_lifecycle import _manifest_is_stable
from zerg.qa.omp_helm_lifecycle import _native_settlement
from zerg.qa.omp_helm_lifecycle import _register_native_source
from zerg.qa.omp_helm_lifecycle import _remove_isolation_after_source_retention
from zerg.qa.omp_helm_lifecycle import _runtime_convergence
from zerg.qa.omp_helm_lifecycle import _served_projection_evidence
from zerg.qa.omp_helm_lifecycle import omp_helm_lifecycle_assertions
from zerg.qa.provider_qualification import _PROFILES


def test_omp_qualification_producers_are_registered_on_their_own_contracts() -> None:
    assert CONSOLE_REGISTRATION.producer_id == "omp.console_lifecycle.v1"
    assert CONSOLE_REGISTRATION.producer_revision == 5
    assert CONSOLE_REGISTRATION.providers == ("omp",)
    assert CONSOLE_REGISTRATION.scenario_id == "omp_console_lifecycle"
    assert CONSOLE_REGISTRATION.scenario_revision == 5
    assert "console_continuation_receipt" in CONSOLE_REGISTRATION.required_artifacts
    assert HELM_REGISTRATION.producer_id == "omp.helm_lifecycle.v1"
    assert HELM_REGISTRATION.producer_revision == 6
    assert HELM_REGISTRATION.scenario_revision == 6
    assert HELM_REGISTRATION.providers == ("omp",)
    assert HELM_REGISTRATION.scenario_id == "omp_helm_lifecycle"
    assert "transcript_flush_receipt" in HELM_REGISTRATION.required_artifacts
    assert "transcript_shipper_receipt" in HELM_REGISTRATION.required_artifacts
    assert "runtime_convergence_receipt" in HELM_REGISTRATION.required_artifacts
    assert ("omp", "omp_print_v1") in _PROFILES
    assert ("omp", "omp_helm_v1") in _PROFILES


def test_omp_native_model_evidence_binds_provider_event_to_retained_source(tmp_path) -> None:
    source = tmp_path / "omp-native.jsonl"
    source.write_text(
        "\n".join(
            json.dumps(event)
            for event in [
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
    (tmp_path / "provider-source-retention.json").write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "path": str(source),
                        "kind": "source_path",
                        "retained": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    evidence = omp_native_model_evidence(
        tmp_path,
        source_canary="omp_console_lifecycle",
        qualification_model="openrouter/fixture-model",
        api_key_configured=True,
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
    assert artifact["native_event_sha256"].startswith("sha256:") and len(artifact["native_event_sha256"]) == 71


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
            return json.dumps({"outcome": "sent"}).encode("utf-8")

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
            "agent_end_evidence_shape": True,
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
        "interrupt_contract_preserved": True,
        "post_interrupt_sendable": True,
        "canary_session_hidden": True,
    }

    assert omp_console_assertions(observation) == {CONSOLE_ASSERTION: True}
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
        "steer_evidence": {"native_source_bound": True, "marker_count": 1, "channel_ack_bound": True},
        "cold_resume_evidence": {
            "native_source_bound": True,
            "channel_terminal_bound": True,
            "marker_count": 1,
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
    observation["abort_evidence"]["terminal"] = False
    assert omp_helm_lifecycle_assertions(observation)["omp_helm_abort_native"] is False


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
