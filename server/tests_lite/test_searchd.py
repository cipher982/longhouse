from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import threading
from pathlib import Path
from uuid import UUID
from uuid import uuid4

import pytest

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.client import CatalogUnavailable
from zerg.searchd.server import SearchDaemon
from zerg.searchd.store import _PUBLISH_AGGREGATES_SQL
from zerg.searchd.store import _SEARCH_SQL
from zerg.searchd.store import _fts_query
from zerg.searchd.store import SearchStore
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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("search.db", '"search db"'),
        ("--no-verify", '"no verify"'),
        ("server/zerg/searchd/store.py", '"server zerg searchd store py"'),
        ('"exact closing text"', '"exact closing text"'),
        ("repair session recall", "repair session recall"),
        ("50068012e", "50068012e"),
        ("---", ""),
    ],
)
def test_fts_query_preserves_compact_identifiers_as_phrases(raw, expected):
    assert _fts_query(raw) == expected


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


def test_publish_aggregate_uses_session_generation_index(tmp_path):
    connection = open_search_database(tmp_path / "search.db")
    try:
        plan = connection.execute(
            f"EXPLAIN QUERY PLAN {_PUBLISH_AGGREGATES_SQL}",
            ("session", "generation", 7, "session", "generation"),
        ).fetchall()
        details = [str(row[3]) for row in plan]
        assert not any(detail == "SCAN e" for detail in details)
        assert any("ix_search_events_session_generation_order" in detail for detail in details)
    finally:
        connection.close()


def test_search_uses_fts_rank_top_k_without_temp_sort(tmp_path):
    connection = open_search_database(tmp_path / "search.db")
    try:
        plan = connection.execute(
            f"EXPLAIN QUERY PLAN {_SEARCH_SQL}",
            ("search db", "42", None, None, None, None, None, None, None, None, None, None, 10),
        ).fetchall()
        details = [str(row[3]) for row in plan]
        assert any("VIRTUAL TABLE INDEX 32:" in detail for detail in details)
        assert not any("TEMP B-TREE FOR ORDER BY" in detail for detail in details)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_search_reads_remain_live_while_projection_writer_is_busy(tmp_path):
    socket_parent = Path("/tmp") / f"lhs-{uuid4().hex[:8]}"
    socket_parent.mkdir(mode=0o700)
    socket_path = socket_parent / "s"
    daemon = SearchDaemon(database_path=tmp_path / "search.db", socket_path=socket_path)
    await daemon.start()
    writer_started = threading.Event()
    release_writer = threading.Event()

    def block_writer():
        writer_started.set()
        release_writer.wait(timeout=2)

    blocked = asyncio.create_task(daemon._run(block_writer))
    client = CatalogClient(socket_path)
    try:
        assert await asyncio.to_thread(writer_started.wait, 1)
        worklog = await asyncio.wait_for(
            client.call(
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
            ),
            timeout=0.2,
        )
        assert worklog["items"] == []
        assert not blocked.done()
    finally:
        release_writer.set()
        await blocked
        await client.close()
        await daemon.close()
        socket_parent.rmdir()


