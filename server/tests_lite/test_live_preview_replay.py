"""A machine batch resent after a lost response is not news.

The Machine Agent resends the identical batch when it never saw the response.
The Runtime Host has usually already applied it, and the live-transcript fast
path has already fanned its preview out, because previews publish before
catalogd applies the batch. These tests pin the replay semantics in
``codex-live-transcript-item-preview.md``: a resend publishes nothing new and
moves no liveness timestamp, while a later update to the same item still does.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections import OrderedDict
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from uuid import uuid4

from tests_lite.live_catalog_harness import live_catalog as live_catalog  # noqa: F401
from tests_lite.live_catalog_harness import live_catalog_client as live_catalog_client  # noqa: F401
from zerg.services import catalogd_supervisor
from zerg.services.session_live_previews import MAX_PUBLISHED_PREVIEW_HEADS
from zerg.services.session_live_previews import LivePreviewCandidate
from zerg.services.session_live_previews import admit_preview_publication
from zerg.services.session_pubsub import get_pubsub
from zerg.services.session_pubsub import reset_pubsub_for_test
from zerg.services.session_pubsub import topic_session

T0 = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)


def _live_event(session_id: str, *, seq: int, item: str, text: str, at_ms: int) -> dict:
    """One bridge delta the way the engine mints it: identity in dedupe_key, order in seq."""
    return {
        "runtime_key": f"codex:{session_id}",
        "session_id": session_id,
        "provider": "codex",
        "device_id": "cinder",
        "source": "codex_bridge_live",
        "kind": "progress_signal",
        "occurred_at": (T0 + timedelta(milliseconds=at_ms)).isoformat(),
        "dedupe_key": f"bridge:live:{session_id}:thread-1:turn-1:{item}:{seq}",
        "payload": {
            "progress_kind": "bridge_live_transcript_delta",
            "thread_id": "thread-1",
            "turn_id": "turn-1",
            "item_id": item,
            "seq": seq,
            "item_seq": seq,
            "live_text": text,
            "turn_completed": False,
        },
    }


def _phase_event(session_id: str, *, at_ms: int) -> dict:
    return {
        "runtime_key": f"codex:{session_id}",
        "session_id": session_id,
        "provider": "codex",
        "device_id": "cinder",
        "source": "codex_bridge",
        "kind": "phase_signal",
        "phase": "running",
        "tool_name": "Shell",
        "freshness_ms": 60_000,
        "occurred_at": (T0 + timedelta(milliseconds=at_ms)).isoformat(),
        "dedupe_key": f"bridge:phase:{session_id}:running",
        "payload": {},
    }


def _published_previews(session_id: str) -> list[dict]:
    """Every transcript_preview frame this bus fanned out for the session, in order."""

    async def collect() -> list[dict]:
        with get_pubsub().subscribe(topic_session(session_id), since_seq=0) as sub:
            frames = []
            while (message := await sub.next_message(timeout=0.01)) is not None:
                frames.append(message.payload)
            return frames

    return [frame["transcript_preview"] for frame in asyncio.run(collect()) if frame["kind"] == "transcript_preview"]


def _row(table: str, where: str, params: tuple) -> dict:
    """One committed row of the live catalog, read the way a second process would."""

    database_path, _ = catalogd_supervisor.catalogd_paths()
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        found = connection.execute(f"SELECT * FROM {table} WHERE {where}", params).fetchone()
        return dict(found) if found is not None else {}
    finally:
        connection.close()


def _post(client, token: str, events: list[dict]) -> dict:
    response = client.post("/agents/runtime/events/batch", json={"events": events}, headers={"X-Agents-Token": token})
    assert response.status_code == 200, response.text
    return response.json()


def _liveness_and_preview_rows(session_id: str) -> dict:
    return {
        "session": _row("live_sessions", "session_id = ?", (session_id,)),
        "runtime": _row("live_runtime_state", "runtime_key = ?", (f"codex:{session_id}",)),
        "preview": _row("live_session_live_previews", "session_id = ?", (session_id,)),
    }


def _resent_batch() -> tuple[str, list[dict]]:
    session_id = str(uuid4())
    return session_id, [
        _phase_event(session_id, at_ms=0),
        _live_event(session_id, seq=1, item="item-1", text="Checking", at_ms=10),
        _live_event(session_id, seq=2, item="item-1", text="Checking the queue", at_ms=20),
        _live_event(session_id, seq=3, item="item-2", text="Second message", at_ms=30),
    ]


def test_resent_batch_publishes_each_live_event_once(live_catalog, live_catalog_client):  # noqa: F811
    reset_pubsub_for_test()
    owner_id = live_catalog.create_user("owner@preview-replay.test")
    token = live_catalog.create_device_token(owner_id=owner_id, device_id="cinder")
    session_id, batch = _resent_batch()

    _post(live_catalog_client, token, batch)
    assert [frame["text"] for frame in _published_previews(session_id)] == ["Second message"]

    # The machine never saw the response, so it sends the identical batch again.
    resent = _post(live_catalog_client, token, batch)

    assert [frame["text"] for frame in _published_previews(session_id)] == ["Second message"]
    assert resent["updated_runtime_keys"] == [], "a replay must not wake runtime subscribers"


def test_resent_batch_moves_no_liveness_timestamp_and_changes_no_row(live_catalog, live_catalog_client):  # noqa: F811
    reset_pubsub_for_test()
    owner_id = live_catalog.create_user("owner@liveness-replay.test")
    token = live_catalog.create_device_token(owner_id=owner_id, device_id="cinder")
    session_id, batch = _resent_batch()

    _post(live_catalog_client, token, batch)
    applied = _liveness_and_preview_rows(session_id)
    assert applied["preview"]["preview_text"] == "Second message"

    # The runtime state stamps updated_at at one-second resolution; without this
    # a replay landing in the same second could not be told from no replay.
    time.sleep(1.1)
    _post(live_catalog_client, token, batch)

    replayed = _liveness_and_preview_rows(session_id)
    assert replayed["runtime"]["updated_at"] == applied["runtime"]["updated_at"], "run liveness reads this"
    assert replayed["session"]["updated_at"] == applied["session"]["updated_at"], "session freshness reads this"
    assert replayed["preview"]["preview_updated_at"] == applied["preview"]["preview_updated_at"]
    # A replay changes no fact: every column of every liveness or preview row is
    # what the first attempt left.
    assert replayed == applied


def test_a_later_update_to_the_same_item_still_publishes_and_a_straggler_cannot_rewind_it(live_catalog, live_catalog_client):  # noqa: F811
    reset_pubsub_for_test()
    owner_id = live_catalog.create_user("owner@preview-replay-later.test")
    token = live_catalog.create_device_token(owner_id=owner_id, device_id="cinder")
    session_id = str(uuid4())
    first_batch = [
        _live_event(session_id, seq=1, item="item-1", text="Check", at_ms=10),
        _live_event(session_id, seq=2, item="item-1", text="Checking", at_ms=20),
    ]

    _post(live_catalog_client, token, first_batch)
    _post(live_catalog_client, token, first_batch)  # resend after a lost response
    assert [frame["text"] for frame in _published_previews(session_id)] == ["Checking"]

    # The same item grows: same turn, same item, higher seq. New identity, so it publishes.
    _post(live_catalog_client, token, [_live_event(session_id, seq=3, item="item-1", text="Checking the queue", at_ms=30)])
    # A later item is a later visible item, not a replay.
    _post(live_catalog_client, token, [_live_event(session_id, seq=4, item="item-2", text="Second message", at_ms=40)])
    assert [frame["text"] for frame in _published_previews(session_id)] == ["Checking", "Checking the queue", "Second message"]

    # The first batch turns up again behind two newer ones (a resend that raced
    # them). It must not step the client back, on the wire or in the projection.
    _post(live_catalog_client, token, first_batch)
    assert [frame["text"] for frame in _published_previews(session_id)] == ["Checking", "Checking the queue", "Second message"]
    assert _row("live_session_live_previews", "session_id = ?", (session_id,))["preview_text"] == "Second message"


def _candidate(
    *,
    seq: int | None,
    item: str = "item-1",
    at_ms: int = 0,
    session_id: str = "s1",
    observation: str | None = None,
) -> LivePreviewCandidate:
    return LivePreviewCandidate(
        session_id=session_id,
        thread_id="thread-1",
        turn_key=f"codex_bridge_live:{session_id}:thread-1:turn-1#{item}",
        seq=seq,
        preview_text="text does not matter",
        provisional_cursor="cursor",
        provisional_complete=False,
        preview_observed_at=T0 + timedelta(milliseconds=at_ms),
        source="codex_bridge_live",
        last_observation_id=observation or f"live:codex_bridge_live:{item}:{seq}",
    )


def test_publication_gate_follows_the_projection_ordering():
    heads: OrderedDict = OrderedDict()

    assert admit_preview_publication(heads, _candidate(seq=2, at_ms=20))
    # The same observation again, and anything at or behind the head: not news.
    assert not admit_preview_publication(heads, _candidate(seq=2, at_ms=20))
    assert not admit_preview_publication(heads, _candidate(seq=1, at_ms=10))
    # A different observation at the head's seq but an earlier instant is a straggler.
    assert not admit_preview_publication(heads, _candidate(seq=2, at_ms=15, observation="live:other-key"))
    # The same item moving forward, and a later item, both publish.
    assert admit_preview_publication(heads, _candidate(seq=3, at_ms=30))
    assert admit_preview_publication(heads, _candidate(seq=4, item="item-2", at_ms=40))
    # An earlier item behind the head is a straggler, whatever its seq says.
    assert not admit_preview_publication(heads, _candidate(seq=9, item="item-1", at_ms=35))
    # Sessions are independent.
    assert admit_preview_publication(heads, _candidate(seq=1, at_ms=0, session_id="s2"))


def test_publication_gate_is_bounded():
    heads: OrderedDict = OrderedDict()
    for index in range(MAX_PUBLISHED_PREVIEW_HEADS + 5):
        assert admit_preview_publication(heads, _candidate(seq=1, session_id=f"s{index}"))
    assert len(heads) == MAX_PUBLISHED_PREVIEW_HEADS
    # The oldest was forgotten: the cost is one republish, never a lost update.
    assert admit_preview_publication(heads, _candidate(seq=1, session_id="s0"))
