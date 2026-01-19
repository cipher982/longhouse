"""Ensure legacy email trigger poller is removed."""

from __future__ import annotations

import importlib

import pytest


def test_email_trigger_service_module_removed():
    """Legacy poller module should no longer exist."""

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("zerg.services.email_trigger_service")
