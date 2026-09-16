"""Focused offline coverage for the host-only DB load sampler."""

from __future__ import annotations

import gzip
import importlib.util
import json
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
        'python_gc_objects_collected_total{generation="0"} 9\n'
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

    args = type(
        "Args",
        (),
        {
            "data_dir": tmp_path,
            "runtime_container": "runtime",
            "containers": "runtime,neighbor",
            "mountpoint": "/data",
        },
    )()

    assert sampler.sample(args) == 0
    assert calls == ["runtime", "resources"]


def _catalog_row(
    observed_at: str,
    *,
    count: int,
    duration_ms: float,
    incarnation: dict[str, object] | None = None,
    p50: float = 1.0,
    p95: float = 5.0,
    p99: float = 9.0,
) -> dict:
    return {
        "status": "ok",
        "observed_at": observed_at,
        "incarnation": incarnation or {"container_id": "c1", "container_started_at": "boot", "process_id": 11},
        "catalogd": {
            "writer_admission": {
                "labels": {
                    "ingest": {
                        "n": count,
                        "total_exec_ms": duration_ms,
                        "queue_wait_ms": {"p50": p50, "p95": p95, "p99": p99},
                        "exec_ms": {"p50": p50, "p95": p95, "p99": p99},
                    }
                }
            }
        },
    }


def test_catalog_counters_reset_without_fabricating_deltas_and_quantiles_stay_separate() -> None:
    rows = [
        _catalog_row("2026-09-16T00:00:00Z", count=10, duration_ms=100, p50=1, p95=5, p99=9),
        _catalog_row("2026-09-16T00:01:00Z", count=20, duration_ms=160, p50=2, p95=6, p99=10),
        _catalog_row("2026-09-16T00:02:00Z", count=3, duration_ms=20, p50=100, p95=600, p99=900),
    ]

    summary = sampler.serializer_summary(rows)

    assert summary["write_count_delta_by_label"] == {"ingest": 10}
    assert summary["exec_total_ms_delta_by_label"] == {"ingest": 60}
    assert summary["data_quality"]["count_counter_resets"] == 1
    assert summary["data_quality"]["duration_counter_resets"] == 1
    assert summary["exec_ms"]["ingest"] == {"p50": 100, "p95": 600, "p99": 900}
    assert summary["exec_ms_series"]["ingest"]["p50"] == [1, 2, 100]
    assert summary["exec_ms_series"]["ingest"]["p95"] == [5, 6, 600]


def test_read_rows_deduplicates_rotation_boundary_and_applies_inclusive_interval(tmp_path: Path) -> None:
    first = _catalog_row("2026-09-16T00:00:00Z", count=10, duration_ms=10)
    second = _catalog_row("2026-09-16T00:01:00Z", count=11, duration_ms=11)
    middle = _catalog_row("2026-09-16T00:00:30Z", count=10, duration_ms=10)
    active = tmp_path / "runtime.ndjson"
    active.write_text(json.dumps(second) + "\n", encoding="utf-8")
    (tmp_path / "runtime.ndjson.1").write_text(json.dumps(middle) + "\n" + json.dumps(second) + "\n", encoding="utf-8")
    with gzip.open(tmp_path / "runtime.ndjson.2.gz", "wt", encoding="utf-8") as handle:
        handle.write(json.dumps(first) + "\n")
        handle.write(json.dumps(second) + "\n")

    rows = sampler.read_rows(active, start="2026-09-16T00:00:00Z", end="2026-09-16T00:01:00Z")

    assert [row["observed_at"] for row in rows] == [
        "2026-09-16T00:00:00Z",
        "2026-09-16T00:00:30Z",
        "2026-09-16T00:01:00Z",
    ]


def test_runtime_sample_persists_only_safe_health_projection(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        sampler,
        "docker_inspect",
        lambda _container: {"name": "runtime", "id": "c1", "image": "image", "pid": 11, "started_at": "boot"},
    )
    health = {
        "status": "healthy",
        "checks": {
            "catalogd": {
                "pid": 11,
                "commit_seq": "2",
                "writer_admission": {
                    "depth": 0,
                    "active_label": None,
                    "labels": {
                        "ingest": {
                            "n": 2,
                            "total_exec_ms": 4.0,
                            "exec_ms": {"p95": 3.0},
                        }
                    },
                },
            },
            "migration": {"log_content": "password=secret"},
            "environment": {"error": "DATABASE_URL=secret"},
        },
    }
    monkeypatch.setattr(
        sampler,
        "docker_curl",
        lambda _container, path, _header: "longhouse_sqlite_wal_bytes 3\nunsafe_secret_metric 9\n"
        if path == "/metrics"
        else json.dumps(health),
    )

    assert sampler.runtime_sample(type("Args", (), {"data_dir": tmp_path, "runtime_container": "runtime"})()) == 0
    row = json.loads((tmp_path / "runtime.ndjson").read_text(encoding="utf-8"))
    assert "health" not in row
    assert "migration" not in json.dumps(row)
    assert row["catalogd"]["writer_admission"]["labels"]["ingest"]["n"] == 2
    assert [metric["name"] for metric in row["metrics"]] == ["longhouse_sqlite_wal_bytes"]
