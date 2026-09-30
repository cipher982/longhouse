"""Auth cookies follow the request scheme (self-host over a LAN/Tailscale IP).

A self-hosted password Runtime Host reached over plain http on a LAN or
Tailscale address used to accept the password and then sit on the login page:
every auth cookie was ``Secure``, and a browser drops a ``Secure`` cookie set
over non-loopback http. ``Secure`` (and the ``__Host-`` names that require it)
now follows the request's effective scheme. The effective scheme is the ASGI
scope scheme, which uvicorn's ProxyHeadersMiddleware rewrites from
``X-Forwarded-Proto`` only for a trusted peer (``FORWARDED_ALLOW_IPS``, default
loopback), so a stranger's forged header on a directly exposed server does not
make a cookie Secure or not. Hosted tenants stay https-only and Secure.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from starlette.requests import HTTPConnection
from starlette.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from tests_lite.live_catalog_harness import LiveCatalog
from tests_lite.live_catalog_harness import provision_live_catalog
from zerg.auth.strategy import request_session_cookie
from zerg.main import api_app


@pytest.fixture()
def live():
    with provision_live_catalog() as catalog:
        yield catalog


def _password() -> str:
    return os.environ["LONGHOUSE_PASSWORD"]


def _issued(response, name: str) -> list[str]:
    """Set-Cookie headers that set ``name`` (not the ``Max-Age=0`` retirements)."""
    return [value for value in response.headers.get_list("set-cookie") if value.startswith(f"{name}=") and "Max-Age=0" not in value]


def _retired(response, name: str) -> list[str]:
    return [value for value in response.headers.get_list("set-cookie") if value.startswith(f"{name}=") and "Max-Age=0" in value]


def _one(response, name: str) -> str:
    issued = _issued(response, name)
    assert len(issued) == 1, response.headers.get_list("set-cookie")
    return issued[0]


def _flags(set_cookie: str) -> set[str]:
    return {part.strip().lower() for part in set_cookie.split(";")[1:]}


@contextmanager
def _behind_proxy(live: LiveCatalog, *, peer: str, trusted: str = "127.0.0.1"):
    """``api_app`` behind the same middleware ``uvicorn.run`` installs by default."""
    with live.http_client():
        yield TestClient(ProxyHeadersMiddleware(api_app, trusted_hosts=trusted), client=(peer, 50000))


def test_plain_http_login_sets_cookies_a_browser_keeps(live: LiveCatalog):
    """The reported bug: http on a LAN/Tailscale IP. No ``Secure``, so no silent login loop."""
    with live.http_client(base_url="http://192.168.64.1:18080") as client:
        login = client.post("/auth/password", json={"password": _password()})
        assert login.status_code == 200, login.text

        session = _one(login, "longhouse_session")
        refresh = _one(login, "longhouse_refresh")
        assert "secure" not in _flags(session)
        assert "secure" not in _flags(refresh)
        # Everything else about the cookies is unchanged.
        assert {"httponly", "samesite=lax", "path=/"} <= _flags(session)
        assert {"httponly", "samesite=lax", "path=/api/auth"} <= _flags(refresh)
        assert not any(value.startswith("__Host-") for value in login.headers.get_list("set-cookie"))

        # The jar honours Secure the way a browser does, so a kept cookie proves the login holds.
        assert client.cookies.get("longhouse_session")
        assert client.get("/users/me").status_code == 200

        # The refresh cookie is scoped to /api/auth, which this client (mounted at the api_app
        # root) never requests, so its jar would not send it: present it the way a browser would.
        refresh_token = client.cookies.get("longhouse_refresh")
        assert refresh_token
        rotated = client.post(
            "/auth/refresh",
            headers={"X-Requested-With": "XMLHttpRequest", "Cookie": f"longhouse_refresh={refresh_token}"},
        )
        assert rotated.status_code == 200, rotated.text
        assert "secure" not in _flags(_one(rotated, "longhouse_session"))
        assert "secure" not in _flags(_one(rotated, "longhouse_refresh"))

        logout = client.post("/auth/logout", headers={"X-Requested-With": "XMLHttpRequest"})
        assert logout.status_code == 204, logout.text
        assert _retired(logout, "longhouse_session")
        assert _retired(logout, "longhouse_refresh")
        assert client.get("/users/me").status_code == 401


def test_https_login_sets_host_prefixed_secure_cookies(live: LiveCatalog):
    with live.http_client(base_url="https://longhouse.example") as client:
        login = client.post("/auth/password", json={"password": _password()})
        assert login.status_code == 200, login.text

        session = _one(login, "__Host-lh_session")
        refresh = _one(login, "__Host-lh_refresh")
        for cookie in (session, refresh):
            assert {"secure", "httponly", "samesite=lax", "path=/"} <= _flags(cookie)
        # The pre-__Host names are retired on a secure origin, never reissued.
        assert _retired(login, "longhouse_session")
        assert not _issued(login, "longhouse_session")

        assert client.get("/users/me").status_code == 200
        rotated = client.post("/auth/refresh", headers={"X-Requested-With": "XMLHttpRequest"})
        assert rotated.status_code == 200, rotated.text
        assert "secure" in _flags(_one(rotated, "__Host-lh_session"))
        assert "secure" in _flags(_one(rotated, "__Host-lh_refresh"))

        logout = client.post("/auth/logout", headers={"X-Requested-With": "XMLHttpRequest"})
        assert logout.status_code == 204, logout.text
        assert _retired(logout, "__Host-lh_session")
        assert _retired(logout, "__Host-lh_refresh")


def test_session_cookie_name_is_read_per_scheme(live: LiveCatalog):
    """A cookie minted for the other scheme's name is ignored, in both directions."""
    with live.http_client(base_url="https://longhouse.example") as https_client:
        assert https_client.post("/auth/password", json={"password": _password()}).status_code == 200
        token = https_client.cookies.get("__Host-lh_session")
        assert token

    with live.http_client(base_url="https://longhouse.example", cookies={"longhouse_session": token}) as client:
        assert client.get("/users/me").status_code == 401, "a bare-name cookie must not authenticate an https request"

    with live.http_client(base_url="http://192.168.64.1:18080", cookies={"__Host-lh_session": token}) as client:
        assert client.get("/users/me").status_code == 401, "a __Host- cookie must not authenticate a plain-http request"

    with live.http_client(base_url="http://192.168.64.1:18080", cookies={"longhouse_session": token}) as client:
        assert client.get("/users/me").status_code == 200


