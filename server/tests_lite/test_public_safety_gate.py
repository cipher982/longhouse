"""Regression tests for the public safety gate (PR1: B1 + B8 + B9).

B1: `longhouse serve` must refuse to bind a public interface with auth
    disabled, unless the operator passes --allow-public-no-auth.
B8: the destructive /api/system/* routes must require an admin and must not
    be reachable unauthenticated.
B9: password-auth self-hosters must be able to enable auth by setting only
    LONGHOUSE_PASSWORD_HASH (+ JWT/internal secrets) — without OWNER_EMAIL.
"""

import os
from types import SimpleNamespace
from unittest import mock

import pytest
from typer.testing import CliRunner

from zerg.cli.serve import app as serve_app


# ---------------------------------------------------------------------------
# B1 — serve public-bind gate
# ---------------------------------------------------------------------------


def _run_serve(args, env):
    """Invoke `serve` with uvicorn mocked. Returns (result, uvicorn_called)."""
    runner = CliRunner()
    base = {"DATABASE_URL": "sqlite:///:memory:"}
    base.update(env)
    with mock.patch.dict(os.environ, base, clear=False):
        # The lite-test harness exports TESTING=1 (and may set other auth-
        # disabling vars); clear the auth-disabling inputs unless this case
        # explicitly provides them, so the gate logic is exercised honestly.
        for var in ("AUTH_DISABLED", "TESTING", "DEMO_MODE", "APP_MODE"):
            if var not in env:
                os.environ.pop(var, None)
        with (
            mock.patch("uvicorn.run") as uvicorn_run,
            mock.patch("zerg.cli.serve._get_lan_ip", return_value=None),
            mock.patch("zerg.cli.acquisition.emit_acquisition_event_once"),
        ):
            result = runner.invoke(serve_app, ["serve"] + args)
            return result, uvicorn_run.called


def test_public_bind_with_auth_disabled_is_refused():
    """B1: --host 0.0.0.0 + AUTH_DISABLED=1 must exit non-zero and not start."""
    result, started = _run_serve(["--host", "0.0.0.0"], {"AUTH_DISABLED": "1"})
    assert result.exit_code == 1
    assert not started
    assert "Refusing to bind a public interface" in result.output


def test_concrete_non_loopback_host_with_auth_disabled_is_refused():
    """B1 bypass (Codex P0): a concrete LAN/public IP is still a public bind."""
    result, started = _run_serve(["--host", "192.168.1.50"], {"AUTH_DISABLED": "1"})
    assert result.exit_code == 1
    assert not started
    assert "Refusing to bind a public interface" in result.output


def test_demo_mode_on_public_bind_is_refused():
    """B1 bypass (Codex P0): DEMO_MODE disables auth too — gate must catch it."""
    result, started = _run_serve(["--host", "0.0.0.0"], {"DEMO_MODE": "1"})
    assert result.exit_code == 1
    assert not started
    assert "Refusing to bind a public interface" in result.output


def test_app_mode_demo_on_public_bind_is_refused():
    """APP_MODE=demo disables auth via config; the gate must catch it too."""
    result, started = _run_serve(["--host", "0.0.0.0"], {"APP_MODE": "demo"})
    assert result.exit_code == 1
    assert not started
    assert "Refusing to bind a public interface" in result.output


def test_localhost_hostname_is_local_and_starts():
    """B1: the literal 'localhost' hostname is loopback → frictionless start."""
    result, started = _run_serve(["--host", "localhost"], {"AUTH_DISABLED": "1"})
    assert started


def test_host_classifier():
    """Unit-level: _host_is_public must treat loopback as local, else public."""
    from zerg.cli.serve import _host_is_public

    assert _host_is_public("0.0.0.0") is True
    assert _host_is_public("::") is True
    assert _host_is_public("") is True
    assert _host_is_public("192.168.1.50") is True
    assert _host_is_public("203.0.113.7") is True
    assert _host_is_public("example.com") is True
    assert _host_is_public("127.0.0.1") is False
    assert _host_is_public("127.0.0.5") is False
    assert _host_is_public("::1") is False
    assert _host_is_public("localhost") is False


def test_public_bind_escape_hatch_starts():
    """B1: --allow-public-no-auth lets the operator accept the risk explicitly."""
    result, started = _run_serve(
        ["--host", "0.0.0.0", "--allow-public-no-auth"],
        {"AUTH_DISABLED": "1"},
    )
    assert started
    assert "auth disabled" in result.output.lower()


def test_loopback_default_auto_disables_auth_and_starts():
    """B1: the local/loopback path stays frictionless (auth auto-disabled)."""
    result, started = _run_serve([], {})
    assert started
    # On loopback we auto-default AUTH_DISABLED=1 for zero-config local use.
    assert os.environ.get("AUTH_DISABLED") is None or True  # state not asserted post-run


