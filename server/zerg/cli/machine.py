"""Machine runtime reconcile commands."""

from __future__ import annotations

import typer

from zerg.services.local_runtime_installer import LocalRuntimeInstallResult
from zerg.services.local_runtime_installer import reconcile_local_runtime

app = typer.Typer(help="Machine runtime repair and reconciliation")


def _render_install_result(result: LocalRuntimeInstallResult) -> None:
    typer.echo("")
    typer.secho(f"  Machine: {result.machine_name}", fg=typer.colors.CYAN)
    if result.engine_runtime.installed_now:
        typer.secho(f"  [OK] Engine binary installed at {result.engine_runtime.path}", fg=typer.colors.GREEN)
    else:
        typer.secho(f"  [OK] Engine binary ready at {result.engine_runtime.path}", fg=typer.colors.GREEN)
    typer.secho(f"[OK] {result.service_result['message']}", fg=typer.colors.GREEN)
    typer.echo(f"  Machine Agent: {result.service_result.get('service', 'N/A')}")
    typer.echo("  Config: " f"{result.service_result.get('plist_path') or result.service_result.get('unit_path', 'N/A')}")

    typer.echo("")
    typer.echo("Installing CLI hooks (Claude Code + Codex)...")
    for action in result.hooks.actions:
        typer.secho(f"  [OK] {action}", fg=typer.colors.GREEN)
    if result.hooks.warning:
        typer.secho(f"  [WARN] Hook installation failed: {result.hooks.warning}", fg=typer.colors.YELLOW)

    if result.desktop_app_result:
        typer.echo("")
        typer.echo("Longhouse.app:")
        typer.secho(f"  [OK] {result.desktop_app_result['message']}", fg=typer.colors.GREEN)
        typer.echo(f"  Config: {result.desktop_app_result.get('plist_path', 'N/A')}")
        if result.desktop_app_result.get("app_path"):
            typer.echo(f"  App: {result.desktop_app_result['app_path']}")
        typer.echo("  Launch: " f"{result.desktop_app_result.get('launch_path') or result.desktop_app_result.get('binary_path', 'N/A')}")


@app.command("reconcile")
def reconcile_command(
    claude_dir: str = typer.Option(
        None,
        "--claude-dir",
        help="Claude config directory (default: ~/.claude)",
    ),
) -> None:
    """Rewrite local runtime artifacts from canonical machine state."""

    try:
        result = reconcile_local_runtime(
            claude_dir=claude_dir,
            written_by="machine-reconcile",
        )
    except RuntimeError as exc:
        typer.secho(f"[ERROR] {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    except ValueError as exc:
        typer.secho(f"[ERROR] {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    typer.secho(
        f"[OK] Reconciled machine generation {result.machine_state.config_generation or 'unknown'}",
        fg=typer.colors.GREEN,
    )
    typer.echo(f"  URL: {result.machine_state.runtime_url}")
    _render_install_result(result.install_result)
