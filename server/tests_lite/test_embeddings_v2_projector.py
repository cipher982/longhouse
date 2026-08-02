from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from uuid import UUID
from uuid import uuid4

import numpy as np
import pytest

from zerg.services.embeddings_v2_projector import EmbeddingsV2Projector
from zerg.services.local_embedder import LocalEmbedderUnavailable
from zerg.services.storage_v2_semantics import StorageV2SemanticRecoveryError


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


def _local_embedder(monkeypatch, function):
    monkeypatch.setattr(
        "zerg.services.embeddings_v2_projector.get_local_embedder",
        lambda: SimpleNamespace(embed_documents=function),
    )


@pytest.mark.asyncio
async def test_embeddings_projector_overlaps_claimed_sessions(monkeypatch):
    store_id = str(uuid4())
    catalog = FakeClient(
        {
            "projector.store.bind.v2": {"changed": True},
            "projector.state.claim.v2": {"claimed": [{"session_id": "one"}, {"session_id": "two"}]},
        }
    )
    search = FakeClient({"search.ping.v2": {"store_id": store_id, "schema_generation": "searchd-test"}})
    projector = EmbeddingsV2Projector(catalog=catalog, search=search, render_workers=SimpleNamespace())
    started: set[str] = set()
    both_started = asyncio.Event()

    async def run_claim(state, claim_token):
        assert claim_token
        started.add(state["session_id"])
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=0.1)

    monkeypatch.setattr(projector, "_run_claim", run_claim)

    assert await projector.run_once(limit=2) == 2
    assert started == {"one", "two"}


