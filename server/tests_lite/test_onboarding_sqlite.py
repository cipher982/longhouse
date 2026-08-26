"""SQLite-only onboarding, walked against a real live catalog.

Longhouse's OSS promise is that a SQLite path is the whole dependency list: a
self-hoster points ``DATABASE_URL`` at a file, boots, logs in with the password
they configured, mints a device token, and the sessions their machine ships
come back. This test walks that flow end to end.

It used to walk it in a subprocess under ``TESTING=1 AUTH_DISABLED=1``, which
is a shape no Runtime Host runs. ``TESTING`` bound every router to its legacy
SQLAlchemy dependency, so the device-token route wrote rows into the cold
database while production mints them over an ``auth.device.create.v2`` RPC, and
``AUTH_DISABLED`` skipped the login the flow actually starts with -- the
onboarding it proved was not the onboarding a self-hoster performs. The
subprocess bought a clean interpreter and a shaped environment;
``live_catalog_harness`` gives both per test with a real ``CatalogDaemon``
behind them, so the subprocess is gone, and with it the ``initialize_database``
call it opened with: a Runtime Host in catalog mode deliberately never
initializes the retired cold database, because catalogd owns the schema.

Every step below now runs the branch production takes. The health probes grade
a real catalogd ping, login resolves a real owner over RPC, the token is a real
catalog row, and the sessions listing reads a real committed transcript.
"""

from __future__ import annotations

import os

import pytest

from tests_lite.live_catalog_harness import DEFAULT_INTERNAL_SECRET
from tests_lite.live_catalog_harness import LiveCatalog
from tests_lite.live_catalog_harness import provision_live_catalog
from zerg.config import get_settings_unchecked
from zerg.config import sqlite_file_path
from zerg.services.catalogd_supervisor import catalogd_paths

DEVICE_ID = "onboarding-smoke"


@pytest.fixture()
def live():
    """A self-hosted Runtime Host: SQLite on disk, catalogd in front of it."""

    with provision_live_catalog() as catalog:
        yield catalog


@pytest.fixture()
def client(live: LiveCatalog):
    """``api_app`` with its import-time db dependencies forced to production's."""

    with live.http_client() as test_client:
        yield test_client


def test_sqlite_onboarding_complete(live: LiveCatalog, client):
    """The whole self-hosted onboarding flow, on the production code path.

    1. The health probes answer 200 and report the live catalog behind them.
    2. The install advertises password login.
    3. Logging in mints the owner and sets the browser session cookie.
    4. The owner mints a device token for their machine.
    5. That token lists the transcript the machine shipped.
    6. It all rests on one SQLite file, created where the configuration pointed.
    """

    # 1. Health. In catalog mode both probes ping catalogd over its socket and
    #    grade the schema, so passing is evidence the daemon is really there --
    #    the legacy branch only proved SQLAlchemy could answer SELECT 1.
    #    Per-check detail is operator information, so reading it takes the
    #    internal token the deploy verifier presents.
    ready = client.get("/readyz")
    assert ready.status_code == 200, ready.text
    # This 200 is reachable only through a compatible catalogd ping; without
    # one the same route answers 503 catalog_unavailable.
    assert ready.json()["status"] == "ok", ready.text

    health = client.get("/health", headers={"X-Internal-Token": DEFAULT_INTERNAL_SECRET})
    assert health.status_code == 200, health.text
    health_body = health.json()
    assert health_body["checks"]["catalogd"]["status"] == "pass", health_body
    assert health_body["checks"]["catalogd"]["ready"] is True, health_body
    assert health_body["checks"]["database"]["connection"] == "catalogd", health_body

    # 2. The login surface a fresh install advertises to its own web UI.
    methods = client.get("/auth/methods")
    assert methods.status_code == 200, methods.text
    assert methods.json()["password"] is True, methods.text

    # 3. Password login. There is no user yet: this call is what creates the
    #    owner, through the same catalog RPC every other login path uses.
    login = client.post("/auth/password", json={"password": os.environ["LONGHOUSE_PASSWORD"]})
    assert login.status_code == 200, login.text
    assert client.cookies.get("longhouse_session"), "password login did not set the browser session cookie"

    me = client.get("/users/me")
    assert me.status_code == 200, me.text
    owner_id = me.json()["id"]

    # 4. Minting a device token is how a machine gets to join. The plain token
    #    comes back once; the catalog keeps only its hash.
    minted = client.post("/devices/tokens", json={"device_id": DEVICE_ID})
    assert minted.status_code == 201, minted.text
    device_token = minted.json()["token"]
    assert device_token.startswith("zdt_"), minted.text

    listed = client.get("/devices/tokens")
    assert listed.status_code == 200, listed.text
    assert [token["device_id"] for token in listed.json()["tokens"]] == [DEVICE_ID], listed.text

    # 5. The machine ships a transcript and reads it back with that token. An
    #    empty 200 would pass against an empty catalog, so seed one first.
    seeded = live.commit_session(owner_id=owner_id, device_id=DEVICE_ID, project="longhouse")
    sessions = client.get(
        "/agents/sessions",
        params={"limit": 5, "days_back": 7},
        headers={"X-Agents-Token": device_token},
    )
    assert sessions.status_code == 200, sessions.text
    assert [session["id"] for session in sessions.json()["sessions"]] == [str(seeded.session_id)], sessions.text

    # 6. The onboarding claim itself, and which file it is about. Everything
    #    above landed in the live catalog's database; the cold archive beside
    #    it is retired and is never even created.
    live_database_path, _socket_path = catalogd_paths()
    assert live_database_path.exists(), f"the live catalog database was not created at {live_database_path}"
    assert live_database_path.is_relative_to(live.root.resolve())

    archive_database_path = sqlite_file_path(get_settings_unchecked().database_url)
    assert archive_database_path is not None
    assert not archive_database_path.exists(), f"catalog mode initialized the retired database at {archive_database_path}"
