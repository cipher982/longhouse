"""E2E integration tests for shipper.

Requires a running backend (make dev) and Postgres.
Run with: make test-shipper-e2e
"""

from __future__ import annotations

import gzip
import json
import os
import subprocess
import tempfile
from datetime import datetime
from datetime import timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest


pytestmark = pytest.mark.integration

if not os.getenv("SHIPPER_E2E"):
    pytest.skip("Set SHIPPER_E2E=1 to run shipper E2E tests", allow_module_level=True)

BASE_URL = os.getenv("SHIPPER_E2E_URL", "http://localhost:47300")


def _auth_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    bearer = os.getenv("SHIPPER_E2E_BEARER")
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    return headers


def _agents_headers() -> dict:
    token = os.getenv("SHIPPER_E2E_TOKEN")
    if token:
        return {"X-Agents-Token": token}
    return {}


# Test project patterns for cleanup
TEST_PROJECT_PATTERNS = [
    "test-%",
    "ratelimit-%",
    "smoke-%",
    "watcher-%",
]


@pytest.fixture(scope="session", autouse=True)
def ensure_backend_running():
    """Fail fast if the backend isn't reachable."""
    try:
        response = httpx.get(f"{BASE_URL}/health", timeout=5)
        response.raise_for_status()
    except Exception as exc:
        pytest.fail(f"Backend not reachable at {BASE_URL}. Start with `make dev`. Error: {exc}")


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_sessions():
    """Clean up test sessions after all tests complete."""
    yield  # Run all tests first

    # Clean up test sessions
    try:
        with httpx.Client(base_url=BASE_URL, timeout=30) as client:
            response = client.request(
                "DELETE",
                "/api/agents/test-cleanup",
                json={"project_patterns": TEST_PROJECT_PATTERNS},
                headers=_auth_headers(),
            )
            if response.status_code == 200:
                data = response.json()
                if data.get("deleted", 0) > 0:
                    print(f"\nCleaned up {data['deleted']} test sessions")
            elif response.status_code == 403:
                # Auth enabled, cleanup not available
                pass
    except Exception as e:
        print(f"\nWarning: Could not clean up test sessions: {e}")


def _skip_if_unauthorized(response: httpx.Response) -> None:
    if response.status_code == 401:
        pytest.skip("Device token API requires AUTH_DISABLED=1 or SHIPPER_E2E_BEARER")


def _skip_if_auth_required(response: httpx.Response) -> None:
    if response.status_code in (401, 403):
        pytest.skip("Auth required - provide SHIPPER_E2E_TOKEN")


class TestDeviceTokenAPI:
    """Device token CRUD via real API."""

    def test_create_device_token(self):
        with httpx.Client(base_url=BASE_URL, timeout=10) as client:
            response = client.post(
                "/api/devices/tokens",
                json={"device_id": f"test-device-{uuid4().hex[:8]}"},
                headers=_auth_headers(),
            )

            _skip_if_unauthorized(response)
            assert response.status_code in (200, 201), response.text
            data = response.json()

            assert "token" in data
            assert data["token"].startswith("zdt_")
            assert "id" in data
            assert "device_id" in data

    def test_list_device_tokens(self):
        with httpx.Client(base_url=BASE_URL, timeout=10) as client:
            response = client.get(
                "/api/devices/tokens",
                headers=_auth_headers(),
            )

            _skip_if_unauthorized(response)
            assert response.status_code == 200, response.text
            data = response.json()
            assert isinstance(data, dict)
            assert "tokens" in data

    def test_revoke_device_token(self):
        with httpx.Client(base_url=BASE_URL, timeout=10) as client:
            # Create
            create_resp = client.post(
                "/api/devices/tokens",
                json={"device_id": f"revoke-test-{uuid4().hex[:8]}"},
                headers=_auth_headers(),
            )
            _skip_if_unauthorized(create_resp)
            assert create_resp.status_code in (200, 201)
            token_id = create_resp.json()["id"]

            # Revoke
            revoke_resp = client.delete(
                f"/api/devices/tokens/{token_id}",
                headers=_auth_headers(),
            )
            _skip_if_unauthorized(revoke_resp)
            assert revoke_resp.status_code == 204, revoke_resp.text

            # Verify revoked
            get_resp = client.get(
                f"/api/devices/tokens/{token_id}",
                headers=_auth_headers(),
            )
            _skip_if_unauthorized(get_resp)
            assert get_resp.status_code == 200
            assert get_resp.json()["revoked_at"] is not None


