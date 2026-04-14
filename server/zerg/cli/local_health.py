"""CLI surface for local Longhouse engine health and menu bar tools."""

from __future__ import annotations

import json
import subprocess
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

import typer

from zerg.services.desktop_app import build_snapshot_arguments
from zerg.services.local_health import collect_local_health
from zerg.services.longhouse_paths import resolve_longhouse_home_from_provider_home
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
    typer.echo(f"  runner env: {runner.get('path') or '-'}")
    typer.echo(f"  runner name: {runner.get('runner_name') or '-'}")
    runner_urls = ", ".join(str(item) for item in list(runner.get("runner_urls") or []) if str(item))
    typer.echo(f"  runner urls: {runner_urls or '-'}")

    reasons = list(snapshot.get("reasons") or [])
    if reasons:
        typer.echo("")
        typer.echo("Reasons")
        for reason in reasons:
            typer.echo(f"  - {reason}")

    actions = list(snapshot.get("suggested_actions") or [])
    if actions:
        typer.echo("")
        typer.echo("Next")
        for action in actions:
            typer.echo(f"  - {action}")


def _collect_snapshot(claude_dir: str | None) -> dict[str, object]:
    state_root = resolve_longhouse_home_from_provider_home(claude_dir) if claude_dir else None
    return collect_local_health(state_root)


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


def _launch_desktop_surface(
    *,
    product: str,
    component: RuntimeComponent | None,
    claude_dir: str | None,
    refresh_seconds: int,
    allow_source_fallback: bool = False,
) -> None:
    config_dir = resolve_longhouse_home_from_provider_home(claude_dir) if claude_dir else None
    ui_url = get_zerg_url(config_dir)
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
            typer.secho(
                f"Longhouse.app is not installed in {desktop_app_canonical_bundle_path()}. "
                "Run `longhouse connect --install` to install or repair the local runtime.",
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
    claude_dir: str | None = typer.Option(
        None,
        "--claude-dir",
        help="Claude config directory override (maps that provider home to the sibling ~/.longhouse state root).",
    ),
) -> None:
    """Show local Longhouse shipping health for this machine."""
    if ctx.invoked_subcommand:
        ctx.obj = {"claude_dir": claude_dir}
        return
    _render_snapshot(_collect_snapshot(claude_dir), json_output=json_output)


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
    refresh_seconds: int = typer.Option(10, "--refresh-seconds", min=2, help="Live refresh cadence in seconds."),
) -> None:
    """Launch the Longhouse desktop app in menu bar mode."""
    claude_dir = (ctx.obj or {}).get("claude_dir")
    _launch_desktop_surface(
        product="LonghouseMenuBarHarnessMenuBar",
        component=RuntimeComponent.DESKTOP_APP,
        claude_dir=claude_dir,
        refresh_seconds=refresh_seconds,
    )
