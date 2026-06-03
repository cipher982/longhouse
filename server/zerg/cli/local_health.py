"""CLI surface for local Longhouse engine health and menu bar tools."""

from __future__ import annotations

import json
import subprocess
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

import typer

from zerg.cli.config_file import load_config
from zerg.services.desktop_app import build_snapshot_arguments
from zerg.services.local_health import collect_local_health
from zerg.services.longhouse_paths import resolve_longhouse_home_from_provider_home
from zerg.services.machine_repair import can_repair_machine_from_state
from zerg.services.machine_repair import recommended_machine_repair_command
from zerg.services.machine_state import normalize_runtime_url
from zerg.services.runtime_artifacts import RuntimeComponent
from zerg.services.runtime_artifacts import desktop_app_canonical_bundle_path
from zerg.services.runtime_artifacts import resolve_installed_runtime_artifact
from zerg.services.shipper import get_zerg_url

app = typer.Typer(
    name="local-health",
    help="Inspect local Longhouse shipping health and launch the Longhouse desktop app.",
    invoke_without_command=True,
    no_args_is_help=False,
)


def _format_age(age_seconds: int | None) -> str:
    if age_seconds is None:
        return "-"
    if age_seconds < 60:
        return f"{age_seconds}s"
    if age_seconds < 3600:
        return f"{age_seconds // 60}m"
    return f"{age_seconds // 3600}h"


def _format_bytes(value: object) -> str:
    size = int(value or 0)
    units = ("B", "KB", "MB", "GB", "TB")
    scaled = float(size)
    for unit in units:
        if scaled < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{size} B"
            return f"{scaled:.1f} {unit}"
        scaled /= 1024
    return f"{size} B"


def _format_rate(value: object, suffix: str) -> str:
    rate = _as_float(value)
    if rate is None:
        return "-"
    if rate >= 100:
        return f"{rate:.0f} {suffix}"
    if rate >= 10:
        return f"{rate:.1f} {suffix}"
    return f"{rate:.2f} {suffix}"


_ARCHIVE_SIZE_BUCKET_LABELS = {
    "tiny_lt_1kb": "tiny",
    "small_lt_1mb": "small",
    "medium_lt_10mb": "medium",
    "large_lt_100mb": "large",
    "huge_gte_100mb": "huge",
}


def _as_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _has_lane_activity(lane: dict[str, object]) -> bool:
    keys = (
        "attempts_1h",
        "successes_1h",
        "server_errors_1h",
        "connect_errors_1h",
        "backpressure_1h",
        "events_1h",
        "bytes_1h",
    )
    return any(int(lane.get(key) or 0) > 0 for key in keys) or bool(
        lane.get("events_per_sec_ewma_10s") or lane.get("bytes_per_sec_ewma_10s")
    )


def _archive_provider_mix(providers: object, *, limit: int = 3) -> str:
    if not isinstance(providers, list):
        return "-"
    rendered: list[str] = []
    for raw in providers[:limit]:
        provider = dict(raw or {})
        name = str(provider.get("provider") or "-")
        ranges = int(provider.get("pending_ranges") or 0)
        size = _format_bytes(provider.get("pending_bytes"))
        rendered.append(f"{name} {ranges} ranges/{size}")
    if len(providers) > limit:
        rendered.append(f"+{len(providers) - limit} more")
    return ", ".join(rendered) or "-"


def _archive_size_mix(size_buckets: object) -> str:
    if not isinstance(size_buckets, dict):
        return "-"
    rendered: list[str] = []
    for bucket, raw_summary in sorted(size_buckets.items()):
        summary = dict(raw_summary or {})
        ranges = int(summary.get("pending_ranges") or 0)
        if ranges <= 0:
            continue
        label = _ARCHIVE_SIZE_BUCKET_LABELS.get(str(bucket), str(bucket))
        rendered.append(f"{label} {ranges}/{_format_bytes(summary.get('pending_bytes'))}")
    return ", ".join(rendered) or "-"


