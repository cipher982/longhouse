from __future__ import annotations

import hashlib
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from uuid import UUID
from uuid import uuid4

import pytest

from zerg.services.search_v2_projector import SearchV2Projector


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method, params=None, **_kwargs):
        value = dict(params or {})
        self.calls.append((method, value))
        response = self.responses[method]
        return response(value) if callable(response) else response


class FakeRenderWorkers:
    def __init__(self, decoded):
        self.decoded = decoded
        self.calls: list[tuple[str, str, str]] = []

    async def read(self, object_path, object_hash, *, lane):
        self.calls.append((object_path, object_hash, lane))
        return self.decoded


@pytest.mark.asyncio
async def test_search_projector_indexes_frozen_manifest_then_completes_claim(monkeypatch):
    session_id = str(uuid4())
    generation_id = str(uuid4())
    source_epoch = uuid4()
    claim_token = str(uuid4())
    object_id = hashlib.sha256(b"render-object").hexdigest()
    record = SimpleNamespace(
        event_id="event-1",
        order_time_us=1_720_780_400_000_000,
        source_position=12,
        event_subordinal=0,
        role="user",
        content_text="speed of light database",
        tool_name=None,
        tool_output_text=None,
        tool_call_id=None,
    )
    decoded = SimpleNamespace(
        object_hash=object_id,
        spec=SimpleNamespace(
            session_id=UUID(session_id),
            render_generation=UUID(generation_id),
            provider="codex",
            machine_id="cinder",
            opaque_source_id="history.jsonl",
            source_epoch=source_epoch,
            records=(record,),
        ),
    )
    now = datetime.now(UTC).replace(microsecond=0)
    catalog = FakeClient(
        {
            "projector.state.claim.v2": {
                "claimed": [
                    {
                        "session_id": session_id,
                        "claimed_revision": "7",
                        "failure_count": 0,
                    }
                ]
            },
            "storage.session.render_objects.list.v2": {
                "found": True,
                "deleted": False,
                "snapshot_revision": "7",
                "generation_id": generation_id,
                "snapshot_object_count": 1,
                "snapshot_event_count": 1,
                "session": {
                    "owner_id": "42",
                    "project": "longhouse",
                    "provider": "codex",
                    "environment": "local",
                    "cwd": "/workspace/longhouse",
                    "git_repo": "cipher982/longhouse",
                    "started_at": now.isoformat(),
                },
                "objects": [
                    {
                        "object_id": object_id,
                        "object_hash": object_id,
                        "object_path": f"render/v2/{object_id}.zst",
                    }
                ],
                "has_more": False,
            },
            "projector.state.complete.v2": {"changed": True},
            "projector.state.fail.v2": {"changed": True},
        }
    )
    search = FakeClient(
        {
            "search.index.object.v2": {"created": True},
            "search.index.publish.v2": {"published": True},
        }
    )
    workers = FakeRenderWorkers(decoded)
    projector = SearchV2Projector(catalog=catalog, search=search, render_workers=workers, worker_id="test-worker")

    # Pin the token so the assertion proves one claim identity is completed.
    import zerg.services.search_v2_projector as projector_module

    monkeypatch.setattr(projector_module, "uuid4", lambda: UUID(claim_token))
    assert await projector.run_once(now=now) == 1

    assert workers.calls == [(f"render/v2/{object_id}.zst", object_id, "background")]
    index_call = next(params for method, params in search.calls if method == "search.index.object.v2")
    assert index_call["desired_revision"] == "7"
    assert index_call["records"][0]["record_ordinal"] == 0
    publish_call = next(params for method, params in search.calls if method == "search.index.publish.v2")
    assert publish_call["object_count"] == 1
    assert publish_call["event_count"] == 1
    complete_call = next(params for method, params in catalog.calls if method == "projector.state.complete.v2")
    assert complete_call["claim_token"] == claim_token
    assert complete_call["completed_revision"] == 7
