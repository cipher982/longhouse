from __future__ import annotations

import asyncio
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import numpy as np
import pytest

from zerg.services.embeddings_v2_projector import PROJECTOR_IDLE_POLL_SECONDS
from zerg.services.embeddings_v2_projector import PROJECTOR_LEASE_SECONDS
from zerg.services.embeddings_v2_projector import EmbeddingsV2Projector
from zerg.services.embeddings_v2_projector import _run_forever
from zerg.services.embeddings_v2_projector import _run_worker
from zerg.services.local_embedder import LocalEmbedderUnavailable


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def call(self, method, params=None, **_kwargs):
        parsed = dict(params or {})
        self.calls.append((method, parsed))
        response = self.responses[method]
        return response(parsed) if callable(response) else response


def _local_embedder(monkeypatch, function):
    monkeypatch.setattr(
        "zerg.services.embeddings_v2_projector.get_local_embedder",
        lambda: SimpleNamespace(embed_documents=function),
    )


def _source(generation_id, records, *, revision="7", provider="codex"):
    source_epoch = str(uuid4())
    return {
        "found": True,
        "generation_id": generation_id,
        "revision": revision,
        "owner_id": "1",
        "provider": provider,
        "event_count": len(records),
        "records": [
            {
                "timestamp": record.order_time_us,
                "machine_id": "machine",
                "provider": provider,
                "opaque_source_id": "source",
                "source_epoch": source_epoch,
                "source_position": record.source_position,
                "event_subordinal": record.event_subordinal,
                "role": record.role,
                "content_text": record.content_text,
                "interaction_kind": getattr(
                    record,
                    "interaction_kind",
                    "durable_user_message" if record.role == "user" else "provider_system",
                ),
                "tool_name": record.tool_name,
                "tool_output_text": record.tool_output_text,
            }
            for record in records
        ],
        "has_more": False,
    }


def _snapshot(generation_id, *, revision="7"):
    return {
        "found": True,
        "deleted": False,
        "retired": False,
        "snapshot_revision": revision,
        "generation_id": generation_id,
        "session": {"owner_id": "1"},
        "objects": [],
        "has_more": False,
    }


@pytest.mark.asyncio
async def test_embedding_projector_workers_refill_independently():
    both_started = asyncio.Event()
    active = 0

    class Projector:
        async def run_once(self, *, limit):
            nonlocal active
            assert limit == 1
            active += 1
            if active == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=0.1)
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _run_forever(Projector(), worker_count=2)
    assert active == 2


@pytest.mark.asyncio
async def test_embedding_projector_backs_off_when_the_claim_ledger_is_empty(monkeypatch):
    class Projector:
        async def run_once(self, *, limit):
            assert limit == 1
            return 0

    async def stop_after_observation(delay):
        assert delay == PROJECTOR_IDLE_POLL_SECONDS
        raise asyncio.CancelledError

    monkeypatch.setattr("zerg.services.embeddings_v2_projector.asyncio.sleep", stop_after_observation)
    with pytest.raises(asyncio.CancelledError):
        await _run_worker(Projector())


@pytest.mark.asyncio
async def test_embedding_projector_retries_invalid_source_contract(monkeypatch):
    session_id = str(uuid4())
    catalog = FakeClient({"projector.state.fail.v2": {"changed": True}})
    projector = EmbeddingsV2Projector(catalog=catalog, search=SimpleNamespace())

    async def project(**_kwargs):
        raise ValueError("unsupported source shape")

    monkeypatch.setattr(projector, "_project", project)
    await projector._run_claim(
        {"session_id": session_id, "claimed_revision": "1", "failure_count": 0},
        str(uuid4()),
    )

    failed = next(params for method, params in catalog.calls if method == "projector.state.fail.v2")
    assert failed["error_code"] == "embedding_projection_failed"
    assert failed["retry_at"] > failed["failed_at"]


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
    projector = EmbeddingsV2Projector(catalog=catalog, search=search)
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
    claim_call = next(params for method, params in catalog.calls if method == "projector.state.claim.v2")
    assert claim_call["lease_seconds"] == PROJECTOR_LEASE_SECONDS == 900


@pytest.mark.asyncio
async def test_embedding_projector_deletes_retired_session(monkeypatch):
    session_id = str(uuid4())
    store_id = str(uuid4())
    catalog = FakeClient(
        {
            "projector.store.bind.v2": {},
            "projector.state.claim.v2": {"claimed": [{"session_id": session_id, "claimed_revision": "9", "failure_count": 0}]},
            "storage.session.render_objects.list.v2": {"found": True, "deleted": False, "retired": True},
            "projector.state.complete.v2": {},
        }
    )
    search = FakeClient(
        {
            "search.ping.v2": {"store_id": store_id, "schema_generation": "test"},
            "search.embedding.source.v2": {"found": False},
            "search.session.delete.v2": {"deleted": True},
        }
    )
    monkeypatch.setattr("zerg.models_config.get_embedding_space_config", lambda: SimpleNamespace(model="test", dims=2))
    projector = EmbeddingsV2Projector(catalog=catalog, search=search, worker_id="test")

    assert await projector.run_once(now=datetime.now(UTC)) == 1
    assert any(method == "search.session.delete.v2" for method, _ in search.calls)
    assert any(method == "projector.state.complete.v2" for method, _ in catalog.calls)


