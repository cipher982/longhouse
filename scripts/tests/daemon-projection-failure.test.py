"""Exercise failed observation, cached phase rebuild, and recovery in a real daemon.

Only the capability handshake is a loopback fixture. No provider or hosted session
is created; HOME, process inventory failure, phase ledger, and logs are disposable.
"""

import argparse
import datetime
import http.server
import json
import os
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from uuid import uuid4


class RuntimeFixture(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path != "/api/agents/storage/v2/capabilities":
            self.send_error(404)
            return
        body = json.dumps(
            {
                "protocol_version": 2,
                "cutover": True,
                "tenant_id": "isolated-projection-regression",
                "machine_id": self.headers["X-Longhouse-Machine-Id"],
                "ingest_path": "/api/agents/storage/v2/envelopes",
                "max_wire_body_bytes": 1048576,
                "max_raw_record_bytes": 1048576,
                "max_records": 100,
                "media_claim_path": "/api/agents/storage/v2/media/claims",
                "media_upload_path_template": "/api/agents/storage/v2/media/{sha256}",
                "max_media_bytes": 1048576,
                "max_media_claims": 100,
                "range_kinds": ["byte_offset", "record_ordinal"],
                "lanes": ["live", "repair"],
                "lane_header": "X-Longhouse-Storage-Lane",
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def interrupted(signum, _frame):
    raise InterruptedError(f"interrupted by signal {signum}")


def exercise(engine):
    receipt = {"engine": str(engine), "hosted_sessions_created": []}
    # macOS Unix sockets reject its long per-user temporary-directory path.
    with tempfile.TemporaryDirectory(prefix="lh-proj-", dir="/tmp") as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        longhouse = home / ".longhouse"
        shim = root / "bin"
        shim.mkdir()
        fail_inventory = root / "inventory-failed"
        ps = shim / "ps"
        ps.write_text('#!/bin/sh\nif [ -e "$PROJECTION_TEST_FAILURE" ]; then exit 1; fi\nexec /bin/ps "$@"\n')
        ps.chmod(0o700)
        env = {
            **os.environ,
            "HOME": str(home),
            "LONGHOUSE_HOME": str(longhouse),
            "PATH": str(shim) + os.pathsep + os.environ["PATH"],
            "PROJECTION_TEST_FAILURE": str(fail_inventory),
        }
        for name in (
            "CODEX_HOME",
            "CLAUDE_CONFIG_DIR",
            "CURSOR_HOME",
            "PI_CODING_AGENT_DIR",
            "OMP_HOME",
            "OMP_AGENT_DIR",
            "XDG_DATA_HOME",
            "XDG_CONFIG_HOME",
        ):
            env[name] = str(home / name.lower())
        db = longhouse / "shipper.db"
        status_path = longhouse / "agent" / "engine-status.json"
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RuntimeFixture)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        child = None
        log_path = root / "daemon.log"
        try:
            with log_path.open("wb") as log:
                child = subprocess.Popen(
                    [
                        str(engine),
                        "connect",
                        "--url",
                        f"http://127.0.0.1:{server.server_port}",
                        "--token",
                        "isolated-test",
                        "--db",
                        str(db),
                        "--machine-name",
                        "projection-regression",
                        "--archive-repair-mode",
                        "paused",
                        "--log-dir",
                        str(root / "logs"),
                    ],
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
            receipt["daemon_pid"] = child.pid
            print(json.dumps({"owned_pid": child.pid, "owned_root": str(root)}), flush=True)

            def observe():
                assert child.poll() is None, f"daemon exited: {log_path.read_text()}"
                try:
                    return json.loads(status_path.read_text())
                except FileNotFoundError:
                    return {}

            def wait_for(predicate):
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    status = observe()
                    if predicate(status.get("local_projection", {})):
                        return status
                    time.sleep(0.05)
                raise AssertionError(f"daemon did not converge: {status}; logs: {log_path.read_text()}")

            healthy = wait_for(lambda projection: projection.get("reconciliation", {}).get("state") == "idle")
            receipt["build"] = healthy.get("build")
            fail_inventory.touch()
            failed = wait_for(lambda projection: projection.get("reconciliation", {}).get("state") == "failed")
            frozen = failed["local_projection"]["generated_at"]
            with sqlite3.connect(db, timeout=5) as connection:
                connection.execute(
                    "INSERT INTO session_phase_state (session_id, provider, phase, source, observed_at, revision) VALUES (?, 'omp', 'idle', 'test', ?, 1)",
                    (str(uuid4()), datetime.datetime.now(datetime.timezone.utc).isoformat()),
                )
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                status = observe()
                projection = status["local_projection"]
                assert projection["generated_at"] == frozen, f"cached phase rebuild re-aged failed observation: {projection}"
                assert projection["reconciliation"]["state"] != "idle", f"cached phase rebuild erased observation failure: {projection}"
                time.sleep(0.05)
            assert projection["engine_pulse_at"] != failed["local_projection"]["engine_pulse_at"], "failure stopped engine pulses"
            fail_inventory.unlink()
            recovered = wait_for(
                lambda projection: projection.get("generated_at") != frozen and projection.get("reconciliation", {}).get("state") == "idle"
            )
            assert recovered["daemon_pid"] == child.pid, "recovery replaced the daemon"
            receipt["failure_preserved_during_phase_rebuild"] = True
            receipt["recovered_without_restart"] = True
        finally:
            if child is not None:
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=5)
                assert child.poll() is not None
                receipt["daemon_reaped"] = True
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            assert not thread.is_alive()
            receipt["fixture_server_stopped"] = True
            print(json.dumps({"daemon_reaped": receipt.get("daemon_reaped"), "fixture_server_stopped": True}), flush=True)
    assert not root.exists()
    receipt["scratch_removed"] = True
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", required=True, type=Path)
    arguments = parser.parse_args()
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    exercise(arguments.engine.resolve())
