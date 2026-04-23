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
    codex_runtime = getattr(result, "codex_runtime", None)
    if codex_runtime:
        if codex_runtime.installed_now:
            typer.secho(f"  [OK] Managed Codex runtime installed at {codex_runtime.path}", fg=typer.colors.GREEN)
        else:
            typer.secho(f"  [OK] Managed Codex runtime ready at {codex_runtime.path}", fg=typer.colors.GREEN)
    service_skipped = str(result.service_result.get("service") or "").strip().lower() == "skipped"
    typer.secho(
        f"[{'WARN' if service_skipped else 'OK'}] {result.service_result['message']}",
        fg=typer.colors.YELLOW if service_skipped else typer.colors.GREEN,
    )
    typer.echo(f"  Machine Agent: {result.service_result.get('service', 'N/A')}")
    typer.echo("  Config: " f"{result.service_result.get('plist_path') or result.service_result.get('unit_path', 'N/A')}")

    typer.echo("")
    typer.echo("Installing CLI hooks (Claude Code + Codex)...")
    for action in result.hooks.actions:
        skipped = "skipped" in action.lower()
        typer.secho(
            f"  [{'WARN' if skipped else 'OK'}] {action}",
            fg=typer.colors.YELLOW if skipped else typer.colors.GREEN,
        )
    if result.hooks.warning:
        typer.secho(f"  [WARN] Hook installation failed: {result.hooks.warning}", fg=typer.colors.YELLOW)

    if result.desktop_app_result:
        desktop_skipped = bool(result.desktop_app_result.get("skipped"))
        typer.echo("")
        typer.echo("Longhouse.app:")
        typer.secho(
            f"  [{'WARN' if desktop_skipped else 'OK'}] {result.desktop_app_result['message']}",
            fg=typer.colors.YELLOW if desktop_skipped else typer.colors.GREEN,
        )
        typer.echo(f"  Config: {result.desktop_app_result.get('plist_path', 'N/A')}")
        if result.desktop_app_result.get("app_path"):
            typer.echo(f"  App: {result.desktop_app_result['app_path']}")
        typer.echo("  Launch: " f"{result.desktop_app_result.get('launch_path') or result.desktop_app_result.get('binary_path', 'N/A')}")


def _run_reconcile(
    *,
    claude_dir: str | None,
    written_by: str,
    runtime_url: str | None = None,
    machine_name: str | None = None,
    menubar: bool | None = None,
    topology_intent: str | None = None,
) -> None:
    try:
        result = reconcile_local_runtime(
            claude_dir=claude_dir,
            written_by=written_by,
            runtime_url=runtime_url,
            machine_name=machine_name,
            menubar=menubar,
            topology_intent=topology_intent,
        )
    except RuntimeError as exc:
        typer.secho(f"[ERROR] {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    except ValueError as exc:
        typer.secho(f"[ERROR] {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    verb = "Updated machine config and reconciled" if written_by == "machine-configure" else "Reconciled"
    typer.secho(
        f"[OK] {verb} machine generation {result.machine_state.config_generation or 'unknown'}",
        fg=typer.colors.GREEN,
    )
    typer.echo(f"  URL: {result.machine_state.runtime_url}")
    _render_install_result(result.install_result)


@app.command("reconcile")
def reconcile_command(
    claude_dir: str = typer.Option(
        None,
        "--claude-dir",
        help="Claude config directory (default: ~/.claude)",
    ),
) -> None:
    """Rewrite local runtime artifacts from canonical machine state."""

    _run_reconcile(
        claude_dir=claude_dir,
        written_by="machine-reconcile",
    )


@app.command("configure")
def configure_command(
    url: str | None = typer.Option(
        None,
        "--url",
        help="Update the canonical Longhouse runtime URL before reconciling.",
    ),
    machine_name: str | None = typer.Option(
        None,
        "--machine-name",
        help="Update the canonical machine label before reconciling.",
    ),
    topology_intent: str | None = typer.Option(
        None,
        "--topology-intent",
        help="Update the machine topology intent before reconciling.",
    ),
    menubar: bool | None = typer.Option(
        None,
        "--menubar/--no-menubar",
        help="Enable or disable the desktop menu bar surface before reconciling.",
    ),
    claude_dir: str | None = typer.Option(
        None,
        "--claude-dir",
        help="Claude config directory (default: ~/.claude)",
    ),
) -> None:
    """Safely update machine config and regenerate local launch artifacts."""

    if url is None and machine_name is None and topology_intent is None and menubar is None:
        typer.secho("[ERROR] Specify at least one config override to apply.", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    _run_reconcile(
        claude_dir=claude_dir,
        written_by="machine-configure",
        runtime_url=url,
        machine_name=machine_name,
        menubar=menubar,
        topology_intent=topology_intent,
    )
