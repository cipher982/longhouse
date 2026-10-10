"""A sender reads what happened to its message without decoding receipt statuses."""

from __future__ import annotations

import json
from datetime import timedelta

from zerg.services.directed_input_envelope import describe_directed_input_delivery

AGE = timedelta(minutes=30)


def _with_receipt(status: str, **extra) -> dict:
    return {"id": 1, "input_receipt": {"status": status, "created_at": "2026-10-09T18:00:00Z", **extra}}


def test_no_receipt_means_stored_for_inbox_only():
    facts = describe_directed_input_delivery({"id": 1, "input_receipt": None}, max_delivery_age=AGE)
    assert facts["state"] == "stored"
    assert "inbox" in facts["meaning"] and "not be injected" in facts["meaning"]
    assert facts["expires_at"] is None


def test_queued_carries_the_delivery_deadline():
    facts = describe_directed_input_delivery(_with_receipt("queued"), max_delivery_age=AGE)
    assert facts["state"] == "queued"
    assert facts["expires_at"] == "2026-10-09T18:30:00Z"
    assert "turn boundary" in facts["meaning"]


def test_delivered_is_not_read():
    facts = describe_directed_input_delivery(_with_receipt("delivered"), max_delivery_age=AGE)
    assert facts["state"] == "delivered"
    assert "not proof the model read it" in facts["meaning"]
    assert facts["expires_at"] is None


def test_expiry_is_named_and_the_inbox_copy_remains():
    expired = _with_receipt("failed", error_json=json.dumps({"reason": "delivery_expired"}))
    facts = describe_directed_input_delivery(expired, max_delivery_age=AGE)
    assert facts["state"] == "expired"
    assert "inbox" in facts["meaning"]
    other = describe_directed_input_delivery(_with_receipt("failed", error_json={"message": "channel closed"}), max_delivery_age=AGE)
    assert other["state"] == "failed" and "channel closed" in other["meaning"]


def test_a_steered_peer_message_says_it_entered_the_running_turn():
    facts = describe_directed_input_delivery(_with_receipt("delivered", intent="steer"), max_delivery_age=AGE)
    assert facts["state"] == "steered"
    assert "running turn" in facts["meaning"]
    assert facts["expires_at"] is None
