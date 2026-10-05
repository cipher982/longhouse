"""A zstd runtime batch is the same batch as its plain body.

The Machine Agent zstd-encodes runtime-event batches because its uplink, not the
Runtime Host, bounds how fast a flooded outbox drains (cinder's carried ~3 MB/s
on 2026-10-05), and runtime events restate their keys, ids and preview text.
"""

# ruff: noqa: F811

from __future__ import annotations

import json
import os
from datetime import UTC
from datetime import datetime
from uuid import uuid4

import zstandard
from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from tests_lite.live_catalog_harness import live_catalog  # noqa: E402, F401, F811
from tests_lite.live_catalog_harness import live_catalog_client  # noqa: E402, F401, F811


def _batch(session_id: str, start: int, count: int) -> bytes:
    events = [
        {
            "runtime_key": f"omp:{session_id}",
            "session_id": session_id,
            "provider": "omp",
            "device_id": "cinder",
            "source": "omp_background",
            "kind": "delegation_signal",
            "occurred_at": datetime(2026, 10, 5, 4, 0, seq % 60, tzinfo=UTC).isoformat(),
            "dedupe_key": f"omp-background:{session_id}:{seq}",
            "payload": {"delegation": {"count": 1, "items": [{"id": "job-1", "status": "running"}]}},
        }
        for seq in range(start, start + count)
    ]
    return json.dumps({"events": events}).encode()


def test_zstd_runtime_batch_applies_like_its_plain_body(live_catalog, live_catalog_client):
    owner_id = live_catalog.create_user("owner@runtime-encoding.test")
    token = live_catalog.create_device_token(owner_id=owner_id, device_id="cinder")
    session_id = str(uuid4())

    def post(body: bytes, encoding: str):
        return live_catalog_client.post(
            "/agents/runtime/events/batch",
            content=body,
            headers={"X-Agents-Token": token, "Content-Type": "application/json", "Content-Encoding": encoding},
        )

    plain = _batch(session_id, 0, 64)
    encoded = zstandard.ZstdCompressor(level=3).compress(_batch(session_id, 64, 64))
    assert len(encoded) * 5 < len(plain), "runtime events compress"

    plain_result = post(plain, "identity")
    encoded_result = post(encoded, "zstd")
    assert plain_result.status_code == 200, plain_result.text
    assert encoded_result.status_code == 200, encoded_result.text
    assert encoded_result.json()["accepted"] == plain_result.json()["accepted"] == 64

    # What the Machine Agent falls back on: refusals that say the body, not
    # its events, cannot be read.
    assert post(b"not zstd", "zstd").status_code == 400
    bomb = zstandard.ZstdCompressor(level=19).compress(b" " * (17 * 1024 * 1024))
    assert post(bomb, "zstd").status_code == 413
