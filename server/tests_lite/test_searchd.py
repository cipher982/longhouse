from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import UUID
from uuid import uuid4

import pytest

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.client import CatalogRemoteError
from zerg.searchd.server import SearchDaemon
from zerg.searchd.store import object_set_hash
from zerg.searchd.store import open_search_database


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
            "thread_id": "thread-subagent",
            "branch_kind": "subagent",
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
            "thread_id": "thread-subagent",
            "branch_kind": "subagent",
        },
    ]


def _search_params(query: str) -> dict:
    return {
        "owner_id": "42",
        "query": query,
        "project": None,
        "provider": None,
        "environment": None,
        "window_start_us": None,
        "window_end_us": None,
        "limit": 10,
    }


def test_searchd_rebuilds_an_incompatible_disposable_store(tmp_path):
    path = tmp_path / "search.db"
    connection = open_search_database(path)
    previous_store_id = connection.execute("SELECT store_id FROM search_meta").fetchone()[0]
    connection.execute("UPDATE search_meta SET schema_generation = 'obsolete'")
    connection.close()

    rebuilt = open_search_database(path)
    try:
        meta = rebuilt.execute("SELECT schema_version, schema_generation FROM search_meta").fetchone()
        assert tuple(meta) == (1, "searchd-v1-frozen-worklog-snapshots")
        assert rebuilt.execute("SELECT store_id FROM search_meta").fetchone()[0] != previous_store_id
        assert rebuilt.execute("SELECT COUNT(*) FROM session_index").fetchone()[0] == 0
    finally:
        rebuilt.close()


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
        assert str(UUID(ping["store_id"])) == ping["store_id"]
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
        assert (await client.call("search.query.v2", _search_params("speed")))["results"] == []
        published = await client.call("search.index.publish.v2", {**base_publish, "object_count": 1})
        assert published["published"] is True
        search = await client.call("search.query.v2", _search_params("speed"))
        assert search["results"][0]["session_id"] == session_id
        assert search["results"][0]["source_object_id"] == object_id
        assert search["results"][0]["record_ordinal"] == 0
        assert "speed" in search["results"][0]["content_snippet"]
        assert "content_text" not in search["results"][0]
        filtered = await client.call("search.query.v2", {**_search_params("speed"), "provider": "claude"})
        assert filtered["results"] == []
        worklog_sessions = await client.call(
            "worklog.day.v2",
            {
                "owner_id": "42",
                "window_start_us": 1_720_780_399_000_000,
                "window_end_us": 1_720_780_401_000_000,
                "include_test": False,
                "section": "sessions",
                "snapshot_id": None,
                "offset": 0,
                "limit": 100,
            },
        )
        assert worklog_sessions["items"][0]["message_count"] == 2
        assert worklog_sessions["items"][0]["day_event_count"] == 2
        assert worklog_sessions["items"][0]["user_messages"] == 1
        assert worklog_sessions["items"][0]["assistant_messages"] == 1
        assert worklog_sessions["items"][0]["is_sidechain"] == 1
        first_worklog_page = await client.call(
            "worklog.day.v2",
            {
                "owner_id": "42",
                "window_start_us": 1_720_780_399_000_000,
                "window_end_us": 1_720_780_401_000_000,
                "include_test": False,
                "section": "events",
                "snapshot_id": worklog_sessions["snapshot_id"],
                "offset": 0,
                "limit": 1,
            },
        )
        assert [event["role"] for event in first_worklog_page["items"]] == ["user"]
        assert first_worklog_page["has_more"] is True

        replacement_id = hashlib.sha256(b"replacement-render-object").hexdigest()
        replacement_params = {
            **index_params,
            "object_id": replacement_id,
            "desired_revision": "8",
            "records": _records("replacement projection"),
        }
        await client.call("search.index.object.v2", replacement_params)
        # Staging revision 8 must not disturb the fully published revision 7.
        assert len((await client.call("search.query.v2", _search_params("speed")))["results"]) == 1
        assert (await client.call("search.query.v2", _search_params("replacement")))["results"] == []
        replacement_publish = {
            **base_publish,
            "desired_revision": "8",
            "object_set_hash": object_set_hash([replacement_id]),
            "object_count": 1,
        }
        assert (await client.call("search.index.publish.v2", replacement_publish))["published"] is True

        second_worklog_page = await client.call(
            "worklog.day.v2",
            {
                "owner_id": "42",
                "window_start_us": 1_720_780_399_000_000,
                "window_end_us": 1_720_780_401_000_000,
                "include_test": False,
                "section": "events",
                "snapshot_id": first_worklog_page["snapshot_id"],
                "offset": first_worklog_page["next_offset"],
                "limit": 1,
            },
        )
        assert [event["role"] for event in second_worklog_page["items"]] == ["assistant"]
        assert second_worklog_page["has_more"] is False
        released = await client.call(
            "worklog.snapshot.release.v2",
            {"snapshot_id": worklog_sessions["snapshot_id"], "owner_id": "42"},
        )
        assert released["released"] is True
        assert (await client.call("search.query.v2", _search_params("speed")))["results"] == []
        assert len((await client.call("search.query.v2", _search_params("replacement")))["results"]) == 1
    finally:
        await client.close()
        await daemon.close()
        socket_parent.rmdir()
