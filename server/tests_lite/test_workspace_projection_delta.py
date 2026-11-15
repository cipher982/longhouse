"""An ingest wake can carry the newest transcript rows, so the web client need not read them back over HTTP."""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

from zerg.routers import timeline as timeline_mod


class _Disconnect:
    def __init__(self, cycles: int) -> None:
        self.cycles = cycles

    async def is_disconnected(self) -> bool:
        self.cycles -= 1
        return self.cycles < 0


def _row(event_id: int, cursor: str | None, text: str) -> dict:
    return {
        "kind": "event",
        "session_id": "s",
        "timestamp": "2026-11-15T12:00:00Z",
        "event": {"id": event_id, "role": "assistant", "content_text": text, "cursor": cursor},
    }


def _workspace(rows: list[dict]) -> dict:
    return {
        "projection": {
            "items": rows,
            "generation_id": "gen-1",
            "head_session_id": "head-1",
            "total": 40,
            "has_more": True,
            "next_cursor": "older",
            "abandoned_events": 0,
        }
    }


def _frames(monkeypatch, *, projection_delta: bool, build, owner_id: int | None = 7, ingests: int = 1) -> list[tuple[str, dict]]:
    from zerg.services.session_pubsub import get_pubsub
    from zerg.services.session_pubsub import reset_pubsub_for_test
    from zerg.services.session_pubsub import topic_session

    reset_pubsub_for_test()
    monkeypatch.setattr(timeline_mod, "WORKSPACE_STREAM_CHANGE_WAIT_SECONDS", 0.05, raising=False)
    monkeypatch.setattr(timeline_mod, "build_storage_v2_workspace", build)
    monkeypatch.setattr(timeline_mod, "read_live_catalog_session", lambda *_a, **_k: (None, None, "9"))
    session_id = uuid4()
    for _ in range(ingests):
        get_pubsub().publish(topic_session(str(session_id)), {"session_id": str(session_id), "kind": "ingest", "source": "storage_v2"})

    async def run() -> list[tuple[str, dict]]:
        out = []
        async for frame in timeline_mod._live_catalog_workspace_stream(
            _Disconnect(8),
            session_id=session_id,
            skip_initial=True,
            last_event_id=0,
            owner_id=owner_id,
            heartbeat_interval_seconds=3600,
            projection_delta=projection_delta,
        ):
            if frame.get("event") in {"workspace_changed", "projection_delta"}:
                out.append((frame["event"], json.loads(frame["data"])))
        return out

    return asyncio.run(run())


def test_an_ingest_wake_is_followed_by_the_newest_rows_as_a_delta(monkeypatch) -> None:
    calls = []

    async def build(**kwargs):
        calls.append(kwargs)
        return _workspace([_row(1, "c1", "old"), _row(2, "c2", "settled"), _row(3, "c3", "new reply")])

    frames = _frames(monkeypatch, projection_delta=True, build=build)
    kinds = [kind for kind, _ in frames]
    assert kinds == ["workspace_changed", "projection_delta"]
    wake = frames[0][1]
    assert wake["change_kind"] == "ingest" and wake["projection_delta"] == "follows"
    delta = frames[1][1]["delta"]
    # Anchored on the window's first row; the rows after it are the delta.
    assert delta["anchor_cursor"] == "c1"
    assert [item["event"]["id"] for item in delta["items"]] == [2, 3]
    assert delta["has_more"] is False and delta["next_cursor"] is None
    assert (delta["generation_id"], delta["head_session_id"]) == ("gen-1", "head-1")
    assert frames[1][1]["pubsub_seq"] == wake["pubsub_seq"]
    # The same read the HTTP delta makes: head rows, lite bodies, newest window.
    assert calls == [
        {
            "session_id": calls[0]["session_id"],
            "owner_id": 7,
            "branch_mode": "head",
            "limit": timeline_mod.PROJECTION_DELTA_WINDOW,
            "anchor": "tail",
            "detail": "lite",
        }
    ]


def test_a_connection_that_did_not_ask_gets_no_marker_and_no_read(monkeypatch) -> None:
    async def build(**_kwargs):
        raise AssertionError("no projection read for a connection that did not ask")

    frames = _frames(monkeypatch, projection_delta=False, build=build)
    assert [kind for kind, _ in frames] == ["workspace_changed"]
    assert "projection_delta" not in frames[0][1]


def test_a_failed_read_sends_no_delta_and_the_stream_goes_on(monkeypatch) -> None:
    async def build(**_kwargs):
        raise RuntimeError("catalog busy")

    frames = _frames(monkeypatch, projection_delta=True, build=build)
    # The wake still says a delta follows; none does, so the client reads over HTTP.
    assert [kind for kind, _ in frames] == ["workspace_changed"]
    assert frames[0][1]["projection_delta"] == "follows"


def test_a_window_with_no_cursor_or_an_oversized_window_is_not_sent(monkeypatch) -> None:
    async def no_cursor(**_kwargs):
        return _workspace([_row(1, None, "a"), _row(2, None, "b")])

    assert [kind for kind, _ in _frames(monkeypatch, projection_delta=True, build=no_cursor)] == ["workspace_changed"]

    async def huge(**_kwargs):
        return _workspace([_row(1, "c1", "a"), _row(2, "c2", "x" * (timeline_mod.PROJECTION_DELTA_MAX_BYTES + 1))])

    assert [kind for kind, _ in _frames(monkeypatch, projection_delta=True, build=huge)] == ["workspace_changed"]


def test_an_ingest_during_a_read_gets_its_own_later_delta(monkeypatch) -> None:
    # Each delta names the newest wake read before it began, so the client
    # keeps waiting for the one that covers the second ingest.
    calls = []

    async def build(**kwargs):
        calls.append(kwargs)
        await asyncio.sleep(0.02)
        return _workspace([_row(1, "c1", "old"), _row(2, "c2", f"read {len(calls)}")])

    frames = _frames(monkeypatch, projection_delta=True, build=build, ingests=2)
    wakes = [data["pubsub_seq"] for kind, data in frames if kind == "workspace_changed"]
    deltas = [data for kind, data in frames if kind == "projection_delta"]
    assert len(wakes) == 2
    assert len(calls) == 2
    assert [delta["pubsub_seq"] for delta in deltas] == wakes
    assert deltas[-1]["delta"]["items"][0]["event"]["content_text"] == "read 2"