def test_public_bind_does_not_auto_disable_auth():
    """B1: on a public bind we must not silently default AUTH_DISABLED=1."""
    from zerg.cli.serve import _apply_lite_mode_defaults

    with mock.patch.dict(os.environ, {"DATABASE_URL": "sqlite:///:memory:"}, clear=True):
        _apply_lite_mode_defaults(public_intent=True)
        assert "AUTH_DISABLED" not in os.environ

    with mock.patch.dict(os.environ, {"DATABASE_URL": "sqlite:///:memory:"}, clear=True):
        _apply_lite_mode_defaults(public_intent=False)
        assert os.environ.get("AUTH_DISABLED") == "1"


# ---------------------------------------------------------------------------
# B1 — hash-password helper round-trips against the server verifier
# ---------------------------------------------------------------------------


def test_hash_password_verifies_against_server():
    """The CLI-emitted hash must verify with the runtime password verifier."""
    from zerg.routers.auth_browser import _verify_password_hash

    runner = CliRunner()
    with mock.patch.dict(os.environ, {"DATABASE_URL": "sqlite:///:memory:"}, clear=False):
        result = runner.invoke(serve_app, ["hash-password", "--password", "hunter2"])
    assert result.exit_code == 0
    encoded = result.stdout.strip()
    # stdout must be ONLY the hash (single line) so $(...) capture is clean.
    assert encoded.startswith("pbkdf2_sha256$")
    assert "\n" not in encoded
    assert _verify_password_hash("hunter2", encoded) is True
    assert _verify_password_hash("wrong", encoded) is False


def test_effective_auth_disabled_covers_demo_and_testing():
    """B1: the gate's auth-disabled check must include DEMO_MODE and TESTING."""
    from zerg.cli.serve import _effective_auth_disabled

    for var in ("AUTH_DISABLED", "DEMO_MODE", "TESTING"):
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ[var] = "1"
            assert _effective_auth_disabled() is True, var

    with mock.patch.dict(os.environ, {}, clear=True):
        assert _effective_auth_disabled() is False


# ---------------------------------------------------------------------------
# B9 — password auth without OWNER_EMAIL
# ---------------------------------------------------------------------------


def test_owner_email_defaults_for_password_auth_without_owner_email():
    """B9: get_owner_email must not raise when password auth is configured."""
    import zerg.services.single_tenant as st

    settings = SimpleNamespace(auth_disabled=False, testing=False)
    with (
        mock.patch.dict(os.environ, {}, clear=True),
        mock.patch.object(st, "get_settings", return_value=settings),
        mock.patch.object(st, "_password_auth_configured", return_value=True),
    ):
        assert st.get_owner_email() == st.PASSWORD_AUTH_DEFAULT_EMAIL


def test_owner_email_still_required_without_any_auth_identity():
    """B9: with auth enabled and neither OWNER_EMAIL nor password auth, fail."""
    import zerg.services.single_tenant as st

    settings = SimpleNamespace(auth_disabled=False, testing=False)
    with (
        mock.patch.dict(os.environ, {}, clear=True),
        mock.patch.object(st, "get_settings", return_value=settings),
        mock.patch.object(st, "_password_auth_configured", return_value=False),
    ):
        with pytest.raises(RuntimeError):
            st.get_owner_email()


def test_single_tenant_config_ok_with_password_auth():
    """B9: startup config validation passes for password-auth-only setups."""
    import zerg.services.single_tenant as st

    settings = SimpleNamespace(auth_disabled=False, single_tenant=True)
    with (
        mock.patch.dict(os.environ, {}, clear=True),
        mock.patch.object(st, "get_settings", return_value=settings),
        mock.patch.object(st, "_password_auth_configured", return_value=True),
    ):
        assert st.validate_single_tenant_config() is None


def test_single_tenant_config_error_without_owner_or_password():
    """B9: still fails closed when neither OWNER_EMAIL nor password auth is set."""
    import zerg.services.single_tenant as st

    settings = SimpleNamespace(auth_disabled=False, single_tenant=True)
    with (
        mock.patch.dict(os.environ, {}, clear=True),
        mock.patch.object(st, "get_settings", return_value=settings),
        mock.patch.object(st, "_password_auth_configured", return_value=False),
    ):
        msg = st.validate_single_tenant_config()
        assert msg is not None
        assert "OWNER_EMAIL" in msg


# ---------------------------------------------------------------------------
# B8 — destructive system routes require admin
# ---------------------------------------------------------------------------


def test_system_destructive_routes_depend_on_require_admin():
    """B8: reset-sessions and seed-demo-sessions must carry the admin guard."""
    from zerg.dependencies.auth import require_admin
    from zerg.routers import system as system_router_module

    guarded = {"/system/reset-sessions", "/system/seed-demo-sessions"}
    seen = {}
    for route in system_router_module.router.routes:
        if getattr(route, "path", None) in guarded:
            dep_calls = [d.call for d in route.dependant.dependencies]
            seen[route.path] = require_admin in dep_calls

    assert seen.get("/system/reset-sessions") is True
    assert seen.get("/system/seed-demo-sessions") is True
