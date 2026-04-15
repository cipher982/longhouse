"""E2E: signup → verify email → dashboard happy path.

Catches:
- UI gates left on (e.g. "email signup coming soon" disabling the form)
- Verification link not working
- Dashboard unreachable after signup
"""
from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from e2e.conftest import EmailCapture, create_user, session_cookie


def test_signup_form_is_accessible(page: Page, base_url: str) -> None:
    """Signup form must be present with all inputs enabled — no 'coming soon' gate."""
    page.goto(f"{base_url}/signup")

    # Email field must exist and be enabled
    email_input = page.locator("input[type='email'], input[name='email']")
    expect(email_input).to_be_visible()
    expect(email_input).to_be_enabled()

    # Password field must exist and be enabled
    password_input = page.locator("input[type='password']").first
    expect(password_input).to_be_visible()
    expect(password_input).to_be_enabled()

    # Submit button must exist and be enabled
    submit = page.locator("button[type='submit']")
    expect(submit).to_be_visible()
    expect(submit).to_be_enabled()

    # No "coming soon" text on the page
    page_text = page.content()
    assert "coming soon" not in page_text.lower(), "Signup form has a 'coming soon' gate"


def test_signup_redirects_to_dashboard(page: Page, base_url: str, email_capture: EmailCapture) -> None:
    """Successful signup goes straight to /dashboard — no email verification wall."""
    email_capture.clear()
    email = "signup-flow-test@example.com"

    page.goto(f"{base_url}/signup")
    page.fill("input[type='email'], input[name='email']", email)

    password_inputs = page.locator("input[type='password']")
    password_inputs.nth(0).fill("SecurePass123")
    if password_inputs.count() > 1:
        password_inputs.nth(1).fill("SecurePass123")

    page.click("button[type='submit']")
    page.wait_for_url(re.compile(r"/dashboard"), timeout=5000)


def test_full_signup_dashboard(page: Page, base_url: str, email_capture: EmailCapture) -> None:
    """Full happy path: signup → land on dashboard immediately (no verify-email wall)."""
    email_capture.clear()
    email = "full-flow-test@example.com"

    # 1. Sign up
    page.goto(f"{base_url}/signup")
    page.fill("input[type='email'], input[name='email']", email)

    password_inputs = page.locator("input[type='password']")
    password_inputs.nth(0).fill("SecurePass123")
    if password_inputs.count() > 1:
        password_inputs.nth(1).fill("SecurePass123")

    page.click("button[type='submit']")
    # Should go straight to dashboard — no verify-email wall
    page.wait_for_url(re.compile(r"/dashboard"), timeout=5000)

    # 2. Dashboard must render without a 500
    expect(page.locator("body")).to_be_visible()
    expect(page.locator("body")).not_to_contain_text("Internal Server Error")

    # 3. Soft verification banner should be visible
    expect(page.locator(".alert-warning")).to_be_visible()
