#!/usr/bin/env python3
"""Tests for hosted provider-live route E2E harness."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS = REPO_ROOT / "scripts/qa/provider-live-route-e2e.py"


class _ServerState:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.bad_mismatch_shape = False


class _Handler(BaseHTTPRequestHandler):
    server_version = "ProviderLiveRouteE2ETest/1"

    def _write_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @property
    def state(self) -> _ServerState:
        return self.server.state  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        if self.path != "/api/agents/machines":
            self._write_json(404, {"detail": "not found"})
            return
        self._write_json(
            200,
            {
                "machines": [
                    {
                        "device_id": "cinder",
                        "online": True,
                        "engine_build": "test-build",
                        "supports": ["opencode.live_proof"],
                    }
                ]
            },
        )

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or "0")
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        self.state.requests.append(body)
        if self.path != "/api/agents/machines/cinder/provider-live-proof":
            self._write_json(404, {"detail": "not found"})
            return
        expected = body["expected_provider_version"]
        if expected == "1.15.11":
            self._write_json(
                200,
                {
                    "device_id": "cinder",
                    "provider": "opencode",
                    "command_id": "cmd-test",
                    "result": {
                        "provider": "opencode",
                        "transport": "provider_live_proof",
                        "artifact": {
                            "artifact_kind": "provider_live_canary",
                            "provider": "opencode",
                            "provider_version": "1.15.11",
                            "verdict": "green",
                        },
                        "provider_version_match": {
                            "status": "match",
                            "expected_provider_version": expected,
                            "artifact_provider_version": "1.15.11",
                        },
                    },
                },
            )
            return
        if self.state.bad_mismatch_shape:
            self._write_json(502, {"detail": "origin hid the typed body"})
            return
        self._write_json(
            409,
            {
                "detail": {
                    "code": "provider_version_mismatch",
                    "message": "provider live proof version mismatch",
                }
            },
        )

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return


def _run_server(state: _ServerState):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.state = state  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _write_inputs(root: Path) -> tuple[Path, Path]:
    token_file = root / "device-token"
    token_file.write_text("zdt_test-token\n", encoding="utf-8")
    proof_dir = root / "proof"
    proof_dir.mkdir()
    (proof_dir / "opencode.json").write_text(
        json.dumps(
            {
                "artifact_kind": "provider_live_canary",
                "provider": "opencode",
                "provider_version": "1.15.11",
                "verdict": "green",
            }
        ),
        encoding="utf-8",
    )
    return token_file, proof_dir


def _run_harness(
    root: Path, api_url: str, token_file: Path, proof_dir: Path
) -> tuple[subprocess.CompletedProcess[str], dict]:
    artifact = root / "artifact.json"
    result = subprocess.run(
        [
            sys.executable,
            str(HARNESS),
            "--api-url",
            api_url,
            "--device-id",
            "cinder",
            "--token-file",
            str(token_file),
            "--proof-dir",
            str(proof_dir),
            "--provider",
            "opencode",
            "--artifact",
            str(artifact),
            "--json",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, json.loads(artifact.read_text(encoding="utf-8"))


def test_route_e2e_requires_match_and_typed_mismatch() -> None:
    state = _ServerState()
    server, api_url = _run_server(state)
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            token_file, proof_dir = _write_inputs(root)
            result, payload = _run_harness(root, api_url, token_file, proof_dir)

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["verdict"] == "green"
        assert payload["engine_build"] == "test-build"
        assert payload["results"][0]["status"] == "pass"
        assert [request["expected_provider_version"] for request in state.requests] == [
            "1.15.11",
            "9.9.9-longhouse-route-e2e",
        ]
    finally:
        server.shutdown()


def test_route_e2e_fails_when_mismatch_is_not_typed() -> None:
    state = _ServerState()
    state.bad_mismatch_shape = True
    server, api_url = _run_server(state)
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            token_file, proof_dir = _write_inputs(root)
            result, payload = _run_harness(root, api_url, token_file, proof_dir)

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["results"][0]["failure_code"] == "provider_live_mismatch_not_typed"
    finally:
        server.shutdown()


def main() -> int:
    tests = [
        test_route_e2e_requires_match_and_typed_mismatch,
        test_route_e2e_fails_when_mismatch_is_not_typed,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
