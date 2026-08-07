"""Catalog mode must not reaper live control ops through the live WriteSerializer."""

from __future__ import annotations

import inspect

import zerg.lifespan as lifespan_module


def test_live_catalog_api_does_not_own_machine_control_reaper():
    # Regression lock for the every-60s hosted error:
    # "WriteSerializer session factory not configured" from an API-side
    # live-machine-control reaper that only started under catalog_mode, where
    # configure_live_write_serializer() is intentionally a no-op.
    source = inspect.getsource(lifespan_module.lifespan)
    assert "live-machine-control-reaper" not in source
    assert "_live_machine_control_operation_reaper_loop" not in dir(lifespan_module)
    assert not hasattr(lifespan_module, "_reap_stale_live_machine_control_operations_once")
