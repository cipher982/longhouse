"""Unit-test for Gmail watch initialisation via connector.

The Gmail connect endpoint creates a connector and, when provided a callback
URL, registers a watch and stores ``history_id`` and ``watch_expiry`` in the
connector's config.
"""

from __future__ import annotations


def test_gmail_connector_initialises_watch(client, monkeypatch):
    """Connecting Gmail with a callback should persist watch metadata on the connector."""

    # Patch token exchange and watch start to deterministic values
    from zerg.routers import auth as auth_router
    from zerg.services import gmail_api as gmail_api_mod

    monkeypatch.setattr(
        auth_router, "_exchange_google_auth_code", lambda _c: {"refresh_token": "rt", "access_token": "at"}
    )
    monkeypatch.setattr(gmail_api_mod, "exchange_refresh_token", lambda _rt: "access")
    monkeypatch.setattr(
        gmail_api_mod,
        "start_watch",
        lambda *, access_token, callback_url, label_ids=None: {"history_id": 42, "watch_expiry": 9999999999999},
    )

    resp = client.post("/api/auth/google/gmail", json={"auth_code": "x", "callback_url": "https://cb"})
    assert resp.status_code == 200
    connector_id = resp.json()["connector_id"]

    # Verify connector contains watch meta
    from zerg.database import default_session_factory
    from zerg.models.models import Connector

    with default_session_factory() as s:
        conn = s.query(Connector).filter(Connector.id == connector_id).first()
        assert conn is not None
        assert conn.config.get("history_id") == 42
        assert conn.config.get("watch_expiry") == 9999999999999


def test_gmail_connector_stores_email_and_uses_pubsub_topic(client, monkeypatch):
    """Connecting Gmail should store emailAddress and prefer Pub/Sub topic watches."""

    from types import SimpleNamespace
    import sys

    from zerg.routers import auth as auth_router
    from zerg.services import gmail_api as gmail_api_mod

    monkeypatch.setattr(
        auth_router, "_exchange_google_auth_code", lambda _c: {"refresh_token": "rt", "access_token": "at"}
    )
    monkeypatch.setattr(gmail_api_mod, "exchange_refresh_token", lambda _rt: "access")

    call_args = {}

    def start_watch(*, access_token, topic_name=None, callback_url=None, label_ids=None):
        call_args["topic_name"] = topic_name
        call_args["callback_url"] = callback_url
        return {
            "history_id": 101,
            "watch_expiry": 1111111111111,
        }

    monkeypatch.setattr(gmail_api_mod, "start_watch", start_watch)

    class _Resp:
        status_code = 200

        def json(self):
            return {"emailAddress": "test@example.com"}

    httpx_stub = SimpleNamespace(get=lambda *args, **kwargs: _Resp())
    monkeypatch.setitem(sys.modules, "httpx", httpx_stub)

    monkeypatch.setattr(
        auth_router,
        "get_settings",
        lambda: SimpleNamespace(
            app_public_url="https://app.test",
            gmail_pubsub_topic="projects/test/topics/gmail",
            testing=True,
        ),
    )

    resp = client.post("/api/auth/google/gmail", json={"auth_code": "x"})
    assert resp.status_code == 200
    connector_id = resp.json()["connector_id"]
    assert call_args["topic_name"] == "projects/test/topics/gmail"
    assert call_args["callback_url"] is None

    from zerg.database import default_session_factory
    from zerg.models.models import Connector

    with default_session_factory() as s:
        conn = s.query(Connector).filter(Connector.id == connector_id).first()
        assert conn is not None
        assert conn.config.get("history_id") == 101
        assert conn.config.get("watch_expiry") == 1111111111111
        assert conn.config.get("emailAddress") == "test@example.com"
