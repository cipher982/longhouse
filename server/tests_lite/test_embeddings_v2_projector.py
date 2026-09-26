from __future__ import annotations

import asyncio
import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import numpy as np
import pytest

from zerg.catalogd.client import CatalogUnavailable
from zerg.services.embeddings_v2_projector import IMPORT_YIELD_RECHECK_SECONDS
from zerg.services.embeddings_v2_projector import PROJECTOR_CLAIM_BATCH
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
        if method == "projector.coverage.certify.v2" and method not in self.responses:
            return {"certified": False, "created": False, "lag_count": 1, "commit_seq": "1"}
        if method == "auth.owner.get.v2" and method not in self.responses:
            return {"found": False}
        response = self.responses[method]
        return response(parsed) if callable(response) else response


def _local_embedder(monkeypatch, function, *, truncate_document=None):
    """Stub the one seam the projector reaches the model through.

    Episode text is budgeted with the model's own tokenizer, so truncation lives
    on the embedder alongside the tokenizer that defines the budget. The default
    stub is a no-op: these tests assert chunking and write batching, not the
    truncation policy, which has its own coverage.
    """

    monkeypatch.setattr(
        "zerg.services.embeddings_v2_projector.get_local_embedder",
        lambda: SimpleNamespace(
            embed_documents=function,
            truncate_document=truncate_document or (lambda text: (text, False)),
        ),
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
            assert limit == PROJECTOR_CLAIM_BATCH
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
            assert limit == PROJECTOR_CLAIM_BATCH
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
    _local_embedder(monkeypatch, lambda texts: np.array([[1, 0] for _ in texts], dtype=np.float32))

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
        offset = 0 if params["after"] is None else int(params["after"][-1]) + 1
        next_cursor = [offset, "machine", "codex", "source", "epoch", offset, 0, offset]
        return {
            **full_source,
            "records": full_source["records"][offset : offset + 1],
            "has_more": offset + 1 < len(records),
            "next_cursor": next_cursor,
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
    assert [params["after"] for params in pages] == [None, [0, "machine", "codex", "source", "epoch", 0, 0, 0]]
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
async def test_cold_embedder_claim_is_released_for_retry(monkeypatch):
    session_id, generation_id, store_id = (str(uuid4()) for _ in range(3))
    catalog, search = _minimal_claim_setup(session_id, generation_id, store_id)
    monkeypatch.setattr("zerg.models_config.get_embedding_space_config", lambda: SimpleNamespace(model="test-model", dims=2))
    cold_error = LocalEmbedderUnavailable("local embedder is still initializing", retryable=True)
    initialization_requests = []

    def cold_embedder():
        raise cold_error

    monkeypatch.setattr("zerg.services.embeddings_v2_projector.get_local_embedder", cold_embedder)
    monkeypatch.setattr(
        "zerg.services.embeddings_v2_projector.request_local_embedder_initialization",
        lambda: initialization_requests.append(True),
    )
    projector = EmbeddingsV2Projector(catalog=catalog, search=search, worker_id="test")

    await projector.run_once(now=datetime.now(UTC))

    failed = next(params for method, params in catalog.calls if method == "projector.state.fail.v2")
    assert failed["error_code"] == "embedding_projection_failed"
    assert datetime.fromisoformat(failed["retry_at"]) > datetime.fromisoformat(failed["failed_at"])
    assert initialization_requests == [True]


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


_INVENTORY = {
    "schema_version": 1,
    "generation": 1,
    "content_sha256": "a" * 64,
    "observed_at": "2026-09-24T12:00:00Z",
    "scan_duration_ms": 0,
    "scan_error_count": 0,
    "source_count": 0,
    "source_bytes": 0,
    "wal_bytes": 0,
    "footprint_bytes": 0,
    "providers": [],
}
_DRAINED = {
    "acknowledged_source_bytes": 0,
    "remaining_source_bytes": 0,
    "acknowledged_records": 0,
    "remaining_records": 0,
    "pending_outbox_count": 0,
    "pending_outbox_bytes": 0,
    "blocked_source_count": 0,
    "blocked_bytes": 0,
    "providers": [],
}


def _heartbeat(state, *, received_at, device_id="laptop", is_offline=0):
    snapshot = {"state": state, "inventory": _INVENTORY}
    if state == "current":
        snapshot["progress"] = _DRAINED
    return {
        "device_id": device_id,
        "received_at": received_at.isoformat(),
        "is_offline": is_offline,
        "raw_json": json.dumps({"history_import": snapshot}),
    }


def _import_catalog(heartbeats):
    return FakeClient(
        {
            "auth.owner.get.v2": {"found": True, "owner_id": 1},
            "machine.health.list.v2": {"heartbeats": heartbeats},
            "projector.state.claim.v2": {"claimed": []},
            "projector.store.bind.v2": {"bound": True},
        }
    )


def _search():
    return FakeClient({"search.ping.v2": {"store_id": str(uuid4()), "schema_generation": "g1"}})


@pytest.mark.asyncio
async def test_embedding_projection_waits_while_a_machine_imports_history():
    now = datetime.now(UTC)
    catalog = _import_catalog([_heartbeat("importing", received_at=now - timedelta(seconds=10))])
    projector = EmbeddingsV2Projector(catalog=catalog, search=_search())

    assert await projector.run_once(now=now) == 0
    assert [method for method, _ in catalog.calls] == ["auth.owner.get.v2", "machine.health.list.v2"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "age_seconds", "is_offline"),
    [
        ("current", 10, 0),
        ("inventory_ready", 10, 0),
        # Not moving data: waiting on these would hold semantic search back forever.
        ("paused", 10, 0),
        ("blocked_source", 10, 0),
        ("importing", 10, 1),
        # A machine that vanished mid-import still reports its last state.
        ("importing", 3600, 0),
    ],
)
async def test_embedding_projection_runs_when_no_import_is_moving(state, age_seconds, is_offline):
    now = datetime.now(UTC)
    catalog = _import_catalog([_heartbeat(state, received_at=now - timedelta(seconds=age_seconds), is_offline=is_offline)])
    projector = EmbeddingsV2Projector(catalog=catalog, search=_search())

    await projector.run_once(now=now)

    assert "projector.state.claim.v2" in [method for method, _ in catalog.calls]


@pytest.mark.asyncio
async def test_embedding_projection_rechecks_import_state_on_a_bounded_cadence():
    now = datetime.now(UTC)
    heartbeats = [_heartbeat("importing", received_at=now)]
    catalog = _import_catalog(heartbeats)
    projector = EmbeddingsV2Projector(catalog=catalog, search=_search())

    assert await projector.run_once(now=now) == 0
    heartbeats[0] = _heartbeat("current", received_at=now)
    assert await projector.run_once(now=now + timedelta(seconds=1)) == 0
    assert sum(1 for method, _ in catalog.calls if method == "machine.health.list.v2") == 1

    later = now + timedelta(seconds=IMPORT_YIELD_RECHECK_SECONDS)
    await projector.run_once(now=later)
    assert sum(1 for method, _ in catalog.calls if method == "machine.health.list.v2") == 2
    assert "projector.state.claim.v2" in [method for method, _ in catalog.calls]


@pytest.mark.asyncio
async def test_embedding_projection_proceeds_when_import_state_is_unreadable():
    """Embeddings are rebuildable: a failed health read must not stall them."""
    catalog = _import_catalog([])

    def unavailable(_params):
        raise CatalogUnavailable("catalogd did not answer")

    catalog.responses["machine.health.list.v2"] = unavailable
    projector = EmbeddingsV2Projector(catalog=catalog, search=_search())

    await projector.run_once(now=datetime.now(UTC))

    assert "projector.state.claim.v2" in [method for method, _ in catalog.calls]