@pytest.mark.asyncio
async def test_embeddings_projector_chunks_dedups_writes_and_completes(monkeypatch):
    session_id, generation_id, store_id = (str(uuid4()) for _ in range(3))
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
    catalog = FakeClient(
        {
            "projector.store.bind.v2": {},
            "projector.state.claim.v2": {"claimed": [{"session_id": session_id, "claimed_revision": "7", "failure_count": 0}]},
            "storage.session.render_objects.list.v2": _snapshot(generation_id),
            "projector.state.complete.v2": {},
            "projector.state.fail.v2": {},
        }
    )
    search = FakeClient(
        {
            "search.ping.v2": {"store_id": store_id, "schema_generation": "test"},
            "search.embedding.source.v2": _source(generation_id, records, provider="claude"),
            "search.embedding.hashes.v2": {
                "hashes": {},
                "published_generation_id": generation_id,
                "published_revision": "7",
            },
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
    projector = EmbeddingsV2Projector(catalog=catalog, search=search, worker_id="test")
    assert await projector.run_once(now=datetime.now(UTC)) == 1
    write = next(params for method, params in search.calls if method == "search.embedding.write.v2")
    assert write["episodes"][0]["episode_ordinal"] == 0
    assert write["complete"] is True
    assert write["desired_episode_ordinals"] == [0]
    assert write["revision"] == "7"
    assert len(seen_texts) == 1
    assert "<command-name>/effort</command-name>" not in seen_texts[0]
    assert any(method == "projector.state.complete.v2" for method, _ in catalog.calls)

    assert sum(method == "storage.session.render_objects.list.v2" for method, _ in catalog.calls) == 1


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
    catalog = FakeClient(
        {
            "projector.store.bind.v2": {},
            "projector.state.claim.v2": {"claimed": [{"session_id": session_id, "claimed_revision": "7", "failure_count": 0}]},
            "storage.session.render_objects.list.v2": _snapshot(generation_id),
            "projector.state.complete.v2": {},
            "projector.state.fail.v2": {},
        }
    )
    search = FakeClient(
        {
            "search.ping.v2": {"store_id": store_id, "schema_generation": "test"},
            "search.embedding.source.v2": _source(generation_id, records),
            "search.embedding.hashes.v2": {
                "hashes": {},
                "published_generation_id": generation_id,
                "published_revision": "7",
            },
            "search.embedding.write.v2": {"written": 1, "skipped": 0},
        }
    )
    config = SimpleNamespace(model="test-model", dims=2)
    monkeypatch.setattr("zerg.models_config.get_embedding_space_config", lambda: config)
    monkeypatch.setattr("zerg.services.embeddings_v2_projector.EMBEDDING_BATCH_SIZE", 1)

    def vectors(texts):
        return np.array([[1, 0] for _ in texts], dtype=np.float32)

    _local_embedder(monkeypatch, vectors)
    projector = EmbeddingsV2Projector(catalog=catalog, search=search, worker_id="test")
    assert await projector.run_once(now=datetime.now(UTC)) == 1

    writes = [params for method, params in search.calls if method == "search.embedding.write.v2"]
    assert len(writes) == 2, "two turn chunks with EMBEDDING_BATCH_SIZE=1 must produce two batches"
    assert [w["complete"] for w in writes] == [False, True]
    assert writes[0]["desired_episode_ordinals"] is None
    assert writes[1]["desired_episode_ordinals"] == [0, 1]
    assert {write["revision"] for write in writes} == {"7"}


@pytest.mark.asyncio
async def test_embedding_projector_rejects_search_revision_behind_claim(monkeypatch):
    session_id, generation_id, store_id = (str(uuid4()) for _ in range(3))
    records = (
        SimpleNamespace(
            role="user",
            content_text="new revision",
            tool_name=None,
            tool_output_text=None,
            order_time_us=1,
            source_position=1,
            event_subordinal=0,
        ),
    )
    catalog = FakeClient(
        {
            "projector.store.bind.v2": {},
            "projector.state.claim.v2": {"claimed": [{"session_id": session_id, "claimed_revision": "9", "failure_count": 0}]},
            "storage.session.render_objects.list.v2": _snapshot(generation_id, revision="9"),
            "projector.state.fail.v2": {},
        }
    )
    search = FakeClient(
        {
            "search.ping.v2": {"store_id": store_id, "schema_generation": "test"},
            "search.embedding.source.v2": _source(generation_id, records, revision="7"),
        }
    )
    monkeypatch.setattr("zerg.models_config.get_embedding_space_config", lambda: SimpleNamespace(model="test", dims=2))

    assert await EmbeddingsV2Projector(catalog=catalog, search=search).run_once() == 1
    failed = next(params for method, params in catalog.calls if method == "projector.state.fail.v2")
    assert failed["error_code"] == "embedding_projection_failed"
    assert not any(method == "projector.state.complete.v2" for method, _ in catalog.calls)
    assert not any(method == "search.embedding.write.v2" for method, _ in search.calls)


@pytest.mark.asyncio
async def test_embedding_projector_pages_one_fenced_source(monkeypatch):
    session_id, generation_id, store_id = (str(uuid4()) for _ in range(3))
    records = tuple(
        SimpleNamespace(
            role=role,
            content_text=text,
            tool_name=None,
            tool_output_text=None,
            order_time_us=index,
            source_position=index,
            event_subordinal=0,
        )
        for index, (role, text) in enumerate((("user", "question"), ("assistant", "answer")))
    )
    full_source = _source(generation_id, records)

    def page(params):
        offset = params["offset"]
        return {
            **full_source,
            "records": full_source["records"][offset : offset + 1],
            "has_more": offset + 1 < len(records),
        }

    catalog = FakeClient(
        {
            "projector.store.bind.v2": {},
            "projector.state.claim.v2": {"claimed": [{"session_id": session_id, "claimed_revision": "7", "failure_count": 0}]},
            "storage.session.render_objects.list.v2": _snapshot(generation_id),
            "projector.state.complete.v2": {},
            "projector.state.fail.v2": {},
        }
    )
    search = FakeClient(
        {
            "search.ping.v2": {"store_id": store_id, "schema_generation": "test"},
            "search.embedding.source.v2": page,
            "search.embedding.hashes.v2": {
                "hashes": {},
                "published_generation_id": generation_id,
                "published_revision": "7",
            },
            "search.embedding.write.v2": {"written": 1, "skipped": 0},
        }
    )
    monkeypatch.setattr("zerg.models_config.get_embedding_space_config", lambda: SimpleNamespace(model="test", dims=2))
    _local_embedder(monkeypatch, lambda texts: np.array([[1, 0] for _ in texts], dtype=np.float32))

    assert await EmbeddingsV2Projector(catalog=catalog, search=search).run_once() == 1
    pages = [params for method, params in search.calls if method == "search.embedding.source.v2"]
    assert [params["offset"] for params in pages] == [0, 1]
    assert pages[0]["expected_generation_id"] == generation_id
    assert pages[0]["expected_revision"] == "7"
    assert pages[1]["expected_generation_id"] == generation_id
    assert pages[1]["expected_revision"] == "7"


def _minimal_claim_setup(session_id, generation_id, store_id):
    """Enough fake RPC responses to reach the embedding-generation call, no further."""
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
    catalog = FakeClient(
        {
            "projector.store.bind.v2": {},
            "projector.state.claim.v2": {"claimed": [{"session_id": session_id, "claimed_revision": "1", "failure_count": 0}]},
            "storage.session.render_objects.list.v2": _snapshot(generation_id, revision="1"),
            "projector.state.complete.v2": {},
            "projector.state.fail.v2": {},
        }
    )
    search = FakeClient(
        {
            "search.ping.v2": {"store_id": store_id, "schema_generation": "test"},
            "search.embedding.source.v2": _source(generation_id, records, revision="1"),
            "search.embedding.hashes.v2": {
                "hashes": {},
                "published_generation_id": generation_id,
                "published_revision": "1",
            },
        }
    )
    return catalog, search


@pytest.mark.asyncio
async def test_permanent_config_error_is_marked_for_quarantine_and_error_log(monkeypatch, caplog):
    """A deterministic config error is handed to catalog quarantine, not a retry timer."""
    session_id, generation_id, store_id = (str(uuid4()) for _ in range(3))
    catalog, search = _minimal_claim_setup(session_id, generation_id, store_id)
    config = SimpleNamespace(model="test-model", dims=2)
    monkeypatch.setattr("zerg.models_config.get_embedding_space_config", lambda: config)

    def broken(_texts):
        raise LocalEmbedderUnavailable("dims mismatch")

    _local_embedder(monkeypatch, broken)
    projector = EmbeddingsV2Projector(catalog=catalog, search=search, worker_id="test")
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
    catalog, search = _minimal_claim_setup(session_id, generation_id, store_id)
    config = SimpleNamespace(model="test-model", dims=2)
    monkeypatch.setattr("zerg.models_config.get_embedding_space_config", lambda: config)

    def flaky(_texts):
        raise TimeoutError("worker timed out")

    _local_embedder(monkeypatch, flaky)
    projector = EmbeddingsV2Projector(catalog=catalog, search=search, worker_id="test")
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
    search = FakeClient(
        {
            "search.ping.v2": {"store_id": store_id, "schema_generation": "test"},
            "search.embedding.source.v2": {"found": False},
        }
    )
    config = SimpleNamespace(model="test-model", dims=2)
    monkeypatch.setattr("zerg.models_config.get_embedding_space_config", lambda: config)

    projector = EmbeddingsV2Projector(catalog=catalog, search=search, worker_id="test")
    assert await projector.run_once(now=datetime.now(UTC)) == 1

    assert any(method == "projector.state.complete.v2" for method, _ in catalog.calls)
    assert not any(method == "projector.state.fail.v2" for method, _ in catalog.calls)
