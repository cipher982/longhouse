#!/usr/bin/env python3
"""Tests for live upstream managed-provider canary artifacts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CANARY = REPO_ROOT / "scripts/qa/provider-live-canary.py"


def _write_exe(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def _fake_opencode(path: Path) -> Path:
    return _write_exe(
        path,
        r'''#!/usr/bin/env python3
import base64
import http.server
import json
import os
import signal
import sys
from urllib.parse import urlparse

args = sys.argv[1:]
if args == ["--version"]:
    print("1.2.3-fake")
    raise SystemExit(0)

if args == ["attach", "--help"]:
    print("opencode attach <url>")
    print("-s, --session session id")
    print("-p, --password basic auth password (defaults to OPENCODE_SERVER_PASSWORD)")
    print("-u, --username basic auth username (defaults to OPENCODE_SERVER_USERNAME or 'opencode')")
    raise SystemExit(0)

if not args or args[0] != "serve":
    print("unexpected fake opencode args: " + json.dumps(args), file=sys.stderr)
    raise SystemExit(2)

username = os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")
password = os.environ.get("OPENCODE_SERVER_PASSWORD", "")
provider_session_id = "ses_fake_provider_live"

def make_doc():
    paths = {
        "/global/health": {"get": {"operationId": "global.health"}},
        "/session": {"post": {"operationId": "session.create"}},
        "/session/{sessionID}": {"get": {"operationId": "session.get"}},
        "/session/{sessionID}/prompt_async": {"post": {"operationId": "session.prompt_async"}},
        "/session/{sessionID}/abort": {"post": {"operationId": "session.abort"}},
    }
    if os.environ.get("FAKE_OPENCODE_OMIT_PROMPT_ASYNC") == "1":
        paths.pop("/session/{sessionID}/prompt_async")
    return {"openapi": "3.1.0", "paths": paths}

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _json(self, payload, status=200):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _empty(self, status=204):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _authorized(self):
        expected = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        return self.headers.get("Authorization") == expected

    def do_GET(self):
        if not self._authorized():
            self._json({"error": "forbidden"}, 403)
            return
        if self.path == "/global/health":
            self._json({"healthy": True, "version": "1.2.3-fake"})
            return
        if self.path == "/doc":
            self._json(make_doc())
            return
        if self.path == f"/session/{provider_session_id}":
            self._json({"id": provider_session_id})
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if not self._authorized():
            self._json({"error": "forbidden"}, 403)
            return
        if parsed.path == "/session":
            self._json({
                "id": provider_session_id,
                "cost": 0,
                "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
            })
            return
        if parsed.path == f"/session/{provider_session_id}/abort":
            if os.environ.get("FAKE_OPENCODE_EMPTY_ABORT") == "1":
                self._empty()
                return
            self._json(True)
            return
        self._json({"error": "not found"}, 404)

server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
print(f"opencode server listening on http://127.0.0.1:{server.server_address[1]}", flush=True)
server.serve_forever()
''',
    )


def _run_canary(root: Path, fake_bin: Path, extra_env: dict[str, str] | None = None):
    artifact = root / "artifact.json"
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        [
            sys.executable,
            str(CANARY),
            "--repo-root",
            str(REPO_ROOT),
            "--provider",
            "opencode",
            "--provider-bin",
            str(fake_bin),
            "--artifact",
            str(artifact),
            "--evidence-root",
            str(root / "evidence"),
            "--json",
        ],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    return result, payload


def test_opencode_live_canary_can_go_green_with_fake_server() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fake_bin = _fake_opencode(root / "bin" / "opencode")
        result, payload = _run_canary(root, fake_bin)

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["provider"] == "opencode"
        assert payload["provider_version"] == "1.2.3-fake"
        assert payload["verdict"] == "green"
        assert payload["canaries"]["binary_identity"]["status"] == "pass"
        assert payload["canaries"]["server_startup"]["status"] == "pass"
        assert payload["canaries"]["schema_probe"]["status"] == "pass"
        assert payload["canaries"]["session_create"]["tokens"]["input"] == 0
        assert payload["canaries"]["session_abort"]["status"] == "pass"


def test_opencode_live_canary_fails_when_schema_drops_prompt_async() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fake_bin = _fake_opencode(root / "bin" / "opencode")
        result, payload = _run_canary(root, fake_bin, {"FAKE_OPENCODE_OMIT_PROMPT_ASYNC": "1"})

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "opencode_schema_probe_failed"
        failures = payload["canaries"]["schema_probe"]["failures"]
        assert failures[0]["failure_code"] == "opencode_schema_missing_path"


def test_opencode_live_canary_accepts_empty_successful_abort_response() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fake_bin = _fake_opencode(root / "bin" / "opencode")
        result, payload = _run_canary(root, fake_bin, {"FAKE_OPENCODE_EMPTY_ABORT": "1"})

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["verdict"] == "green"
        assert payload["canaries"]["session_abort"]["status"] == "pass"


def main() -> int:
    tests = [
        test_opencode_live_canary_can_go_green_with_fake_server,
        test_opencode_live_canary_fails_when_schema_drops_prompt_async,
        test_opencode_live_canary_accepts_empty_successful_abort_response,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