@pytest.mark.asyncio
async def test_embeddings_projector_chunks_dedups_writes_and_completes(monkeypatch):
    session_id, generation_id, store_id = (str(uuid4()) for _ in range(3))
    object_id = hashlib.sha256(b"render").hexdigest()
    records = (
        SimpleNamespace(
            role="user",
            content_text="<command-name>/effort</command-name>",
            interaction_kind="local_control",
            tool_name=None,
            tool_output_text=None,
            order_time_us=0,
            source_position=0,
            event_subordinal=0,
        ),
        SimpleNamespace(
            role="user",
            content_text="find the important answer",
            interaction_kind="durable_user_message",
            tool_name=None,
            tool_output_text=None,
            order_time_us=1,
            source_position=1,
            event_subordinal=0,
        ),
        SimpleNamespace(
            role="assistant",
            content_text="the important answer is here",
            interaction_kind="provider_system",
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
            provider="claude",
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
    monkeypatch.setattr("zerg.models_config.get_embedding_space_config", lambda: config)

    seen_texts = []

    def vectors(texts):
        seen_texts.extend(texts)
        return np.array([[1, 0] for _ in texts], dtype=np.float32)

    _local_embedder(monkeypatch, vectors)
    projector = EmbeddingsV2Projector(catalog=catalog, search=search, render_workers=FakeWorkers(decoded), worker_id="test")
    assert await projector.run_once(now=datetime.now(UTC)) == 1
    write = next(params for method, params in search.calls if method == "search.embedding.write.v2")
    assert write["episodes"][0]["episode_ordinal"] == 0
    assert write["complete"] is True
    assert write["desired_episode_ordinals"] == [0]
    assert len(seen_texts) == 1
    assert "<command-name>/effort</command-name>" not in seen_texts[0]
    assert any(method == "projector.state.complete.v2" for method, _ in catalog.calls)

    decoded.spec.source_envelope_id = "a" * 64

    async def pending_recovery(**_kwargs):
        raise StorageV2SemanticRecoveryError("raw companion unavailable")

    monkeypatch.setattr("zerg.services.embeddings_v2_projector.recover_render_interaction_kinds", pending_recovery)
    missing_raw_projector = EmbeddingsV2Projector(
        catalog=catalog,
        search=search,
        render_workers=FakeWorkers(decoded),
        raw_workers=FakeWorkers(decoded),
        worker_id="missing-raw",
    )
    assert await missing_raw_projector.run_once(now=datetime.now(UTC)) == 1
    failed = [params for method, params in catalog.calls if method == "projector.state.fail.v2"][-1]
    assert failed["error_code"] == "semantic_recovery_pending"


@pytest.mark.asyncio
async def test_embeddings_projector_marks_complete_only_on_final_batch(monkeypatch):
    """Regression guard: a multi-batch completion pass must not tell searchd to
    delete episodes that weren't rewritten in an earlier, non-final batch.

    searchd's write_episode_embeddings only prunes stale episode_embeddings rows
    when a call arrives with complete=True, using that call's ordinals (or the
    ordinals explicitly passed as desired_episode_ordinals) as the keep-set. If
    every batch in a multi-batch pass claimed complete=True with only its own
    chunk in `episodes`, the first batch's write would immediately delete the
    second batch's not-yet-written chunk.
    """
    session_id, generation_id, store_id = (str(uuid4()) for _ in range(3))
    object_id = hashlib.sha256(b"render").hexdigest()
    records = tuple(
        SimpleNamespace(
            role="user" if i % 2 == 0 else "assistant",
            content_text=f"turn {i}",
            tool_name=None,
            tool_output_text=None,
            order_time_us=i,
            source_position=i,
            event_subordinal=0,
        )
        for i in range(4)
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
    monkeypatch.setattr("zerg.models_config.get_embedding_space_config", lambda: config)
    monkeypatch.setattr("zerg.services.embeddings_v2_projector.EMBEDDING_BATCH_SIZE", 1)

    def vectors(texts):
        return np.array([[1, 0] for _ in texts], dtype=np.float32)

    _local_embedder(monkeypatch, vectors)
    projector = EmbeddingsV2Projector(catalog=catalog, search=search, render_workers=FakeWorkers(decoded), worker_id="test")
    assert await projector.run_once(now=datetime.now(UTC)) == 1

    writes = [params for method, params in search.calls if method == "search.embedding.write.v2"]
    assert len(writes) == 2, "two turn chunks with EMBEDDING_BATCH_SIZE=1 must produce two batches"
    assert [w["complete"] for w in writes] == [False, True]
    assert writes[0]["desired_episode_ordinals"] is None
    assert writes[1]["desired_episode_ordinals"] == [0, 1]


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
async def test_permanent_config_error_is_marked_for_quarantine_and_error_log(monkeypatch, caplog):
    """A deterministic config error is handed to catalog quarantine, not a retry timer."""
    session_id, generation_id, store_id = (str(uuid4()) for _ in range(3))
    catalog, search, decoded = _minimal_claim_setup(session_id, generation_id, store_id)
    config = SimpleNamespace(model="test-model", dims=2)
    monkeypatch.setattr("zerg.models_config.get_embedding_space_config", lambda: config)

    def broken(_texts):
        raise LocalEmbedderUnavailable("dims mismatch")

    _local_embedder(monkeypatch, broken)
    projector = EmbeddingsV2Projector(catalog=catalog, search=search, render_workers=FakeWorkers(decoded), worker_id="test")
    import logging

    with caplog.at_level(logging.ERROR):
        await projector.run_once(now=datetime.now(UTC))

    fail_params = next(params for method, params in catalog.calls if method == "projector.state.fail.v2")
    assert fail_params["error_code"] == "embedding_config_permanent"
    failed_at = datetime.fromisoformat(fail_params["failed_at"])
    retry_at = datetime.fromisoformat(fail_params["retry_at"])
    assert retry_at == failed_at
    assert any(record.levelno == logging.ERROR for record in caplog.records)


@pytest.mark.asyncio
async def test_transient_error_keeps_fast_backoff(monkeypatch):
    """A generic/transient failure (network blip, catalog drift) must keep the
    existing fast exponential backoff, not get parked behind the 24h permanent-error
    delay -- only a local model contract error should ever get the long retry.
    """
    session_id, generation_id, store_id = (str(uuid4()) for _ in range(3))
    catalog, search, decoded = _minimal_claim_setup(session_id, generation_id, store_id)
    config = SimpleNamespace(model="test-model", dims=2)
    monkeypatch.setattr("zerg.models_config.get_embedding_space_config", lambda: config)

    def flaky(_texts):
        raise TimeoutError("worker timed out")

    _local_embedder(monkeypatch, flaky)
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
    monkeypatch.setattr("zerg.models_config.get_embedding_space_config", lambda: config)

    projector = EmbeddingsV2Projector(catalog=catalog, search=search, render_workers=FakeWorkers(None), worker_id="test")
    assert await projector.run_once(now=datetime.now(UTC)) == 1

    assert any(method == "projector.state.complete.v2" for method, _ in catalog.calls)
    assert not any(method == "projector.state.fail.v2" for method, _ in catalog.calls)
