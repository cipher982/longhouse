"""E2E: login happy path and error states."""
from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from e2e.conftest import create_user


def test_login_form_present(page: Page, base_url: str) -> None:
    """Login form is present and interactive on the homepage."""
    page.goto(f"{base_url}/")

    email_input = page.locator("input[type='email'], input[name='email']")
    expect(email_input).to_be_visible()
    expect(email_input).to_be_enabled()

    password_input = page.locator("input[type='password']").first
    expect(password_input).to_be_visible()
    expect(password_input).to_be_enabled()

    submit = page.locator("button[type='submit']")
    expect(submit).to_be_visible()
    expect(submit).to_be_enabled()


def test_login_valid_credentials(page: Page, base_url: str, db_session) -> None:  # type: ignore[no-untyped-def]
    """Valid credentials → redirect to /dashboard."""
    email = "login-valid@example.com"
    create_user(db_session, email=email, password="ValidPass123", verified=True)

    page.goto(f"{base_url}/")
    page.fill("input[type='email'], input[name='email']", email)
    page.fill("input[type='password']", "ValidPass123")
    page.click("button[type='submit']")

    page.wait_for_url(re.compile(r"/dashboard"), timeout=5000)


def test_login_bad_password_shows_error(page: Page, base_url: str, db_session) -> None:  # type: ignore[no-untyped-def]
    """Wrong password → stays on login page and shows an error message."""
    email = "login-bad-pw@example.com"
    create_user(db_session, email=email, password="CorrectPass123", verified=True)

    page.goto(f"{base_url}/")
    page.fill("input[type='email'], input[name='email']", email)
    page.fill("input[type='password']", "WrongPassword!")
    page.click("button[type='submit']")

    # Should stay on / (or show error), not redirect to /dashboard
    page.wait_for_timeout(1000)
    assert "/dashboard" not in page.url, "Should not reach dashboard with wrong password"

    # Some error indication must be visible
    page_text = page.content().lower()
    assert any(kw in page_text for kw in ("invalid", "incorrect", "error", "wrong")), (
        "No error message shown after bad password"
    )
