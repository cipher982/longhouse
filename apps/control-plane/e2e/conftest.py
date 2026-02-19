"""Shared fixtures for control plane E2E tests.

Server starts once per session (session-scoped) with:
- SQLite DB in a temp file
- send_verification_email patched to capture URLs (no real SES calls)
- Playwright page fixture pointing to the local server

NOTE: auth.py encodes absolute redirect URLs using settings.root_domain
(e.g. https://control.longhouse.ai/dashboard). In tests, the custom `page`
fixture intercepts those navigations and reroutes them to the local test server.
"""
from __future__ import annotations

import os
import socket
import tempfile
import threading
import time
from unittest.mock import patch

import pytest
import uvicorn
from fastapi.responses import RedirectResponse as _OrigRedirectResponse

# ------------------------------------------------------------------
# Set required env vars BEFORE any control_plane imports so the
# Settings singleton and db engine pick up the test values.
# ------------------------------------------------------------------
_DB_PATH = tempfile.mktemp(prefix="cp_e2e_", suffix=".db")  # noqa: S306 (temp file is fine for tests)
os.environ.setdefault("CONTROL_PLANE_ADMIN_TOKEN", "test-admin-e2e")
os.environ.setdefault("CONTROL_PLANE_JWT_SECRET", "test-jwt-secret-e2e-32chars-padding")
os.environ.setdefault("CONTROL_PLANE_INSTANCE_JWT_SECRET", "test-instance-jwt-e2e")
os.environ.setdefault("CONTROL_PLANE_INSTANCE_INTERNAL_API_SECRET", "test-internal-e2e")
os.environ.setdefault("CONTROL_PLANE_INSTANCE_FERNET_SECRET", "test-fernet-e2e")
os.environ.setdefault("CONTROL_PLANE_INSTANCE_TRIGGER_SIGNING_SECRET", "test-trigger-e2e")
os.environ["CONTROL_PLANE_DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

# Now safe to import control_plane
from control_plane.db import Base, SessionLocal, engine  # noqa: E402
from control_plane.main import app  # noqa: E402
from control_plane.models import User  # noqa: E402
from control_plane.routers.auth import _hash_password, _issue_session_token  # noqa: E402


# ------------------------------------------------------------------
# Email capture
# ------------------------------------------------------------------

class EmailCapture:
    """Thread-safe store for emails captured during tests."""

    def __init__(self) -> None:
        self._emails: list[dict] = []
        self._lock = threading.Lock()

    def capture(self, to: str, url: str) -> None:
        with self._lock:
            self._emails.append({"to": to, "url": url})

    def get_verify_url(self, to: str) -> str | None:
        """Return the most recent verification URL sent to *to*."""
        with self._lock:
            for email in reversed(self._emails):
                if email["to"] == to:
                    return email["url"]
        return None

    def clear(self) -> None:
        with self._lock:
            self._emails.clear()


# ------------------------------------------------------------------
# Uvicorn background thread
# ------------------------------------------------------------------

class _UvicornThread(threading.Thread):
    def __init__(self, port: int) -> None:
        super().__init__(daemon=True)
        self.port = port
        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        )

    def run(self) -> None:
        self.server.run()

    def stop(self) -> None:
        self.server.should_exit = True


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------------------
# Session-scoped fixtures
# ------------------------------------------------------------------

@pytest.fixture(scope="session")
def email_capture() -> EmailCapture:
    return EmailCapture()


@pytest.fixture(scope="session")
def cp_server(email_capture: EmailCapture):  # type: ignore[return]
    """Start control plane on a random port; yield base_url for the session."""
    # Create tables (startup event will also call create_all but doing it here ensures
    # the DB exists before any threading begins)
    Base.metadata.create_all(bind=engine)

    port = _free_port()
    server_thread = _UvicornThread(port)

    # auth.py uses absolute redirect URLs (https://control.longhouse.ai/...) and sets
    # Secure cookies.  Both break on the plain-HTTP test server:
    #   1. Absolute redirects → browser navigates to production; we need relative paths
    #   2. Secure cookie flag → Chrome won't send the cookie over HTTP
    # Patch both in auth.py's namespace so the browser stays on the test server.

    def _local_redirect_response(url: str, status_code: int = 302, **kwargs):
        """Strip the production domain from absolute redirect URLs."""
        for domain in ("https://control.longhouse.ai", "https://longhouse.ai"):
            if url.startswith(domain):
                url = url[len(domain):] or "/"
                break
        return _OrigRedirectResponse(url, status_code=status_code, **kwargs)

    def _insecure_set_session(response, token: str) -> None:
        """Set session cookie without the Secure flag so it works over HTTP."""
        response.set_cookie(
            "cp_session",
            token,
            httponly=True,
            secure=False,  # Test server runs plain HTTP
            samesite="lax",
            max_age=7 * 24 * 60 * 60,
        )

    with (
        patch("control_plane.routers.auth.RedirectResponse", _local_redirect_response),
        patch("control_plane.routers.auth._set_session", _insecure_set_session),
        patch(
            "control_plane.services.email.send_verification_email",
            side_effect=lambda to, url: email_capture.capture(to, url),
        ),
        patch(
            "control_plane.services.email.send_password_reset_email",
            side_effect=lambda to, url: email_capture.capture(to, url),
        ),
    ):
        server_thread.start()

        # Wait for uvicorn to signal it is ready
        deadline = time.monotonic() + 10.0
        while not server_thread.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("Control plane test server failed to start")
            time.sleep(0.01)

        yield f"http://127.0.0.1:{port}"

        server_thread.stop()
        server_thread.join(timeout=5)


@pytest.fixture(scope="session")
def base_url(cp_server: str) -> str:  # type: ignore[return]
    """Override pytest-playwright's base_url to point at the local test server."""
    return cp_server


# ------------------------------------------------------------------
# Per-test helpers
# ------------------------------------------------------------------

@pytest.fixture()
def db_session(cp_server):  # noqa: ARG001 — ensures server is up before we touch the DB
    """Yield a SQLAlchemy session connected to the test database."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_user(
    db,
    email: str = "user@example.com",
    password: str = "testpass123",
    verified: bool = False,
) -> User:
    """Helper: create a User directly in the test DB."""
    user = User(email=email, password_hash=_hash_password(password), email_verified=verified)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def session_cookie(user: User) -> dict[str, str]:
    """Return a cookie dict that authenticates *user* in the browser."""
    return {"cp_session": _issue_session_token(user)}
