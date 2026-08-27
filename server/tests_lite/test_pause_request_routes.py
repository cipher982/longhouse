"""Provider-question routes against a real live catalog.

A pause request is a provider question. On a Runtime Host it is born as a
``pause_request`` runtime event from the provider bridge, catalogd materializes
it as a ``LiveInteractionRequest``, and both the browser route and the machine
route read and resolve it through catalogd. These tests seed it the way the
bridge does and then drive the routes, so nothing here depends on the retired
archive pause table.
"""

from __future__ import annotations

import os
from datetime import UTC
from datetime import datetime
from uuid import UUID
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from tests_lite.live_catalog_harness import LiveCatalog  # noqa: E402
from tests_lite.live_catalog_harness import live_catalog  # noqa: E402, F401
from tests_lite.live_catalog_harness import live_catalog_client  # noqa: E402, F401
from zerg.routers import session_chat  # noqa: E402
from zerg.services.managed_local_control import ManagedLocalSendResult  # noqa: E402

DEVICE_ID = "cinder"
OWNER_EMAIL = "owner@pause-routes.test"
PROVIDER_REQUEST_ID = "req-1"

_QUESTIONS = [
    {
        "id": "storage",
        "header": "Storage",
        "question": "Which storage backend should I use?",
        "multiSelect": False,
        "options": [
            {"label": "SQLite", "description": "Keep it local."},
            {"label": "Postgres", "description": "Use a service."},
        ],
    }
]


class _SeededPause:
    """One owner, one session, one pending provider question in the catalog."""

    def __init__(self, *, owner_id: int, email: str, session_id: UUID, interaction: dict) -> None:
        self.owner_id = owner_id
        self.email = email
        self.session_id = session_id
        self.interaction = interaction

    @property
    def interaction_id(self) -> str:
        return str(self.interaction["id"])

    @property
    def request_key(self) -> str:
        return str(self.interaction["request_key"])

    @property
    def runtime_key(self) -> str:
        return str(self.interaction["runtime_key"])


def _pause_request_event(
    *,
    session_id: UUID,
    runtime_key: str,
    can_respond: bool,
    occurred_at: datetime,
    provider_request_id: str = PROVIDER_REQUEST_ID,
) -> dict:
    """Exactly what the codex bridge ships for a structured provider question."""

    return {
        "runtime_key": runtime_key,
        "session_id": str(session_id),
        "provider": "codex",
        "device_id": DEVICE_ID,
        "source": "codex_bridge",
        "kind": "pause_request",
        "occurred_at": occurred_at.isoformat(),
        "dedupe_key": f"pause-request:{provider_request_id}",
        "payload": {
            "request_id": provider_request_id,
            "provider_request_id": provider_request_id,
            "kind": "structured_question",
            "can_respond": can_respond,
            "title": "Choose storage",
            "summary": "The agent needs a product decision.",
            "provider_ref": {"source": "codex_bridge", "reply_transport": "managed_push"},
            "request_payload": {"questions": _QUESTIONS},
        },
    }


def _seed_pause(live: LiveCatalog, *, can_respond: bool) -> _SeededPause:
    owner_id = live.create_user(OWNER_EMAIL)
    seeded = live.commit_session(owner_id=owner_id, device_id=DEVICE_ID)
    runtime_key = f"codex:{seeded.session_id}"
    live.rpc(
        "session.runtime.apply.v2",
        {
            "events": [
                _pause_request_event(
                    session_id=seeded.session_id,
                    runtime_key=runtime_key,
                    can_respond=can_respond,
                    occurred_at=datetime.now(UTC).replace(microsecond=0),
                )
            ]
        },
    )
    listed = live.rpc("interaction.list.v2", {"session_id": str(seeded.session_id), "status": "pending", "limit": 20})
    assert listed["total"] == 1, listed
    return _SeededPause(
        owner_id=owner_id,
        email=OWNER_EMAIL,
        session_id=seeded.session_id,
        interaction=listed["interactions"][0],
    )


def _browser_cookies(live: LiveCatalog, pause: _SeededPause) -> dict[str, str]:
    return {"longhouse_session": live.browser_cookie(owner_id=pause.owner_id, email=pause.email)}


def _read_interaction(live: LiveCatalog, pause: _SeededPause) -> dict:
    listed = live.rpc("interaction.list.v2", {"session_id": str(pause.session_id), "status": None, "limit": 20})
    return next(item for item in listed["interactions"] if str(item["id"]) == pause.interaction_id)


