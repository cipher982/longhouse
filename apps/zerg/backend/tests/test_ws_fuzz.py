"""Property-based fuzzing for the WebSocket contract (Phase-4).

This test uses *hypothesis* to generate random but **valid** inbound
WebSocket payloads and ensures that the backend:

1. Never crashes / raises an unhandled exception while processing the
   sequence.
2. Responds to every *ping* within **100 ms** (contract requirement).

Other message types (``subscribe_thread`` / ``send_message``) are also
included in the random stream so we exercise the DB persistence layer and
topic-broadcast path, but we do **not** assert on their contents – only that
the connection stays open.

The strategy definition is intentionally *minimal*: we derive the shape from
the Pydantic models directly rather than parsing the full JSON-Schema.  This
keeps the test fast and avoids an additional build-time dependency while
still covering a wide range of inputs.

The module is skipped automatically when *hypothesis* is not present so the
standard CI pipeline remains unchanged until the dependency is added to the
dev requirements.
"""

from __future__ import annotations

import time
import uuid

#
# Test relies on *hypothesis* + *hypothesis-jsonschema*.
from typing import List

import pytest

from tests.conftest import TEST_COMMIS_MODEL

# ``hypothesis`` is optional – auto-skip when missing.
try:
    from hypothesis import given  # type: ignore
    from hypothesis import settings  # type: ignore
    from hypothesis import strategies as st  # type: ignore

except ModuleNotFoundError:  # pragma: no cover – optional dependency
    pytest.skip("hypothesis not installed", allow_module_level=True)

# Import strategy generator – if missing, skip as well.
try:
    from backend.tests.jsonschema_strategies import strategy_for  # type: ignore

except Exception:  # pragma: no cover – unlikely
    pytest.skip("strategy helper not available", allow_module_level=True)


# ---------------------------------------------------------------------------
# Helper – build strategy sequence rather than hand-rolling fields.
# ---------------------------------------------------------------------------


def _message_strategy(thread_id: int):  # noqa: D401 – generate any *one* message
    """Return a strategy that yields **validated** WebSocket payloads.

    We reuse :pyfunc:`strategy_for` which in turn derives JSON Schema from the
    Pydantic models – single source-of-truth.
    """

    # For *send_message* we inject the concrete *thread_id* so the payload
    # passes runtime checks.
    send_msg = strategy_for("send_message").map(lambda d: {**d, "thread_id": thread_id})
    ping = strategy_for("ping")

    return st.one_of(ping, send_msg)


# ---------------------------------------------------------------------------
# Property-based test
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("client", "db_session")
@settings(max_examples=25, deadline=500)  # 500 ms deadline per example to keep the suite quick
@given(st.data())
def test_ws_fuzz_roundtrip(client, db_session, data):  # type: ignore[valid-type]
    """Randomised round-trip test exercising the WebSocket handler."""

    # ------------------------------------------------------------------
    # 1) Set-up – create *one* user, fiche and thread which the strategies
    #    refer to through *thread_id*.  Keeping a single thread avoids
    #    fallout from implicit topic subscriptions when many IDs are in
    #    play while still touching the persistence layer.
    # ------------------------------------------------------------------

    from zerg.crud import crud

    user = crud.create_user(
        db_session,
        email="fuzz@example.com",
    )

    fiche = crud.create_fiche(
        db_session,
        owner_id=user.id,
        name="FuzzAgent",
        system_instructions="",
        task_instructions="",
        model=TEST_COMMIS_MODEL,
    )

    thread = crud.create_thread(
        db_session,
        fiche_id=fiche.id,
        title="Fuzz thread",
    )

    # Refresh so subsequent operations see committed rows.
    db_session.commit()

    # ------------------------------------------------------------------
    # 2) Generate a *sequence* (≤30) of random messages referencing the
    #    freshly created thread.
    # ------------------------------------------------------------------

    seq_strategy = st.lists(_message_strategy(thread.id), min_size=1, max_size=30)
    messages: List[dict] = data.draw(seq_strategy, label="msg_sequence")

    # 3) Run the sequence against the live WebSocket endpoint.

    with client.websocket_connect("/api/ws") as websocket:  # type: ignore[func-returns-value]
        # Ensure we are subscribed so *send_message* broadcasts come back
        # to this client.  We reuse the helper to keep traffic realistic.
        websocket.send_json(
            {
                "type": "subscribe",
                "topics": [f"user:{user.id}"],
                "message_id": f"init-{uuid.uuid4()}",
            }
        )

        # Consume any optional payloads (subscribe_thread currently does not
        # send immediate data, but future versions might).  We use *poll*
        # style with a very small timeout so we never hang.
        _drain_messages(websocket, timeout=0.05)

        # Iterate through the random sequence.
        for msg in messages:
            if msg["type"] == "ping":
                start = time.perf_counter()
                websocket.send_json(msg)

                # Must receive *pong* within 100 ms --------------------
                response = _receive_with_timeout(websocket, 0.1)
                assert response["type"] == "pong"
                elapsed_ms = (time.perf_counter() - start) * 1_000
                assert elapsed_ms <= 100, f"Ping round-trip exceeded 100 ms ({elapsed_ms:.1f} ms)"
            else:
                # Non-ping messages – simply send and allow the backend to
                # process. We opportunistically drain the queue afterwards
                # so the *receive* buffer does not grow unbounded.
                websocket.send_json(msg)
                _drain_messages(websocket, timeout=0.05)

        # If we reach this point without assertion failure the example
        # passes.  The context-manager will close the socket so no extra
        # cleanup is needed.


# ---------------------------------------------------------------------------
# Small helpers – avoid blocking the pytest runner
# ---------------------------------------------------------------------------


def _receive_with_timeout(ws, timeout: float):  # noqa: D401 – helper
    """Attempt to ``receive_json`` with a *timeout* in seconds."""

    import threading

    result = {}
    exc: list[BaseException] = []  # len==0 means success

    def _commis():  # noqa: D401 – nested helper
        try:
            nonlocal result
            result = ws.receive_json()
        except BaseException as e:  # pragma: no cover – propagate in caller
            exc.append(e)

    t = threading.Thread(target=_commis, daemon=True)
    t.start()
    t.join(timeout)

    if t.is_alive():
        raise AssertionError("Timed-out waiting for WebSocket message")
    if exc:
        raise exc[0]
    return result


def _drain_messages(ws, timeout: float = 0.01):  # noqa: D401 – helper
    """Read and discard all messages available within *timeout* seconds."""

    end_time = time.perf_counter() + timeout
    while True:
        remaining = end_time - time.perf_counter()
        if remaining <= 0:
            break
        try:
            _receive_with_timeout(ws, remaining)
        except AssertionError:
            break  # no more messages available within the window
        except Exception:
            # Ignore any payload – we are only draining.
            continue
