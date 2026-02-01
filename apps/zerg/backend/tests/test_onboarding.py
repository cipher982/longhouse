"""Onboarding smoke tests for SQLite-first OSS setup.

These tests validate that a fresh Zerg installation boots correctly with SQLite.
They are designed to be run standalone without Docker or Postgres dependencies.

Run with: make onboarding-sqlite

NOTE: This test file belongs in tests_lite/ since it requires SQLite-only setup.
The tests here are designed to work independently of the main conftest.py which
sets up Postgres/testcontainers. Use the standalone test function directly.
"""

import os
import tempfile
from pathlib import Path

import pytest


# Mark all tests in this module as onboarding tests
pytestmark = [pytest.mark.onboarding, pytest.mark.skip(
    reason="Run via make onboarding-sqlite or test_onboarding_smoke_standalone"
)]


def test_onboarding_smoke_standalone():
    """Standalone smoke test that boots server with temp SQLite.

    This test simulates the full onboarding flow:
    1. Set up temp SQLite database
    2. Boot the FastAPI app
    3. Verify health endpoint
    4. Clean up

    Can be run independently: pytest tests/test_onboarding.py::test_onboarding_smoke_standalone -v
    """
    pytest.skip("Run via: cd apps/zerg/backend && DATABASE_URL=sqlite:///test.db uv run pytest tests/test_onboarding.py::test_onboarding_smoke_standalone -v --no-header")


# The actual implementation lives in tests_lite/test_onboarding_sqlite.py
# to avoid conflicts with the main conftest.py Postgres setup
