"""Machine runtime reconcile commands."""

from __future__ import annotations

import typer

from zerg.services.local_runtime_installer import LocalRuntimeInstallResult
from zerg.services.local_runtime_installer import reconcile_local_runtime
from zerg.services.machine_repair import MachineRepairResult
from zerg.services.machine_repair import repair_machine_runtime

app = typer.Typer(help="Machine runtime repair and reconciliation")


def _render_install_result(result: LocalRuntimeInstallResult) -> None:
    typer.echo("")
    typer.secho(f"  Machine: {result.machine_name}", fg=typer.colors.CYAN)
    if result.engine_runtime.installed_now:
        typer.secho(f"  [OK] Engine binary installed at {result.engine_runtime.path}", fg=typer.colors.GREEN)
    else:
        typer.secho(f"  [OK] Engine binary ready at {result.engine_runtime.path}", fg=typer.colors.GREEN)
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


def _render_health_summary(snapshot: dict[str, object]) -> None:
    typer.echo("")
    typer.echo("Health")
    typer.echo(f"  state: {snapshot.get('health_state')}")
    typer.echo(f"  severity: {snapshot.get('severity')}")
    typer.echo(f"  headline: {snapshot.get('headline')}")

    reasons = [str(item) for item in list(snapshot.get("reasons") or []) if str(item).strip()]
    if reasons:
        typer.echo("  reasons: " + ", ".join(reasons))

    actions = [str(item) for item in list(snapshot.get("suggested_actions") or []) if str(item).strip()]
    if actions:
        typer.echo("  next: " + actions[0])


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


def _run_repair(*, claude_dir: str | None) -> None:
    try:
        result: MachineRepairResult = repair_machine_runtime(
            claude_dir=claude_dir,
        )
    except RuntimeError as exc:
        typer.secho(f"[ERROR] {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    except ValueError as exc:
        typer.secho(f"[ERROR] {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    machine_state = result.reconcile_result.machine_state
    typer.secho(
        f"[OK] Repaired machine generation {machine_state.config_generation or 'unknown'}",
        fg=typer.colors.GREEN,
    )
    typer.echo(f"  URL: {machine_state.runtime_url}")
    _render_install_result(result.reconcile_result.install_result)

    spool_replay = result.spool_replay
    typer.echo("")
    typer.echo("Shipping")
    if spool_replay.success:
        summary = spool_replay.summary or {}
        replayed = summary.get("spool_replayed")
        pending = summary.get("spool_pending")
        if replayed is not None or pending is not None:
            typer.secho(
                f"  [OK] Queued shipping replayed={replayed or 0}, pending={pending or 0}",
                fg=typer.colors.GREEN,
            )
        else:
            typer.secho("  [OK] Queued shipping replay completed", fg=typer.colors.GREEN)
    elif spool_replay.warning:
        typer.secho(f"  [WARN] {spool_replay.warning}", fg=typer.colors.YELLOW)

    _render_health_summary(result.health_snapshot)


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


@app.command("repair")
def repair_command(
    claude_dir: str = typer.Option(
        None,
        "--claude-dir",
        help="Claude config directory (default: ~/.claude)",
    ),
) -> None:
    """Repair an already-configured local machine and verify post-repair health."""

    _run_repair(
        claude_dir=claude_dir,
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
        help="Legacy metadata only. Not used for launch safety.",
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
    if topology_intent is not None:
        typer.secho(
            "[WARN] --topology-intent is legacy metadata and no longer affects launch safety.",
            fg=typer.colors.YELLOW,
        )

    _run_reconcile(
        claude_dir=claude_dir,
        written_by="machine-configure",
        runtime_url=url,
        machine_name=machine_name,
        menubar=menubar,
        topology_intent=topology_intent,
    )