def test_trusted_proxy_forwarded_https_makes_cookies_secure(live: LiveCatalog):
    """Caddy on the same host (peer 127.0.0.1) terminating TLS: Secure, as before."""
    with _behind_proxy(live, peer="127.0.0.1") as client:
        login = client.post(
            "/auth/password",
            json={"password": _password()},
            headers={"X-Forwarded-Proto": "https"},
        )
        assert login.status_code == 200, login.text
        assert "secure" in _flags(_one(login, "__Host-lh_session"))
        assert "secure" in _flags(_one(login, "__Host-lh_refresh"))


def test_untrusted_peer_cannot_forge_forwarded_proto(live: LiveCatalog):
    """A LAN client sending ``X-Forwarded-Proto: https`` to the directly exposed port is still http."""
    with _behind_proxy(live, peer="192.168.64.50") as client:
        login = client.post(
            "/auth/password",
            json={"password": _password()},
            headers={"X-Forwarded-Proto": "https"},
        )
        assert login.status_code == 200, login.text
        assert "secure" not in _flags(_one(login, "longhouse_session"))
        assert not any(value.startswith("__Host-") for value in login.headers.get_list("set-cookie"))


def test_trusted_proxy_that_forwards_http_stays_insecure(live: LiveCatalog):
    with _behind_proxy(live, peer="127.0.0.1") as client:
        login = client.post(
            "/auth/password",
            json={"password": _password()},
            headers={"X-Forwarded-Proto": "http"},
        )
        assert login.status_code == 200, login.text
        assert "secure" not in _flags(_one(login, "longhouse_session"))


def test_forced_secure_override_covers_an_opaque_tls_proxy(live: LiveCatalog, monkeypatch):
    """``LONGHOUSE_COOKIE_SECURE=1``: the proxy is not trusted by uvicorn, the operator says TLS is in front."""
    monkeypatch.setenv("LONGHOUSE_COOKIE_SECURE", "1")
    with live.http_client(base_url="http://192.168.64.1:18080") as client:
        login = client.post("/auth/password", json={"password": _password()})
        assert login.status_code == 200, login.text
        assert "secure" in _flags(_one(login, "__Host-lh_session"))


def _connection(scheme: str, cookie: str, *, kind: str = "http") -> HTTPConnection:
    return HTTPConnection(
        {
            "type": kind,
            "scheme": scheme,
            "path": "/",
            "headers": [(b"cookie", cookie.encode())],
        }
    )


def test_websocket_handshake_reads_the_cookie_its_scheme_set():
    both = "longhouse_session=plain; __Host-lh_session=host"

    assert request_session_cookie(_connection("ws", both, kind="websocket")) == "plain"
    assert request_session_cookie(_connection("wss", both, kind="websocket")) == "host"


def test_hosted_reads_and_writes_only_the_secure_cookie_whatever_the_scheme():
    hosted = SimpleNamespace(control_plane_url="https://control.longhouse.ai")
    both = "longhouse_session=plain; __Host-lh_session=host"

    for scheme in ("http", "https", "ws", "wss"):
        assert request_session_cookie(_connection(scheme, both), hosted) == "host"
