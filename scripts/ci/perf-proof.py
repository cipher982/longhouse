#!/usr/bin/env python3
"""Collect a small, trendable Longhouse performance proof artifact.

This is intentionally a proof lane, not a benchmark lab. It captures the
device-path metrics that map to product claims: native cold invocation latency,
shipper digest throughput/RSS, mixed live/archive latency, and optional hosted
provider-route duration when credentials are present.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import Sequence

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "artifacts" / "perf-proof" / "perf-proof.json"


@dataclass(frozen=True)
class CommandResult:
    cmd: list[str]
    returncode: int
    elapsed_ms: float
    stdout: str
    stderr: str


def percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * pct
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def summarize_samples(samples: Sequence[float]) -> dict[str, float | int]:
    if not samples:
        return {"iterations": 0, "mean_ms": 0.0, "p50_ms": 0.0, "p95_ms": 0.0, "min_ms": 0.0, "max_ms": 0.0}
    return {
        "iterations": len(samples),
        "mean_ms": statistics.mean(samples),
        "p50_ms": percentile(samples, 0.50),
        "p95_ms": percentile(samples, 0.95),
        "min_ms": min(samples),
        "max_ms": max(samples),
    }


def run_command(cmd: Sequence[str], *, cwd: Path = ROOT, env: dict[str, str] | None = None) -> CommandResult:
    started = time.perf_counter()
    proc = subprocess.run(
        list(cmd),
        cwd=str(cwd),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    return CommandResult(
        cmd=list(cmd),
        returncode=proc.returncode,
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def git_value(args: Sequence[str]) -> str | None:
    proc = subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def command_exists(path: str | Path) -> bool:
    raw = str(path)
    if "/" in raw:
        return Path(raw).exists()
    return shutil.which(raw) is not None


def time_command(label: str, cmd: Sequence[str], *, iterations: int) -> dict[str, Any]:
    if not command_exists(cmd[0]):
        return {"status": "skipped", "reason": f"command not found: {cmd[0]}", "cmd": list(cmd)}

    warmup = run_command(cmd)
    if warmup.returncode != 0:
        return {
            "status": "failed",
            "cmd": list(cmd),
            "returncode": warmup.returncode,
            "stderr_tail": warmup.stderr[-800:],
        }

    samples: list[float] = []
    for _ in range(iterations):
        result = run_command(cmd)
        if result.returncode != 0:
            return {
                "status": "failed",
                "cmd": list(cmd),
                "returncode": result.returncode,
                "samples_ms": samples,
                "stderr_tail": result.stderr[-800:],
            }
        samples.append(result.elapsed_ms)

    return {
        "status": "ok",
        "label": label,
        "cmd": list(cmd),
        **summarize_samples(samples),
    }


def parse_number(raw: str) -> float:
    return float(raw.replace(",", ""))


def parse_engine_bench_output(output: str) -> dict[str, Any]:
    metrics: dict[str, Any] = {"raw_excerpt": output[-3000:]}
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("Files:"):
            metrics["files"] = int(parse_number(stripped.split(":", 1)[1].strip()))
        elif stripped.startswith("Bytes:"):
            metrics["decoded_mb"] = parse_number(stripped.split(":", 1)[1].strip().split()[0])
        elif stripped.startswith("Decoded bytes:"):
            metrics["decoded_mb"] = parse_number(stripped.split(":", 1)[1].strip().split()[0])
        elif stripped.startswith("Compressed:"):
            metrics["compressed_mb"] = parse_number(stripped.split(":", 1)[1].strip().split()[0])
        elif stripped.startswith("Events:"):
            metrics["events"] = int(parse_number(stripped.split(":", 1)[1].strip()))
        elif stripped.startswith("Events shipped:"):
            metrics["events"] = int(parse_number(stripped.split(":", 1)[1].strip()))
        elif stripped.startswith("Total:"):
            value = stripped.split(":", 1)[1].strip()
            if value.endswith("s"):
                metrics["total_s"] = parse_number(value.rstrip("s"))
            elif "on disk" in value:
                parts = value.split()
                if len(parts) >= 2:
                    metrics["total_on_disk"] = {"value": parse_number(parts[0]), "unit": parts[1]}
        elif stripped.startswith("Throughput:"):
            parts = stripped.split(":", 1)[1].strip().split()
            if parts:
                metrics["throughput_mb_s"] = parse_number(parts[0])
        elif stripped.startswith("Events/s:"):
            metrics["events_s"] = parse_number(stripped.split(":", 1)[1].strip())
        elif stripped.startswith("Peak RSS:"):
            metrics["peak_rss_mb"] = parse_number(stripped.split(":", 1)[1].strip().split()[0])
        elif stripped.startswith("Ship latency:"):
            metrics["ship_latency"] = _parse_p50_p95(stripped)
        elif stripped.startswith("Server queue:"):
            metrics["server_queue_latency"] = _parse_p50_p95(stripped)
        elif stripped.startswith("Server exec:"):
            metrics["server_exec_latency"] = _parse_p50_p95(stripped)
        elif stripped.startswith("Live latency:") and "no successful" not in stripped:
            metrics["live_latency"] = _parse_p50_p95(stripped)
        elif stripped.startswith("Live SLA:"):
            metrics["live_sla"] = stripped.split(":", 1)[1].strip()
    return metrics


def _parse_p50_p95(line: str) -> dict[str, float]:
    # Example: Ship latency:   p50 7.8ms / p95 7.8ms
    tokens = line.replace("/", " ").replace("ms", "").split()
    p50 = tokens[tokens.index("p50") + 1] if "p50" in tokens else "0"
    p95 = tokens[tokens.index("p95") + 1] if "p95" in tokens else "0"
    return {"p50_ms": parse_number(p50), "p95_ms": parse_number(p95)}


def run_engine_bench(engine_bin: Path, *, mixed: bool) -> dict[str, Any]:
    if not engine_bin.exists():
        return {"status": "skipped", "reason": f"engine binary missing: {engine_bin}"}

    if not mixed:
        cmd = [
            str(engine_bin),
            "bench",
            "--synthetic-files",
            os.environ.get("PERF_PROOF_SYNTHETIC_FILES", "16"),
            "--synthetic-events-per-file",
            os.environ.get("PERF_PROOF_SYNTHETIC_EVENTS_PER_FILE", "500"),
            "--synthetic-bytes-per-event",
            os.environ.get("PERF_PROOF_SYNTHETIC_BYTES_PER_EVENT", "2048"),
            "--level",
            "L3",
            "--compress",
            "--parallel",
            "--workers",
            os.environ.get("PERF_PROOF_WORKERS", "4"),
        ]
        result = run_command(cmd, cwd=ROOT / "engine")
        return _bench_result_payload("shipper_parse_compress", result)

    with tempfile.TemporaryDirectory(prefix="lh-perf-proof-") as temp_dir:
        port_file = Path(temp_dir) / "port"
        server = subprocess.Popen(
            [sys.executable, str(ROOT / "scripts" / "qa" / "shipper_synthetic_echo.py"), "--port-file", str(port_file)],
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            for _ in range(50):
                if port_file.exists() and port_file.read_text(encoding="utf-8").strip():
                    break
                if server.poll() is not None:
                    stderr = server.stderr.read() if server.stderr else ""
                    return {"status": "failed", "reason": "synthetic echo server exited", "stderr_tail": stderr[-800:]}
                time.sleep(0.1)
            if not port_file.exists():
                return {"status": "failed", "reason": "synthetic echo server did not publish a port"}
            port = port_file.read_text(encoding="utf-8").strip()
            cmd = [
                str(engine_bin),
                "bench",
                "--synthetic-files",
                "6",
                "--synthetic-events-per-file",
                "50",
                "--synthetic-bytes-per-event",
                "1024",
                "--level",
                "L3",
                "--ship-url",
                f"http://127.0.0.1:{port}",
                "--ship-token",
                "synthetic",
                "--ship-concurrency",
                "4",
                "--mixed-live-count",
                "8",
                "--mixed-live-max-p95-ms",
                "10000",
            ]
            result = run_command(cmd, cwd=ROOT / "engine")
            return _bench_result_payload("mixed_live_archive", result)
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()


def _bench_result_payload(label: str, result: CommandResult) -> dict[str, Any]:
    output = result.stdout + result.stderr
    payload: dict[str, Any] = {
        "status": "ok" if result.returncode == 0 else "failed",
        "label": label,
        "cmd": result.cmd,
        "returncode": result.returncode,
        "wall_ms": result.elapsed_ms,
        "metrics": parse_engine_bench_output(output),
    }
    if result.returncode != 0:
        payload["stderr_tail"] = result.stderr[-1200:]
    return payload


def run_provider_live_route(output_root: Path) -> dict[str, Any]:
    token = os.environ.get("LONGHOUSE_DEVICE_TOKEN")
    proof_dir = Path(os.environ.get("PROVIDER_LIVE_PROOF_DIR", Path.home() / ".longhouse" / "provider-live-proof"))
    if not token:
        return {"status": "skipped", "reason": "LONGHOUSE_DEVICE_TOKEN is not set"}
    if not proof_dir.exists():
        return {"status": "skipped", "reason": f"provider-live proof dir missing: {proof_dir}"}
    artifact_path = output_root / "provider-live-route-e2e.json"
    with tempfile.TemporaryDirectory(prefix="lh-provider-token-") as temp_dir:
        token_file = Path(temp_dir) / "device-token"
        token_file.write_text(token.rstrip() + "\n", encoding="utf-8")
        token_file.chmod(0o600)
        result = run_command(
            [
                sys.executable,
                str(ROOT / "scripts" / "qa" / "provider-live-route-e2e.py"),
                "--provider",
                "auto",
                "--token-file",
                str(token_file),
                "--artifact",
                str(artifact_path),
                "--require-verdict",
                "non-red",
            ],
            cwd=ROOT,
        )
    return {
        "status": "ok" if result.returncode == 0 else "failed",
        "cmd": result.cmd,
        "returncode": result.returncode,
        "wall_ms": result.elapsed_ms,
        "artifact": str(artifact_path),
        "stderr_tail": result.stderr[-1200:] if result.returncode != 0 else "",
    }


def build_artifact(args: argparse.Namespace) -> dict[str, Any]:
    output_path = Path(args.output).resolve()
    output_root = output_path.parent
    engine_bin = Path(args.engine_bin).resolve()
    python_cli = (
        Path(args.python_cli).resolve() if args.python_cli else ROOT / "server" / ".venv" / "bin" / "longhouse"
    )

    startup = {
        "rust_engine_cold_help_invocation": time_command(
            "rust_engine_cold_help_invocation",
            [str(engine_bin), "--help"],
            iterations=args.startup_iterations,
        ),
        "python_cli_cold_help_invocation": time_command(
            "python_cli_cold_help_invocation",
            [str(python_cli), "--help"],
            iterations=args.startup_iterations,
        ),
    }
    startup["summary"] = startup_summary(startup)

    benchmarks = {
        "startup": startup,
        "shipper_parse_compress": run_engine_bench(engine_bin, mixed=False),
        "mixed_live_archive": run_engine_bench(engine_bin, mixed=True),
        "provider_live_route_e2e": run_provider_live_route(output_root),
    }

    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "git": {
            "sha": git_value(["rev-parse", "HEAD"]),
            "branch": git_value(["branch", "--show-current"]),
        },
        "environment": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": sys.version.split()[0],
            "runner": os.environ.get("RUNNER_NAME"),
            "github_run_id": os.environ.get("GITHUB_RUN_ID"),
        },
        "benchmarks": benchmarks,
    }


def startup_summary(startup: dict[str, Any]) -> dict[str, Any]:
    rust = startup.get("rust_engine_cold_help_invocation") or {}
    python = startup.get("python_cli_cold_help_invocation") or {}
    if rust.get("status") != "ok" or python.get("status") != "ok":
        return {"status": "partial", "reason": "one or both cold invocation probes were skipped or failed"}
    rust_p50 = float(rust["p50_ms"])
    python_p50 = float(python["p50_ms"])
    return {
        "status": "ok",
        "p50_help_invocation_speedup": python_p50 / rust_p50 if rust_p50 > 0 else None,
        "p50_help_invocation_latency_reduction_pct": (1.0 - rust_p50 / python_p50) * 100.0
        if python_p50 > 0
        else None,
    }


def write_summary_markdown(artifact: dict[str, Any], path: Path) -> None:
    lines = ["# Perf Proof", ""]
    sha = (artifact.get("git") or {}).get("sha") or "unknown"
    lines.append(f"- SHA: `{sha}`")
    lines.append(f"- Generated: `{artifact.get('generated_at')}`")
    lines.append("")
    lines.append("| Surface | Status | Key Signal |")
    lines.append("| --- | --- | --- |")
    benches = artifact.get("benchmarks") or {}
    startup = benches.get("startup") or {}
    startup_summary_payload = startup.get("summary") or {}
    lines.append(
        "| Cold help invocation | {status} | p50 speedup: {speedup} |".format(
            status=startup_summary_payload.get("status", "unknown"),
            speedup=_fmt_float(startup_summary_payload.get("p50_help_invocation_speedup"), suffix="x"),
        )
    )
    parse_metrics = ((benches.get("shipper_parse_compress") or {}).get("metrics") or {})
    lines.append(
        "| Shipper parse+compress | {status} | {throughput} MB/s, RSS {rss} MB |".format(
            status=(benches.get("shipper_parse_compress") or {}).get("status", "unknown"),
            throughput=_fmt_float(parse_metrics.get("throughput_mb_s")),
            rss=_fmt_float(parse_metrics.get("peak_rss_mb")),
        )
    )
    mixed_metrics = ((benches.get("mixed_live_archive") or {}).get("metrics") or {})
    live = mixed_metrics.get("live_latency") or {}
    lines.append(
        "| Mixed live/archive | {status} | live p95 {p95} ms, SLA {sla} |".format(
            status=(benches.get("mixed_live_archive") or {}).get("status", "unknown"),
            p95=_fmt_float(live.get("p95_ms")),
            sla=mixed_metrics.get("live_sla", "-"),
        )
    )
    provider = benches.get("provider_live_route_e2e") or {}
    lines.append(
        "| Provider route E2E | {status} | {signal} |".format(
            status=provider.get("status", "unknown"),
            signal=_provider_signal(provider),
        )
    )
    lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fmt_float(value: Any, *, suffix: str = "") -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.1f}{suffix}"
    except (TypeError, ValueError):
        return "-"


def _provider_signal(provider: dict[str, Any]) -> str:
    if provider.get("status") == "skipped":
        return provider.get("reason") or "skipped"
    return f"wall {_fmt_float(provider.get('wall_ms'))} ms"


def failed_surfaces(artifact: dict[str, Any]) -> list[str]:
    failed: list[str] = []
    benchmarks = artifact.get("benchmarks") or {}
    for name, payload in benchmarks.items():
        if not isinstance(payload, dict):
            continue
        if name == "startup":
            for probe_name, probe in payload.items():
                if probe_name == "summary" or not isinstance(probe, dict):
                    continue
                if probe.get("status") == "failed":
                    failed.append(f"startup.{probe_name}")
            continue
        if payload.get("status") == "failed":
            failed.append(name)
    return failed


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--summary", type=Path, default=None)
    parser.add_argument("--engine-bin", type=Path, default=ROOT / "engine" / "target" / "release" / "longhouse-engine")
    parser.add_argument("--python-cli", type=Path, default=None)
    parser.add_argument(
        "--startup-iterations",
        type=int,
        default=int(os.environ.get("PERF_PROOF_STARTUP_ITERATIONS", "20")),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.startup_iterations <= 0:
        raise SystemExit("--startup-iterations must be positive")
    artifact = build_artifact(args)
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary_path = Path(args.summary).resolve() if args.summary else output.with_suffix(".md")
    write_summary_markdown(artifact, summary_path)
    print(f"perf proof artifact: {output}")
    print(f"perf proof summary: {summary_path}")
    failed = failed_surfaces(artifact)
    if failed:
        print(f"perf proof failed surfaces: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
