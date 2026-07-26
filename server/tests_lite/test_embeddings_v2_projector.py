from __future__ import annotations

import hashlib
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from uuid import UUID
from uuid import uuid4

import numpy as np
import pytest

from zerg.services.embeddings_v2_projector import EmbeddingsV2Projector


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def call(self, method, params=None, **_kwargs):
        self.calls.append((method, dict(params or {})))
        return self.responses[method]


class FakeWorkers:
    def __init__(self, decoded):
        self.decoded = decoded

    async def read(self, *_args, **_kwargs):
        return self.decoded


@pytest.mark.asyncio
async def test_embeddings_projector_chunks_dedups_writes_and_completes(monkeypatch):
    session_id, generation_id, store_id = (str(uuid4()) for _ in range(3))
    object_id = hashlib.sha256(b"render").hexdigest()
    records = (
        SimpleNamespace(role="user", content_text="find the important answer", tool_name=None, tool_output_text=None, order_time_us=1),
        SimpleNamespace(
            role="assistant", content_text="the important answer is here", tool_name=None, tool_output_text=None, order_time_us=2
        ),
    )
    decoded = SimpleNamespace(
        object_hash=object_id, spec=SimpleNamespace(session_id=UUID(session_id), render_generation=UUID(generation_id), records=records)
    )
    catalog = FakeClient(
        {
            "projector.store.bind.v2": {},
            "projector.state.claim.v2": {"claimed": [{"session_id": session_id, "claimed_revision": "1", "failure_count": 0}]},
            "storage.session.render_objects.list.v2": {
                "found": True,
                "deleted": False,
                "snapshot_revision": "1",
                "generation_id": generation_id,
                "objects": [{"object_id": object_id, "object_hash": object_id, "object_path": "render.zst"}],
                "has_more": False,
            },
            "projector.state.complete.v2": {},
            "projector.state.fail.v2": {},
        }
    )
    search = FakeClient(
        {
            "search.ping.v2": {"store_id": store_id, "schema_generation": "test"},
            "search.embedding.hashes.v2": {"hashes": {}},
            "search.embedding.write.v2": {"written": 1, "skipped": 0},
        }
    )
    config = SimpleNamespace(model="test-model", dims=2)
    monkeypatch.setattr("zerg.models_config.get_embedding_config", lambda: config)

    async def vectors(texts, _config):
        return [np.array([1, 0], dtype=np.float32) for _ in texts]

    monkeypatch.setattr("zerg.services.embeddings_v2_projector.generate_embeddings", vectors)
    projector = EmbeddingsV2Projector(catalog=catalog, search=search, render_workers=FakeWorkers(decoded), worker_id="test")
    assert await projector.run_once(now=datetime.now(UTC)) == 1
    write = next(params for method, params in search.calls if method == "search.embedding.write.v2")
    assert write["episodes"][0]["episode_ordinal"] == 0
    assert any(method == "projector.state.complete.v2" for method, _ in catalog.calls)