def _render_snapshot(snapshot: dict[str, object], *, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps(snapshot, indent=2))
        return

    severity = str(snapshot["severity"])
    color = {
        "green": typer.colors.GREEN,
        "yellow": typer.colors.YELLOW,
        "red": typer.colors.RED,
        "gray": typer.colors.WHITE,
    }.get(severity, typer.colors.WHITE)

    typer.secho(
        f"{snapshot['headline']} ({snapshot['health_state']}, {severity})",
        fg=color,
        bold=True,
    )

    service = dict(snapshot["service"])
    engine_status = dict(snapshot["engine_status"])
    payload = dict(engine_status.get("payload") or {})
    outbox = dict(snapshot["outbox"])
    launch_readiness = dict(snapshot.get("launch_readiness") or {})
    runner = dict(launch_readiness.get("runner") or {})

    typer.echo("")
    typer.echo("Service")
    typer.echo(f"  status: {service.get('status', '-')}")
    typer.echo(f"  platform: {service.get('platform', '-')}")
    if service.get("service_name"):
        typer.echo(f"  name: {service['service_name']}")
    if service.get("service_file"):
        typer.echo(f"  file: {service['service_file']}")
    if service.get("log_path"):
        typer.echo(f"  logs: {service['log_path']}")

    typer.echo("")
    typer.echo("Engine")
    typer.echo(f"  status file: {engine_status.get('path', '-')}")
    typer.echo(f"  exists: {'yes' if engine_status.get('exists') else 'no'}")
    typer.echo(f"  age: {_format_age(engine_status.get('age_seconds'))}")
    typer.echo(f"  last ship: {payload.get('last_ship_at') or '-'}")
    typer.echo(f"  spool pending: {payload.get('spool_pending_count', 0)}")
    typer.echo(f"  spool dead: {payload.get('spool_dead_count', 0)}")
    typer.echo(f"  ship failures: {payload.get('consecutive_ship_failures', 0)}")
    typer.echo(f"  offline: {'yes' if payload.get('is_offline') else 'no'}")

    ship_lanes = dict(payload.get("ship_lanes") or {})
    active_lanes = [(name, dict(raw or {})) for name, raw in ship_lanes.items() if _has_lane_activity(dict(raw or {}))]
    if active_lanes:
        typer.echo("")
        typer.echo("Ship Lanes")
        for lane_name, lane in sorted(active_lanes):
            bps = _format_bytes(lane.get("bytes_per_sec_ewma_10s"))
            eps = _format_rate(lane.get("events_per_sec_ewma_10s"), "events/s")
            p95 = lane.get("latency_p95_ms_1h")
            typer.echo(
                "  "
                f"{lane_name}: {lane.get('successes_1h', 0)}/{lane.get('attempts_1h', 0)} ok, "
                f"{lane.get('backpressure_1h', 0)} backpressure, "
                f"p95 {p95 if p95 is not None else '-'}ms, "
                f"{eps}, {bps}/s"
            )

    archive_repair = dict(snapshot.get("archive_repair") or {})
    if archive_repair and (
        archive_repair.get("pending_ranges") or archive_repair.get("pending_bytes") or archive_repair.get("dead_ranges")
    ):
        limiter = dict(payload.get("adaptive_backlog_limiter") or {})
        typer.echo("")
        typer.echo("Archive Repair")
        typer.echo(f"  state: {archive_repair.get('state') or '-'}")
        typer.echo(f"  mode: {archive_repair.get('mode') or '-'}")
        typer.echo(f"  pending ranges: {archive_repair.get('pending_ranges', 0)}")
        typer.echo(f"  pending bytes: {_format_bytes(archive_repair.get('pending_bytes'))}")
        typer.echo(f"  pending paths: {archive_repair.get('pending_paths', 0)}")
        if archive_repair.get("pending_sessions"):
            typer.echo(f"  pending sessions: {archive_repair.get('pending_sessions', 0)}")
        ready_ranges = int(archive_repair.get("ready_ranges") or 0)
        deferred_ranges = int(archive_repair.get("deferred_ranges") or 0)
        if ready_ranges or deferred_ranges:
            typer.echo(f"  eligibility: {ready_ranges} ready now, {deferred_ranges} deferred")
        huge_ranges = int(archive_repair.get("huge_pending_ranges") or 0)
        if huge_ranges:
            typer.echo(f"  huge ranges: {huge_ranges} ranges, {_format_bytes(archive_repair.get('huge_pending_bytes'))}")
        oldest = archive_repair.get("oldest_pending_at")
        newest = archive_repair.get("newest_pending_at")
        if oldest or newest:
            typer.echo(f"  pending age: oldest {oldest or '-'}, newest {newest or '-'}")
        next_retry_min = archive_repair.get("next_retry_at_min")
        next_retry_max = archive_repair.get("next_retry_at_max")
        next_deferred = archive_repair.get("next_deferred_retry_at")
        if next_retry_min or next_retry_max or next_deferred:
            typer.echo("  retry window: " f"{next_retry_min or '-'} to {next_retry_max or '-'}; " f"next deferred {next_deferred or '-'}")
        providers = _archive_provider_mix(archive_repair.get("providers"))
        if providers != "-":
            typer.echo(f"  providers: {providers}")
        size_mix = _archive_size_mix(archive_repair.get("size_buckets"))
        if size_mix != "-":
            typer.echo(f"  size mix: {size_mix}")
        typer.echo(f"  dead ranges: {archive_repair.get('dead_ranges', 0)}")
        if limiter:
            current_cap = limiter.get("current_cap")
            floor = limiter.get("floor")
            ceiling = limiter.get("ceiling")
            target = limiter.get("target_queue_wait_ms")
            ewma = limiter.get("ewma_queue_wait_ms")
            last = limiter.get("last_observed_queue_wait_ms")
            ewma_value = _as_float(ewma)
            target_value = _as_float(target)
            typer.echo(f"  archive cap: {current_cap} (floor {floor}, ceiling {ceiling})")
            typer.echo(
                "  host queue wait: "
                f"ewma {_format_rate(ewma, 'ms')}, "
                f"last {_format_rate(last, 'ms')}, "
                f"target {_format_rate(target, 'ms')}"
            )
            total_backpressure = int(limiter.get("total_backpressure") or 0)
            retry_after = limiter.get("last_backpressure_retry_after_ms")
            cooldown = limiter.get("backpressure_cooldown_remaining_ms")
            if total_backpressure or retry_after or cooldown:
                typer.echo(
                    "  backpressure: "
                    f"{total_backpressure} total, "
                    f"retry-after {_format_rate(retry_after, 'ms')}, "
                    f"cooldown {_format_rate(cooldown, 'ms')}"
                )
            if current_cap == floor and ewma_value is not None and target_value is not None and ewma_value > target_value:
                typer.echo("  throttle: host queue pressure is holding archive at the floor")

    control_channel = dict(snapshot.get("control_channel") or {})
    if control_channel:
        typer.echo("")
        typer.echo("Control Channel")
        typer.echo(f"  status: {control_channel.get('status') or '-'}")
        typer.echo(f"  ws url: {control_channel.get('ws_url') or '-'}")
        launchable = ", ".join(control_channel.get("launchable_providers") or []) or "-"
        typer.echo(f"  launch providers: {launchable}")
        operations = dict(control_channel.get("control_operations_by_provider") or {})
        if operations:
            rendered_operations = ", ".join(
                f"{provider}:{'/'.join(str(item) for item in ops)}" for provider, ops in sorted(operations.items())
            )
            typer.echo(f"  provider operations: {rendered_operations}")
        typer.echo(f"  codex launch: {'yes' if control_channel.get('can_launch_codex') else 'no'}")
        if control_channel.get("launch_blocked_by"):
            typer.echo(f"  launch blocked by: {control_channel['launch_blocked_by']}")
        if control_channel.get("last_error_code") or control_channel.get("last_error_message"):
            last_error_code = control_channel.get("last_error_code") or "-"
            last_error_message = control_channel.get("last_error_message") or "-"
            typer.echo(f"  last error: {last_error_code} - {last_error_message}")

    provider_clis = dict(snapshot.get("provider_clis") or {})
    if provider_clis:
        typer.echo("")
        typer.echo("Provider CLIs")
        for provider, raw_info in sorted(provider_clis.items()):
            info = dict(raw_info or {})
            typer.echo(f"  {provider}: {info.get('path') or '-'}")
            typer.echo(f"    source: {info.get('source') or '-'}")
            if info.get("resolution_error"):
                typer.echo(f"    resolution error: {info['resolution_error']}")

    provider_contracts = dict(snapshot.get("provider_contracts") or {})
    contract_providers = dict(provider_contracts.get("providers") or {})
    if contract_providers:
        typer.echo("")
        typer.echo("Provider Contracts")
        for provider, raw_info in sorted(contract_providers.items()):
            info = dict(raw_info or {})
            typer.echo(f"  {provider}: {info.get('control_plane') or '-'}")
            operations = dict(info.get("operations") or {})
            supported = [
                f"{operation}:{dict(evidence).get('evidence_level') or '-'}"
                for operation, evidence in sorted(operations.items())
                if dict(evidence).get("supported")
            ]
            typer.echo(f"    supported: {', '.join(supported) or '-'}")

    provider_support_state = dict(snapshot.get("provider_support_state") or {})
    support_providers = dict(provider_support_state.get("providers") or {})
    if support_providers:
        typer.echo("")
        typer.echo("Provider Support")
        for provider, raw_info in sorted(support_providers.items()):
            info = dict(raw_info or {})
            capabilities = dict(info.get("capabilities") or {})
            proof = dict(info.get("proof") or {})
            version = dict(info.get("version_readiness") or {})
            live_ops = list(capabilities.get("live_control_operations") or [])
            missing_live_ops = list(capabilities.get("missing_live_control_operations") or [])
            supported_ops = list(capabilities.get("supported_operations") or [])
            unsupported_ops = list(capabilities.get("unsupported_operations") or [])
            typer.echo(f"  {provider}: {info.get('state') or '-'}")
            if live_ops:
                typer.echo(f"    live: {', '.join(str(item) for item in live_ops)}")
            if missing_live_ops:
                typer.echo(f"    missing live: {', '.join(str(item) for item in missing_live_ops)}")
            if supported_ops:
                typer.echo(f"    contract: {', '.join(str(item) for item in supported_ops)}")
            if unsupported_ops:
                typer.echo(f"    unsupported: {', '.join(str(item) for item in unsupported_ops)}")
            if proof:
                minimum_ops = ", ".join(str(item) for item in list(proof.get("minimum_evidence_operations") or []))
                release_failed_ops = ", ".join(str(item) for item in list(proof.get("release_failed_operations") or []))
                release_gap_ops = ", ".join(str(item) for item in list(proof.get("release_gap_operations") or []))
                typer.echo(
                    "    proof: "
                    f"{proof.get('state') or '-'}; "
                    f"minimum={proof.get('minimum_evidence_level') or '-'}"
                    f"{f' ({minimum_ops})' if minimum_ops else ''}"
                )
                if release_failed_ops:
                    typer.echo(f"    release failed: {release_failed_ops}")
                elif release_gap_ops:
                    typer.echo(f"    release gaps: {release_gap_ops}")
            if version:
                typer.echo(f"    version readiness: {version.get('state') or '-'}")

    provider_release_status = dict(snapshot.get("provider_release_status") or {})
    release_statuses = dict(provider_release_status.get("statuses") or {})
    if release_statuses or provider_release_status.get("skipped_reason"):
        typer.echo("")
        typer.echo("Provider Release Status")
    if provider_release_status.get("skipped_reason"):
        typer.echo(f"  skipped: {provider_release_status.get('skipped_reason')}")
    if release_statuses:
        for provider, raw_info in sorted(release_statuses.items()):
            info = dict(raw_info or {})
            typer.echo(f"  {provider}: {info.get('status') or '-'}")
            if info.get("verdict"):
                typer.echo(f"    verdict: {info.get('verdict')}")
            if info.get("schema_status") and info.get("schema_status") != "ok":
                typer.echo(f"    schema: {info.get('schema_status')}")
            if info.get("freshness_status") and info.get("freshness_status") != "fresh":
                typer.echo(f"    freshness: {info.get('freshness_status')}")
            if info.get("artifact_version") or info.get("current_version"):
                current_version = info.get("current_version") or "-"
                artifact_version = info.get("artifact_version") or "-"
                typer.echo(f"    version: local={current_version} artifact={artifact_version}")
            if info.get("failure_code"):
                typer.echo(f"    failure: {info.get('failure_code')}")
            if info.get("evidence_root"):
                typer.echo(f"    evidence: {info.get('evidence_root')}")

    provider_live_proof = dict(snapshot.get("provider_live_proof") or {})
    live_proof_statuses = dict(provider_live_proof.get("statuses") or {})
    has_configured_live_proof = any(dict(info or {}).get("configured") for info in live_proof_statuses.values())
    if has_configured_live_proof or provider_live_proof.get("skipped_reason"):
        typer.echo("")
        typer.echo("Provider Live Proof")
    if provider_live_proof.get("skipped_reason"):
        typer.echo(f"  skipped: {provider_live_proof.get('skipped_reason')}")
    if live_proof_statuses:
        for provider, raw_info in sorted(live_proof_statuses.items()):
            info = dict(raw_info or {})
            if not info.get("configured"):
                continue
            typer.echo(f"  {provider}: {info.get('status') or '-'}")
            if info.get("version_match"):
                typer.echo(f"    version match: {info.get('version_match')}")
            if info.get("freshness_status") and info.get("freshness_status") != "fresh":
                typer.echo(f"    freshness: {info.get('freshness_status')}")
            if info.get("artifact_version") or info.get("current_version"):
                current_version = info.get("current_version") or "-"
                artifact_version = info.get("artifact_version") or "-"
                typer.echo(f"    version: local={current_version} proof={artifact_version}")
            if info.get("failure_code"):
                typer.echo(f"    failure: {info.get('failure_code')}")
            if info.get("evidence_root"):
                typer.echo(f"    evidence: {info.get('evidence_root')}")

    provider_live_route_e2e = dict(snapshot.get("provider_live_route_e2e") or {})
    if provider_live_route_e2e.get("enabled") or provider_live_route_e2e.get("skipped_reason"):
        typer.echo("")
        typer.echo("Provider Live Route E2E")
    if provider_live_route_e2e.get("skipped_reason"):
        typer.echo(f"  skipped: {provider_live_route_e2e.get('skipped_reason')}")
    elif provider_live_route_e2e.get("enabled"):
        typer.echo(f"  status: {provider_live_route_e2e.get('status') or '-'}")
        providers = ", ".join(str(item) for item in list(provider_live_route_e2e.get("providers") or []))
        typer.echo(f"  providers: {providers or '-'}")
        coverage = provider_live_route_e2e.get("coverage_status") or "-"
        expected = ", ".join(str(item) for item in list(provider_live_route_e2e.get("expected_providers") or []))
        covered = ", ".join(str(item) for item in list(provider_live_route_e2e.get("covered_providers") or []))
        missing = ", ".join(str(item) for item in list(provider_live_route_e2e.get("missing_providers") or []))
        typer.echo(f"  coverage: {coverage}; expected={expected or '-'}; covered={covered or '-'}")
        if missing:
            typer.echo(f"  missing: {missing}")
        if provider_live_route_e2e.get("freshness_status"):
            freshness = provider_live_route_e2e.get("freshness_status")
            age = _format_age(provider_live_route_e2e.get("generated_at_age_seconds"))
            typer.echo(f"  freshness: {freshness} ({age})")
        if provider_live_route_e2e.get("engine_build"):
            typer.echo(f"  engine: {provider_live_route_e2e.get('engine_build')}")
        if provider_live_route_e2e.get("device_id"):
            typer.echo(f"  device: {provider_live_route_e2e.get('device_id')}")
        if provider_live_route_e2e.get("failure_code"):
            typer.echo(f"  failure: {provider_live_route_e2e.get('failure_code')}")
        if provider_live_route_e2e.get("message"):
            typer.echo(f"  message: {provider_live_route_e2e.get('message')}")
        source = dict(provider_live_route_e2e.get("source") or {})
        if source.get("path"):
            typer.echo(f"  evidence: {source.get('path')}")
        for raw_result in list(provider_live_route_e2e.get("results") or []):
            result = dict(raw_result or {})
            provider = result.get("provider") or "-"
            status = result.get("status") or "-"
            match = result.get("match_status_code") or "-"
            mismatch = result.get("mismatch_status_code") or "-"
            mismatch_code = result.get("mismatch_code") or "-"
            retry_note = ""
            match_attempts = result.get("match_attempt_count")
            mismatch_attempts = result.get("mismatch_attempt_count")
            if isinstance(match_attempts, int) and match_attempts > 1:
                retry_note += f"; match_attempts={match_attempts}"
            if isinstance(mismatch_attempts, int) and mismatch_attempts > 1:
                retry_note += f"; mismatch_attempts={mismatch_attempts}"
            typer.echo(f"    {provider}: {status}; match={match}; mismatch={mismatch}/{mismatch_code}{retry_note}")

    provider_hook_diagnostics = dict(snapshot.get("provider_hook_diagnostics") or {})
    hook_events = list(provider_hook_diagnostics.get("events") or [])
    if provider_hook_diagnostics.get("state") == "session_cwd_missing" or hook_events:
        typer.echo("")
        typer.echo("Provider Hook Diagnostics")
        typer.echo(f"  state: {provider_hook_diagnostics.get('state') or '-'}")
        typer.echo(f"  deleted cwd errors: {provider_hook_diagnostics.get('deleted_cwd_error_count', 0)}")
        latest = dict(provider_hook_diagnostics.get("latest") or {})
        if latest:
            typer.echo(f"  latest session: {latest.get('session_id') or '-'}")
            typer.echo(f"  missing cwd: {latest.get('cwd') or '-'}")
            typer.echo(f"  observed: {latest.get('timestamp') or '-'}")

    managed_session_contracts = dict(snapshot.get("managed_session_contracts") or {})
    contract_issues = list(managed_session_contracts.get("issues") or [])
    if contract_issues:
        typer.echo("")
        typer.echo("Managed Session Contracts")
        typer.echo(f"  state: {managed_session_contracts.get('state') or '-'}")
        typer.echo(f"  issues: {managed_session_contracts.get('issue_count', len(contract_issues))}")
        latest = dict(managed_session_contracts.get("latest") or {})
        if latest:
            typer.echo(f"  latest: {latest.get('headline') or latest.get('reason') or '-'}")
            typer.echo(f"  latest session: {latest.get('session_id') or '-'}")
            typer.echo(f"  action: {latest.get('action') or '-'}")

    typer.echo("")
    typer.echo("Outbox")
    typer.echo(f"  path: {outbox.get('path', '-')}")
    typer.echo(f"  files: {outbox.get('file_count', 0)}")
    typer.echo(f"  oldest: {_format_age(outbox.get('oldest_age_seconds'))}")

    typer.echo("")
    typer.echo("Launch")
    typer.echo(f"  state: {launch_readiness.get('state', '-')}")
    typer.echo(f"  stored url: {launch_readiness.get('stored_url') or '-'}")
    typer.echo(f"  machine name: {launch_readiness.get('machine_name') or '-'}")
    typer.echo(f"  service machine: {launch_readiness.get('service_machine_name') or '-'}")
    typer.echo(f"  remote command Runner env: {runner.get('path') or '-'}")
    typer.echo(f"  remote command Runner name: {runner.get('runner_name') or '-'}")
    runner_urls = ", ".join(str(item) for item in list(runner.get("runner_urls") or []) if str(item))
    typer.echo(f"  remote command Runner urls: {runner_urls or '-'}")
    typer.echo("  note: the remote command Runner is separate from the Machine Agent shipping path")

    reasons = list(snapshot.get("reasons") or [])
    if reasons:
        typer.echo("")
        typer.echo("Reasons")
        for reason in reasons:
            typer.echo(f"  - {reason}")

    launch_warnings = list(launch_readiness.get("warnings") or [])
    if launch_warnings:
        typer.echo("")
        typer.echo("Launch warnings")
        for warning in launch_warnings:
            typer.echo(f"  - {warning}")

    actions = list(snapshot.get("suggested_actions") or [])
    if actions:
        typer.echo("")
        typer.echo("Next")
        for action in actions:
            typer.echo(f"  - {action}")