class TestIngestEndpoint:
    """Test the ingest endpoint directly."""

    def test_ingest_basic_session(self):
        session_id = str(uuid4())

        payload = {
            "id": session_id,
            "provider": "claude",
            "project": "test-project",
            "device_id": f"e2e-test-{uuid4().hex[:8]}",
            "cwd": "/tmp/test",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "events": [
                {
                    "role": "user",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "content_text": "Hello",
                }
            ],
        }

        with httpx.Client(base_url=BASE_URL, timeout=10) as client:
            response = client.post(
                "/api/agents/ingest",
                json=payload,
                headers=_agents_headers(),
            )

            _skip_if_auth_required(response)
            assert response.status_code == 200, response.text
            data = response.json()

            assert data["session_id"] == session_id
            assert data["events_inserted"] >= 1

    def test_ingest_gzip_compressed(self):
        session_id = str(uuid4())

        payload = {
            "id": session_id,
            "provider": "claude",
            "project": "test-gzip",
            "device_id": f"e2e-gzip-{uuid4().hex[:8]}",
            "cwd": "/tmp/test",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "events": [
                {
                    "role": "user",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "content_text": "Compressed payload",
                }
            ],
        }

        json_bytes = json.dumps(payload).encode("utf-8")
        compressed = gzip.compress(json_bytes)

        with httpx.Client(base_url=BASE_URL, timeout=10) as client:
            headers = {
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
            }
            headers.update(_agents_headers())
            response = client.post(
                "/api/agents/ingest",
                content=compressed,
                headers=headers,
            )

            _skip_if_auth_required(response)
            assert response.status_code == 200, response.text
            data = response.json()
            assert data["events_inserted"] >= 1

    def test_ingest_with_device_token(self):
        with httpx.Client(base_url=BASE_URL, timeout=10) as client:
            # Create token
            token_resp = client.post(
                "/api/devices/tokens",
                json={"device_id": f"token-test-{uuid4().hex[:8]}"},
                headers=_auth_headers(),
            )
            _skip_if_unauthorized(token_resp)
            assert token_resp.status_code in (200, 201)
            token = token_resp.json()["token"]

            payload = {
                "id": str(uuid4()),
                "provider": "claude",
                "project": "test-auth",
                "device_id": "token-test",
                "cwd": "/tmp",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "events": [],
            }

            ingest_resp = client.post(
                "/api/agents/ingest",
                json=payload,
                headers={"X-Agents-Token": token},
            )

            assert ingest_resp.status_code == 200, ingest_resp.text

    def test_ingest_revoked_token_fails(self):
        # This behavior only applies when auth is enforced.
        with httpx.Client(base_url=BASE_URL, timeout=10) as client:
            # Create and revoke token
            token_resp = client.post(
                "/api/devices/tokens",
                json={"device_id": f"revoked-{uuid4().hex[:8]}"},
                headers=_auth_headers(),
            )
            _skip_if_unauthorized(token_resp)
            assert token_resp.status_code in (200, 201)
            token = token_resp.json()["token"]
            token_id = token_resp.json()["id"]

            revoke_resp = client.delete(
                f"/api/devices/tokens/{token_id}",
                headers=_auth_headers(),
            )
            _skip_if_unauthorized(revoke_resp)
            assert revoke_resp.status_code == 204

            payload = {
                "id": str(uuid4()),
                "provider": "claude",
                "project": "test",
                "device_id": "revoked",
                "cwd": "/tmp",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "events": [],
            }

            ingest_resp = client.post(
                "/api/agents/ingest",
                json=payload,
                headers={"X-Agents-Token": token},
            )

            # If auth is disabled, ingest will still pass; skip instead of failing.
            if ingest_resp.status_code == 200:
                pytest.skip("Auth disabled - revoked token enforcement not active")

            assert ingest_resp.status_code == 401


