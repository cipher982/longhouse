"""A machine token may only load and steer sessions its owner actually owns.

Every `/api/agents/sessions/*` control route resolves the caller from the device
token and then loads the session. The load used to be unscoped, so any valid
device token on the host could send text to, interrupt, or terminate any other
user's live session, and could tell an existing session id from a made-up one.

These tests pin both halves: the ownership gate on the load, and the fact that a
non-owner's answer is identical to the answer for a session that never existed.
Both run against a real live catalog, because the binding these routes enforce
is the one catalogd resolves -- with no daemon behind them every caller gets the
same 404 and the boundary proves nothing.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException

from tests_lite.live_catalog_harness import live_catalog  # noqa: F401
from tests_lite.live_catalog_harness import live_catalog_client  # noqa: F401
from zerg.services.session_chat_impl import _load_session_for_continuation


def test_session_load_is_scoped_to_the_owner_that_holds_the_device(live_catalog):  # noqa: F811
    owner = live_catalog.create_user("owner@owner-scope.test")
    intruder = live_catalog.create_user("intruder@owner-scope.test")
    seeded = live_catalog.commit_session(owner_id=owner, device_id="owner-laptop")

    # A Runtime Host hands these routes no SQLAlchemy session at all; the
    # binding comes back from catalogd, scoped to the resolved caller.
    loaded = _load_session_for_continuation(None, str(seeded.session_id), owner_id=owner)
    assert str(loaded.id) == str(seeded.session_id)

    unknown_session = uuid4()
    with pytest.raises(HTTPException) as intruder_error:
        _load_session_for_continuation(None, str(seeded.session_id), owner_id=intruder)
    with pytest.raises(HTTPException) as missing_error:
        _load_session_for_continuation(None, str(unknown_session), owner_id=intruder)

    # The non-owner's answer must not be distinguishable from the answer
    # for a session id that was never issued to anybody: same status, and
    # the same sentence with only the id the caller already typed in it.
    assert intruder_error.value.status_code == 404
    assert missing_error.value.status_code == 404
    assert intruder_error.value.detail == f"Session {seeded.session_id} not found"
    assert missing_error.value.detail == f"Session {unknown_session} not found"


def test_device_token_cannot_steer_a_session_it_does_not_own(live_catalog, live_catalog_client):  # noqa: F811
    owner = live_catalog.create_user("owner@owner-scope.test")
    intruder = live_catalog.create_user("intruder@owner-scope.test")
    seeded = live_catalog.commit_session(owner_id=owner, device_id="owner-laptop")
    owner_token = live_catalog.create_device_token(owner_id=owner, device_id="owner-laptop")
    intruder_token = live_catalog.create_device_token(owner_id=intruder, device_id="intruder-laptop")

    headers = {"X-Agents-Token": intruder_token}
    unknown_session = str(uuid4())
    send = live_catalog_client.post(
        f"/agents/sessions/{seeded.session_id}/send-live",
        json={"message": "whoami"},
        headers=headers,
    )
    interrupt = live_catalog_client.post(f"/agents/sessions/{seeded.session_id}/interrupt-live", headers=headers)
    terminate = live_catalog_client.post(f"/agents/sessions/{seeded.session_id}/terminate-live", headers=headers)
    pauses = live_catalog_client.get(f"/agents/sessions/{seeded.session_id}/pause-requests", headers=headers)
    send_unknown = live_catalog_client.post(
        f"/agents/sessions/{unknown_session}/send-live",
        json={"message": "whoami"},
        headers=headers,
    )

    for response in (send, interrupt, terminate, pauses):
        assert response.status_code == 404, response.text

    # An existing session the caller does not own answers exactly like a
    # session id that does not exist, so the route cannot be used to
    # enumerate ids on a shared Runtime Host.
    assert send_unknown.status_code == 404, send_unknown.text
    assert send.json()["detail"] == f"Session {seeded.session_id} not found"
    assert send_unknown.json()["detail"] == f"Session {unknown_session} not found"

    # The owner's token reaches the same session on the same route, so the
    # 404s above are the ownership gate and not a missing catalog.
    owner_pauses = live_catalog_client.get(
        f"/agents/sessions/{seeded.session_id}/pause-requests",
        headers={"X-Agents-Token": owner_token},
    )
    assert owner_pauses.status_code == 200, owner_pauses.text
