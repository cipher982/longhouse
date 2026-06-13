"""Shared terminal UI for managed provider launches (claude/codex/opencode/agy).

One definition of the "hearth" launch experience so the four provider CLIs can't
drift apart again. This module is pure presentation — no network, no DB, no
control flow. The rich import is guarded so a rendering problem can never break a
launch; we fall back to plain typer lines.

Design notes (see review):
- The opening panel asserts steerability, so callers must print it only once the
  control surface is actually up. Providers whose bridge starts a beat later
  (codex, opencode) call launch_panel() AFTER bridge readiness. Providers without
  a proven steer surface at launch (antigravity) pass steerable=False for softer
  copy.
- exit_bookend() is print-only. Each provider keeps its own exit policy (return
  vs raise) and decides when to call the clean vs crash variant.
"""

from __future__ import annotations

import typer

from zerg.cli._common import build_session_url
from zerg.cli._common import build_short_session_url

PROVIDER_LABELS: dict[str, str] = {
    "claude": "Claude",
    "codex": "Codex",
    "opencode": "OpenCode",
    "antigravity": "Antigravity",
}


def display_host(url: str) -> str:
    """Strip scheme + trailing slash so a link reads cleanly in the panel."""
    host = url.strip()
    for scheme in ("https://", "http://"):
        if host.startswith(scheme):
            host = host[len(scheme) :]
            break
    return host.rstrip("/")


def quiet_diagnostic_logs(verbose: bool) -> None:
    """Keep the happy path clean for every provider launcher.

    connect.py installs a root INFO handler at import time, so hook-install and
    httpx request lines leak into the terminal as `[INFO] ...` noise. Quiet those
    loggers unless the user asked for --verbose. Call this at the top of each
    provider's launch command so all four behave the same."""
    if verbose:
        return
    import logging

    for name in ("zerg.services.shipper.hooks", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def progress(message: str) -> None:
    """Low-key progress line. Stays visible (not verbose-gated) — bridge startup
    can take a few seconds and silence reads as a hang."""
    typer.secho(message, fg=typer.colors.BRIGHT_BLACK)


def launch_panel(
    *,
    provider_label: str,
    base_url: str,
    machine_name: str,
    session_id: str,
    verbose: bool,
    steerable: bool = True,
    attach_command: str | None = None,
) -> None:
    """Opening hearth panel: the session is live and yours to drive from anywhere.

    Leads with the steer-from-anywhere capability (the product's wedge) rather
    than a static status lamp. `steerable=False` softens the claim to a watch-only
    link for providers without a proven live control surface at launch.

    Verbose appends the diagnostic block: full session id, full timeline URL, and
    the attach command when one is supplied.
    """
    short_link = display_host(build_short_session_url(base_url, session_id))
    call_to_action = "Steer from anywhere" if steerable else "Watch on your timeline"

    try:
        from rich.box import ROUNDED
        from rich.console import Console
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text

        body = Table.grid(padding=(0, 0))
        body.add_column()
        body.add_row(Text("🔥  The hearth is lit", style="bold") + Text(f" on {machine_name}", style="orange3"))
        body.add_row("")
        body.add_row(Text(call_to_action, style="dim"))
        body.add_row(Text("→ ", style="dim") + Text(short_link, style="bold cyan"))

        Console().print(
            Panel(
                body,
                title=Text(f"⬡ Longhouse — {provider_label}", style="bold"),
                title_align="left",
                box=ROUNDED,
                border_style="orange3",
                padding=(1, 3),
                expand=False,
            )
        )
    except Exception:
        # Presentation must never break a launch — plain fallback.
        typer.secho(f"🔥  The hearth is lit on {machine_name}", fg=typer.colors.YELLOW)
        typer.echo(f"  {call_to_action} → {short_link}")

    if verbose:
        typer.echo("")
        typer.secho(f"{provider_label} session details (--verbose):", fg=typer.colors.BRIGHT_BLACK)
        typer.echo(f"  Session ID: {session_id}")
        typer.echo(f"  Session URL: {build_session_url(base_url, session_id)}")
        if attach_command:
            typer.echo(f"  Attach: {attach_command}")


def exit_bookend(
    *,
    exit_code: int,
    machine_name: str,
    reattach_command: str | None = None,
    reattachable_on_nonzero_exit: bool = False,
) -> None:
    """Closing bookend keyed on the real process exit code. Print-only.

    - clean exit                              -> the hearth is banked, thread saved
    - crash, reattachable_on_nonzero_exit     -> the hearth still burns, rejoin it
      (codex: the bridge is left running and is reattachable, so "scattered/dead"
       would be a lie)
    - crash, otherwise                        -> the fire scattered, rekindle it
    """
    if exit_code == 0:
        typer.secho(f"·  The hearth banked on {machine_name} — thread saved.", fg=typer.colors.BRIGHT_BLACK)
        return

    if reattachable_on_nonzero_exit:
        typer.secho(f"🔥  The hearth still burns — detached (exit {exit_code}).", fg=typer.colors.YELLOW)
        if reattach_command:
            typer.echo(f"   Rejoin: {reattach_command}")
        return

    typer.secho(f"✗  The fire scattered (exit {exit_code}). Rekindle:", fg=typer.colors.YELLOW)
    if reattach_command:
        typer.echo(f"   {reattach_command}")
