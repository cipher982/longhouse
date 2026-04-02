"""CLI wrapper management commands.

Usage:
    longhouse wrap --install               # opt in to both default launchers
    longhouse wrap --install --provider claude
    longhouse wrap --uninstall
    longhouse wrap --status
"""

from __future__ import annotations

import json
from typing import Optional

import typer

from zerg.services.shipper.wrappers import SUPPORTED_PROVIDERS
from zerg.services.shipper.wrappers import get_wrapper_status
from zerg.services.shipper.wrappers import install_wrappers
from zerg.services.shipper.wrappers import uninstall_wrappers


def wrap(
    install: bool = typer.Option(False, "--install", help="Install opt-in default-launcher wrappers."),
    uninstall: bool = typer.Option(False, "--uninstall", help="Remove default-launcher wrappers."),
    status: bool = typer.Option(False, "--status", help="Show wrapper status."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
    provider: Optional[str] = typer.Option(
        None,
        "--provider",
        help=f"Limit to a single provider ({', '.join(SUPPORTED_PROVIDERS)}).",
    ),
) -> None:
    """Manage opt-in default-launcher wrappers for Longhouse sessions.

    Wrapper mode makes bare ``claude`` / ``codex`` interactive launches go
    through Longhouse. If Longhouse is unreachable, the wrapper falls back
    to the native CLI automatically.

    Default install is non-invasive (sidecar mode only).
    Use ``--install`` to opt in to wrapper mode.

    Inspect with:        type claude
    Bypass at any time:  LONGHOUSE_BYPASS=1 claude ...
    """
    actions = sum([install, uninstall, status])
    if actions == 0:
        status = True
    if actions > 1:
        typer.secho("Specify exactly one of --install, --uninstall, or --status.", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    providers = [provider] if provider else None

    if install:
        _do_install(providers, json_output=json_output)
    elif uninstall:
        _do_uninstall(providers, json_output=json_output)
    else:
        _do_status(json_output=json_output)


def _do_install(providers: list[str] | None, *, json_output: bool) -> None:
    try:
        results = install_wrappers(providers)
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1)

    if json_output:
        typer.echo(json.dumps({"action": "install", "providers": providers, "results": results}, indent=2))
        return

    typer.secho("Wrapper install results:", bold=True)
    for key, msg in results.items():
        if "skipped" in msg:
            typer.secho(f"  {key}: {msg}", fg=typer.colors.YELLOW)
        else:
            typer.secho(f"  {key}: {msg}", fg=typer.colors.GREEN)

    typer.echo("")
    typer.echo("Open a new terminal (or source your shell profile) for wrappers to take effect.")
    typer.echo("Inspect:  type claude")
    typer.echo("Bypass:   LONGHOUSE_BYPASS=1 claude ...")
    typer.echo("Remove:   longhouse wrap --uninstall")


def _do_uninstall(providers: list[str] | None, *, json_output: bool) -> None:
    try:
        results = uninstall_wrappers(providers)
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1)

    if json_output:
        typer.echo(json.dumps({"action": "uninstall", "providers": providers, "results": results}, indent=2))
        return

    typer.secho("Wrapper uninstall results:", bold=True)
    for key, msg in results.items():
        typer.secho(f"  {key}: {msg}", fg=typer.colors.GREEN)

    typer.echo("")
    typer.echo("Open a new terminal for changes to take effect.")


def _do_status(*, json_output: bool) -> None:
    info = get_wrapper_status()

    if json_output:
        typer.echo(json.dumps(info, indent=2))
        return

    any_installed = False
    for provider in SUPPORTED_PROVIDERS:
        pinfo = info.get(provider, {})
        installed = pinfo.get("installed", False)
        real_bin = pinfo.get("real_binary", "not found")
        if installed:
            any_installed = True
            typer.secho(f"  {provider}: wrapped (falls back to native on setup failure)", fg=typer.colors.GREEN)
            typer.echo(f"    real: {real_bin}")
        else:
            typer.echo(f"  {provider}: not wrapped  (real: {real_bin})")

    profile_info = info.get("profile", {})
    profile_path = profile_info.get("path", "?")
    if any_installed:
        typer.secho(f"  profile: {profile_path}", fg=typer.colors.GREEN)
    else:
        typer.echo(f"  profile: {profile_path}")

    if not any_installed:
        typer.echo("")
        typer.echo("Enable opt-in wrapper mode:  longhouse wrap --install")
