"""Regression tests for PR2: public read/ops surface hardening (B10).

- /api/health: verbose detail only for trusted callers; 503 only on critical
  infra failure (not on non-critical unhealthy like missing build identity).
- /metrics: not a public scrape surface.
- /api/ops/beacon: body size cap.
- agents API: per-token rate-limit helper.
"""

import contextlib

from fastapi.testclient import TestClient


@contextlib.contextmanager
def _client():
    """TestClient as a context manager so lifespan/startup runs (creates FTS)."""
    from zerg.main import app

    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# /api/health — trust gating + status code
# ---------------------------------------------------------------------------


def test_health_trusted_caller_sees_verbose_checks():
    """TestClient is loopback/testclient → trusted → full checks present."""
    with _client() as client:
        resp = client.get("/api/health")
    # Healthy in test env → 200 with verbose checks.
    assert resp.status_code == 200
    body = resp.json()
    assert "checks" in body
    assert "database" in body["checks"]


def test_health_untrusted_caller_gets_minimal_body(monkeypatch):
    """A non-loopback, unauthenticated caller must not see infra detail."""
    from zerg.routers import health as health_mod

    # Force the request to look untrusted regardless of client host.
    monkeypatch.setattr(health_mod, "_request_is_trusted", lambda request: False)

    with _client() as client:
        resp = client.get("/api/health")
    body = resp.json()
    # Minimal body: status/message plus the (public) build identity only — no
    # per-check internals, env, db path, or email detail.
    assert "checks" not in body
    assert set(body.keys()) <= {"status", "message", "build"}
    if "build" in body:
        # build block carries only version/commit info, nothing infra-sensitive.
        assert set(body["build"].keys()) <= {
            "version",
            "commit",
            "commit_short",
            "dirty",
            "built_at",
            "channel",
            "error",
            "detail",
        }


def test_health_minimal_body_has_no_db_url_or_email(monkeypatch):
    from zerg.routers import health as health_mod

    monkeypatch.setattr(health_mod, "_request_is_trusted", lambda request: False)
    with _client() as client:
        raw = client.get("/api/health").text.lower()
    assert "sqlite" not in raw
    assert "/users/" not in raw
    assert "@" not in raw  # no email addresses


def test_request_not_trusted_on_auth_disabled_public_instance(monkeypatch):
    """The demo scenario: auth_disabled + public origin + remote caller.

    The browser-auth helper returns the dev admin user for ANY request when auth
    is disabled, so the admin-session trust branch must be skipped — otherwise
    every anonymous caller gets verbose health on the public no-auth demo.
    """
    from types import SimpleNamespace

    from zerg.routers import health as health_mod

    settings = SimpleNamespace(
        auth_disabled=True,
        public_site_url="https://longhouse.ai",
        app_public_url="https://longhouse.ai",
        public_api_url=None,
        internal_api_secret="x" * 32,
    )
    monkeypatch.setattr(health_mod, "get_settings", lambda: settings)

    class _Req:
        client = SimpleNamespace(host="203.0.113.7")  # remote
        headers: dict = {}

    assert health_mod._request_is_trusted(_Req()) is False


# ---------------------------------------------------------------------------
# /metrics — not public
# ---------------------------------------------------------------------------


def test_metrics_access_helper_denies_remote_unauthenticated(monkeypatch):
    from types import SimpleNamespace

    from zerg.routers import metrics as metrics_mod

    # Simulate a remote, unauthenticated request with auth enabled.
    monkeypatch.setattr(metrics_mod, "get_settings", lambda: SimpleNamespace(auth_disabled=False, internal_api_secret="x" * 32, public_site_url="https://demo.longhouse.ai", app_public_url=None, public_api_url=None))

    class _Req:
        client = SimpleNamespace(host="203.0.113.9")
        headers: dict = {}

    assert metrics_mod._metrics_access_allowed(_Req()) is False


def test_metrics_access_helper_allows_loopback_when_no_public_origin(monkeypatch):
    from types import SimpleNamespace

    from zerg.routers import metrics as metrics_mod

    # No public origin configured → loopback is a safe trust signal.
    monkeypatch.setattr(metrics_mod, "get_settings", lambda: SimpleNamespace(auth_disabled=False, internal_api_secret="x" * 32, public_site_url=None, app_public_url=None, public_api_url=None))

    class _Req:
        client = SimpleNamespace(host="127.0.0.1")
        headers: dict = {}

    assert metrics_mod._metrics_access_allowed(_Req()) is True


