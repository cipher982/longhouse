"""E2E: auth and email-verification gates.

Verifies that protected routes redirect correctly and that unauthenticated
or unverified users cannot access gated pages.
"""
from __future__ import annotations

import re

import pytest
from playwright.sync_api import BrowserContext, Page, expect

from e2e.conftest import create_user, session_cookie


def test_unauthenticated_dashboard_redirects_to_login(page: Page, base_url: str) -> None:
    """/dashboard without a session cookie → redirected to / (login page)."""
    page.goto(f"{base_url}/dashboard")
    page.wait_for_url(re.compile(r"^http://127\.\d+\.\d+\.\d+:\d+/$"), timeout=5000)


def test_unverified_user_dashboard_redirects_to_verify(
    page: Page,
    base_url: str,
    db_session,  # type: ignore[no-untyped-def]
    context: BrowserContext,
) -> None:
    """Logged-in but unverified user → /dashboard redirects to /verify-email."""
    email = "unverified-gate@example.com"
    user = create_user(db_session, email=email, password="Pass12345", verified=False)

    cookie = session_cookie(user)
    context.add_cookies([
        {
            "name": "cp_session",
            "value": cookie["cp_session"],
            "domain": "127.0.0.1",
            "path": "/",
        }
    ])

    page.goto(f"{base_url}/dashboard")
    page.wait_for_url(re.compile(r"/verify-email"), timeout=5000)


def test_verified_user_can_reach_dashboard(
    page: Page,
    base_url: str,
    db_session,  # type: ignore[no-untyped-def]
    context: BrowserContext,
) -> None:
    """Verified user → /dashboard loads (not redirected elsewhere)."""
    email = "verified-gate@example.com"
    user = create_user(db_session, email=email, password="Pass12345", verified=True)

    cookie = session_cookie(user)
    context.add_cookies([
        {
            "name": "cp_session",
            "value": cookie["cp_session"],
            "domain": "127.0.0.1",
            "path": "/",
        }
    ])

    page.goto(f"{base_url}/dashboard")
    # Should stay on /dashboard (not redirected away)
    page.wait_for_url(re.compile(r"/dashboard"), timeout=5000)
    expect(page.locator("body")).to_be_visible()
