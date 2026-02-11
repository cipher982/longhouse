"""E2E test for real-time file watching.

Requires a running backend (make dev).
Run with: make test-shipper-e2e
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from datetime import timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

pytestmark = pytest.mark.integration

if not os.getenv("SHIPPER_E2E"):
    pytest.skip("Set SHIPPER_E2E=1 to run shipper watcher E2E tests", allow_module_level=True)

BASE_URL = os.getenv("SHIPPER_E2E_URL", "http://localhost:47300")


def _agents_headers() -> dict:
    token = os.getenv("SHIPPER_E2E_TOKEN")
    if token:
        return {"X-Agents-Token": token}
    return {}


@pytest.fixture(scope="session", autouse=True)
def ensure_backend_running():
    try:
        response = httpx.get(f"{BASE_URL}/api/health", timeout=5)
        response.raise_for_status()
    except Exception as exc:
        pytest.fail(f"Backend not reachable at {BASE_URL}. Start with `make dev`. Error: {exc}")


@pytest.mark.asyncio
async def test_watcher_real_time_sync(tmp_path: Path):
    """File changes should sync in near real-time."""
    from zerg.services.shipper import SessionShipper
    from zerg.services.shipper import SessionWatcher
    from zerg.services.shipper import ShipperConfig

    claude_dir = tmp_path
    projects_dir = claude_dir / "projects" / "watcher-test"
    projects_dir.mkdir(parents=True)

    config = ShipperConfig(
        api_url=BASE_URL,
        claude_config_dir=claude_dir,
        api_token=os.getenv("SHIPPER_E2E_TOKEN"),
    )
    shipper = SessionShipper(config=config)
    watcher = SessionWatcher(shipper, debounce_ms=100, fallback_scan_interval=0)

    await watcher.start()

    try:
        session_id = str(uuid4())
        session_file = projects_dir / f"{session_id}.jsonl"

        event = {
            "type": "user",
            "uuid": str(uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "cwd": "/tmp/watcher-test",
            "message": {"content": "Real-time test"},
        }

        session_file.write_text(json.dumps(event) + "\n")

        await asyncio.sleep(1.0)

        async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
            response = await client.get("/api/agents/sessions", params={"limit": 10}, headers=_agents_headers())
            if response.status_code in (401, 403):
                pytest.skip("Auth required - provide SHIPPER_E2E_TOKEN to run watcher test")
            sessions = response.json().get("sessions", [])

            found = any(s.get("project") == "watcher-test" for s in sessions)
            assert found, f"Session not found in API. Sessions: {sessions}"

    finally:
        await watcher.stop()
