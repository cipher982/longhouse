from __future__ import annotations

import argparse
import json
import time

import zerg.qa.codex_helm_launch_visibility as launch


def test_registration_binds_one_codex_provider_release_cell():
    registration = launch.REGISTRATION.to_dict()

    assert registration["subject_kind"] == "provider_release"
    assert registration["provider_artifact_required"] is True
    assert registration["providers"] == ["codex"]
    assert registration["scenario_id"] == "codex_helm_launch_visibility"
    assert registration["assertion_cells"] == [
        {"assertion_id": "helm_launch_visibility_preserved", "variant": None}
    ]
    assert registration["credential_binding_ids"] == ["codex_provider_token", "runtime_host_control"]


def test_recording_proxy_retains_registration_identity_without_authority_tokens():
    proxy = launch.RuntimeHostRecordingProxy("https://runtime.invalid")
    try:
        proxy._capture_registration(  # noqa: SLF001 - prove the evidence redaction boundary
            json.dumps(
                {
                    "provider": "codex",
                    "launch_actor": "human_shell",
                    "launch_surface": "terminal",
                    "session_id": "session-1",
                }
            ).encode(),
            json.dumps(
                {
                    "session_id": "session-1",
                    "run_id": "run-1",
                    "coordination_authority": {"token": "must-not-survive"},
                }
            ).encode(),
            200,
        )

        record = proxy.wait_registration(after=0, timeout=0.1)
        assert record["response"] == {"session_id": "session-1", "run_id": "run-1"}
        assert "must-not-survive" not in json.dumps(record)
    finally:
        proxy.server.server_close()


def test_wait_canonical_launch_requires_exact_run_open_and_default_visibility(monkeypatch):
    registration = {
        "request": {"session_id": "session-1"},
        "response": {"session_id": "session-1", "run_id": "run-1"},
    }

    def request(_args, path: str, _method: str, _body):
        assert path == "sessions/session-1/state-diagnostics"
        return {
            "catalog_commit_seq": 9,
            "shadow": {"mode": "helm", "control": {"connection": "connected"}, "control_run_id": "run-1"},
            "explain": {
                "working_set": "open",
                "launch_actor": "human_shell",
                "launch_surface": "terminal",
                "origin_kind": "managed_local",
                "fact_sources": {"control": {"source": "provider_control"}},
            },
        }

    monkeypatch.setattr(launch, "_runtime_request", request)
    monkeypatch.setattr(launch, "_session_visible", lambda *_args, **_kwargs: True)
    result = launch._wait_canonical_launch(  # noqa: SLF001 - pure product-proof seam
        argparse.Namespace(wait_ready_secs=0.1),
        registration=registration,
        project="proof",
        device_id="machine",
        expected_actor="human_shell",
        expected_surface="terminal",
        expect_visible=True,
        launched_at=time.monotonic(),
    )

    assert result["working_set"] == "open"
    assert result["control_run_id"] == "run-1"
    assert result["default_timeline_visible"] is True
