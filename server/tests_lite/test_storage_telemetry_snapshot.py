from __future__ import annotations

import pytest

import zerg.services.storage_telemetry_snapshot as snapshot_module
from zerg import metrics
from zerg.services.godview_metrics import refresh_storage_telemetry_gauges


class _Catalog:
    async def call(self, method, params, **_kwargs):
        assert method == "storage.telemetry.summary.v2"
        assert params == {}
        return {
            "objects": {
                "raw": {"count": 2, "bytes": 20},
                "render": {"count": 3, "bytes": 30},
                "media": {"count": 1, "bytes": 50},
            },
            "projectors": [
                {
                    "projector": "search-v2",
                    "lagging": 4,
                    "failed": 1,
                    "claimed": 2,
                    "oldest_lag_updated_at": "2026-07-20T12:00:00+00:00",
                }
            ],
            "commit_seq": "9",
            "observed_at": "2026-07-20T12:01:00+00:00",
        }


def _gauge_value(gauge, **labels) -> float | None:
    for family in gauge.collect():
        for sample in family.samples:
            if all(sample.labels.get(key) == value for key, value in labels.items()):
                return sample.value
    return None


@pytest.mark.asyncio
async def test_catalog_snapshot_projects_bounded_object_and_projector_gauges(monkeypatch):
    snapshot_module.reset_storage_telemetry_snapshot_for_tests()
    monkeypatch.setattr(snapshot_module, "get_catalogd_client", lambda: _Catalog())

    snapshot = await snapshot_module.refresh_storage_telemetry_snapshot(force=True)
    refresh_storage_telemetry_gauges()

    assert snapshot.total_stored_bytes == 100
    assert _gauge_value(metrics.storage_total_stored_bytes) == 100
    assert _gauge_value(metrics.storage_object_count, kind="raw") == 2
    assert _gauge_value(metrics.projector_lag_sessions, projector="search-v2") == 4
    assert _gauge_value(metrics.projector_failed_sessions, projector="search-v2") == 1
    assert _gauge_value(metrics.telemetry_health, component="catalog_state") == 1
    assert _gauge_value(metrics.telemetry_health, component="projector_state") == 1
    assert _gauge_value(metrics.telemetry_health, component="recall_state") == 0


@pytest.mark.asyncio
async def test_catalog_snapshot_failure_is_explicit_not_green(monkeypatch):
    class BrokenCatalog:
        async def call(self, *_args, **_kwargs):
            raise ValueError("bad summary")

    snapshot_module.reset_storage_telemetry_snapshot_for_tests()
    monkeypatch.setattr(snapshot_module, "get_catalogd_client", lambda: BrokenCatalog())

    snapshot = await snapshot_module.refresh_storage_telemetry_snapshot(force=True)
    refresh_storage_telemetry_gauges()

    assert snapshot.fresh is False
    assert snapshot.last_error == "ValueError: bad summary"
    assert _gauge_value(metrics.telemetry_health, component="catalog_state") == 0
