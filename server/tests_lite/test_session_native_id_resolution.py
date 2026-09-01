"""Provider-native session ids resolve on the agents read surface.

Why this exists: the one id a user actually possesses is the provider-native one
(``claude --resume <uuid>``). Longhouse mints its own session ids for managed
launches, and until this fix no read path accepted the native id — the recovery
workflow 404'd on the only id the user had. These tests pin the resolve-then-404
contract: primary key first, ``provider_session_id`` thread alias second.

The alias lives in the live catalog, and the read routes resolve it through
``session.alias.resolve.v2`` after a primary-key miss, so every session below is
a real managed shell in a real catalog with a real transcript behind it, and the
native id is bound the way a managed hook binds it: a presence signal carrying
``provider_session_id``.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from uuid import UUID
from uuid import uuid4

from tests_lite.live_catalog_harness import LiveCatalog
from tests_lite.live_catalog_harness import live_catalog  # noqa: F401
from tests_lite.live_catalog_harness import live_catalog_client  # noqa: F401
from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.services.session_kernel_projection import resolve_session_id_by_provider_session_id

DEVICE_ID = "cinder"
PROVIDER = "codex"
PROMPT = "run the migration and tell me if anything breaks"


def _headers(live_catalog: LiveCatalog, owner_id: int) -> dict[str, str]:
    return {"X-Agents-Token": live_catalog.create_device_token(owner_id=owner_id, device_id=DEVICE_ID)}


def _helm_session(
    live_catalog: LiveCatalog,
    client,
    *,
    owner_id: int,
    headers: dict[str, str],
    native_id: str | None = None,
    text: str = PROMPT,
) -> UUID:
    """A managed-launch-shaped session: Longhouse id != provider-native id."""

    session_id = uuid4()
    now = datetime.now(UTC)
    created = live_catalog.rpc(
        "session.console.create.v2",
        {
            "session": {
                "session_id": str(session_id),
                "thread_id": str(uuid4()),
                "owner_id": owner_id,
                "provider": PROVIDER,
                "device_id": DEVICE_ID,
                "cwd": "/workspace/longhouse",
                "started_at": now.isoformat(),
            }
        },
    )
    assert created["created"] is True, created
    live_catalog.commit_session(owner_id=owner_id, session_id=session_id, texts=(text,), device_id=DEVICE_ID)
    if native_id is not None:
        bound = client.post(
            "/agents/presence",
            json={
                "session_id": str(session_id),
                "state": "thinking",
                "provider": PROVIDER,
                "provider_session_id": native_id,
            },
            headers=headers,
        )
        assert bound.status_code == 204, bound.text
        assert (
            live_catalog.rpc(
                "session.alias.resolve.v2",
                {"provider_session_id": native_id, "owner_id": owner_id},
            )["found"]
            is True
        )
    return session_id


def test_tail_resolves_provider_native_id(live_catalog, live_catalog_client):
    owner_id = live_catalog.create_user("owner@native-id.test")
    headers = _headers(live_catalog, owner_id)
    native_id = str(uuid4())
    longhouse_id = _helm_session(live_catalog, live_catalog_client, owner_id=owner_id, headers=headers, native_id=native_id)

    resp = live_catalog_client.get(
        f"/agents/sessions/{native_id}/tail",
        params={"roles": "user,assistant"},
        headers=headers,
    )

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    # The response teaches the caller the canonical id.
    assert payload["session_id"] == str(longhouse_id)
    assert payload["events"][0]["content"].startswith("run the migration")


def test_get_session_resolves_native_id_and_carries_it_in_the_body(live_catalog, live_catalog_client):
    owner_id = live_catalog.create_user("owner@native-id.test")
    headers = _headers(live_catalog, owner_id)
    native_id = str(uuid4())
    longhouse_id = _helm_session(live_catalog, live_catalog_client, owner_id=owner_id, headers=headers, native_id=native_id)

    resp = live_catalog_client.get(f"/agents/sessions/{native_id}", headers=headers)

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["id"] == str(longhouse_id)
    assert payload["provider_session_id"] == native_id
    assert resp.headers.get("X-Provider-Session-ID") == native_id


def test_export_resolves_provider_native_id(live_catalog, live_catalog_client):
    """Export answers on the native id with the session's own transcript.

    The live export resolves the alias and streams raw records; it sets no
    provider-id header, so the transcript itself is what proves which session
    answered.
    """

    owner_id = live_catalog.create_user("owner@native-id.test")
    headers = _headers(live_catalog, owner_id)
    native_id = str(uuid4())
    _helm_session(live_catalog, live_catalog_client, owner_id=owner_id, headers=headers, native_id=native_id)

    resp = live_catalog_client.get(f"/agents/sessions/{native_id}/export", headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.text.strip() == PROMPT


def test_unknown_id_still_404s(live_catalog, live_catalog_client):
    owner_id = live_catalog.create_user("owner@native-id.test")
    headers = _headers(live_catalog, owner_id)
    _helm_session(live_catalog, live_catalog_client, owner_id=owner_id, headers=headers, native_id=str(uuid4()))

    stranger = uuid4()

    assert live_catalog_client.get(f"/agents/sessions/{stranger}/tail", headers=headers).status_code == 404
    assert live_catalog_client.get(f"/agents/sessions/{stranger}", headers=headers).status_code == 404


def test_primary_key_wins_over_a_colliding_alias(live_catalog, live_catalog_client):
    """A Longhouse id that appears as another session's alias must never reroute."""
    owner_id = live_catalog.create_user("owner@native-id.test")
    headers = _headers(live_catalog, owner_id)
    a_id = _helm_session(live_catalog, live_catalog_client, owner_id=owner_id, headers=headers, text="session A content")
    # Session B claims A's Longhouse id as its provider-native alias.
    b_id = _helm_session(live_catalog, live_catalog_client, owner_id=owner_id, headers=headers, native_id=str(a_id))

    resp = live_catalog_client.get(f"/agents/sessions/{a_id}", headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == str(a_id), "PK lookup must win over the alias"
    # The alias still resolves for a direct resolver call.
    resolved = live_catalog.rpc(
        "session.alias.resolve.v2",
        {"provider_session_id": str(a_id), "owner_id": owner_id},
    )
    assert resolved["found"] is True
    assert UUID(resolved["session_id"]) == b_id


def test_resolver_handles_blank_and_missing_values(tmp_path):
    """The archive-side resolver stays total: no value, no row, no exception."""

    engine = make_engine(f"sqlite:///{tmp_path / 'native_id_resolver.db'}")
    Base.metadata.create_all(bind=engine)
    factory = make_sessionmaker(engine)
    try:
        with factory() as db:
            assert resolve_session_id_by_provider_session_id(db, None) is None
            assert resolve_session_id_by_provider_session_id(db, "  ") is None
            assert resolve_session_id_by_provider_session_id(db, str(uuid4())) is None
    finally:
        engine.dispose()