def test_metrics_access_helper_denies_loopback_behind_public_proxy(monkeypatch):
    """Reverse-proxy topology: loopback is NOT trusted when a public origin is set."""
    from types import SimpleNamespace

    from zerg.routers import metrics as metrics_mod

    monkeypatch.setattr(metrics_mod, "get_settings", lambda: SimpleNamespace(auth_disabled=False, internal_api_secret="x" * 32, public_site_url="https://demo.longhouse.ai", app_public_url=None, public_api_url=None))

    class _Req:
        client = SimpleNamespace(host="127.0.0.1")
        headers: dict = {}

    assert metrics_mod._metrics_access_allowed(_Req()) is False


def test_metrics_access_helper_allows_metrics_token(monkeypatch):
    from types import SimpleNamespace

    from zerg.routers import metrics as metrics_mod

    monkeypatch.setattr(metrics_mod, "get_settings", lambda: SimpleNamespace(auth_disabled=False, internal_api_secret="x" * 32, public_site_url="https://demo.longhouse.ai", app_public_url=None, public_api_url=None))
    monkeypatch.setenv("LONGHOUSE_METRICS_TOKEN", "secret-metrics-token")

    class _Req:
        client = SimpleNamespace(host="203.0.113.9")
        headers = {"X-Metrics-Token": "secret-metrics-token"}

    assert metrics_mod._metrics_access_allowed(_Req()) is True


# ---------------------------------------------------------------------------
# /api/ops/beacon — body cap
# ---------------------------------------------------------------------------


def test_beacon_rejects_oversized_body_without_storing():
    from zerg.routers import ops as ops_mod

    # Reset buffer for determinism.
    ops_mod._frontend_errors.clear()
    huge = "x" * (ops_mod._BEACON_MAX_BODY_BYTES + 1)
    with _client() as client:
        resp = client.post("/api/ops/beacon", json={"msg": huge})
    assert resp.status_code == 200  # beacon never errors
    assert len(ops_mod._frontend_errors) == 0  # but nothing stored


def test_beacon_accepts_small_body():
    from zerg.routers import ops as ops_mod

    ops_mod._frontend_errors.clear()
    ops_mod._beacon_rate_buckets.clear()
    with _client() as client:
        resp = client.post("/api/ops/beacon", json={"msg": "boom"})
    assert resp.status_code == 200
    assert len(ops_mod._frontend_errors) == 1
    assert ops_mod._frontend_errors[0]["msg"] == "boom"


def test_beacon_oversized_content_length_rejected_before_read():
    """A large Content-Length must be rejected without buffering the body."""
    from zerg.routers import ops as ops_mod

    ops_mod._frontend_errors.clear()
    ops_mod._beacon_rate_buckets.clear()
    big = "y" * (ops_mod._BEACON_MAX_BODY_BYTES + 100)
    with _client() as client:
        resp = client.post("/api/ops/beacon", content=big, headers={"Content-Type": "application/json"})
    assert resp.status_code == 200
    assert len(ops_mod._frontend_errors) == 0


def test_beacon_rate_limited_per_ip():
    from zerg.routers import ops as ops_mod

    ops_mod._frontend_errors.clear()
    ops_mod._beacon_rate_buckets.clear()
    with _client() as client:
        # Exceed the per-IP cap; extra beacons are silently dropped.
        for _ in range(ops_mod._BEACON_RATE_MAX + 5):
            client.post("/api/ops/beacon", json={"msg": "x"})
    assert len(ops_mod._frontend_errors) == ops_mod._BEACON_RATE_MAX


def test_health_db_minimal_for_untrusted(monkeypatch):
    """/api/health/db must not disclose schema detail to untrusted callers."""
    from zerg.routers import health as health_mod

    monkeypatch.setattr(health_mod, "_request_is_trusted", lambda request: False)
    with _client() as client:
        resp = client.get("/api/health/db")
    body = resp.json()
    assert "tables_verified" not in body
    assert "missing_table" not in body


# ---------------------------------------------------------------------------
# agents API rate limit helper
# ---------------------------------------------------------------------------


def test_agents_rate_limit_helper_trips_after_max(monkeypatch):
    from zerg.dependencies import agents_auth

    monkeypatch.setattr(agents_auth, "_RATE_LIMIT_MAX_REQUESTS", 3)
    monkeypatch.setattr(agents_auth, "_RATE_LIMIT_WINDOW_SECONDS", 60.0)
    agents_auth._rate_buckets.clear()

    key = "device:test"
    # First 3 allowed.
    for _ in range(3):
        agents_auth._enforce_rate_limit(key)

    # 4th trips 429.
    import pytest
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        agents_auth._enforce_rate_limit(key)
    assert exc.value.status_code == 429
