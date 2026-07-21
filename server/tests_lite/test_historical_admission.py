from __future__ import annotations

from types import SimpleNamespace

import zerg.services.historical_admission as admission


def _disk_usage(*, free: int, total: int = 1000):
    return SimpleNamespace(total=total, used=total - free, free=free)


def test_disk_watermark_pauses_historical_work(monkeypatch, tmp_path):
    admission.reset_historical_admission_for_tests()
    monkeypatch.setenv("LONGHOUSE_HISTORICAL_MIN_FREE_BYTES", "200")
    monkeypatch.setenv("LONGHOUSE_HISTORICAL_MIN_FREE_RATIO", "0.10")
    monkeypatch.setattr(admission.shutil, "disk_usage", lambda _path: _disk_usage(free=100))

    decision = admission.evaluate_historical_admission(root=tmp_path, admitted_bytes=10, stored_bytes=0)

    assert decision.admitted is False
    assert decision.reason == "disk_watermark"
    assert decision.disk_free_bytes == 100


def test_historical_byte_budget_refuses_burst_without_affecting_disk(monkeypatch, tmp_path):
    admission.reset_historical_admission_for_tests()
    monkeypatch.setenv("LONGHOUSE_HISTORICAL_MIN_FREE_BYTES", "0")
    monkeypatch.setenv("LONGHOUSE_HISTORICAL_MIN_FREE_RATIO", "0")
    monkeypatch.setenv("LONGHOUSE_HISTORICAL_BYTES_PER_SECOND", "10")
    monkeypatch.setenv("LONGHOUSE_HISTORICAL_BURST_BYTES", "10")
    monkeypatch.setattr(admission.shutil, "disk_usage", lambda _path: _disk_usage(free=900))

    first = admission.evaluate_historical_admission(root=tmp_path, admitted_bytes=8, stored_bytes=0)
    second = admission.evaluate_historical_admission(root=tmp_path, admitted_bytes=8, stored_bytes=0)

    assert first.admitted is True
    assert second.admitted is False
    assert second.reason == "historical_byte_budget"
    assert second.retry_after_seconds >= 1


def test_historical_unit_larger_than_burst_is_explicitly_rejected(monkeypatch, tmp_path):
    admission.reset_historical_admission_for_tests()
    monkeypatch.setenv("LONGHOUSE_HISTORICAL_MIN_FREE_BYTES", "0")
    monkeypatch.setenv("LONGHOUSE_HISTORICAL_MIN_FREE_RATIO", "0")
    monkeypatch.setenv("LONGHOUSE_HISTORICAL_BYTES_PER_SECOND", "10")
    monkeypatch.setenv("LONGHOUSE_HISTORICAL_BURST_BYTES", "10")
    monkeypatch.setattr(admission.shutil, "disk_usage", lambda _path: _disk_usage(free=900))

    decision = admission.evaluate_historical_admission(root=tmp_path, admitted_bytes=11, stored_bytes=0)

    assert decision.admitted is False
    assert decision.reason == "historical_unit_exceeds_burst"


def test_stored_byte_ceiling_uses_accounted_bytes_not_tenant_estimates(monkeypatch, tmp_path):
    admission.reset_historical_admission_for_tests()
    monkeypatch.setenv("LONGHOUSE_HISTORICAL_MIN_FREE_BYTES", "0")
    monkeypatch.setenv("LONGHOUSE_HISTORICAL_MIN_FREE_RATIO", "0")
    monkeypatch.setenv("LONGHOUSE_TENANT_STORED_BYTES_CEILING", "100")
    monkeypatch.setattr(admission.shutil, "disk_usage", lambda _path: _disk_usage(free=900))

    below = admission.evaluate_historical_admission(root=tmp_path, admitted_bytes=10, stored_bytes=99)
    at_limit = admission.evaluate_historical_admission(root=tmp_path, admitted_bytes=10, stored_bytes=100)
    unknown = admission.evaluate_historical_admission(root=tmp_path, admitted_bytes=10, stored_bytes=None)

    assert below.admitted is True
    assert at_limit.reason == "stored_byte_ceiling"
    assert unknown.reason == "stored_usage_unavailable"


def test_legacy_archive_can_skip_storage_ceiling_but_keeps_disk_watermark(monkeypatch, tmp_path):
    admission.reset_historical_admission_for_tests()
    monkeypatch.setenv("LONGHOUSE_HISTORICAL_MIN_FREE_BYTES", "0")
    monkeypatch.setenv("LONGHOUSE_HISTORICAL_MIN_FREE_RATIO", "0")
    monkeypatch.setenv("LONGHOUSE_TENANT_STORED_BYTES_CEILING", "1")
    monkeypatch.setattr(admission.shutil, "disk_usage", lambda _path: _disk_usage(free=900))

    decision = admission.evaluate_historical_admission(
        root=tmp_path,
        admitted_bytes=10,
        stored_bytes=None,
        enforce_stored_ceiling=False,
    )

    assert decision.admitted is True
