#!/usr/bin/env python3
"""Tests for the perf proof artifact collector."""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "ci"))

SCRIPT = REPO_ROOT / "scripts/ci/perf-proof.py"


def _module():
    spec = importlib.util.spec_from_file_location("perf_proof", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load perf-proof.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

def _soak_module():
    script = REPO_ROOT / "scripts" / "ci" / "perf-soak.py"
    spec = importlib.util.spec_from_file_location("perf_soak", script)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load perf-soak.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module




def test_parse_engine_parse_bench_output() -> None:
    perf = _module()
    output = """
=== Benchmark Results ===
Mode:       parallel (4 workers)
Files:      32
Bytes:      68.15 MB
Events:     32000
Total:      0.145s
Throughput: 471.2 MB/s
Events/s:   221268
Peak RSS:   64.7 MB
Total:      0.02 GB on disk
"""
    metrics = perf.parse_engine_bench_output(output)
    assert metrics["files"] == 32
    assert metrics["decoded_mb"] == 68.15
    assert metrics["events"] == 32000
    assert metrics["throughput_mb_s"] == 471.2
    assert metrics["events_s"] == 221268
    assert metrics["peak_rss_mb"] == 64.7
    assert metrics["total_on_disk"] == {"value": 0.02, "unit": "GB"}


def test_parse_mixed_live_archive_output() -> None:
    perf = _module()
    output = """
=== Bench Mode B (network) ===
Files:          6
Concurrency:    4
Decoded bytes:  0.35 MB
Wire bytes:     0.86 MB
Events shipped: 300
Total:          0.713s
Throughput:     0.5 MB/s decoded, 0.0 MB/s on wire
Events/s:       421
Ship latency:   p50 7.8ms / p95 7.8ms
Server queue:   p50 2.0ms / p95 2.0ms
Server exec:    p50 5.0ms / p95 5.0ms

=== Mixed Live Probes ===
Live probes:    8
Live latency:   p50 3.0ms / p95 3.2ms
Live SLA:       PASS
"""
    metrics = perf.parse_engine_bench_output(output)
    assert metrics["decoded_mb"] == 0.35
    assert metrics["wire_mb"] == 0.86
    assert metrics["events"] == 300
    assert metrics["ship_latency"] == {"p50_ms": 7.8, "p95_ms": 7.8}
    assert metrics["server_queue_latency"] == {"p50_ms": 2.0, "p95_ms": 2.0}
    assert metrics["server_exec_latency"] == {"p50_ms": 5.0, "p95_ms": 5.0}
    assert metrics["live_latency"] == {"p50_ms": 3.0, "p95_ms": 3.2}
    assert metrics["live_sla"] == "PASS"


def test_parse_mixed_output_omits_unavailable_server_timing() -> None:
    perf = _module()
    metrics = perf.parse_engine_bench_output(
        "Server queue:   (no X-Ingest-* headers seen)\n"
    )
    assert "server_queue_latency" not in metrics


def test_startup_summary_reports_speedup() -> None:
    perf = _module()
    startup = {
        "rust_engine_cold_help_invocation": {"status": "ok", "p50_ms": 6.0},
        "python_cli_cold_help_invocation": {"status": "ok", "p50_ms": 300.0},
    }
    summary = perf.startup_summary(startup)
    assert summary["status"] == "ok"
    assert summary["p50_help_invocation_speedup"] == 50.0
    assert round(summary["p50_help_invocation_latency_reduction_pct"], 1) == 98.0


def test_percentile_handles_empty_single_and_interpolated_samples() -> None:
    perf = _module()
    assert perf.percentile([], 0.95) == 0.0
    assert perf.percentile([7.0], 0.95) == 7.0
    assert perf.percentile([10.0, 20.0, 30.0], 0.50) == 20.0
    assert perf.percentile([10.0, 20.0, 30.0], 0.95) == 29.0


def test_startup_summary_is_partial_when_probe_skipped() -> None:
    perf = _module()
    startup = {
        "rust_engine_cold_help_invocation": {"status": "ok", "p50_ms": 6.0},
        "python_cli_cold_help_invocation": {"status": "skipped", "reason": "missing"},
    }
    summary = perf.startup_summary(startup)
    assert summary["status"] == "partial"


def test_summary_markdown_contains_core_surfaces() -> None:
    perf = _module()
    artifact = {
        "generated_at": "2026-06-29T00:00:00Z",
        "git": {"sha": "abc123"},
        "benchmarks": {
            "startup": {"summary": {"status": "ok", "p50_help_invocation_speedup": 47.1}},
            "shipper_parse_compress": {
                "status": "ok",
                "metrics": {"throughput_mb_s": 471.2, "peak_rss_mb": 64.7},
            },
            "mixed_live_archive": {
                "status": "ok",
                "metrics": {"live_latency": {"p95_ms": 3.2}, "live_sla": "PASS"},
            },
        },
    }
    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "summary.md"
        perf.write_summary_markdown(artifact, path)
        text = path.read_text(encoding="utf-8")
    assert "Cold help invocation" in text
    assert "Shipper parse+compress" in text
    assert "Mixed live/archive" in text


def test_shipper_signal_treats_zero_rss_as_unavailable() -> None:
    perf = _module()
    assert perf._shipper_signal({"throughput_mb_s": 480.5, "peak_rss_mb": 0.0}) == "480.5 MB/s, RSS unavailable"
    assert perf._shipper_signal({"throughput_mb_s": 480.5, "peak_rss_mb": 32.6}) == "480.5 MB/s, RSS 32.6 MB"


def test_failed_surfaces_ignores_skips_but_reports_failures() -> None:
    perf = _module()
    artifact = {
        "benchmarks": {
            "startup": {
                "summary": {"status": "partial"},
                "rust_engine_cold_help_invocation": {"status": "failed"},
                "python_cli_cold_help_invocation": {"status": "skipped"},
            },
            "shipper_parse_compress": {"status": "ok"},
            "mixed_live_archive": {"status": "failed"},
        }
    }
    assert perf.failed_surfaces(artifact) == [
        "startup.rust_engine_cold_help_invocation",
        "mixed_live_archive",
    ]

def test_soak_budget_and_baseline_regression_rules() -> None:
    soak = _soak_module()
    previous = {
        "scenarios": {
            "active": {
                "engine": {
                    "cpu_percent": 20.0,
                    "disk_write_bytes_per_second": 300_000,
                    "processes_spawned_per_minute": 3.0,
                },
                "desktop_app": {"cpu_percent": 2.0},
            }
        }
    }
    scenarios = {
        "idle": {
            "engine": {
                "cpu_percent": 1.0,
                "disk_write_bytes_per_second": 50_000,
                "processes_spawned_per_minute": 0.0,
            },
            "desktop_app": {"cpu_percent": 1.0},
        },
        "active": {
            "engine": {
                "cpu_percent": 31.0,
                "disk_write_bytes_per_second": 600_000,
                "processes_spawned_per_minute": 8.0,
            },
            "desktop_app": {"cpu_percent": 4.0},
        },
    }
    failures = soak.evaluate_budgets_and_regressions(scenarios, "macOS", previous)
    failed_metrics = {failure["metric"] for failure in failures}
    assert failed_metrics == {
        "engine.cpu_percent",
        "engine.disk_write_bytes_per_second",
        "engine.processes_spawned_per_minute",
        "desktop_app.cpu_percent",
    }
    assert scenarios["idle"]["budgets"]["engine.processes_spawned_per_minute"]["status"] == "pass"
    assert scenarios["active"]["budgets"]["engine.cpu_percent"]["regressed_over_50_percent"]
    fallback = {
        "idle": {
            "engine": {
                "cpu_percent": 1.0,
                "disk_write_bytes_per_second": 50_000,
                "processes_spawned_per_minute": 0.0,
            },
            "desktop_core_renderer": {"cpu_percent": 0.4},
        }
    }
    assert soak.evaluate_budgets_and_regressions(
        fallback, "macOS", None, desktop_app_measured=False
    ) == []
    assert "desktop_app.cpu_percent" not in fallback["idle"]["budgets"]


def test_soak_darwin_rusage_info_v4_layout() -> None:
    soak = _soak_module()
    if soak.platform.system() == "Darwin":
        import ctypes

        _, usage_type = soak._mac_proc_api()
        assert ctypes.sizeof(usage_type) == 296
        before = soak._mac_proc_sample(os.getpid())
        clock_before = time.process_time()
        deadline = clock_before + 0.3
        while time.process_time() < deadline:
            pass
        after = soak._mac_proc_sample(os.getpid())
        # CPU must be in nanoseconds: raw mach ticks read 41.7x low on Apple
        # Silicon and made a busy engine look idle.
        sampled = (after["cpu_ns"] - before["cpu_ns"]) / 1e9
        clocked = time.process_time() - clock_before
        assert after["rss_bytes"]
        assert 0.7 * clocked <= sampled <= 1.3 * clocked, (sampled, clocked)




def test_soak_linux_wakeups_are_unavailable_not_zero() -> None:
    soak = _soak_module()
    if soak.platform.system() == "Linux":
        assert soak._linux_proc_sample(os.getpid())["wakeups"] is None


def test_cinder_fixture_generator_writes_reader_shaped_state() -> None:
    import perf_soak_fixtures as fixtures

    shape = {name: {**definition, "count": 1} for name, definition in fixtures.DEFAULT_SHAPE.items()}
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        longhouse_home = home / ".longhouse"
        workspace = root / "workspace"
        db_path = longhouse_home / "agent" / "longhouse-shipper.db"
        db_path.parent.mkdir(parents=True)
        with sqlite3.connect(db_path) as db:
            db.executescript(
                """
                CREATE TABLE source_epoch_registry (
                    source_epoch TEXT PRIMARY KEY, provider TEXT NOT NULL,
                    opaque_source_id TEXT NOT NULL, file_incarnation TEXT NOT NULL,
                    predecessor_epoch TEXT, start_reason TEXT NOT NULL,
                    max_observed_len INTEGER NOT NULL, source_revision TEXT,
                    bound_session_id TEXT, provider_session_id TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    ended_at TEXT, end_reason TEXT
                );
                CREATE TABLE file_state (
                    path TEXT PRIMARY KEY, provider TEXT NOT NULL,
                    queued_offset INTEGER NOT NULL, acked_offset INTEGER NOT NULL,
                    file_identity TEXT, acked_cursor_fingerprint TEXT,
                    session_id TEXT, provider_session_id TEXT, last_updated TEXT NOT NULL
                );
                CREATE TABLE session_binding (
                    path TEXT PRIMARY KEY, session_id TEXT NOT NULL, provider TEXT NOT NULL,
                    updated_at TEXT NOT NULL, provider_session_id TEXT, state TEXT NOT NULL,
                    bound_at TEXT, last_seen_at TEXT
                );
                CREATE TABLE session_phase_state (
                    session_id TEXT PRIMARY KEY, provider TEXT NOT NULL, phase TEXT NOT NULL,
                    tool_name TEXT, source TEXT NOT NULL, observed_at TEXT NOT NULL,
                    run_id TEXT, revision INTEGER NOT NULL
                );
                """
            )
        fixture = fixtures.create_fixtures(home, longhouse_home, workspace, db_path, shape)
        assert all(item["count"] == 1 for item in fixture["files"].values())
        assert fixture["database_rows"] == {
            "source_epoch_registry": 1,
            "file_state": 1,
            "session_binding": 1,
            "session_phase_state": 1,
        }
        transcript = Path(fixture["active_shadow_transcript"])
        event = json.loads(transcript.read_text(encoding="utf-8").splitlines()[0])
        assert event["type"] == "user"
        assert event["cwd"] == fixture["workspace"]



def main() -> int:
    tests = [
        test_parse_engine_parse_bench_output,
        test_parse_mixed_live_archive_output,
        test_parse_mixed_output_omits_unavailable_server_timing,
        test_startup_summary_reports_speedup,
        test_percentile_handles_empty_single_and_interpolated_samples,
        test_startup_summary_is_partial_when_probe_skipped,
        test_summary_markdown_contains_core_surfaces,
        test_shipper_signal_treats_zero_rss_as_unavailable,
        test_failed_surfaces_ignores_skips_but_reports_failures,
        test_soak_budget_and_baseline_regression_rules,
        test_soak_linux_wakeups_are_unavailable_not_zero,
        test_soak_darwin_rusage_info_v4_layout,
        test_cinder_fixture_generator_writes_reader_shaped_state,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
