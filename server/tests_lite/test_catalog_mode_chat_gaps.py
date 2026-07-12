from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from uuid import uuid4

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.routers import agents_storage_v2
from zerg.routers import session_chat
from zerg.services import session_chat_impl


def test_catalog_draft_reply_uses_storage_v2_render_tail(monkeypatch):
    session_id = uuid4()
    source_session = SimpleNamespace(
        id=session_id,
        provider="codex",
        project="longhouse",
        cwd="/work/longhouse",
        git_branch="main",
        status="active",
    )
    requested: list[dict[str, object]] = []
    prompts: list[dict[str, object]] = []

    async def fake_events_page(**kwargs):
        requested.append(kwargs)
        return {
            "events": [
                {
                    "event_id": "legacy:42",
                    "role": "user",
                    "content_text": "Finish the catalog cutover.",
                    "branch_kind": "head",
                },
                {
                    "event_id": "native-event",
                    "role": "assistant",
                    "content_text": "The focused checks are green.",
                    "branch_kind": None,
                },
                {
                    "event_id": "abandoned-event",
                    "role": "assistant",
                    "content_text": "This abandoned branch must not be used.",
                    "branch_kind": "abandoned",
                },
            ]
        }

    class FakeCompletions:
        async def create(self, **kwargs):
            prompts.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="Ship the green cutover."))]
            )

    class FakeClient:
        chat = SimpleNamespace(completions=FakeCompletions())

        async def close(self):
            return None

    monkeypatch.setattr(session_chat_impl.database_module, "live_catalog_enabled", lambda: True)
    monkeypatch.setattr(session_chat_impl, "_assert_live_session_send_available", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(agents_storage_v2, "read_storage_v2_session_events_page", fake_events_page)
    monkeypatch.setattr(
        session_chat_impl,
        "get_llm_client_for_use_case",
        lambda _use_case: (FakeClient(), "draft-model", "test"),
    )

    response = asyncio.run(
        session_chat_impl._build_managed_local_draft_reply_response(
            source_session=source_session,
            request_id="draft-test",
            max_chars=500,
            db=None,
            owner_id=7,
        )
    )

    assert response.draft_text == "Ship the green cutover."
    assert response.based_on_event_ids == [42]
    assert requested == [
        {
            "session_id": session_id,
            "owner_id": "7",
            "cursor": None,
            "anchor": "tail",
            "limit": 80,
        }
    ]
    prompt = prompts[0]["messages"][1]["content"]
    assert "Finish the catalog cutover." in prompt
    assert "focused checks are green" in prompt
    assert "abandoned branch" not in prompt


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

    monkeypatch.setattr(session_chat.database_module, "live_catalog_enabled", lambda: True)
    monkeypatch.setattr(session_chat, "_load_session_for_continuation", lambda db, sid: source_session)
    monkeypatch.setattr(session_chat, "_catalog_recent_input_summaries", fake_recent)
    monkeypatch.setattr(session_chat, "cancel_live_queued_receipt_catalog", fake_cancel)

    response = asyncio.run(
        session_chat.cancel_session_input_endpoint(
            str(session_id),
            91,
            db=None,
            _current_user=SimpleNamespace(id=7),
        )
    )

    assert response == {"cancelled": True, "live_input_id": "receipt-91", "input_id": 91}
    assert cancelled == [(session_id, "receipt-91")]