def _collect_snapshot(claude_dir: str | None, *, fast: bool = False) -> dict[str, object]:
    state_root = resolve_longhouse_home_from_provider_home(claude_dir) if claude_dir else None
    return collect_local_health(state_root, fast=fast)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


@contextmanager
def _desktop_package_path():
    repo_path = _repo_root() / "desktop" / "LonghouseMenuBarHarness"
    if repo_path.exists():
        yield repo_path
        return

    packaged_path = resources.files("zerg").joinpath("_desktop", "LonghouseMenuBarHarness")
    with resources.as_file(packaged_path) as resolved:
        yield resolved


def _prebuilt_runtime_artifact(component: RuntimeComponent):
    return resolve_installed_runtime_artifact(component)


def _resolve_local_runtime_url(claude_dir: str | None) -> str | None:
    browser_config_dir = Path(claude_dir) if claude_dir else None
    config = load_config(claude_dir=browser_config_dir)

    public_url = normalize_runtime_url(config.server.public_url)
    if public_url:
        return public_url

    host = str(config.server.host or "").strip()
    port = int(config.server.port or 0)
    if not host or port <= 0:
        return None

    if host == "0.0.0.0":
        client_host = "127.0.0.1"
    elif host in {"::", "[::]"}:
        client_host = "[::1]"
    elif ":" in host and not host.startswith("["):
        client_host = f"[{host}]"
    else:
        client_host = host

    return f"http://{client_host}:{port}"


