from __future__ import annotations

import argparse
import http.server
import json
import os
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

import zerg.qa.codex_helm_launch_visibility as launch


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
    monkeypatch.setattr(launch, "_session_visible", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        launch,
        "_browser_workspace",
        lambda *_args, **_kwargs: {
            "session": {
                "capabilities": {
                    "live_control_available": True,
                    "input_mode": "live",
                    "can_send_input": True,
                    "composer_enabled": True,
                    "composer_disabled_reason": None,
                    "control_label": "live",
                }
            }
        },
    )
    result = launch._wait_canonical_launch(  # noqa: SLF001 - pure product-proof seam
        argparse.Namespace(wait_ready_secs=0.1),
        registration=registration,
        project="proof",
        device_id="machine",
        expected_actor="human_shell",
        expected_surface="terminal",
        expect_visible=False,
        expect_open=True,
        launched_at=time.monotonic(),
    )

    assert result["working_set"] == "open"
    assert result["control_run_id"] == "run-1"
    assert result["default_timeline_visible"] is False


def test_infrastructure_failure_retains_cause_without_claiming_a_product_verdict(tmp_path, monkeypatch):
    codex_bin = tmp_path / "codex"
    codex_bin.write_bytes(b"fixture")

    class Proxy:
        def __init__(self, _target):
            pass

        def start(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(launch, "RuntimeHostRecordingProxy", Proxy)
    monkeypatch.setattr(launch.bridge_canary, "_run", lambda *_args, **_kwargs: SimpleNamespace(stdout="codex 1.0"))
    monkeypatch.setattr(
        launch,
        "_human_launch_sequence",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError('Runtime Host HTTP 503: {"detail":{"code":"resource_exhausted"}}')),
    )

    result = launch.run_scenario(
        argparse.Namespace(
            api_url="https://runtime.invalid",
            codex_bin=codex_bin,
            evidence_root=tmp_path / "evidence",
        )
    )

    assert result["status"] == "fail"
    assert result["failure_code"] == "codex_helm_launch_visibility_failed"
    assert "Runtime Host HTTP 503" in result["error"]
    assert "observation" not in result
    assert "assertions" not in result
    assert {item["path"] for item in result["artifact_manifest"]} == {"provider-binary-receipt.json"}


@pytest.mark.parametrize("bridge_receipt", [{}, {"verification": {"verified": False, "alive_pids": [42]}}])
def test_stop_launch_refuses_unverified_bridge_and_closes_wrapper(tmp_path, monkeypatch, bridge_receipt):
    process = subprocess.Popen(["/bin/sleep", "60"])

    def close():
        process.terminate()
        process.wait(timeout=5)

    tui = SimpleNamespace(process=process, close=close)
    monkeypatch.setattr(launch.bridge_canary, "_stop_bridge", lambda *_args: bridge_receipt)
    try:
        with pytest.raises(RuntimeError, match="bridge cleanup"):
            launch._stop_launch(
                argparse.Namespace(),
                tui=tui,
                session_id="owned-session",
                isolation_root=tmp_path,
            )
        assert process.poll() is not None
    finally:
        if process.poll() is None:
            close()
