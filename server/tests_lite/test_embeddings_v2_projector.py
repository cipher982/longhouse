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
from zerg.services.session_processing.embeddings import PermanentEmbeddingConfigError


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
        SimpleNamespace(
            role="user",
            content_text="find the important answer",
            tool_name=None,
            tool_output_text=None,
            order_time_us=1,
            source_position=1,
            event_subordinal=0,
        ),
        SimpleNamespace(
            role="assistant",
            content_text="the important answer is here",
            tool_name=None,
            tool_output_text=None,
            order_time_us=2,
            source_position=2,
            event_subordinal=0,
        ),
    )
    decoded = SimpleNamespace(
        object_hash=object_id,
        spec=SimpleNamespace(
            session_id=UUID(session_id),
            render_generation=UUID(generation_id),
            records=records,
            machine_id="machine",
            provider="codex",
            opaque_source_id="source",
            source_epoch=uuid4(),
        ),
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
                "session": {"owner_id": "1"},
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


def _minimal_claim_setup(session_id, generation_id, store_id):
    """Enough fake RPC responses to reach the embedding-generation call, no further."""
    object_id = hashlib.sha256(b"render").hexdigest()
    records = (
        SimpleNamespace(
            role="user",
            content_text="find the important answer",
            tool_name=None,
            tool_output_text=None,
            order_time_us=1,
            source_position=1,
            event_subordinal=0,
        ),
    )
    decoded = SimpleNamespace(
        object_hash=object_id,
        spec=SimpleNamespace(
            session_id=UUID(session_id),
            render_generation=UUID(generation_id),
            records=records,
            machine_id="machine",
            provider="codex",
            opaque_source_id="source",
            source_epoch=uuid4(),
        ),
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
                "session": {"owner_id": "1"},
                "objects": [{"object_id": object_id, "object_hash": object_id, "object_path": "render.zst"}],
                "has_more": False,
            },
            "projector.state.complete.v2": {},
            "projector.state.fail.v2": {},
        }
    )
    search = FakeClient(
        {"search.ping.v2": {"store_id": store_id, "schema_generation": "test"}, "search.embedding.hashes.v2": {"hashes": {}}}
    )
    return catalog, search, decoded


@pytest.mark.asyncio
async def test_permanent_config_error_gets_long_retry_and_error_log(monkeypatch, caplog):
    """A deterministic config error (bad provider, persistent dims mismatch) must not
    get the same fast exponential backoff as a transient catalog/network hiccup --
    retrying it will produce the identical failure forever and just burns API calls.
    """
    session_id, generation_id, store_id = (str(uuid4()) for _ in range(3))
    catalog, search, decoded = _minimal_claim_setup(session_id, generation_id, store_id)
    config = SimpleNamespace(model="test-model", dims=2)
    monkeypatch.setattr("zerg.models_config.get_embedding_config", lambda: config)

    async def broken(_texts, _config):
        raise PermanentEmbeddingConfigError("dims mismatch")

    monkeypatch.setattr("zerg.services.embeddings_v2_projector.generate_embeddings", broken)
    projector = EmbeddingsV2Projector(catalog=catalog, search=search, render_workers=FakeWorkers(decoded), worker_id="test")
    import logging

    with caplog.at_level(logging.ERROR):
        await projector.run_once(now=datetime.now(UTC))

    fail_params = next(params for method, params in catalog.calls if method == "projector.state.fail.v2")
    assert fail_params["error_code"] == "embedding_config_permanent"
    failed_at = datetime.fromisoformat(fail_params["failed_at"])
    retry_at = datetime.fromisoformat(fail_params["retry_at"])
    assert (retry_at - failed_at).total_seconds() >= 23 * 3600
    assert any(record.levelno == logging.ERROR for record in caplog.records)


@pytest.mark.asyncio
async def test_transient_error_keeps_fast_backoff(monkeypatch):
    """A generic/transient failure (network blip, catalog drift) must keep the
    existing fast exponential backoff, not get parked behind the 24h permanent-error
    delay -- only PermanentEmbeddingConfigError should ever get the long retry.
    """
    session_id, generation_id, store_id = (str(uuid4()) for _ in range(3))
    catalog, search, decoded = _minimal_claim_setup(session_id, generation_id, store_id)
    config = SimpleNamespace(model="test-model", dims=2)
    monkeypatch.setattr("zerg.models_config.get_embedding_config", lambda: config)

    async def flaky(_texts, _config):
        raise TimeoutError("upstream timed out")

    monkeypatch.setattr("zerg.services.embeddings_v2_projector.generate_embeddings", flaky)
    projector = EmbeddingsV2Projector(catalog=catalog, search=search, render_workers=FakeWorkers(decoded), worker_id="test")
    await projector.run_once(now=datetime.now(UTC))

    fail_params = next(params for method, params in catalog.calls if method == "projector.state.fail.v2")
    assert fail_params["error_code"] == "embedding_projection_failed"
    failed_at = datetime.fromisoformat(fail_params["failed_at"])
    retry_at = datetime.fromisoformat(fail_params["retry_at"])
    assert (retry_at - failed_at).total_seconds() <= 300


@pytest.mark.asyncio
async def test_embeddings_projector_completes_cleanly_for_never_rendered_session(monkeypatch):
    """A session that exists but has never been rendered (render_state
    'pending', no current_render_generation -- seen on zero-message CI/
    benchmark artifacts) must complete as a no-op, not crash.

    Regression guard: page.get("generation_id") is None here, and passing
    that straight into _uuid() raised "badly formed hexadecimal UUID
    string" -- a deterministic failure for this session that retried
    forever at real cost since it could never succeed.
    """
    session_id, store_id = (str(uuid4()) for _ in range(2))
    catalog = FakeClient(
        {
            "projector.store.bind.v2": {},
            "projector.state.claim.v2": {"claimed": [{"session_id": session_id, "claimed_revision": "1", "failure_count": 0}]},
            "storage.session.render_objects.list.v2": {
                "found": True,
                "deleted": False,
                "snapshot_revision": "1",
                "generation_id": None,
                "objects": [],
                "has_more": False,
            },
            "projector.state.complete.v2": {},
            "projector.state.fail.v2": {},
        }
    )
    search = FakeClient({"search.ping.v2": {"store_id": store_id, "schema_generation": "test"}})
    config = SimpleNamespace(model="test-model", dims=2)
    monkeypatch.setattr("zerg.models_config.get_embedding_config", lambda: config)

    projector = EmbeddingsV2Projector(catalog=catalog, search=search, render_workers=FakeWorkers(None), worker_id="test")
    assert await projector.run_once(now=datetime.now(UTC)) == 1

    assert any(method == "projector.state.complete.v2" for method, _ in catalog.calls)
    assert not any(method == "projector.state.fail.v2" for method, _ in catalog.calls)
