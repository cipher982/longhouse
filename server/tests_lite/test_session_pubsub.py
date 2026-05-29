"""SessionPubsub unit tests."""

import asyncio

import pytest

from zerg.services.session_pubsub import SessionPubsub
from zerg.services.session_pubsub import TOPIC_TIMELINE
from zerg.services.session_pubsub import topic_session


@pytest.mark.asyncio
async def test_publish_then_subscribe_replays_from_since_seq():
    bus = SessionPubsub()
    t = topic_session("abc")
    seq1 = bus.publish(t, {"event_id": 1})
    seq2 = bus.publish(t, {"event_id": 2})
    seq3 = bus.publish(t, {"event_id": 3})

    with bus.subscribe(t, since_seq=seq1) as sub:
        msg = await sub.next_message(timeout=0.1)
        assert msg and msg.seq == seq2
        msg = await sub.next_message(timeout=0.1)
        assert msg and msg.seq == seq3


def test_replay_gap_is_none_when_cursor_is_in_buffer():
    bus = SessionPubsub()
    t = topic_session("abc")
    seq1 = bus.publish(t, {"event_id": 1})
    bus.publish(t, {"event_id": 2})

    assert bus.replay_gap(t, since_seq=seq1) is None


def test_replay_gap_reports_cursor_older_than_ring():
    bus = SessionPubsub(buffer_size=2)
    t = topic_session("abc")
    bus.publish(t, {"event_id": 1})
    bus.publish(t, {"event_id": 2})
    bus.publish(t, {"event_id": 3})
    bus.publish(t, {"event_id": 4})

    gap = bus.replay_gap(t, since_seq=0)
    assert gap is None

    gap = bus.replay_gap(t, since_seq=1)
    assert gap is not None
    assert gap.reason == "cursor_too_old"
    assert gap.requested_seq == 1
    assert gap.earliest_seq == 3
    assert gap.latest_seq == 4


def test_replay_gap_reports_cursor_from_prior_process():
    bus = SessionPubsub()
    t = topic_session("abc")

    gap = bus.replay_gap(t, since_seq=777)

    assert gap is not None
    assert gap.reason == "buffer_unavailable"
    assert gap.requested_seq == 777
    assert gap.earliest_seq is None
    assert gap.latest_seq == 0


def test_replay_gap_reports_cursor_ahead_of_current_domain():
    bus = SessionPubsub()
    t = topic_session("abc")
    bus.publish(t, {"event_id": 1})

    gap = bus.replay_gap(t, since_seq=777)

    assert gap is not None
    assert gap.reason == "cursor_ahead"
    assert gap.earliest_seq == 1
    assert gap.latest_seq == 1


@pytest.mark.asyncio
async def test_publish_wakes_live_subscriber():
    bus = SessionPubsub()
    t = TOPIC_TIMELINE
    with bus.subscribe(t) as sub:
        # No buffered msgs; publisher writes after subscribe.
        async def publish_soon():
            await asyncio.sleep(0.01)
            bus.publish(t, {"kind": "hello"})

        task = asyncio.create_task(publish_soon())
        msg = await sub.next_message(timeout=0.5)
        assert msg and msg.payload == {"kind": "hello"}
        await task


@pytest.mark.asyncio
async def test_fanout_delivers_to_all_subscribers():
    bus = SessionPubsub()
    t = topic_session("xyz")
    with bus.subscribe(t) as s1, bus.subscribe(t) as s2:
        bus.publish(t, {"k": 1})
        m1 = await s1.next_message(timeout=0.1)
        m2 = await s2.next_message(timeout=0.1)
        assert m1 and m2
        assert m1.seq == m2.seq == 1


@pytest.mark.asyncio
async def test_slow_subscriber_drops_oldest_not_newest():
    bus = SessionPubsub(subscriber_queue_size=2)
    t = topic_session("slow")
    with bus.subscribe(t) as sub:
        bus.publish(t, {"i": 1})
        bus.publish(t, {"i": 2})
        bus.publish(t, {"i": 3})
        # Queue holds 2, oldest dropped — sub sees {2, 3}.
        m = await sub.next_message(timeout=0.1)
        assert m.payload == {"i": 2}
        m = await sub.next_message(timeout=0.1)
        assert m.payload == {"i": 3}
        assert sub.drops == 1


@pytest.mark.asyncio
async def test_topics_are_isolated():
    bus = SessionPubsub()
    with bus.subscribe(topic_session("a")) as sa, bus.subscribe(topic_session("b")) as sb:
        bus.publish(topic_session("a"), {"k": "a"})
        ma = await sa.next_message(timeout=0.1)
        mb = await sb.next_message(timeout=0.05)
        assert ma and ma.payload == {"k": "a"}
        assert mb is None


@pytest.mark.asyncio
async def test_unsubscribe_stops_delivery():
    bus = SessionPubsub()
    t = topic_session("u")
    sub = bus.subscribe(t)
    bus.publish(t, {"before": True})
    sub.close()
    bus.publish(t, {"after": True})
    # Draining after close returns the queued before-msg but not after.
    m = await sub.next_message(timeout=0.0)
    assert m is None
