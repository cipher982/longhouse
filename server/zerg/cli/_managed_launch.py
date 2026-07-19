"""Shared managed-launch core for provider CLI wrappers (`longhouse claude`, `longhouse codex`).

The provider wrappers differ in real, load-bearing ways -- PTY/foreground
process handling, bridge lifecycle, provider binary resolution, and
attach-failure/cleanup semantics -- and those stay provider-local by design
(see CLAUDE.md: "prefer obvious seams over clever reuse -- if two flows
behave differently, split them"). This module holds only the pieces that are
genuinely identical across wrappers today: the
`/api/sessions/managed-local/this-device` request/response shape, the shared
credential/preflight preamble, and a couple of copy/paste-prone one-liners.

Orchestration helpers here take the caller's own step functions
(`load_credentials`, `run_preflight`, `opener`, ...) as parameters instead of
importing and calling a single hardcoded implementation. Each provider
wrapper still calls this module using its own module-local names for those
steps (e.g. `claude.py`'s `_load_api_credentials`,
`codex.py`'s `_ensure_managed_launch_preflight`), so the existing
`monkeypatch.setattr(claude_cli, "...")` / `monkeypatch.setattr(codex_cli,
"...")` coverage in tests_lite/test_claude_cli.py and
tests_lite/test_codex_cli.py keeps intercepting the same call sites it did
before this module existed.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from collections.abc import Mapping
from pathlib import Path

import httpx
import typer

from zerg.cli import _launch_ui as launch_ui
from zerg.cli._common import ManagedLocalLaunchResponse
from zerg.cli._common import git_output
from zerg.cli._common import load_api_credentials as _common_load_api_credentials
from zerg.cli._managed_contract import record_managed_provider_contract
from zerg.services.session_launch_provenance import human_shell_provenance_for_interactive_tty
from zerg.services.shipper import get_zerg_url
from zerg.services.shipper import load_token
from zerg.session_loop_mode import SessionLoopMode

EXIT_SETUP_FAILED = 78


def infer_git_context(cwd: Path) -> tuple[str | None, str | None]:
    git_repo = git_output(cwd, "rev-parse", "--show-toplevel")
    git_branch = git_output(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    if git_branch == "HEAD":
        git_branch = None
    return git_repo, git_branch


def interactive_human_shell_launch_provenance(
    *,
    env: Mapping[str, str | None] | None = None,
    stdin_is_tty: bool | None = None,
    stdout_is_tty: bool | None = None,
) -> tuple[str | None, str | None]:
    return human_shell_provenance_for_interactive_tty(
        env=env or os.environ,
        stdin_is_tty=sys.stdin.isatty() if stdin_is_tty is None else stdin_is_tty,
        stdout_is_tty=sys.stdout.isatty() if stdout_is_tty is None else stdout_is_tty,
    )


def add_interactive_human_shell_launch_env(env: dict[str, str]) -> None:
    launch_actor, launch_surface = interactive_human_shell_launch_provenance()
    if launch_actor:
        env["LONGHOUSE_LAUNCH_ACTOR"] = launch_actor
    if launch_surface:
        env["LONGHOUSE_LAUNCH_SURFACE"] = launch_surface


def build_managed_local_launch_payload(
    *,
    cwd: Path,
    provider: str,
    project: str | None,
    name: str | None,
    loop_mode: SessionLoopMode,
    machine_name: str,
    native_claude_channels_available: bool | None = None,
    claude_launch_env: dict[str, str] | None = None,
    permission_mode: str = "bypass",
    launch_actor: str | None = None,
    launch_surface: str | None = None,
) -> dict:
    """Build the exact JSON body posted to /api/sessions/managed-local/this-device.

    Public so contract tests can import it and validate against the live
    server schema without reproducing the payload shape in two places.
    """
    git_repo, git_branch = infer_git_context(cwd)
    payload: dict = {
        "cwd": str(cwd),
        "provider": provider,
        "project": project,
        "git_repo": git_repo,
        "git_branch": git_branch,
        "display_name": name,
        "loop_mode": loop_mode.value,
        "machine_name": machine_name,
        "permission_mode": permission_mode,
    }
    if launch_actor:
        payload["launch_actor"] = launch_actor
    if launch_surface:
        payload["launch_surface"] = launch_surface
    if provider == "claude":
        payload["native_claude_channels_available"] = native_claude_channels_available
        if claude_launch_env:
            payload["claude_launch_env"] = claude_launch_env
    return payload


def launch_managed_local_from_api(
    *,
    url: str,
    token: str,
    cwd: Path,
    project: str | None,
    loop_mode: SessionLoopMode,
    name: str | None,
    machine_name: str,
    native_claude_channels_available: bool | None = None,
    claude_launch_env: dict[str, str] | None = None,
    provider: str = "claude",
    permission_mode: str = "bypass",
    verbose: bool = False,
) -> ManagedLocalLaunchResponse:
    launch_actor, launch_surface = interactive_human_shell_launch_provenance()
    payload = build_managed_local_launch_payload(
        cwd=cwd,
        provider=provider,
        project=project,
        name=name,
        loop_mode=loop_mode,
        machine_name=machine_name,
        native_claude_channels_available=native_claude_channels_available,
        claude_launch_env=claude_launch_env,
        permission_mode=permission_mode,
        launch_actor=launch_actor,
        launch_surface=launch_surface,
    )

    launch_url = f"{url.rstrip('/')}/api/sessions/managed-local/this-device"
    if verbose:
        typer.echo(f"Creating Longhouse managed {provider} session: POST {launch_url}")
    try:
        with httpx.Client(timeout=30) as client:
            response = client.post(
                launch_url,
                headers={"X-Agents-Token": token},
                json=payload,
            )
    except httpx.ConnectError:
        typer.secho(f"Could not connect to {url}", fg=typer.colors.RED)
        raise typer.Exit(code=EXIT_SETUP_FAILED)
    except httpx.TimeoutException:
        typer.secho(
            f"Timed out waiting for Longhouse to create the managed {provider} session at {url}. "
            f"No local {provider} process was started.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=EXIT_SETUP_FAILED)

    if response.status_code == 401:
        typer.secho("Authentication failed. Run 'longhouse auth' to re-authenticate.", fg=typer.colors.RED)
        raise typer.Exit(code=EXIT_SETUP_FAILED)

    if response.status_code == 422:
        # Almost always means CLI enum/schema drifted from the server since
        # the user's CLI was installed. Surface a recovery path instead of a
        # raw validation dump.
        try:
            errors = response.json()
        except ValueError:
            errors = response.text[:200]
        typer.secho(
            "Longhouse server rejected the launch request (422).\n"
            "Your CLI likely drifted from the server schema. Update with:\n"
            "  cd ~/git/zerg/longhouse && make dogfood-refresh\n"
            f"Server detail: {errors}",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=EXIT_SETUP_FAILED)

    if response.status_code != 200:
        detail = ""
        try:
            payload = response.json()
            detail = str(payload.get("detail") or "").strip()
        except ValueError:
            detail = response.text.strip()
        message = detail or response.text[:200] or "Longhouse session launch failed"
        typer.secho(message, fg=typer.colors.RED)
        raise typer.Exit(code=EXIT_SETUP_FAILED)

    body = response.json()
    run_id = str(body.get("run_id") or "").strip()
    if not run_id:
        typer.secho(
            "Longhouse server did not return the managed run identity. Update the server before launching with this CLI.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=EXIT_SETUP_FAILED)
    raw_provider_session_id = body.get("provider_session_id")
    provider_session_id = str(raw_provider_session_id).strip() if raw_provider_session_id else None
    return ManagedLocalLaunchResponse(
        session_id=str(body["session_id"]),
        run_id=run_id,
        provider_session_id=provider_session_id,
        attach_command=str(body["attach_command"]),
        source_runner_name=str(body.get("source_runner_name") or machine_name),
        managed_transport=str(body.get("managed_transport") or "") or None,
        permission_mode=str(body.get("permission_mode") or "bypass"),
        hook_token=(str(body["hook_token"]) if body.get("hook_token") else None),
    )


def resolve_managed_launch_credentials(
    *,
    url: str | None,
    token: str | None,
    config_dir: Path | None,
    exit_code: int = EXIT_SETUP_FAILED,
) -> tuple[str, str]:
    """Both wrappers store their config under the provider home (`~/.claude`),
    not a bare Longhouse home, so both resolve credentials the same way."""
    return _common_load_api_credentials(
        url=url,
        token=token,
        config_dir=config_dir,
        exit_code=exit_code,
        config_dir_is_provider_home=True,
        resolve_url=get_zerg_url,
        resolve_token=load_token,
    )


def start_managed_launch(
    *,
    config_dir: str | None,
    url: str | None,
    token: str | None,
    verbose: bool,
    exit_code: int,
    load_credentials: Callable[..., tuple[str, str]],
) -> tuple[str, str, Path | None]:
    """Shared leading step: resolve config dir, then load credentials.

    Callers still call `launch_ui.quiet_diagnostic_logs(verbose)` themselves
    before this, at the top of their own command body -- that call stays
    directly visible in each provider module's source on purpose. It is
    covered by a source-contract guard
    (test_managed_provider_contracts.py::test_managed_launcher_uses_shared_launch_ui_template)
    that intentionally checks each launcher file for it, so a new provider
    can't quietly skip the shared launch UI. Folding it in here would hide it
    from that guard.

    Takes `load_credentials` as a parameter (rather than calling a hardcoded
    implementation) so each wrapper's own module-local credential function --
    and any `monkeypatch.setattr(claude_cli, "_load_api_credentials", ...)` /
    `monkeypatch.setattr(codex_cli, "_load_api_credentials", ...)` pointed at
    it -- stays the thing that actually runs.
    """
    resolved_config_dir = Path(config_dir) if config_dir else None
    resolved_url, resolved_token = load_credentials(
        url=url,
        token=token,
        config_dir=resolved_config_dir,
        exit_code=exit_code,
    )
    return resolved_url, resolved_token, resolved_config_dir


def finish_managed_launch_preflight(
    *,
    url: str,
    machine_name: str,
    config_dir: Path | None,
    exit_code: int,
    verbose: bool,
    run_preflight: Callable[..., None],
) -> None:
    """Shared trailing step: run the machine-contract preflight check, then
    print the "Preparing your session..." progress line.

    Takes `run_preflight` as a parameter for the same reason as
    `start_managed_launch` takes `load_credentials`: it keeps
    `monkeypatch.setattr(claude_cli, "_ensure_managed_launch_preflight", ...)`
    (and the `codex_cli` equivalent) intercepting the real call site.
    """
    run_preflight(
        url=url,
        machine_name=machine_name,
        config_dir=config_dir,
        exit_code=exit_code,
    )
    launch_ui.progress("Preparing your session…")
    if verbose:
        typer.echo(f"Longhouse: {url}")


def maybe_open_session_url(
    *,
    open_browser: bool,
    session_url: str,
    opener: Callable[[str], bool],
) -> None:
    if not open_browser:
        return
    typer.echo("Opening session in browser...")
    if not opener(session_url):
        typer.secho(f"Could not open browser automatically. Visit: {session_url}", fg=typer.colors.YELLOW)


def record_contract_or_warn(**kwargs: object) -> Path | None:
    try:
        return record_managed_provider_contract(**kwargs)  # type: ignore[arg-type]
    except Exception as exc:
        typer.secho(
            f"Longhouse warning: could not record managed-session contract: {exc}",
            fg=typer.colors.YELLOW,
            err=True,
        )
        return None
