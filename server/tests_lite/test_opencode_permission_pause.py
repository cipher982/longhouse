"""The OpenCode plugin emits a pause_request runtime event for permission.asked;
the server must ingest it as an answerable permission_prompt pause request with
the managed-push reply transport (so Phase 2 dispatch pushes the answer back via
the bridge)."""

from __future__ import annotations

import os
from datetime import datetime
from datetime import timezone
from uuid import UUID

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-opencode-perm")
os.environ.setdefault("INTERNAL_API_SECRET", Fernet.generate_key().decode())

from tests_lite.live_catalog_harness import live_catalog  # noqa: E402, F401
from tests_lite.live_catalog_harness import live_catalog_client  # noqa: E402, F401
from zerg.database import Base  # noqa: E402
from zerg.database import make_engine  # noqa: E402
from zerg.database import make_sessionmaker  # noqa: E402
from zerg.models.agents import AgentSession  # noqa: E402
from zerg.routers import session_chat  # noqa: E402
from zerg.services.managed_local_control import ManagedLocalSendResult  # noqa: E402
from zerg.services.session_pause_requests import is_pull_reply_transport  # noqa: E402
from zerg.services.session_pause_requests import is_user_facing_pause_request  # noqa: E402
from zerg.services.session_pause_requests import load_active_pause_request_for_session  # noqa: E402
from zerg.services.session_runtime import RuntimeEventIngest  # noqa: E402
from zerg.services.session_runtime import ingest_runtime_events  # noqa: E402

OPENCODE_DEVICE_ID = "cinder"


def _make_db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'opencode_perm.db'}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed(db):
    session = AgentSession(
        provider="opencode",
        environment="test",
        project="opencode-perm",
        started_at=datetime.now(timezone.utc),
        user_messages=1,
        assistant_messages=1,
    )
    db.add(session)
    db.flush()
    db.refresh(session)
    return session


def test_opencode_permission_asked_becomes_answerable_push_pause_request(tmp_path):
    SF = _make_db(tmp_path)
    with SF() as db:
        session = _seed(db)
        runtime_key = f"opencode:{session.id}"
        # Mirror exactly what the embedded opencode plugin emits for permission.asked.
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="opencode",
                    device_id="cinder",
                    source="opencode_event",
                    kind="pause_request",
                    occurred_at=datetime.now(timezone.utc),
                    dedupe_key="oc-perm-1",
                    payload={
                        "request_id": "perm-abc",
                        "provider_request_id": "perm-abc",
                        "kind": "permission_prompt",
                        "can_respond": True,
                        "provider_ref": {
                            "source": "opencode_bridge",
                            "reply_transport": "managed_push",
                            "opencode_request_id": "perm-abc",
                        },
                        "tool_name": "bash",
                        "title": "Permission: bash",
                        "summary": "OpenCode wants to use bash",
                    },
                )
            ],
        )
        db.commit()

        row = load_active_pause_request_for_session(db, session.id)
        assert row is not None
        assert row.kind == "permission_prompt"
        assert row.can_respond is True
        assert row.provider_request_id == "perm-abc"
        assert is_user_facing_pause_request(row) is True
        # OpenCode answers PUSH over the bridge — must NOT resolve in place.
        assert is_pull_reply_transport(row) is False
        assert (row.provider_ref_json or {}).get("reply_transport") == "managed_push"


# --- Answering one, on the route a browser actually calls ------------------
#
# The pause request lives in the live catalog on a Runtime Host, so these seed
# it there through the same runtime event the plugin emits and then drive the
# real route. The dispatch itself is the seam: what matters is that the exact
# OpenCode request id reaches managed control, because that is what the bridge
# replies to.


def _seed_live_opencode_permission(live, *, request_id: str) -> tuple[int, str, UUID, str]:
    email = f"oc-{request_id}@test.local"
    owner_id = live.create_user(email)
    seeded = live.commit_session(owner_id=owner_id, device_id=OPENCODE_DEVICE_ID)
    runtime_key = f"opencode:{seeded.session_id}"
    live.rpc(
        "session.runtime.apply.v2",
        {
            "events": [
                {
                    "runtime_key": runtime_key,
                    "session_id": str(seeded.session_id),
                    "provider": "opencode",
                    "device_id": OPENCODE_DEVICE_ID,
                    "source": "opencode_event",
                    "kind": "pause_request",
                    "occurred_at": datetime.now(timezone.utc).isoformat(),
                    "dedupe_key": f"oc-perm-{request_id}",
                    "payload": {
                        "request_id": request_id,
                        "provider_request_id": request_id,
                        "kind": "permission_prompt",
                        "can_respond": True,
                        "provider_ref": {
                            "source": "opencode_bridge",
                            "reply_transport": "managed_push",
                            "opencode_request_id": request_id,
                        },
                        "tool_name": "bash",
                        "title": "Permission: bash",
                        "summary": "OpenCode wants to use bash",
                    },
                }
            ]
        },
    )
    listed = live.rpc("interaction.list.v2", {"session_id": str(seeded.session_id), "status": "pending", "limit": 20})
    assert listed["total"] == 1, listed
    interaction = listed["interactions"][0]
    assert interaction["source"] == "opencode_bridge"
    assert interaction["reply_transport"] == "managed_push"
    cookie = live.browser_cookie(owner_id=owner_id, email=email)
    return owner_id, str(interaction["id"]), seeded.session_id, cookie


def _answer_opencode_permission(
    monkeypatch,
    live,
    client,
    *,
    request_id: str,
    decision: str,
    provider_status: str,
) -> list[dict]:
    _owner_id, interaction_id, session_id, cookie = _seed_live_opencode_permission(live, request_id=request_id)
    calls: list[dict] = []

    async def _fake_answer(**kwargs):
        calls.append(kwargs)
        return ManagedLocalSendResult(ok=True, exit_code=0, response_data={"status": provider_status})

    monkeypatch.setattr(session_chat, "answer_pause_request_on_managed_local_session", _fake_answer)

    response = client.post(
        f"/sessions/{session_id}/pause-requests/{interaction_id}/response",
        json={"decision": decision},
        cookies={"longhouse_session": cookie},
    )
    assert response.status_code == 200, response.text
    expected_status = "resolved" if decision == "answer" else "rejected"
    assert response.json()["status"] == expected_status
    return calls


def test_opencode_permission_answer_dispatches_exact_provider_request(monkeypatch, live_catalog, live_catalog_client):  # noqa: F811
    """The interaction resolves only after managed control accepts its request id."""

    calls = _answer_opencode_permission(
        monkeypatch,
        live_catalog,
        live_catalog_client,
        request_id="perm-xyz",
        decision="answer",
        provider_status="resolved",
    )
    assert len(calls) == 1
    assert calls[0]["provider_request_id"] == "perm-xyz"
    assert calls[0]["decision"] == "answer"


def test_opencode_permission_deny_dispatches_exact_provider_request(monkeypatch, live_catalog, live_catalog_client):  # noqa: F811
    calls = _answer_opencode_permission(
        monkeypatch,
        live_catalog,
        live_catalog_client,
        request_id="perm-deny",
        decision="reject",
        provider_status="rejected",
    )
    assert len(calls) == 1
    assert calls[0]["provider_request_id"] == "perm-deny"
    assert calls[0]["decision"] == "reject"
