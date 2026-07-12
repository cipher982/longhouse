from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.client import CatalogRemoteError
from zerg.searchd.server import SearchDaemon
from zerg.searchd.store import object_set_hash


def _records(text: str) -> list[dict]:
    return [
        {
            "event_id": "event-1",
            "record_ordinal": 0,
            "order_time_us": 1_720_780_400_000_000,
            "source_position": 10,
            "event_subordinal": 0,
            "role": "user",
            "content_text": text,
            "tool_name": None,
            "tool_output_text": None,
            "tool_call_id": None,
        },
        {
            "event_id": "event-2",
            "record_ordinal": 1,
            "order_time_us": 1_720_780_400_000_001,
            "source_position": 11,
            "event_subordinal": 0,
            "role": "assistant",
            "content_text": "indexed answer",
            "tool_name": None,
            "tool_output_text": None,
            "tool_call_id": None,
        },
    ]


@pytest.mark.asyncio
async def test_searchd_publishes_only_complete_generations_and_serves_search_worklog(tmp_path):
    socket_parent = Path("/tmp") / f"lhs-{uuid4().hex[:8]}"
    socket_parent.mkdir(mode=0o700)
    socket_path = socket_parent / "s"
    daemon = SearchDaemon(database_path=tmp_path / "search.db", socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    session_id = str(uuid4())
    generation_id = str(uuid4())
    source_epoch = str(uuid4())
    object_id = hashlib.sha256(b"render-object").hexdigest()
    try:
        ping = await client.call("search.ping.v2")
        assert ping["ready"] is True
        index_params = {
            "session_id": session_id,
            "generation_id": generation_id,
            "object_id": object_id,
            "desired_revision": "7",
            "provider": "codex",
            "machine_id": "cinder",
            "project": "longhouse",
            "environment": "local",
            "cwd": "/workspace/longhouse",
            "git_repo": "cipher982/longhouse",
            "opaque_source_id": "codex/session.jsonl",
            "source_epoch": source_epoch,
            "records": _records("find the speed database"),
        }
        indexed = await client.call("search.index.object.v2", index_params)
        assert indexed["created"] is True
        replay = await client.call("search.index.object.v2", index_params)
        assert replay["exact_replay"] is True
        with pytest.raises(CatalogRemoteError, match="identity conflicts") as conflict:
            await client.call(
                "search.index.object.v2",
                {**index_params, "records": _records("different projection for the same immutable object")},
            )
        assert conflict.value.code == "invalid_request"
        base_publish = {
            "session_id": session_id,
            "generation_id": generation_id,
            "owner_id": "42",
            "desired_revision": "7",
            "event_count": 2,
            "object_set_hash": object_set_hash([object_id]),
            "project": "longhouse",
            "provider": "codex",
            "environment": "local",
            "cwd": "/workspace/longhouse",
            "git_repo": "cipher982/longhouse",
            "started_at": "2026-07-12T12:00:00+00:00",
        }
        lagged = await client.call("search.index.publish.v2", {**base_publish, "object_count": 2})
        assert lagged["projection_lag"] is True
        wrong_set = await client.call(
            "search.index.publish.v2",
            {**base_publish, "object_count": 1, "object_set_hash": hashlib.sha256(b"wrong-set").hexdigest()},
        )
        assert wrong_set["projection_lag"] is True
        assert (await client.call("search.query.v2", {"owner_id": "42", "query": "speed", "limit": 10}))["results"] == []
        published = await client.call("search.index.publish.v2", {**base_publish, "object_count": 1})
        assert published["published"] is True
        search = await client.call("search.query.v2", {"owner_id": "42", "query": "speed", "limit": 10})
        assert search["results"][0]["session_id"] == session_id
        assert search["results"][0]["source_object_id"] == object_id
        assert search["results"][0]["record_ordinal"] == 0
        worklog = await client.call(
            "worklog.day.v2",
            {
                "owner_id": "42",
                "window_start_us": 1_720_780_399_000_000,
                "window_end_us": 1_720_780_401_000_000,
                "include_test": False,
                "limit": 100,
            },
        )
        assert [event["role"] for event in worklog["events"]] == ["user", "assistant"]
        assert worklog["truncated"] is False
    finally:
        await client.close()
        await daemon.close()
        socket_parent.rmdir()
