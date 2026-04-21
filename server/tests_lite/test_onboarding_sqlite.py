"""SQLite-only onboarding smoke tests.

These tests validate that Zerg boots and operates correctly with SQLite as the
sole database backend. No Docker or Postgres required.

Run with: make onboarding-sqlite

NOTE: These tests use subprocess isolation to avoid module state pollution
between tests. Each test runs in a fresh Python process with env vars passed
safely via subprocess.run(env=...) to avoid path injection issues.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from cryptography.fernet import Fernet


def test_sqlite_onboarding_complete():
    """Complete SQLite onboarding smoke test.

    This single test validates the full OSS onboarding flow:
    1. Create a temp SQLite database
    2. Boot the FastAPI server (in-process via TestClient)
    3. Verify /api/health endpoint returns 200 with valid JSON status
    4. Verify /api/agents/sessions endpoint works
    5. Verify database file was created

    Uses subprocess to ensure clean Python state (no module pollution).
    Environment variables are passed safely via subprocess.run(env=...).
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "zerg_smoke.db"

        # Script reads DATABASE_URL from environment (set below)
        script = """
import os
import json

# Import after env is set (env passed via subprocess)
from zerg.database import initialize_database
initialize_database()

from fastapi.testclient import TestClient
from zerg.main import app

client = TestClient(app)

# Test 1: Health endpoint returns 200
# Use /api/health which is the canonical API health endpoint
print("Test 1: Health endpoint...")
response = client.get("/api/health")
print(f"  Status: {response.status_code}")
print(f"  Content-Type: {response.headers.get('content-type', 'N/A')}")
print(f"  Body length: {len(response.content)} bytes")

if response.status_code != 200:
    raise AssertionError(f"Health check failed with status {response.status_code}")

# Test 2: Health returns valid JSON with status (REQUIRED)
print("Test 2: Health JSON parsing...")
if not response.content:
    raise AssertionError("Health endpoint returned empty response")

data = response.json()  # Let JSONDecodeError propagate
status = data.get("status")
print(f"  Health status: {status}")

if status not in ("healthy", "ok"):  # /api/health uses "ok"
    raise AssertionError(f"Unexpected health status: {status}")

# Test 3: Mint a real device token for machine routes
print("Test 3: Device token bootstrap...")
token_response = client.post("/api/devices/tokens", json={"device_id": "onboarding-smoke"})
print(f"  Status: {token_response.status_code}")
if token_response.status_code != 201:
    raise AssertionError(f"Device token creation failed: {token_response.status_code} {token_response.text}")

device_token = token_response.json()["token"]

# Test 4: Sessions endpoint works with device token
print("Test 4: Sessions endpoint...")
response2 = client.get("/api/agents/sessions", headers={"X-Agents-Token": device_token})
print(f"  Status: {response2.status_code}")
if response2.status_code != 200:
    raise AssertionError(f"Sessions endpoint failed: {response2.status_code}")

# Test 5: Database file created (use env var, not hardcoded path)
print("Test 5: Database file...")
db_url = os.environ.get("DATABASE_URL", "")
if db_url.startswith("sqlite:///"):
    db_file = db_url.replace("sqlite:///", "")
    from pathlib import Path
    if not Path(db_file).exists():
        raise AssertionError(f"SQLite database file was not created at {db_file}")
    print(f"  Database exists at {db_file}")

print("")
print("SUCCESS: All SQLite onboarding tests passed")
"""

        # Build environment with safe path handling
        env = os.environ.copy()
        env["DATABASE_URL"] = f"sqlite:///{db_path}"
        env["TESTING"] = "1"
        env["AUTH_DISABLED"] = "1"
        env["SINGLE_TENANT"] = "1"
        env["FERNET_SECRET"] = Fernet.generate_key().decode()

        # Ensure a build-identity resource exists for the subprocess. The
        # loader reads `importlib.resources.files("zerg") / "build_identity.json"`,
        # so we stage a placeholder inside the live `zerg/` package directory if
        # the dev/CI generator has not run yet. The subprocess honors whichever
        # file is present at subprocess launch.
        import json as _json
        staged = Path(__file__).resolve().parent.parent / "zerg" / "build_identity.json"
        staged_existed = staged.exists()
        if not staged_existed:
            staged.write_text(
                _json.dumps(
                    {
                        "version": "0.0.0",
                        "commit": "0" * 40,
                        "commit_short": "00000000",
                        "dirty": False,
                        "built_at": "2026-04-21T00:00:00Z",
                        "channel": "dev",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

        try:
            result = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                cwd=Path(__file__).parent.parent,  # Run from backend dir
                env=env,
            )
        finally:
            # Only clean up a placeholder we wrote ourselves — never delete a
            # real generator-staged identity that the rest of the suite relies on.
            if not staged_existed and staged.exists():
                staged.unlink()

        # Print output for debugging
        if result.stdout:
            print("STDOUT:", result.stdout)
        if result.stderr and "INFO" not in result.stderr:  # Filter noise
            print("STDERR:", result.stderr)

        if result.returncode != 0:
            pytest.fail(f"Smoke test failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}")

        assert "SUCCESS" in result.stdout, f"Test did not complete successfully: {result.stdout}"
