from __future__ import annotations

import argparse
import http.server
import inspect
import json
import os
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import pytest

import zerg.qa.codex_helm_launch_visibility as launch


def test_registration_binds_one_codex_provider_release_cell():
    registration = launch.REGISTRATION.to_dict()

    assert registration["subject_kind"] == "provider_release"
    assert registration["provider_artifact_required"] is True
    assert registration["providers"] == ["codex"]
    assert registration["scenario_id"] == "codex_helm_launch_visibility"
    assert registration["assertion_cells"] == [{"assertion_id": "helm_launch_visibility_preserved", "variant": None}]
    assert registration["credential_binding_ids"] == ["codex_provider_token", "runtime_host_control"]
    assert registration["producer_revision"] == 7
    assert registration["scenario_revision"] == 5


def test_helm_launch_bridge_socket_fits_linux_unix_path_budget():
    for prefix in ("lch-", "lca-"):
        isolation_root = Path("/tmp") / f"{prefix}{'x' * 8}"
        socket_path = launch.bridge_canary._bridge_state_root(isolation_root) / f"{'s' * 36}.sock"
        assert len(os.fsencode(socket_path)) < 108


def test_recording_proxy_reports_wrapper_exit_before_registration():
    proxy = launch.RuntimeHostRecordingProxy("https://runtime.invalid")
    process = subprocess.Popen(["/usr/bin/true"], stdout=subprocess.PIPE)
    process.wait(timeout=5)
    try:
        with pytest.raises(RuntimeError, match="exited before registration"):
            proxy.wait_registration(after=0, timeout=5, process=process)
    finally:
        proxy.server.server_close()


def test_cleanup_evidence_binds_both_registered_cleanup_requirements():
    cleanups = [
        {"status": "pass", "axes": {"owned_processes_dead": True}},
        {"status": "pass", "axes": {"owned_processes_dead": True}},
    ]

    status, requirements = launch._cleanup_evidence(cleanups)  # noqa: SLF001

    assert status == "pass"
    assert requirements == {
        "runtime_host_canary_isolated": True,
        "owned_processes_dead": True,
    }


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


def test_recording_proxy_forwards_and_retains_managed_launch_identity():
    observed = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - stdlib callback name
            observed["user_agent"] = self.headers.get("User-Agent")
            body = json.dumps({"session_id": "session-1", "run_id": "run-1"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    host, port = upstream.server_address[:2]
    proxy = launch.RuntimeHostRecordingProxy(f"http://{host}:{port}")
    proxy.start()
    try:
        request = urllib.request.Request(
            f"{proxy.url}/api/sessions/managed-local/this-device",
            headers={"User-Agent": "longhouse-engine/test-version"},
            data=json.dumps({"provider": "codex", "session_id": "session-1"}).encode(),
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 200
        assert observed["user_agent"] == "longhouse-engine/test-version"
        assert proxy.wait_registration(after=0, timeout=0.1)["response"] == {
            "session_id": "session-1",
            "run_id": "run-1",
        }
    finally:
        proxy.close()
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=5)


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


def test_launch_lane_records_the_session_before_asserting_visibility():
    """A failed visibility assertion must still leave the bridge stoppable.

    ``created_session_ids`` is the only thing that tells the finally block to
    stop the codex bridge. When the id was read *after* ``_wait_canonical_launch``
    a raising assertion skipped it entirely, and every failed launch orphaned a
    codex-bridge and app-server for the life of the container. The automation
    lane in the same module already reads the id before it asserts.
    """
    source = inspect.getsource(launch._human_launch_sequence)  # noqa: SLF001 - ordering is the contract

    records_session = source.index("created_session_ids.append(session_id)")
    asserts_visibility = source.index("fresh_canonical = _wait_canonical_launch(")

    assert records_session < asserts_visibility, (
        "the launch lane must record the session id before waiting on canonical "
        "visibility, or a failed wait leaks the bridge it just started"
    )
