"""A runtime batch that cannot fit one catalogd frame is 413, never 503.

On 2026-10-05 the route answered that batch "Catalog mutation is temporarily
unavailable", so the Machine Agent resent it for ten hours and every runtime
event behind it, a run's terminal signal included, waited. The batch's size
decides the answer, not catalogd's health: the catalog client here points at a
socket nobody serves, which is exactly what makes the two cases distinguishable.
"""

from __future__ import annotations

import os
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.catalogd.client import CatalogClient  # noqa: E402
from zerg.catalogd.protocol import MAX_PAYLOAD_BYTES  # noqa: E402
from zerg.dependencies.agents_auth import require_single_tenant  # noqa: E402
from zerg.dependencies.agents_auth import verify_agents_caller  # noqa: E402
from zerg.main import api_app  # noqa: E402


def _progress(session_id: str, seq: int, pad: str) -> dict:
    return {
        "runtime_key": f"omp:{session_id}",
        "session_id": session_id,
        "provider": "omp",
        "device_id": "cinder",
        "source": "omp_print",
        "kind": "progress_signal",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "dedupe_key": f"omp-print:{session_id}:stdout:{seq}",
        "payload": {"progress_kind": "omp_print_stream", "seq": seq, "pad": pad},
    }


def test_runtime_batch_larger_than_one_catalog_frame_is_413_not_503(tmp_path, monkeypatch):
    from zerg.routers import runtime as runtime_router

    catalog = CatalogClient(tmp_path / "nobody-serves-this.sock", default_timeout_seconds=0.5)
    monkeypatch.setattr(runtime_router, "get_catalogd_client", lambda: catalog)
    api_app.dependency_overrides[verify_agents_caller] = lambda: SimpleNamespace(owner_id=1, device_id="cinder", id="token-1")
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    api_app.dependency_overrides[runtime_router._runtime_db_dependency] = lambda: None
    session_id = str(uuid4())
    pad = "x" * (MAX_PAYLOAD_BYTES // 3)
    try:
        with TestClient(api_app, raise_server_exceptions=False) as client:
            oversized = client.post(
                "/agents/runtime/events/batch",
                json={"events": [_progress(session_id, seq, pad) for seq in range(4)]},
                headers={"X-Agents-Token": "dev"},
            )
            assert oversized.status_code == 413, oversized.text
            assert oversized.json()["detail"]["code"] == "runtime_batch_too_large"

            one_event = client.post(
                "/agents/runtime/events/batch",
                json={"events": [_progress(session_id, 0, pad)]},
                headers={"X-Agents-Token": "dev"},
            )
            assert one_event.status_code == 503, one_event.text
            assert one_event.json()["detail"]["code"] == "catalog_unavailable"
    finally:
        api_app.dependency_overrides.clear()
