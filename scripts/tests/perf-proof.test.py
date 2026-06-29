#!/usr/bin/env python3
"""Tests for the perf proof artifact collector."""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts/ci/perf-proof.py"


def _module():
    spec = importlib.util.spec_from_file_location("perf_proof", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load perf-proof.py")
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
Compressed:     0.01 MB
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
    assert metrics["compressed_mb"] == 0.01
    assert metrics["events"] == 300
    assert metrics["ship_latency"] == {"p50_ms": 7.8, "p95_ms": 7.8}
    assert metrics["server_queue_latency"] == {"p50_ms": 2.0, "p95_ms": 2.0}
    assert metrics["server_exec_latency"] == {"p50_ms": 5.0, "p95_ms": 5.0}
    assert metrics["live_latency"] == {"p50_ms": 3.0, "p95_ms": 3.2}
    assert metrics["live_sla"] == "PASS"


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
            "provider_live_route_e2e": {"status": "skipped", "reason": "LONGHOUSE_DEVICE_TOKEN is not set"},
        },
    }
    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "summary.md"
        perf.write_summary_markdown(artifact, path)
        text = path.read_text(encoding="utf-8")
    assert "Cold help invocation" in text
    assert "Shipper parse+compress" in text
    assert "Mixed live/archive" in text
    assert "Provider route E2E" in text
    assert "LONGHOUSE_DEVICE_TOKEN is not set" in text


def test_provider_live_route_passes_artifact_and_temp_token_file() -> None:
    perf = _module()
    captured: dict[str, object] = {}

    def fake_run_command(cmd, *, cwd, env=None):  # noqa: ANN001, ANN202
        captured["cmd"] = list(cmd)
        token_file = Path(cmd[cmd.index("--token-file") + 1])
        captured["token_file"] = token_file
        assert token_file.read_text(encoding="utf-8") == "secret-token\n"
        artifact = Path(cmd[cmd.index("--artifact") + 1])
        artifact.write_text("{}\n", encoding="utf-8")
        return perf.CommandResult(cmd=list(cmd), returncode=0, elapsed_ms=12.3, stdout="ok", stderr="")

    previous_token = os.environ.get("LONGHOUSE_DEVICE_TOKEN")
    previous_proof_dir = os.environ.get("PROVIDER_LIVE_PROOF_DIR")
    previous_run_command = perf.run_command
    with tempfile.TemporaryDirectory() as temp_dir:
        proof_dir = Path(temp_dir) / "provider-live-proof"
        proof_dir.mkdir()
        output_root = Path(temp_dir) / "out"
        output_root.mkdir()
        try:
            os.environ["LONGHOUSE_DEVICE_TOKEN"] = "secret-token"
            os.environ["PROVIDER_LIVE_PROOF_DIR"] = str(proof_dir)
            perf.run_command = fake_run_command
            payload = perf.run_provider_live_route(output_root)
        finally:
            perf.run_command = previous_run_command
            if previous_token is None:
                os.environ.pop("LONGHOUSE_DEVICE_TOKEN", None)
            else:
                os.environ["LONGHOUSE_DEVICE_TOKEN"] = previous_token
            if previous_proof_dir is None:
                os.environ.pop("PROVIDER_LIVE_PROOF_DIR", None)
            else:
                os.environ["PROVIDER_LIVE_PROOF_DIR"] = previous_proof_dir

    cmd = captured["cmd"]
    assert payload["status"] == "ok"
    assert "--artifact" in cmd
    assert "--require-verdict" in cmd
    assert cmd[cmd.index("--require-verdict") + 1] == "non-red"
    assert Path(payload["artifact"]).name == "provider-live-route-e2e.json"
    assert not Path(captured["token_file"]).exists()


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
            "provider_live_route_e2e": {"status": "skipped"},
            "mixed_live_archive": {"status": "failed"},
        }
    }
    assert perf.failed_surfaces(artifact) == [
        "startup.rust_engine_cold_help_invocation",
        "mixed_live_archive",
    ]


def main() -> int:
    tests = [
        test_parse_engine_parse_bench_output,
        test_parse_mixed_live_archive_output,
        test_startup_summary_reports_speedup,
        test_percentile_handles_empty_single_and_interpolated_samples,
        test_startup_summary_is_partial_when_probe_skipped,
        test_summary_markdown_contains_core_surfaces,
        test_provider_live_route_passes_artifact_and_temp_token_file,
        test_failed_surfaces_ignores_skips_but_reports_failures,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
