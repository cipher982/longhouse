"""Tests for TestClient fixtures used in the suite."""

from __future__ import annotations


def test_unauthenticated_client_raises_by_default(unauthenticated_client):
    """Default unauthenticated client should raise server exceptions."""

    assert unauthenticated_client._transport.raise_server_exceptions is True  # noqa: SLF001


def test_unauthenticated_client_no_raise_flag(unauthenticated_client_no_raise):
    """No-raise client should return error responses instead of raising."""

    assert unauthenticated_client_no_raise._transport.raise_server_exceptions is False  # noqa: SLF001