def _launch_desktop_surface(
    *,
    product: str,
    component: RuntimeComponent | None,
    claude_dir: str | None,
    refresh_seconds: int,
    allow_source_fallback: bool = False,
) -> None:
    config_dir = resolve_longhouse_home_from_provider_home(claude_dir) if claude_dir else None
    ui_url = get_zerg_url(config_dir) or _resolve_local_runtime_url(claude_dir)
    health_arguments = build_snapshot_arguments(claude_dir=claude_dir)

    prebuilt_artifact = _prebuilt_runtime_artifact(component) if component is not None else None
    if prebuilt_artifact is not None:
        command = [
            str(prebuilt_artifact.launch_path),
            "--live",
            "--refresh-seconds",
            str(refresh_seconds),
            "--health-exec",
            health_arguments[0],
        ]
        for argument in health_arguments[1:]:
            command.extend(["--health-arg", argument])
        if ui_url:
            command.extend(["--ui-url", ui_url])
        if component == RuntimeComponent.DESKTOP_APP:
            cwd = Path(prebuilt_artifact.path)
        else:
            cwd = Path(prebuilt_artifact.launch_path).parent
    else:
        if not allow_source_fallback:
            repair_command = recommended_machine_repair_command(
                can_reconcile_from_state=can_repair_machine_from_state(claude_dir=claude_dir)
            )
            typer.secho(
                f"Longhouse.app is not installed in {desktop_app_canonical_bundle_path()}. "
                f"{repair_command.replace('Run: ', '')} to install or repair the local runtime.",
                fg=typer.colors.RED,
            )
            raise typer.Exit(code=1)
        with _desktop_package_path() as package_path:
            command = [
                "swift",
                "run",
                "--package-path",
                str(package_path),
                product,
                "--live",
                "--refresh-seconds",
                str(refresh_seconds),
                "--health-exec",
                health_arguments[0],
            ]
            for argument in health_arguments[1:]:
                command.extend(["--health-arg", argument])
            if ui_url:
                command.extend(["--ui-url", ui_url])
            cwd = package_path
            try:
                subprocess.run(command, check=True, cwd=cwd)
                return
            except FileNotFoundError as exc:
                typer.secho(f"Missing required tool: {exc.filename}", fg=typer.colors.RED)
                raise typer.Exit(code=1) from exc
            except subprocess.CalledProcessError as exc:
                typer.secho(f"Longhouse desktop UI failed with exit code {exc.returncode}.", fg=typer.colors.RED)
                raise typer.Exit(code=exc.returncode or 1) from exc

    try:
        subprocess.run(command, check=True, cwd=cwd)
    except FileNotFoundError as exc:
        typer.secho(f"Missing required tool: {exc.filename}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    except subprocess.CalledProcessError as exc:
        typer.secho(f"Longhouse desktop UI failed with exit code {exc.returncode}.", fg=typer.colors.RED)
        raise typer.Exit(code=exc.returncode or 1) from exc


@app.callback()
def local_health_callback(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
    fast: bool = typer.Option(
        False,
        "--fast",
        help="Use the menu-bar fast path. Avoid broad process scans and deep diagnostics.",
    ),
    deep: bool = typer.Option(
        False,
        "--deep",
        help="Force the deep diagnostic path. This is the default for CLI compatibility.",
    ),
    claude_dir: str | None = typer.Option(
        None,
        "--claude-dir",
        help="Claude config directory override (maps that provider home to the sibling ~/.longhouse state root).",
    ),
) -> None:
    """Show local Longhouse shipping health for this machine."""
    if fast and deep:
        raise typer.BadParameter("Use only one of --fast or --deep.")
    if ctx.invoked_subcommand:
        ctx.obj = {"claude_dir": claude_dir}
        return
    _render_snapshot(_collect_snapshot(claude_dir, fast=fast), json_output=json_output)


@app.command("window", hidden=True)
def local_health_window(
    ctx: typer.Context,
    refresh_seconds: int = typer.Option(10, "--refresh-seconds", min=2, help="Live refresh cadence in seconds."),
) -> None:
    """Launch the developer window-host for desktop UI debugging."""
    claude_dir = (ctx.obj or {}).get("claude_dir")
    _launch_desktop_surface(
        product="LonghouseMenuBarHarnessApp",
        component=RuntimeComponent.DESKTOP_WINDOW,
        claude_dir=claude_dir,
        refresh_seconds=refresh_seconds,
        allow_source_fallback=True,
    )


@app.command("menubar")
def local_health_menubar(
    ctx: typer.Context,
    refresh_seconds: int = typer.Option(30, "--refresh-seconds", min=2, help="Live refresh cadence in seconds."),
) -> None:
    """Launch the Longhouse desktop app in menu bar mode."""
    claude_dir = (ctx.obj or {}).get("claude_dir")
    _launch_desktop_surface(
        product="LonghouseMenuBarHarnessMenuBar",
        component=RuntimeComponent.DESKTOP_APP,
        claude_dir=claude_dir,
        refresh_seconds=refresh_seconds,
    )
