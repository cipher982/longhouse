from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from datetime import timezone
from uuid import uuid4

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

import httpx

pytest_plugins = ("tests_lite.live_catalog_harness",)

from zerg.main import api_app
from zerg.routers.telemetry import _canary_workspace_stream
from zerg.services.session_pubsub import reset_pubsub_for_test


class _Request:
    async def is_disconnected(self):
        return False


def test_runtime_ingest_drives_live_canary_workspace_sse_without_replay(live_catalog):
    reset_pubsub_for_test()
    owner_id = live_catalog.create_user("canary-runtime-stream@longhouse.test")
    agents_token = live_catalog.create_device_token(owner_id=owner_id, device_id="cube-canary")
    session_id = uuid4()
    envelope = live_catalog.envelope_body(
        session_id=session_id,
        device_id="cube-canary",
        provider="canary",
        project="canary",
        texts=("canary bootstrap",),
        now=datetime.now(timezone.utc),
    )

    async def run() -> tuple[dict, int]:
        stream_ready = asyncio.Event()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
            committed = await client.post(
                "/agents/storage/v2/envelopes",
                json=envelope,
                headers={"X-Agents-Token": agents_token, "X-Longhouse-Storage-Lane": "live"},
            )
            assert committed.status_code == 200, committed.text

            binding_at = datetime.now(timezone.utc)
            binding = {
                "runtime_key": f"canary:{session_id}",
                "session_id": str(session_id),
                "provider": "canary",
                "device_id": "cube-canary",
                "source": "canary_producer",
                "kind": "binding_signal",
                "phase": None,
                "tool_name": None,
                "occurred_at": binding_at.isoformat().replace("+00:00", "Z"),
                "dedupe_key": f"canary:{session_id}:binding",
                "payload": {"canary_bootstrap": True},
            }
            bound = await client.post(
                "/agents/runtime/events/batch",
                json={"events": [binding]},
                headers={"X-Agents-Token": agents_token},
            )
            assert bound.status_code == 200, bound.text

            async def receive_marker() -> dict:
                stream = _canary_workspace_stream(_Request(), session_id=session_id, owner_id=owner_id)
                try:
                    async for event in stream:
                        if event.get("event") == "connected":
                            stream_ready.set()
                        elif event.get("event") == "canary_observation":
                            return json.loads(event["data"])
                    raise AssertionError("canary workspace stream ended without an observation")
                finally:
                    await stream.aclose()

            stream_task = asyncio.create_task(receive_marker())
            await asyncio.wait_for(stream_ready.wait(), timeout=2)
            emitted_at = datetime.now(timezone.utc)
            emitted_at_ms = int(emitted_at.timestamp() * 1000)
            progress = {
                "runtime_key": f"canary:{session_id}",
                "session_id": str(session_id),
                "provider": "canary",
                "device_id": "cube-canary",
                "source": "canary_producer",
                "kind": "progress_signal",
                "phase": None,
                "tool_name": None,
                "occurred_at": emitted_at.isoformat().replace("+00:00", "Z"),
                "dedupe_key": f"canary:{session_id}:41",
                "payload": {"canary_seq": 41, "canary_emitted_at_ms": emitted_at_ms},
            }
            ingested = await client.post(
                "/agents/runtime/events/batch",
                json={"events": [progress]},
                headers={"X-Agents-Token": agents_token},
            )
            assert ingested.status_code == 200, ingested.text
            assert f"canary:{session_id}" in ingested.json()["updated_runtime_keys"]
            assert ingested.headers["X-Canary-Received-At-Ms"].isdecimal()
            marker = await asyncio.wait_for(stream_task, timeout=5)
            return marker, emitted_at_ms

    try:
        with live_catalog.http_client():
            marker, emitted_at_ms = asyncio.run(run())
    finally:
        reset_pubsub_for_test()

    assert marker["canary_seq"] == 41
    assert marker["canary_emitted_at_ms"] == emitted_at_ms
    assert marker["pubsub_seq"] != marker["canary_seq"]
    assert set(marker) == {
        "canary_seq",
        "canary_emitted_at_ms",
        "server_fanout_at_ms",
        "server_now_ms",
        "pubsub_seq",
    }
