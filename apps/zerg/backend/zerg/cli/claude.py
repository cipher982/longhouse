"""Managed-local Claude launcher CLI."""

from __future__ import annotations

import asyncio
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import typer
from sqlalchemy.orm import Session

from zerg.crud import get_user_by_email
from zerg.crud import runner_crud
from zerg.database import get_session_factory
from zerg.database import initialize_database
from zerg.models.models import Runner
from zerg.services.managed_local_launcher import ManagedLocalLaunchError
from zerg.services.managed_local_launcher import ManagedLocalLaunchParams
from zerg.services.managed_local_launcher import ManagedLocalLaunchResult
from zerg.services.managed_local_launcher import launch_managed_local_session
from zerg.session_loop_mode import SessionLoopMode


@dataclass(frozen=True)
class ResolvedRunnerTarget:
    owner_id: int
    owner_email: str | None
    runner_target: str
    runner_name: str


def _interactive_stdio() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _run_attach_command(attach_command: str) -> int:
    parts = shlex.split(attach_command)
    completed = subprocess.run(parts, check=False)
    return int(completed.returncode)


def _resolve_runner_by_name(db: Session, name: str) -> list[Runner]:
    return db.query(Runner).filter(Runner.name == name).order_by(Runner.id.asc()).all()


def _resolve_runner_target(
    db: Session,
    *,
    runner_target: str,
    owner_email: str | None,
) -> ResolvedRunnerTarget:
    target = runner_target.strip()
    if not target:
        raise typer.BadParameter("Runner target is required.", param_hint="--runner")

    owner = None
    if owner_email:
        owner = get_user_by_email(db, owner_email.strip())
        if owner is None:
            raise typer.BadParameter(f"Owner '{owner_email}' was not found.", param_hint="--owner-email")

    if target.startswith("runner:"):
        try:
            runner_id = int(target.split(":", 1)[1])
        except ValueError as exc:
            raise typer.BadParameter(
                "Runner must be a name or runner:<id>.",
                param_hint="--runner",
            ) from exc
        runner = runner_crud.get_runner(db, runner_id)
        if runner is None:
            raise typer.BadParameter(f"Runner '{target}' was not found.", param_hint="--runner")
        if owner is not None and runner.owner_id != owner.id:
            raise typer.BadParameter(
                f"Runner '{target}' is not owned by '{owner.email}'.",
                param_hint="--owner-email",
            )
        return ResolvedRunnerTarget(
            owner_id=runner.owner_id,
            owner_email=getattr(runner.owner, "email", None),
            runner_target=target,
            runner_name=runner.name,
        )

    if owner is not None:
        runner = runner_crud.get_runner_by_name(db, owner.id, target)
        if runner is None:
            raise typer.BadParameter(
                f"Runner '{target}' was not found for '{owner.email}'.",
                param_hint="--runner",
            )
        return ResolvedRunnerTarget(
            owner_id=runner.owner_id,
            owner_email=owner.email,
            runner_target=runner.name,
            runner_name=runner.name,
        )

    matches = _resolve_runner_by_name(db, target)
    if not matches:
        raise typer.BadParameter(f"Runner '{target}' was not found.", param_hint="--runner")
    if len(matches) > 1:
        owners = []
        for match in matches:
            owner_label = getattr(match.owner, "email", None) or f"owner_id={match.owner_id}"
            owners.append(f"runner:{match.id} ({owner_label})")
        raise typer.BadParameter(
            "Runner name is ambiguous. Re-run with --owner-email or a runner:<id> target. Matches: " + ", ".join(owners),
            param_hint="--runner",
        )

    runner = matches[0]
    return ResolvedRunnerTarget(
        owner_id=runner.owner_id,
        owner_email=getattr(runner.owner, "email", None),
        runner_target=runner.name,
        runner_name=runner.name,
    )


def _launch_managed_local_from_cli(
    db: Session,
    *,
    runner_target: str,
    cwd: str,
    project: str | None,
    loop_mode: SessionLoopMode,
    name: str | None,
    owner_email: str | None,
) -> tuple[ResolvedRunnerTarget, ManagedLocalLaunchResult]:
    resolved = _resolve_runner_target(db, runner_target=runner_target, owner_email=owner_email)
    launch_result = asyncio.run(
        launch_managed_local_session(
            db,
            ManagedLocalLaunchParams(
                owner_id=resolved.owner_id,
                runner_target=resolved.runner_target,
                cwd=cwd,
                project=project,
                display_name=name,
                loop_mode=loop_mode.value,
            ),
        )
    )
    return resolved, launch_result


def claude(
    runner: str = typer.Option(..., "--runner", help="Runner name or runner:<id>."),
    cwd: Path = typer.Option(..., "--cwd", exists=True, file_okay=False, dir_okay=True, resolve_path=True),
    project: str | None = typer.Option(None, "--project", help="Optional session project label."),
    loop_mode: SessionLoopMode = typer.Option(
        SessionLoopMode.MANUAL,
        "--loop-mode",
        help="Loop mode to store on the managed-local session.",
    ),
    name: str | None = typer.Option(None, "--name", help="Optional display name for the Claude session."),
    owner_email: str | None = typer.Option(
        None,
        "--owner-email",
        help="Owner email for disambiguating runner names when needed.",
    ),
    attach: bool = typer.Option(
        True,
        "--attach/--no-attach",
        help="Auto-attach to the tmux session when running interactively.",
    ),
) -> None:
    """Launch a managed-local Claude Code session on an existing runner."""

    initialize_database()
    db = get_session_factory()()
    attach_command: str | None = None
    attach_requested = attach

    try:
        resolved, result = _launch_managed_local_from_cli(
            db,
            runner_target=runner,
            cwd=str(cwd),
            project=project,
            loop_mode=loop_mode,
            name=name,
            owner_email=owner_email,
        )
        session = result.session
        attach_command = result.attach_command

        typer.secho(
            f"Managed local Claude session launched on {resolved.runner_name}.",
            fg=typer.colors.GREEN,
        )
        typer.echo(f"Session ID: {session.id}")
        typer.echo(f"Provider session ID: {session.provider_session_id}")
        typer.echo(f"Attach: {attach_command}")

        if not attach_requested:
            return
        if not _interactive_stdio():
            typer.secho("Skipping auto-attach because stdin/stdout are not TTYs.", fg=typer.colors.YELLOW)
            return

        typer.echo("Attaching...")
        exit_code = _run_attach_command(attach_command)
        if exit_code != 0:
            typer.secho(
                f"Auto-attach exited with code {exit_code}. Run the printed attach command manually.",
                fg=typer.colors.YELLOW,
            )
    except ManagedLocalLaunchError as exc:
        db.rollback()
        typer.secho(exc.detail, fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    finally:
        db.close()