@pytest.mark.asyncio
async def test_timed_out_search_is_interrupted_before_later_reads(tmp_path):
    socket_parent = Path("/tmp") / f"lhs-{uuid4().hex[:8]}"
    socket_parent.mkdir(mode=0o700)
    socket_path = socket_parent / "s"
    daemon = SearchDaemon(database_path=tmp_path / "search.db", socket_path=socket_path)
    await daemon.start()
    assert daemon._read_store is not None
    assert daemon._read_connection is not None
    client = CatalogClient(socket_path)

    def slow_search(**_params):
        daemon._read_connection.execute("BEGIN")
        return daemon._read_connection.execute(
            """
            WITH RECURSIVE counter(value) AS (
                SELECT 0 UNION ALL SELECT value + 1 FROM counter WHERE value < 100000000
            )
            SELECT SUM(value) FROM counter
            """
        ).fetchone()

    original_search = daemon._read_store.search
    daemon._read_store.search = slow_search
    try:
        with pytest.raises((CatalogRemoteError, CatalogUnavailable)) as timeout:
            await client.call("search.query.v2", _search_params("slow"), timeout_seconds=0.05)
        if isinstance(timeout.value, CatalogRemoteError):
            assert timeout.value.code == "deadline_exceeded"
        daemon._read_store.search = original_search
        ping = await client.call("search.ping.v2", timeout_seconds=0.2)
        assert ping["ready"] is True
        assert daemon._read_connection.in_transaction is False
    finally:
        daemon._read_store.search = original_search
        await client.close()
        await daemon.close()
        socket_parent.rmdir()


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

        with sqlite3.connect(tmp_path / "search.db") as legacy:
            legacy.execute(
                "UPDATE indexed_objects SET projection_hash = ? WHERE object_id = ?",
                (hashlib.sha256(b"legacy-hash-including-session-metadata").hexdigest(), object_id),
            )
        replay_at_new_revision = await client.call(
            "search.index.object.v2",
            {
                **index_params,
                "desired_revision": "8",
                "project": "renamed-longhouse",
                "environment": "hosted",
                "cwd": "/workspace/renamed-longhouse",
                "git_repo": "cipher982/renamed-longhouse",
            },
        )
        assert replay_at_new_revision["exact_replay"] is True
        assert replay_at_new_revision["identity_upgraded"] is True
        exact_replay = await client.call(
            "search.index.object.v2",
            {
                **index_params,
                "desired_revision": "8",
                "project": "renamed-longhouse",
                "environment": "hosted",
                "cwd": "/workspace/renamed-longhouse",
                "git_repo": "cipher982/renamed-longhouse",
            },
        )
        assert exact_replay["identity_upgraded"] is False
        with pytest.raises(CatalogRemoteError, match="identity conflicts"):
            await client.call(
                "search.index.object.v2",
                {
                    **index_params,
                    "desired_revision": "8",
                    "records": _records("different payload after identity upgrade"),
                },
            )
        republished = await client.call(
            "search.index.publish.v2",
            {
                **base_publish,
                "desired_revision": "8",
                "object_count": 1,
                "project": "renamed-longhouse",
                "environment": "hosted",
                "cwd": "/workspace/renamed-longhouse",
                "git_repo": "cipher982/renamed-longhouse",
            },
        )
        assert republished["published"] is True
        renamed = await client.call(
            "search.query.v2",
            {**_search_params("speed"), "project": "renamed-longhouse", "environment": "hosted"},
        )
        assert renamed["results"][0]["session_id"] == session_id
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
            "desired_revision": "9",
            "records": _records("replacement projection"),
        }
        await client.call("search.index.object.v2", replacement_params)
        # Staging revision 9 must not disturb the fully published revision 8.
        assert len((await client.call("search.query.v2", _search_params("speed")))["results"]) == 1
        assert (await client.call("search.query.v2", _search_params("replacement")))["results"] == []
        replacement_publish = {
            **base_publish,
            "desired_revision": "9",
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


def test_searchd_upgrades_legacy_empty_object_for_same_subject_only(tmp_path):
    connection = open_search_database(tmp_path / "search.db")
    store = SearchStore(connection)
    session_id = str(uuid4())
    generation_id = str(uuid4())
    object_id = hashlib.sha256(b"empty-render-object").hexdigest()
    source_epoch = str(uuid4())
    params = {
        "session_id": session_id,
        "generation_id": generation_id,
        "object_id": object_id,
        "desired_revision": 1,
        "provider": "claude",
        "machine_id": "cinder",
        "project": "longhouse",
        "environment": "local",
        "cwd": "/workspace/longhouse",
        "git_repo": "cipher982/longhouse",
        "opaque_source_id": "claude/session.jsonl",
        "source_epoch": source_epoch,
        "records": [],
    }
    try:
        assert store.index_object(**params)["created"] is True
        connection.execute(
            "UPDATE indexed_objects SET projection_hash = ? WHERE object_id = ?",
            (hashlib.sha256(b"legacy-empty-object-hash").hexdigest(), object_id),
        )
        upgraded = store.index_object(**{**params, "desired_revision": 2, "project": "renamed-longhouse"})
        assert upgraded["identity_upgraded"] is True
        assert store.index_object(**{**params, "desired_revision": 2})["identity_upgraded"] is False
        with pytest.raises(ValueError, match="identity conflicts"):
            store.index_object(**{**params, "session_id": str(uuid4()), "desired_revision": 3})
    finally:
        connection.close()
