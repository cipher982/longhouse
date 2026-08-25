"""Session sharing is disabled: nothing mints a share link, nothing serves one.

``zerg/routers/session_shares.py`` holds the reasoning. The short version:
``SessionShare`` and ``SessionShareEvent`` are declared on the archive ``Base``,
every real deployment runs the live catalog, and catalogd creates a different
set of schemas -- so the tables do not exist where a share would have to be
written. The two routers ``main.py`` includes are empty, and the handlers are
shelved on routers nothing includes.

The suite this file replaced asserted the whole share lifecycle against a
schema production never has: it called ``Base.metadata.create_all()``, minted
tokens over HTTP, and passed. That is precisely how a capability that could not
work shipped looking green, so the tests here pin the disable itself rather
than re-testing the shelved handlers through the archive schema. Coverage of
the workspace route's ``share_token`` attribution went with them: its only
token source was the endpoint that no longer exists.

``test_create_share_fails_closed_when_signing_secret_is_weak`` stays. It is a
service-level property that a revival must not lose, and it needs no route.
"""

from __future__ import annotations

import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "lh-share-tests-secret")
os.environ.setdefault("INTERNAL_API_SECRET", "lh-test-internal")
os.environ.setdefault("GOOGLE_CLIENT_ID", "lh-test-google-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "lh-test-google-client")

import zerg.dependencies.auth as auth_deps  # noqa: E402
from zerg.database import Base  # noqa: E402
from zerg.database import make_engine  # noqa: E402
from zerg.database import make_sessionmaker  # noqa: E402
from zerg.main import api_app  # noqa: E402
from zerg.models import User  # noqa: E402
from zerg.models.agents import AgentSession  # noqa: E402
from zerg.routers import session_shares  # noqa: E402
from zerg.services.session_shares import SessionShareMisconfigured  # noqa: E402
from zerg.services.session_shares import create_session_share  # noqa: E402

# Every path the shelved handlers used to answer on, as a client would send it.
# Shaped like a share token so the routes match, but deliberately
# low-entropy: nothing here should look like a real credential.
SHARE_TOKEN = "lhshr_0000.not-a-real-share-token-for-tests-only"
SHARE_REQUESTS = (
    ("POST", f"/timeline/sessions/{uuid4()}/shares"),
    ("DELETE", "/timeline/session-shares/1"),
    ("GET", f"/timeline/session-shares/{SHARE_TOKEN}/resolve"),
    ("GET", f"/public/session-shares/{SHARE_TOKEN}/preview"),
)


def _make_db(tmp_path):
    db_path = tmp_path / "test_session_shares.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def test_the_included_share_routers_are_empty():
    """The two names ``main.py`` mounts carry no routes at all.

    Emptiness is the disable. Mounting the handlers to return 404 or 501 would
    keep four dead endpoints in the published schema and leave the
    unauthenticated preview route reachable; with nothing mounted there is no
    route to match and no dependency chain to run.
    """

    assert session_shares.router.routes == []
    assert session_shares.public_router.routes == []
    # Shelved, not deleted -- the handlers still exist, on routers nothing
    # includes. If someone deletes the file's contents this test still passes,
    # so state the other half: the shelf is not empty either.
    shelved_paths = {route.path for route in session_shares._shelved.routes}
    shelved_public_paths = {route.path for route in session_shares._shelved_public.routes}
    assert shelved_paths == {
        "/timeline/sessions/{session_id}/shares",
        "/timeline/session-shares/{share_id}",
        "/timeline/session-shares/{token}/resolve",
    }
    assert shelved_public_paths == {"/public/session-shares/{token}/preview"}


def test_no_share_route_is_mounted_on_the_api():
    """Nothing in the served app answers on a share path.

    Checked against the route table rather than a status code: an unknown
    token 404s from a *mounted* handler too, so a 404 alone cannot tell
    "unmounted" from "not found".
    """

    mounted = {getattr(route, "path", "") for route in api_app.routes}
    assert not [path for path in mounted if "session-shares" in path or path.endswith("/shares")]


def test_share_endpoints_are_absent_from_the_published_schema():
    """No client can generate against a capability that cannot exist."""

    paths = api_app.openapi()["paths"]
    assert not [path for path in paths if "session-shares" in path or path.endswith("/shares")]


@pytest.mark.parametrize(("method", "path"), SHARE_REQUESTS)
def test_every_share_request_is_unroutable(method, path):
    """Including the unauthenticated public preview, which needed no cookie."""

    from fastapi.testclient import TestClient

    api_app.dependency_overrides.clear()
    try:
        with TestClient(api_app) as client:
            response = client.request(method, path, json={"note": None, "expires_in_days": 30})
        assert response.status_code == 404, response.text
    finally:
        api_app.dependency_overrides.clear()


def test_create_share_fails_closed_when_signing_secret_is_weak(tmp_path):
    """A share token is an HMAC; without a real key it must not be minted.

    Service-level, so it survives the routes being shelved -- and it is the
    property a revival most needs to keep.
    """

    from unittest.mock import patch

    session_local = _make_db(tmp_path)
    with session_local() as db:
        db.add(User(id=2, email="sharer@example.com", display_name="Sharer", role="USER"))
        session = AgentSession(
            id=uuid4(),
            provider="codex",
            environment="development",
            project="share-test",
            device_name="cinder",
            started_at=datetime.now(timezone.utc) - timedelta(minutes=30),
            user_messages=1,
            assistant_messages=1,
            tool_calls=0,
        )
        db.add(session)
        db.commit()
        session_id = str(session.id)

        with patch.object(auth_deps, "JWT_SECRET", ""):
            with pytest.raises(SessionShareMisconfigured):
                create_session_share(db, session_id=session_id, created_by_user_id=2)