def _resolved_answer(**overrides) -> ManagedLocalSendResult:
    response_data = {
        "request_key": overrides.pop("request_key", None),
        "provider_request_id": PROVIDER_REQUEST_ID,
        "status": "resolved",
        "response_payload": {
            "request": {"decision": "answer", "answers": {"storage": ["SQLite"]}},
            "provider_result": {"answers": {"storage": {"answers": ["SQLite"]}}},
        },
        "response_text": "Use SQLite.",
    }
    response_data.update(overrides)
    return ManagedLocalSendResult(ok=True, exit_code=0, response_data=response_data)


def test_browser_lists_pending_pause_requests(live_catalog, live_catalog_client):  # noqa: F811
    pause = _seed_pause(live_catalog, can_respond=True)

    response = live_catalog_client.get(
        f"/sessions/{pause.session_id}/pause-requests",
        cookies=_browser_cookies(live_catalog, pause),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    request = body["requests"][0]
    assert request["id"] == pause.interaction_id
    assert request["status"] == "pending"
    assert request["can_respond"] is True
    assert request["questions"][0]["id"] == "storage"


def test_machine_lists_pending_pause_requests_with_a_device_token(live_catalog, live_catalog_client):  # noqa: F811
    pause = _seed_pause(live_catalog, can_respond=True)
    token = live_catalog.create_device_token(owner_id=pause.owner_id, device_id=DEVICE_ID)

    response = live_catalog_client.get(
        f"/agents/sessions/{pause.session_id}/pause-requests",
        headers={"X-Agents-Token": token},
    )

    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1


def test_non_answerable_pause_response_returns_structured_conflict(live_catalog, live_catalog_client):  # noqa: F811
    pause = _seed_pause(live_catalog, can_respond=False)

    response = live_catalog_client.post(
        f"/sessions/{pause.session_id}/pause-requests/{pause.interaction_id}/response",
        json={"decision": "answer", "answers": {"storage": ["SQLite"]}},
        cookies=_browser_cookies(live_catalog, pause),
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "pause_request_not_answerable"
    assert response.json()["detail"]["pause_request_id"] == pause.interaction_id


def test_answerable_pause_response_dispatches_and_resolves(monkeypatch, live_catalog, live_catalog_client):  # noqa: F811
    pause = _seed_pause(live_catalog, can_respond=True)
    calls: list[dict[str, object]] = []

    async def fake_answer(**kwargs):
        calls.append(kwargs)
        return _resolved_answer(request_key=pause.request_key)

    monkeypatch.setattr(session_chat, "answer_pause_request_on_managed_local_session", fake_answer)

    response = live_catalog_client.post(
        f"/sessions/{pause.session_id}/pause-requests/{pause.interaction_id}/response",
        json={"decision": "answer", "answers": {"storage": ["SQLite"]}, "message": "Use SQLite."},
        cookies=_browser_cookies(live_catalog, pause),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "resolved"
    assert body["pause_request"]["status"] == "resolved"
    assert len(calls) == 1
    assert calls[0]["request_key"] == pause.request_key
    assert calls[0]["answers"] == {"storage": ["SQLite"]}

    stored = _read_interaction(live_catalog, pause)
    assert stored["status"] == "resolved"
    assert stored["response_text"] == "Use SQLite."
    assert stored["response_payload"]["provider_result"]["answers"]["storage"]["answers"] == ["SQLite"]


def test_pause_response_builds_message_from_structured_answers(monkeypatch, live_catalog, live_catalog_client):  # noqa: F811
    pause = _seed_pause(live_catalog, can_respond=True)
    calls: list[dict[str, object]] = []

    async def fake_answer(**kwargs):
        calls.append(kwargs)
        return ManagedLocalSendResult(
            ok=True,
            exit_code=0,
            response_data={"status": "resolved", "response_payload": {"source": "fake"}},
        )

    monkeypatch.setattr(session_chat, "answer_pause_request_on_managed_local_session", fake_answer)

    response = live_catalog_client.post(
        f"/sessions/{pause.session_id}/pause-requests/{pause.interaction_id}/response",
        json={"decision": "answer", "answers": {"storage": ["SQLite"]}},
        cookies=_browser_cookies(live_catalog, pause),
    )

    assert response.status_code == 200, response.text
    assert len(calls) == 1
    # The label is the question the provider asked, not the short header.
    assert calls[0]["message"] == "Which storage backend should I use?: SQLite"
    assert _read_interaction(live_catalog, pause)["response_text"] == "Which storage backend should I use?: SQLite"


def test_route_response_converges_with_a_later_pause_resolution_event(monkeypatch, live_catalog, live_catalog_client):  # noqa: F811
    """The user's answer and the provider's own resolution are one fact.

    Resolving through the route already emits the ``pause_resolution`` runtime
    event itself, so when the bridge later ships its own copy of that event the
    interaction must not resolve a second time or move its recorded answer.
    """

    pause = _seed_pause(live_catalog, can_respond=True)

    async def fake_answer(**_kwargs):
        return _resolved_answer(request_key=pause.request_key)

    monkeypatch.setattr(session_chat, "answer_pause_request_on_managed_local_session", fake_answer)

    response = live_catalog_client.post(
        f"/sessions/{pause.session_id}/pause-requests/{pause.interaction_id}/response",
        json={"decision": "answer", "answers": {"storage": ["SQLite"]}, "message": "Use SQLite."},
        cookies=_browser_cookies(live_catalog, pause),
    )
    assert response.status_code == 200, response.text
    resolved = _read_interaction(live_catalog, pause)
    assert resolved["status"] == "resolved"

    live_catalog.rpc(
        "session.runtime.apply.v2",
        {
            "events": [
                {
                    "runtime_key": pause.runtime_key,
                    "session_id": str(pause.session_id),
                    "provider": "codex",
                    "device_id": DEVICE_ID,
                    "source": "codex_bridge",
                    "kind": "pause_resolution",
                    "occurred_at": datetime.now(UTC).isoformat(),
                    "dedupe_key": f"pause-resolution:{PROVIDER_REQUEST_ID}",
                    "payload": {
                        "request_key": pause.request_key,
                        "provider_request_id": PROVIDER_REQUEST_ID,
                        "status": "resolved",
                        "response_payload": {"provider_result": {"status": "resolved"}},
                        "response_text": "Answered in the terminal.",
                    },
                }
            ]
        },
    )

    converged = _read_interaction(live_catalog, pause)
    assert converged["status"] == "resolved"
    assert converged["resolved_at"] == resolved["resolved_at"]
    assert converged["response_text"] == "Use SQLite."
    assert converged["response_payload"] == resolved["response_payload"]


@pytest.mark.parametrize("decision", ["reject", "cancel"])
def test_pause_response_reject_and_cancel_persist_rejected(monkeypatch, live_catalog, live_catalog_client, decision):  # noqa: F811
    pause = _seed_pause(live_catalog, can_respond=True)

    async def fake_answer(**kwargs):
        assert kwargs["decision"] == decision
        return ManagedLocalSendResult(
            ok=True,
            exit_code=0,
            response_data={
                "request_key": pause.request_key,
                "provider_request_id": PROVIDER_REQUEST_ID,
                "status": "rejected",
                "response_payload": {
                    "request": {"decision": decision},
                    "provider_result": {"status": "rejected"},
                },
                "response_text": f"{decision}ed",
            },
        )

    monkeypatch.setattr(session_chat, "answer_pause_request_on_managed_local_session", fake_answer)

    response = live_catalog_client.post(
        f"/sessions/{pause.session_id}/pause-requests/{pause.interaction_id}/response",
        json={"decision": decision},
        cookies=_browser_cookies(live_catalog, pause),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "rejected"
    assert body["pause_request"]["status"] == "rejected"

    stored = _read_interaction(live_catalog, pause)
    assert stored["status"] == "rejected"
    assert stored["response_payload"]["request"]["decision"] == decision


def test_pause_response_dispatch_failure_leaves_request_pending(monkeypatch, live_catalog, live_catalog_client):  # noqa: F811
    pause = _seed_pause(live_catalog, can_respond=True)

    async def fake_answer(**_kwargs):
        return ManagedLocalSendResult(ok=False, exit_code=12, error="bridge offline")

    monkeypatch.setattr(session_chat, "answer_pause_request_on_managed_local_session", fake_answer)

    response = live_catalog_client.post(
        f"/sessions/{pause.session_id}/pause-requests/{pause.interaction_id}/response",
        json={"decision": "answer", "answers": {"storage": ["SQLite"]}},
        cookies=_browser_cookies(live_catalog, pause),
    )

    assert response.status_code == 502, response.text
    assert response.json()["detail"]["code"] == "pause_response_dispatch_failed"
    assert response.json()["detail"]["retryable"] is True
    assert response.json()["detail"]["refetch_required"] is True

    stored = _read_interaction(live_catalog, pause)
    assert stored["status"] == "pending"
    assert stored["response_text"] is None


def test_unknown_pause_request_id_is_not_found(live_catalog, live_catalog_client):  # noqa: F811
    pause = _seed_pause(live_catalog, can_respond=True)

    response = live_catalog_client.post(
        f"/sessions/{pause.session_id}/pause-requests/{uuid4()}/response",
        json={"decision": "answer"},
        cookies=_browser_cookies(live_catalog, pause),
    )

    assert response.status_code == 404, response.text
