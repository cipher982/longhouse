"""Focused offline coverage for the host-only DB load sampler."""

from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/ops/db-load-observability.py"
SPEC = importlib.util.spec_from_file_location("db_load_observability", SCRIPT)
assert SPEC and SPEC.loader
sampler = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sampler)


def test_parse_prometheus_preserves_labels_and_skips_comments() -> None:
    samples = sampler.parse_prometheus(
        "# HELP ignored\n"
        'longhouse_write_serializer_exec_ms{label="ingest",quantile="p95"} 12.5\n'
        "python_gc_objects_collected_total{generation=\"0\"} 9\n"
    )

    assert samples == [
        {
            "name": "longhouse_write_serializer_exec_ms",
            "labels": {"label": "ingest", "quantile": "p95"},
            "value": 12.5,
        },
        {"name": "python_gc_objects_collected_total", "labels": {"generation": "0"}, "value": 9.0},
    ]


def test_serializer_summary_uses_counter_delta_not_lifetime_total() -> None:
    rows = [
        {
            "status": "ok",
            "health": {"checks": {"write_serializer": {"label_counts": {"ingest": 10, "heartbeat": 2}}}},
        },
        {
            "status": "ok",
            "health": {
                "checks": {
                    "write_serializer": {
                        "label_counts": {"ingest": 25, "heartbeat": 5},
                        "rolling_by_label": {"ingest": {"exec_ms": {"p95": 17}, "queue_wait_ms": {"p95": 3}}},
                    },
                    "sqlite_wal": {"wal_bytes": 4096},
                }
            },
        },
    ]

    summary = sampler.serializer_summary(rows)

    assert summary["write_count_delta_by_label"] == {"ingest": 15, "heartbeat": 3}
    assert summary["exec_ms"]["ingest"]["p95"] == 17
    assert summary["wal_bytes"] == {"min": 4096.0, "max": 4096.0}


def test_sample_keeps_resource_capture_running_after_runtime_error(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []

    monkeypatch.setattr(sampler, "runtime_sample", lambda _args: calls.append("runtime") or 1)
    monkeypatch.setattr(sampler, "resource_sample", lambda _args: calls.append("resources") or 0)

    args = type("Args", (), {
        "data_dir": tmp_path,
        "runtime_container": "runtime",
        "containers": "runtime,neighbor",
        "mountpoint": "/data",
    })()

    assert sampler.sample(args) == 0
    assert calls == ["runtime", "resources"]