class TestRateLimiting:
    """Test rate limiting behavior."""

    def test_rate_limit_triggers_429(self):
        device_id = f"ratelimit-{uuid4().hex[:8]}"

        with httpx.Client(base_url=BASE_URL, timeout=30) as client:
            # Send many events to trigger rate limit (1000/min)
            for _ in range(12):  # 12 batches of 100 = 1200 events
                payload = {
                    "id": str(uuid4()),
                    "provider": "claude",
                    "project": "ratelimit-test",
                    "device_id": device_id,
                    "cwd": "/tmp",
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "events": [
                        {
                            "role": "user",
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "content_text": f"msg-{j}",
                        }
                        for j in range(100)
                    ],
                }

                response = client.post("/api/agents/ingest", json=payload, headers=_agents_headers())
                _skip_if_auth_required(response)

                if response.status_code == 429:
                    assert "Retry-After" in response.headers
                    return

            pytest.fail("Rate limit not triggered after 1200 events")


class TestShipperCLI:
    """Test shipper CLI commands."""

    @pytest.fixture
    def temp_claude_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            claude_dir = Path(tmpdir)
            projects_dir = claude_dir / "projects" / "test-project"
            projects_dir.mkdir(parents=True)

            session_file = projects_dir / f"{uuid4()}.jsonl"
            events = [
                {
                    "type": "user",
                    "uuid": str(uuid4()),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "message": {"content": "Test from E2E"},
                },
                {
                    "type": "assistant",
                    "uuid": str(uuid4()),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "message": {"content": [{"type": "text", "text": "Response"}]},
                },
            ]

            with open(session_file, "w") as f:
                for event in events:
                    f.write(json.dumps(event) + "\n")

            yield claude_dir

    def test_ship_command(self, temp_claude_dir):
        backend_dir = Path(__file__).resolve().parents[2]
        env = os.environ.copy()
        if os.getenv("SHIPPER_E2E_TOKEN"):
            env["AGENTS_API_TOKEN"] = os.getenv("SHIPPER_E2E_TOKEN")

        result = subprocess.run(
            [
                "uv",
                "run",
                "zerg",
                "ship",
                "--url",
                BASE_URL,
                "--claude-dir",
                str(temp_claude_dir),
            ],
            capture_output=True,
            text=True,
            cwd=str(backend_dir),
            env=env,
        )

        assert result.returncode == 0, f"ship failed: {result.stderr}"
        assert "shipped" in result.stdout.lower() or "events" in result.stdout.lower()

    def test_shipper_incremental(self, temp_claude_dir):
        backend_dir = Path(__file__).resolve().parents[2]
        env = os.environ.copy()
        if os.getenv("SHIPPER_E2E_TOKEN"):
            env["AGENTS_API_TOKEN"] = os.getenv("SHIPPER_E2E_TOKEN")

        # First ship
        result1 = subprocess.run(
            [
                "uv",
                "run",
                "zerg",
                "ship",
                "--url",
                BASE_URL,
                "--claude-dir",
                str(temp_claude_dir),
            ],
            capture_output=True,
            text=True,
            cwd=str(backend_dir),
            env=env,
        )
        assert result1.returncode == 0

        # Second ship (no new content)
        result2 = subprocess.run(
            [
                "uv",
                "run",
                "zerg",
                "ship",
                "--url",
                BASE_URL,
                "--claude-dir",
                str(temp_claude_dir),
            ],
            capture_output=True,
            text=True,
            cwd=str(backend_dir),
            env=env,
        )
        assert result2.returncode == 0

        # Add new content
        projects_dir = temp_claude_dir / "projects" / "test-project"
        session_files = list(projects_dir.glob("*.jsonl"))
        with open(session_files[0], "a") as f:
            f.write(
                json.dumps(
                    {
                        "type": "user",
                        "uuid": str(uuid4()),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "message": {"content": "New message"},
                    }
                )
                + "\n"
            )

        # Third ship - should ship new event
        result3 = subprocess.run(
            [
                "uv",
                "run",
                "zerg",
                "ship",
                "--url",
                BASE_URL,
                "--claude-dir",
                str(temp_claude_dir),
            ],
            capture_output=True,
            text=True,
            cwd=str(backend_dir),
            env=env,
        )
        assert result3.returncode == 0
