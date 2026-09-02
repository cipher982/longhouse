"""Longhouse sends resolve to the durable events they became, by identity."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID
from uuid import uuid4

from sqlalchemy.orm import Session

from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.store import CatalogStore
from zerg.models.live_store import LiveSessionCatalog
from zerg.services.live_session_inputs import upsert_live_input_receipt
from zerg.services.session_input_links import input_origins_by_event
from zerg.services.session_input_links import user_input_candidates
from zerg.services.storage_v2_workspace import _workspace_envelope


def _seed_delivered_receipt(
    engine, *, session_id: UUID, text: str, client_request_id: str, created_at: datetime, status: str = "delivered"
) -> None:
    with Session(engine) as db:
        if db.get(LiveSessionCatalog, str(session_id)) is None:
            db.add(
                LiveSessionCatalog(
                    session_id=str(session_id),
                    provider="claude",
                    environment="production",
                    project="longhouse",
                    device_id="cinder",
                    cwd="/workspace/longhouse",
                    started_at=created_at,
                    last_activity_at=created_at,
                    primary_thread_id=str(uuid4()),
                )
            )
        receipt = upsert_live_input_receipt(
            db,
            owner_id=7,
            session_id=session_id,
            provider="claude",
            text=text,
            intent="auto",
            status=status,
            client_request_id=client_request_id,
            now=created_at,
        )
        # The upsert stamps wall-clock time; the link rule is about when the send was accepted.
        receipt.created_at = created_at
        db.commit()


def _candidate(event_id: str, text: str, at: datetime) -> dict[str, object]:
    # The catalogd handler parses wire timestamps before the store sees them.
    return {"event_id": event_id, "timestamp": at, "text": text}


def _event(event_id: str, cursor: str, role: str, text: str) -> dict[str, object]:
    return {
        "event_id": event_id,
        "cursor": cursor,
        "role": role,
        "content_text": text,
        "timestamp": "2026-09-01T12:00:00+00:00",
        "tool_name": None,
        "tool_call_id": None,
        "tool_input_json": None,
        "tool_output_text": None,
        "branch_kind": None,
    }


def test_user_input_candidates_keep_only_user_text_events():
    at_us = int(datetime(2026, 9, 1, 12, 0, tzinfo=UTC).timestamp() * 1_000_000)
    records = [
        SimpleNamespace(role="user", tool_name=None, content_text="  ship   it ", event_id="u-1", order_time_us=at_us),
        SimpleNamespace(role="user", tool_name="Bash", content_text="tool result", event_id="t-1", order_time_us=at_us),
        SimpleNamespace(role="assistant", tool_name=None, content_text="done", event_id="a-1", order_time_us=at_us),
        SimpleNamespace(role="user", tool_name=None, content_text="   ", event_id="u-2", order_time_us=at_us),
        {"role": "user", "tool_name": None, "content_text": "wire dict", "event_id": "u-3", "order_time_us": at_us},
    ]
    candidates = user_input_candidates(records)
    assert [candidate["event_id"] for candidate in candidates] == ["u-1", "u-3"]
    assert candidates[0]["timestamp"] == "2026-09-01T12:00:00+00:00"


def test_store_links_delivered_receipts_to_matching_user_events(tmp_path):
    engine = create_catalog_engine(tmp_path / "links.db")
    initialize_catalog_schema(engine)
    session_id = uuid4()
    sent_at = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    _seed_delivered_receipt(engine, session_id=session_id, text="fix the  flaky test", client_request_id="req-1", created_at=sent_at)
    _seed_delivered_receipt(
        engine, session_id=session_id, text="then ship", client_request_id="req-2", created_at=sent_at + timedelta(seconds=30)
    )
    _seed_delivered_receipt(
        engine, session_id=session_id, text="never delivered", client_request_id="req-3", created_at=sent_at, status="failed"
    )
    store = CatalogStore(engine)

    result = store.link_input_receipts_to_events(
        session_id=str(session_id),
        candidates=[
            # Earlier than the receipt by more than the clock-skew allowance: a prior turn with the same words.
            _candidate("old-echo", "fix the flaky test", sent_at - timedelta(minutes=5)),
            _candidate("echo-1", "fix the flaky test\n", sent_at + timedelta(seconds=2)),
            _candidate("echo-2", "then ship", sent_at + timedelta(seconds=31)),
            _candidate("unrelated", "never delivered", sent_at + timedelta(seconds=1)),
        ],
        observed_at=sent_at + timedelta(minutes=1),
    )

    linked = {entry["client_request_id"]: entry["durable_event_id"] for entry in result["linked"]}
    assert linked == {"req-1": "echo-1", "req-2": "echo-2"}

    receipts = store.list_session_input_receipts(session_id=str(session_id))["receipts"]
    by_request = {receipt["client_request_id"]: receipt for receipt in receipts}
    assert by_request["req-1"]["durable_event_id"] == "echo-1"
    assert by_request["req-2"]["durable_event_id"] == "echo-2"
    assert by_request["req-3"]["durable_event_id"] is None

    # A second batch with the same text does not steal an already-linked receipt.
    again = store.link_input_receipts_to_events(
        session_id=str(session_id),
        candidates=[_candidate("echo-1-dup", "fix the flaky test", sent_at + timedelta(seconds=3))],
        observed_at=sent_at + timedelta(minutes=2),
    )
    assert again["linked"] == []


def test_workspace_envelope_stamps_input_origin_and_lists_receipts():
    session_id = uuid4()
    session = SimpleNamespace(
        provider="claude",
        runtime_display=SimpleNamespace(lifecycle="open"),
        capabilities=SimpleNamespace(live_control_available=True, can_start_turn=True),
        model_dump=lambda mode: {"id": str(session_id)},
    )
    receipts = [
        {
            "client_request_id": "req-1",
            "intent": "auto",
            "status": "delivered",
            "created_at": "2026-09-01T12:00:00+00:00",
            "event_id": "echo-1",
        },
        {"client_request_id": "req-9", "intent": "auto", "status": "queued", "created_at": "2026-09-01T12:01:00+00:00", "event_id": None},
    ]
    events = [
        _event("echo-1", "c1", "user", "fix the flaky test"),
        _event("reply-1", "c2", "assistant", "on it"),
    ]
    envelope = _workspace_envelope(
        session_id=str(session_id),
        session=session,
        session_commit_seq="7",
        branch_mode="head",
        anchor="tail",
        cursor=None,
        storage={"commit_seq": "3", "session": {"updated_at": "2026-09-01T12:00:00+00:00"}},
        page={"events": events, "total": 2, "generation_id": "g1", "next_cursor": None},
        receipts=receipts,
    )
    items = envelope["projection"]["items"]
    assert items[0]["event"]["input_origin"] == {"authored_via": "longhouse", "session_input_id": None, "client_request_id": "req-1"}
    assert items[1]["event"]["input_origin"] is None
    assert envelope["session"]["input_receipts"] == receipts


def test_input_origins_by_event_keeps_first_receipt_per_event():
    origins = input_origins_by_event(
        [
            {"client_request_id": "req-a", "event_id": "e1"},
            {"client_request_id": "req-b", "event_id": "e1"},
            {"client_request_id": "req-c", "event_id": None},
        ]
    )
    assert origins == {"e1": {"authored_via": "longhouse", "session_input_id": None, "client_request_id": "req-a"}}
