"""CLI wrapper management commands.

Usage:
    longhouse wrap --install               # wrap both claude and codex
    longhouse wrap --install --provider claude
    longhouse wrap --uninstall
    longhouse wrap --status
"""

from __future__ import annotations

from typing import Optional

import typer

from zerg.services.shipper.wrappers import SUPPORTED_PROVIDERS
from zerg.services.shipper.wrappers import get_wrapper_status
from zerg.services.shipper.wrappers import install_wrappers
from zerg.services.shipper.wrappers import uninstall_wrappers


def wrap(
    install: bool = typer.Option(False, "--install", help="Install CLI wrapper shims."),
    uninstall: bool = typer.Option(False, "--uninstall", help="Remove CLI wrapper shims."),
    status: bool = typer.Option(False, "--status", help="Show wrapper status."),
    provider: Optional[str] = typer.Option(
        None,
        "--provider",
        help=f"Limit to a single provider ({', '.join(SUPPORTED_PROVIDERS)}).",
    ),
) -> None:
    """Manage CLI wrapper shims for managed-local sessions.

    Wrapper mode makes bare ``claude`` / ``codex`` invocations go through
    Longhouse managed-local launch, giving each session first-class identity
    from the start.

    Default install is non-invasive (sidecar mode only).
    Use ``--install`` to opt in to wrapper mode.

    Bypass at any time:  LONGHOUSE_BYPASS=1 claude ...
    """
    actions = sum([install, uninstall, status])
    if actions == 0:
        # Default to --status when no flag given
        status = True
    if actions > 1:
        typer.secho("Specify exactly one of --install, --uninstall, or --status.", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    providers = [provider] if provider else None

    if install:
        _do_install(providers)
    elif uninstall:
        _do_uninstall(providers)
    else:
        _do_status()


def _do_install(providers: list[str] | None) -> None:
    try:
        results = install_wrappers(providers)
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1)

    typer.secho("Wrapper install results:", bold=True)
    for key, msg in results.items():
        if "skipped" in msg:
            typer.secho(f"  {key}: {msg}", fg=typer.colors.YELLOW)
        else:
            typer.secho(f"  {key}: {msg}", fg=typer.colors.GREEN)

    typer.echo("")
    typer.echo("Open a new terminal (or source your shell profile) for wrappers to take effect.")
    typer.echo("Bypass at any time:  LONGHOUSE_BYPASS=1 claude ...")
    typer.echo("Remove wrappers:     longhouse wrap --uninstall")


def _do_uninstall(providers: list[str] | None) -> None:
    try:
        results = uninstall_wrappers(providers)
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1)

    typer.secho("Wrapper uninstall results:", bold=True)
    for key, msg in results.items():
        typer.secho(f"  {key}: {msg}", fg=typer.colors.GREEN)

    typer.echo("")
    typer.echo("Open a new terminal for changes to take effect.")


def _do_status() -> None:
    info = get_wrapper_status()

    any_installed = False
    for provider in SUPPORTED_PROVIDERS:
        pinfo = info.get(provider, {})
        installed = pinfo.get("installed", False)
        real_bin = pinfo.get("real_binary", "not found")
        if installed:
            any_installed = True
            typer.secho(f"  {provider}: wrapped", fg=typer.colors.GREEN)
            typer.echo(f"    shim:  {pinfo.get('shim_path')}")
            typer.echo(f"    real:  {real_bin}")
        else:
            typer.echo(f"  {provider}: not wrapped  (real: {real_bin})")

    profile_info = info.get("profile", {})
    profile_installed = profile_info.get("installed", False)
    profile_path = profile_info.get("path", "?")
    if profile_installed:
        typer.secho(f"  profile: PATH injected in {profile_path}", fg=typer.colors.GREEN)
    else:
        typer.echo(f"  profile: no PATH block ({profile_path})")

    if not any_installed:
        typer.echo("")
        typer.echo("Enable wrapper mode:  longhouse wrap --install")
