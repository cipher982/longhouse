#!/usr/bin/env python3
"""Tests for hosted provider-live route E2E harness."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS = REPO_ROOT / "scripts/qa/provider-live-route-e2e.py"


class _ServerState:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.operations: dict[str, dict] = {}
        self.next_operation_id = 0
        self.bad_mismatch_shape = False
        self.provider_verdicts: dict[str, str] = {}
        self.transient_match_failures: dict[str, int] = {}
        self.session_search_hits: list[str] = []
        self.session_search_requests: list[dict] = []
        self.operation_poll_failures_remaining = 0
        self.operation_running_polls_remaining = 0

    def create_operation(self, provider: str, operation: dict) -> dict:
        self.next_operation_id += 1
        operation_id = f"op-{self.next_operation_id}"
        operation["operation_id"] = operation_id
        self.operations[operation_id] = operation
        return {
            "operation_id": operation_id,
            "status": "running",
            "status_url": f"/api/agents/machines/operations/{operation_id}",
            "device_id": "cinder",
            "provider": provider,
        }


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
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/agents/sessions":
            query = urllib.parse.parse_qs(parsed.query)
            marker = (query.get("query") or [""])[0]
            self.state.session_search_requests.append({"marker": marker, "query": query})
            sessions = (
                [{"id": "opencode-session-1", "provider": "opencode"}]
                if marker in self.state.session_search_hits
                else []
            )
            self._write_json(200, {"sessions": sessions, "total": len(sessions)})
            return
        if parsed.path.startswith("/api/agents/machines/operations/"):
            operation_id = parsed.path.rsplit("/", 1)[-1]
            operation = self.state.operations.get(operation_id)
            if operation is None:
                self._write_json(404, {"detail": "not found"})
                return
            if self.state.operation_poll_failures_remaining > 0:
                self.state.operation_poll_failures_remaining -= 1
                self._write_json(503, {"detail": {"code": "poll_transient", "message": "temporary poll failure"}})
                return
            if self.state.operation_running_polls_remaining > 0:
                self.state.operation_running_polls_remaining -= 1
                running = dict(operation)
                running["status"] = "running"
                running.pop("result", None)
                running.pop("error", None)
                self._write_json(200, running)
                return
            self._write_json(200, operation)
            return
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
                        "supports": ["claude.live_proof", "opencode.live_proof"],
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
        provider = body["provider"]
        expected = body["expected_provider_version"]
        matched_versions = {"claude": "2.1.153", "opencode": "1.15.11"}
        if expected == matched_versions.get(provider):
            transient_remaining = self.state.transient_match_failures.get(provider, 0)
            if transient_remaining > 0:
                self.state.transient_match_failures[provider] = transient_remaining - 1
                self._write_json(503, {"detail": "Request timed out"})
                return
            accepted = self.state.create_operation(
                provider,
                {
                    "device_id": "cinder",
                    "provider": provider,
                    "command_id": "cmd-test",
                    "command_type": "provider.live_proof",
                    "status": "succeeded",
                    "result": {
                        "provider": provider,
                        "transport": "provider_live_proof",
                        "artifact": {
                            "artifact_kind": "provider_live_canary",
                            "provider": provider,
                            "provider_version": expected,
                            "verdict": self.state.provider_verdicts.get(provider, "green"),
                            "canaries": {
                                "prompt_async_no_reply_delivery": {
                                    "status": "pass",
                                    "provider_session_id": "ses_opencode_test",
                                    "message_marker": "LONGHOUSE_OPENCODE_NOREPLY_TEST",
                                    "message_marker_sha256": "marker-sha",
                                }
                            }
                            if provider == "opencode"
                            else {},
                        },
                        "provider_version_match": {
                            "status": "match",
                            "expected_provider_version": expected,
                            "artifact_provider_version": expected,
                        },
                    },
                },
            )
            self._write_json(
                202,
                accepted,
            )
            return
        if self.state.bad_mismatch_shape:
            self._write_json(502, {"detail": "origin hid the typed body"})
            return
        accepted = self.state.create_operation(
            provider,
            {
                "device_id": "cinder",
                "provider": provider,
                "command_id": "cmd-test-mismatch",
                "command_type": "provider.live_proof",
                "status": "failed",
                "error": {
                    "code": "provider_version_mismatch",
                    "message": "provider live proof version mismatch",
                },
            },
        )
        self._write_json(
            202,
            accepted,
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
    (proof_dir / "claude.json").write_text(
        json.dumps(
            {
                "artifact_kind": "provider_live_canary",
                "provider": "claude",
                "provider_version": "2.1.153",
                "verdict": "green",
            }
        ),
        encoding="utf-8",
    )
    return token_file, proof_dir


def _run_harness(
    root: Path, api_url: str, token_file: Path, proof_dir: Path, *extra_args: str
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
            "--artifact",
            str(artifact),
            "--json",
            *extra_args,
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
        assert payload["mismatch_providers"] == ["claude"]
        assert [result["provider"] for result in payload["results"]] == ["claude", "opencode"]
        assert [result["status"] for result in payload["results"]] == ["pass", "pass"]
        assert [(request["provider"], request["expected_provider_version"]) for request in state.requests] == [
            ("claude", "2.1.153"),
            ("claude", "9.9.9-longhouse-route-e2e"),
            ("opencode", "1.15.11"),
        ]
    finally:
        server.shutdown()


def test_route_e2e_can_run_typed_mismatch_for_every_provider() -> None:
    state = _ServerState()
    server, api_url = _run_server(state)
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            token_file, proof_dir = _write_inputs(root)
            result, payload = _run_harness(root, api_url, token_file, proof_dir, "--mismatch-provider", "all")

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["verdict"] == "green"
        assert payload["mismatch_providers"] == ["claude", "opencode"]
        assert [(request["provider"], request["expected_provider_version"]) for request in state.requests] == [
            ("claude", "2.1.153"),
            ("claude", "9.9.9-longhouse-route-e2e"),
            ("opencode", "1.15.11"),
            ("opencode", "9.9.9-longhouse-route-e2e"),
        ]
    finally:
        server.shutdown()


def test_route_e2e_accepts_yellow_verdict_by_default() -> None:
    state = _ServerState()
    state.provider_verdicts["claude"] = "yellow"
    server, api_url = _run_server(state)
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            token_file, proof_dir = _write_inputs(root)
            result, payload = _run_harness(root, api_url, token_file, proof_dir)

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["require_verdict"] == "non-red"
        assert payload["verdict"] == "green"
        assert payload["results"][0]["provider"] == "claude"
        assert payload["results"][0]["status"] == "pass"
        assert payload["results"][0]["verdict"] == "yellow"
    finally:
        server.shutdown()


def test_route_e2e_rejects_yellow_verdict_when_green_is_required() -> None:
    state = _ServerState()
    state.provider_verdicts["claude"] = "yellow"
    server, api_url = _run_server(state)
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            token_file, proof_dir = _write_inputs(root)
            result, payload = _run_harness(
                root,
                api_url,
                token_file,
                proof_dir,
                "--require-verdict",
                "green",
            )

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["results"][0]["failure_code"] == "provider_live_verdict_not_green"
    finally:
        server.shutdown()


def test_route_e2e_retries_transient_match_failure() -> None:
    state = _ServerState()
    state.transient_match_failures["opencode"] = 5
    server, api_url = _run_server(state)
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            token_file, proof_dir = _write_inputs(root)
            result, payload = _run_harness(root, api_url, token_file, proof_dir, "--retry-delay-s", "0")

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["verdict"] == "green"
        opencode = payload["results"][1]
        assert opencode["provider"] == "opencode"
        assert opencode["match_attempt_count"] == 6
        assert opencode["match_attempts"][0]["status_code"] == 503
        assert opencode["match_attempts"][0]["retryable"] is True
        assert [(request["provider"], request["expected_provider_version"]) for request in state.requests] == [
            ("claude", "2.1.153"),
            ("claude", "9.9.9-longhouse-route-e2e"),
            ("opencode", "1.15.11"),
            ("opencode", "1.15.11"),
            ("opencode", "1.15.11"),
            ("opencode", "1.15.11"),
            ("opencode", "1.15.11"),
            ("opencode", "1.15.11"),
        ]
    finally:
        server.shutdown()


def test_route_e2e_retries_transient_operation_poll_without_reposting() -> None:
    state = _ServerState()
    state.operation_poll_failures_remaining = 1
    server, api_url = _run_server(state)
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            token_file, proof_dir = _write_inputs(root)
            result, payload = _run_harness(
                root,
                api_url,
                token_file,
                proof_dir,
                "--provider",
                "claude",
                "--skip-mismatch",
                "--retry-delay-s",
                "0",
            )

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["verdict"] == "green"
        assert [(request["provider"], request["expected_provider_version"]) for request in state.requests] == [
            ("claude", "2.1.153"),
        ]
    finally:
        server.shutdown()


def test_route_e2e_polls_operation_for_process_timeout_not_http_timeout() -> None:
    state = _ServerState()
    state.operation_running_polls_remaining = 2
    server, api_url = _run_server(state)
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            token_file, proof_dir = _write_inputs(root)
            result, payload = _run_harness(
                root,
                api_url,
                token_file,
                proof_dir,
                "--provider",
                "claude",
                "--skip-mismatch",
                "--http-timeout-s",
                "1",
                "--process-timeout-s",
                "5",
            )

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["verdict"] == "green"
        assert [(request["provider"], request["expected_provider_version"]) for request in state.requests] == [
            ("claude", "2.1.153"),
        ]
    finally:
        server.shutdown()


def test_route_e2e_can_require_opencode_transcript_marker() -> None:
    state = _ServerState()
    state.session_search_hits.append("LONGHOUSE_OPENCODE_NOREPLY_TEST")
    server, api_url = _run_server(state)
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            token_file, proof_dir = _write_inputs(root)
            result, payload = _run_harness(
                root,
                api_url,
                token_file,
                proof_dir,
                "--provider",
                "opencode",
                "--skip-mismatch",
                "--require-opencode-transcript",
                "--transcript-attempts",
                "1",
            )

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["verdict"] == "green"
        [opencode_result] = payload["results"]
        assert opencode_result["status"] == "pass"
        assert opencode_result["transcript"]["status"] == "pass"
        assert opencode_result["transcript"]["matched_session_ids"] == ["opencode-session-1"]
        assert state.session_search_requests[0]["marker"] == "LONGHOUSE_OPENCODE_NOREPLY_TEST"
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
            result, payload = _run_harness(root, api_url, token_file, proof_dir, "--retry-delay-s", "0")

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["results"][0]["failure_code"] == "provider_live_mismatch_not_typed"
    finally:
        server.shutdown()


def main() -> int:
    tests = [
        test_route_e2e_requires_match_and_typed_mismatch,
        test_route_e2e_can_run_typed_mismatch_for_every_provider,
        test_route_e2e_accepts_yellow_verdict_by_default,
        test_route_e2e_rejects_yellow_verdict_when_green_is_required,
        test_route_e2e_retries_transient_match_failure,
        test_route_e2e_retries_transient_operation_poll_without_reposting,
        test_route_e2e_polls_operation_for_process_timeout_not_http_timeout,
        test_route_e2e_can_require_opencode_transcript_marker,
        test_route_e2e_fails_when_mismatch_is_not_typed,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
