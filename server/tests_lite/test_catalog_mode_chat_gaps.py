from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from uuid import uuid4

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.routers import session_chat


def test_catalog_legacy_input_id_cancels_matching_live_receipt(monkeypatch):
    session_id = uuid4()
    source_session = SimpleNamespace(id=session_id)
    cancelled: list[tuple[object, str]] = []

    async def fake_recent(_session_id):
        assert _session_id == session_id
        return (
            [
                session_chat.QueuedInputSummary(
                    id=91,
                    live_input_id="receipt-91",
                    text="queued prompt",
                    intent="queue",
                    status="queued",
                )
            ],
            1,
        )

    async def fake_cancel(*, session_id, receipt_id):
        cancelled.append((session_id, receipt_id))
        return SimpleNamespace(id=receipt_id, archive_session_input_id=91)

    def fake_load(db, sid, *, owner_id):
        # Session lookup is owner-scoped now; the endpoint must pass the caller.
        assert owner_id == 7
        return source_session

    monkeypatch.setattr(session_chat, "_load_session_for_continuation", fake_load)
    monkeypatch.setattr(session_chat, "_catalog_recent_input_summaries", fake_recent)
    monkeypatch.setattr(session_chat, "cancel_live_queued_receipt_catalog", fake_cancel)

    response = asyncio.run(
        session_chat.cancel_session_input_endpoint(
            str(session_id),
            91,
            db=None,
            current_user=SimpleNamespace(id=7),
        )
    )

    assert response == {"cancelled": True, "live_input_id": "receipt-91", "input_id": 91}
    assert cancelled == [(session_id, "receipt-91")]
