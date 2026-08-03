#!/usr/bin/env python3
"""Run the same terminal-output-to-web-pixel profile for every launch provider."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
PROFILER = ROOT / "scripts" / "ops" / "profile-managed-session-propagation.py"
DEFAULT_OUTPUT_ROOT = ROOT / "artifacts" / "provider-to-pixel"
PROVIDER_CASES = {
    "codex": "managed_codex_warm_live_graceful_close",
    "claude": "managed_claude_warm_live_graceful_close",
    "cursor": "managed_cursor_helm_content_promotion",
    "opencode": "managed_opencode_warm_lifecycle",
}
SUMMARY_METRICS = (
    "waterfall_total_provider_to_first_render_ms",
    "waterfall_provider_to_engine_observed_ms",
    "waterfall_engine_observed_to_enqueued_ms",
    "waterfall_engine_enqueued_to_job_started_ms",
    "waterfall_engine_job_started_to_http_send_ms",
    "waterfall_http_send_to_server_handler_ms",
    "waterfall_server_handler_to_store_returned_ms",
    "waterfall_server_store_to_fanout_ms",
    "waterfall_server_fanout_to_client_received_ms",
    "waterfall_client_received_to_rendered_ms",
    "warm_live_output_local_to_paint_ms",
    "durable_archive_local_to_hosted_ms",
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def command_json(argv: list[str]) -> dict[str, Any] | None:
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def provider_identity(provider: str) -> dict[str, Any]:
    executable = shutil.which(provider)
    if not executable:
        return {"available": False}
    path = Path(executable).resolve()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    completed = subprocess.run(
        [str(path), "--version"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    return {
        "available": True,
        "path": str(path),
        "sha256": digest,
        "version": (completed.stdout + completed.stderr).strip()[:500],
    }


def hosted_identity(subdomain: str) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(
            f"https://{subdomain}.longhouse.ai/api/health", timeout=15
        ) as response:
            value = json.load(response)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def format_ms(value: Any) -> str:
    return "-" if value is None else f"{value}ms"


def write_summary(path: Path, payload: dict[str, Any]) -> None:
    rows = [
        "# Provider-to-Pixel Matrix",
        "",
        f"- Run: `{payload['run_id']}`",
        f"- Generated: `{payload['generated_at']}`",
        f"- Requested iterations per provider: `{payload['iterations']}`",
        "",
        "| Provider | Clean | Verdict | Local output → web p50 | p95 | Waterfall bottleneck p50 |",
        "| --- | ---: | --- | ---: | ---: | --- |",
    ]
    for provider in payload["providers"]:
        aggregate = provider.get("aggregate") or {}
        clean = aggregate.get("clean_metrics") or {}
        total = clean.get("waterfall_total_provider_to_first_render_ms") or {}
        bottleneck = None
        for key in SUMMARY_METRICS[1:10]:
            metric = clean.get(key) or {}
            value = metric.get("p50")
            if isinstance(value, int) and (bottleneck is None or value > bottleneck[1]):
                bottleneck = (key.removeprefix("waterfall_").removesuffix("_ms"), value)
        rows.append(
            "| "
            + " | ".join(
                [
                    provider["provider"],
                    str(aggregate.get("clean_observation_count") or 0),
                    str(aggregate.get("batch_verdict") or "missing"),
                    format_ms(total.get("p50")),
                    format_ms(total.get("p95")),
                    f"{bottleneck[0]} {bottleneck[1]}ms" if bottleneck else "-",
                ]
            )
            + " |"
        )
    path.write_text("\n".join(rows) + "\n")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--providers",
        default=",".join(PROVIDER_CASES),
        help="Comma-separated provider list (default: all launch providers).",
    )
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--run-id")
    parser.add_argument("--output-dir")
    parser.add_argument("--subdomain", default="david010")
    parser.add_argument("--container")
    parser.add_argument("--ssh-target", default="zerg")
    parser.add_argument("--project", default="longhouse")
    parser.add_argument("--browser-ui-base-url")
    parser.add_argument(
        "--browser-transport",
        choices=["default", "disable-quic"],
        default="disable-quic",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    providers = [value.strip() for value in args.providers.split(",") if value.strip()]
    unknown = sorted(set(providers) - set(PROVIDER_CASES))
    if unknown:
        raise SystemExit(f"unknown providers: {', '.join(unknown)}")
    if args.iterations < 1:
        raise SystemExit("--iterations must be >= 1")

    run_id = args.run_id or f"provider-to-pixel-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    output_dir = Path(args.output_dir or DEFAULT_OUTPUT_ROOT / run_id).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "schema": "longhouse.provider_to_pixel_matrix.v1",
        "run_id": run_id,
        "generated_at": utc_now(),
        "iterations": args.iterations,
        "local_longhouse": command_json(["longhouse", "build-identity", "--json"]),
        "hosted": hosted_identity(args.subdomain),
        "provider_identities": {
            provider: provider_identity(provider) for provider in providers
        },
        "providers": [],
    }
    exit_code = 0
    for provider in providers:
        provider_run_id = f"{run_id}-{provider}"
        provider_dir = output_dir / provider
        command = [
            sys.executable,
            str(PROFILER),
            "--profile",
            "warm-live",
            "--provider",
            provider,
            "--ownership",
            "managed",
            "--sla-case",
            PROVIDER_CASES[provider],
            "--iterations",
            str(args.iterations),
            "--run-id",
            provider_run_id,
            "--output-dir",
            str(provider_dir),
            "--subdomain",
            args.subdomain,
            "--ssh-target",
            args.ssh_target,
            "--project",
            args.project,
            "--browser-ui-base-url",
            args.browser_ui_base_url or f"https://{args.subdomain}.longhouse.ai",
            "--browser-transport",
            args.browser_transport,
        ]
        if args.container:
            command.extend(["--container", args.container])
        if provider == "codex":
            command.extend(["--trust-longhouse-codex-hooks", "--codex-effort", "low"])
        completed = subprocess.run(command, cwd=ROOT, check=False)
        batch = read_json(provider_dir / "batch-metrics.json")
        payload["providers"].append(
            {
                "provider": provider,
                "sla_case_id": PROVIDER_CASES[provider],
                "exit_code": completed.returncode,
                "artifact_dir": str(provider_dir),
                "aggregate": batch.get("aggregate") or {},
                "runs": batch.get("runs") or [],
            }
        )
        if completed.returncode == 1:
            exit_code = 1
        elif completed.returncode != 0 and exit_code == 0:
            exit_code = 2

    payload["generated_at"] = utc_now()
    (output_dir / "matrix.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True)
    )
    write_summary(output_dir / "summary.md", payload)
    print(output_dir / "summary.md")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
