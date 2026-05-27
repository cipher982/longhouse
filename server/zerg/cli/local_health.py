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
            typer.echo(
                "  last error: " f"{control_channel.get('last_error_code') or '-'}" f" - {control_channel.get('last_error_message') or '-'}"
            )

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
        cwd = Path(prebuilt_artifact.path) if component == RuntimeComponent.DESKTOP_APP else Path(prebuilt_artifact.launch_path).parent
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
