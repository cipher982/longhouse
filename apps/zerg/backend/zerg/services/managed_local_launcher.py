"""Managed local session launcher.

Phase 2 keeps this intentionally small:
- resolve a reachable runner owned by the current user
- create a managed-local AgentSession row
- launch stock Claude Code inside a detached tmux session on that runner
- verify the tmux session exists

No chat routing or Loop behavior changes live here yet.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy.orm import Session

from zerg.crud import runner_crud
from zerg.models.agents import AgentSession
from zerg.services.managed_local_runtime import mark_managed_local_session_launched
from zerg.services.managed_local_tmux import build_tmux_attach_command
from zerg.services.managed_local_tmux import build_tmux_current_command_command
from zerg.services.managed_local_tmux import build_tmux_has_session_command
from zerg.services.managed_local_tmux import build_tmux_kill_session_command
from zerg.services.managed_local_tmux import build_tmux_launch_command
from zerg.services.managed_local_tmux import build_tmux_set_remain_on_exit_command
from zerg.services.managed_local_tmux import normalize_tmux_session_name
from zerg.services.managed_local_tmux import validate_managed_transport
from zerg.services.runner_connection_manager import get_runner_connection_manager
from zerg.services.runner_job_dispatcher import get_runner_job_dispatcher
from zerg.session_execution_home import ManagedSessionTransport
from zerg.session_execution_home import SessionExecutionHome


class ManagedLocalLaunchError(RuntimeError):
    """Expected managed-local launch failure with user-facing detail."""

    def __init__(self, detail: str, *, status_code: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


@dataclass(frozen=True)
class ManagedLocalLaunchParams:
    owner_id: int
    runner_target: str
    cwd: str
    project: str | None = None
    git_repo: str | None = None
    git_branch: str | None = None
    display_name: str | None = None
    loop_mode: str = "manual"
    managed_transport: str = ManagedSessionTransport.TMUX.value


@dataclass(frozen=True)
class ManagedLocalLaunchResult:
    session: AgentSession
    attach_command: str


def _resolve_runner(db: Session, owner_id: int, target: str):
    if not target:
        raise ManagedLocalLaunchError("runner_target is required", status_code=400)

    if target.startswith("runner:"):
        try:
            runner_id = int(target.split(":", 1)[1])
        except ValueError as exc:
            raise ManagedLocalLaunchError(
                "runner_target must be runner:<id> or a runner name",
                status_code=400,
            ) from exc
        runner = runner_crud.get_runner(db, runner_id)
        if runner is None or runner.owner_id != owner_id:
            raise ManagedLocalLaunchError(f"Runner '{target}' not found", status_code=404)
        return runner

    runner = runner_crud.get_runner_by_name(db, owner_id, target)
    if runner is None:
        raise ManagedLocalLaunchError(f"Runner '{target}' not found", status_code=404)
    return runner


def _require_runner_ready(runner, *, owner_id: int) -> None:
    if runner.status == "revoked":
        raise ManagedLocalLaunchError(f"Runner '{runner.name}' has been revoked", status_code=409)

    connection_manager = get_runner_connection_manager()
    if not connection_manager.is_online(owner_id, runner.id):
        raise ManagedLocalLaunchError(f"Runner '{runner.name}' is offline", status_code=409)

    capabilities = runner.capabilities or []
    if "exec.full" not in capabilities:
        raise ManagedLocalLaunchError(
            f"Runner '{runner.name}' must have exec.full capability for managed local launch",
            status_code=400,
        )


def _derive_project(cwd: str, project: str | None) -> str:
    if project and project.strip():
        return project.strip()
    return Path(cwd).name or "managed-local"


def _build_entry_command(*, provider_session_id: str, display_name: str | None) -> str:
    parts = ["claude-code", "--session-id", provider_session_id]
    if display_name and display_name.strip():
        parts.extend(["-n", display_name.strip()])
    inner = "source ~/.zshrc >/dev/null 2>&1; exec " + " ".join(shlex.quote(part) for part in parts)
    return f"zsh -lc {shlex.quote(inner)}"


def _build_preflight_command(*, cwd: str) -> str:
    quoted_cwd = shlex.quote(cwd)
    checks = [
        "source ~/.zshrc >/dev/null 2>&1",
        "command -v tmux >/dev/null 2>&1 || { echo 'tmux is not installed' >&2; exit 11; }",
        "command -v claude-code >/dev/null 2>&1 || { echo 'claude-code wrapper is not available' >&2; exit 12; }",
        f"test -d {quoted_cwd} || {{ echo 'working directory does not exist' >&2; exit 13; }}",
    ]
    return f"zsh -lc {shlex.quote('; '.join(checks))}"


async def launch_managed_local_session(db: Session, params: ManagedLocalLaunchParams) -> ManagedLocalLaunchResult:
    transport = validate_managed_transport(params.managed_transport)
    if transport != ManagedSessionTransport.TMUX.value:
        raise ManagedLocalLaunchError(f"Unsupported managed transport '{transport}'", status_code=400)

    cwd = params.cwd.strip()
    if not cwd:
        raise ManagedLocalLaunchError("cwd is required", status_code=400)

    runner = _resolve_runner(db, params.owner_id, params.runner_target)
    _require_runner_ready(runner, owner_id=params.owner_id)
    dispatcher = get_runner_job_dispatcher()

    preflight_result = await dispatcher.dispatch_job(
        db=db,
        owner_id=params.owner_id,
        runner_id=runner.id,
        command=_build_preflight_command(cwd=cwd),
        timeout_secs=10,
        commis_id=None,
        run_id=None,
    )
    if not preflight_result.get("ok"):
        detail = preflight_result.get("error", {}).get("message", "Managed local preflight failed")
        raise ManagedLocalLaunchError(detail, status_code=502)

    preflight_data = preflight_result.get("data", {})
    if int(preflight_data.get("exit_code", 1)) != 0:
        stderr = (preflight_data.get("stderr") or "").strip()
        stdout = (preflight_data.get("stdout") or "").strip()
        detail = stderr or stdout or "Managed local preflight failed"
        raise ManagedLocalLaunchError(detail, status_code=400)

    session_uuid = uuid4()
    provider_session_id = str(session_uuid)
    project = _derive_project(cwd, params.project)
    display_name = (params.display_name or project).strip() or project
    managed_session_name = normalize_tmux_session_name(f"{display_name}-{session_uuid.hex[:8]}")

    session = AgentSession(
        id=session_uuid,
        provider="claude",
        environment="development",
        project=project,
        device_id=runner.name,
        cwd=cwd,
        git_repo=params.git_repo,
        git_branch=params.git_branch,
        started_at=datetime.now(timezone.utc),
        ended_at=None,
        provider_session_id=provider_session_id,
        thread_root_session_id=session_uuid,
        continued_from_session_id=None,
        continuation_kind="local",
        origin_label=runner.name,
        user_messages=0,
        assistant_messages=0,
        tool_calls=0,
        is_writable_head=1,
        is_sidechain=0,
        loop_mode=params.loop_mode,
        execution_home=SessionExecutionHome.MANAGED_LOCAL.value,
        managed_transport=transport,
        source_runner_id=runner.id,
        source_runner_name=runner.name,
        managed_session_name=managed_session_name,
    )
    db.add(session)
    db.flush()

    entry_command = _build_entry_command(provider_session_id=provider_session_id, display_name=params.display_name)
    launch_command = build_tmux_launch_command(
        session_name=managed_session_name,
        cwd=cwd,
        launch_command=entry_command,
    )
    verify_session_command = build_tmux_has_session_command(session_name=managed_session_name)
    verify_command = build_tmux_current_command_command(session_name=managed_session_name)
    preserve_failures_command = build_tmux_set_remain_on_exit_command(session_name=managed_session_name, mode="failed")

    async def _cleanup_tmux_session() -> None:
        try:
            await dispatcher.dispatch_job(
                db=db,
                owner_id=params.owner_id,
                runner_id=runner.id,
                command=build_tmux_kill_session_command(session_name=managed_session_name),
                timeout_secs=10,
                commis_id=None,
                run_id=None,
            )
        except Exception:
            return

    launch_result = await dispatcher.dispatch_job(
        db=db,
        owner_id=params.owner_id,
        runner_id=runner.id,
        command=launch_command,
        timeout_secs=20,
        commis_id=None,
        run_id=None,
    )
    if not launch_result.get("ok"):
        detail = launch_result.get("error", {}).get("message", "Managed local launch failed")
        raise ManagedLocalLaunchError(detail, status_code=502)

    launch_data = launch_result.get("data", {})
    if int(launch_data.get("exit_code", 1)) != 0:
        stderr = (launch_data.get("stderr") or "").strip()
        stdout = (launch_data.get("stdout") or "").strip()
        detail = stderr or stdout or "Managed local launcher exited non-zero"
        raise ManagedLocalLaunchError(detail, status_code=502)

    preserve_result = await dispatcher.dispatch_job(
        db=db,
        owner_id=params.owner_id,
        runner_id=runner.id,
        command=preserve_failures_command,
        timeout_secs=10,
        commis_id=None,
        run_id=None,
    )
    if not preserve_result.get("ok"):
        detail = preserve_result.get("error", {}).get("message", "Managed local pane retention setup failed")
        await _cleanup_tmux_session()
        raise ManagedLocalLaunchError(detail, status_code=502)

    preserve_data = preserve_result.get("data", {})
    if int(preserve_data.get("exit_code", 1)) != 0:
        stderr = (preserve_data.get("stderr") or "").strip()
        stdout = (preserve_data.get("stdout") or "").strip()
        detail = stderr or stdout or "Managed local pane retention setup failed"
        await _cleanup_tmux_session()
        raise ManagedLocalLaunchError(detail, status_code=502)

    verify_session_result = await dispatcher.dispatch_job(
        db=db,
        owner_id=params.owner_id,
        runner_id=runner.id,
        command=verify_session_command,
        timeout_secs=10,
        commis_id=None,
        run_id=None,
    )
    if not verify_session_result.get("ok"):
        detail = verify_session_result.get("error", {}).get("message", "Managed local session verification failed")
        await _cleanup_tmux_session()
        raise ManagedLocalLaunchError(detail, status_code=502)

    verify_session_data = verify_session_result.get("data", {})
    if int(verify_session_data.get("exit_code", 1)) != 0:
        stderr = (verify_session_data.get("stderr") or "").strip()
        stdout = (verify_session_data.get("stdout") or "").strip()
        detail = stderr or stdout or "tmux session did not start successfully"
        await _cleanup_tmux_session()
        raise ManagedLocalLaunchError(detail, status_code=502)

    verify_result = await dispatcher.dispatch_job(
        db=db,
        owner_id=params.owner_id,
        runner_id=runner.id,
        command=verify_command,
        timeout_secs=10,
        commis_id=None,
        run_id=None,
    )
    if not verify_result.get("ok"):
        detail = verify_result.get("error", {}).get("message", "Managed local session verification failed")
        await _cleanup_tmux_session()
        raise ManagedLocalLaunchError(detail, status_code=502)

    verify_data = verify_result.get("data", {})
    if int(verify_data.get("exit_code", 1)) != 0:
        stderr = (verify_data.get("stderr") or "").strip()
        stdout = (verify_data.get("stdout") or "").strip()
        detail = stderr or stdout or "tmux session did not start successfully"
        await _cleanup_tmux_session()
        raise ManagedLocalLaunchError(detail, status_code=502)

    pane_command = (verify_data.get("stdout") or "").strip().lower()
    if pane_command in {"", "bash", "sh", "zsh", "fish"}:
        await _cleanup_tmux_session()
        raise ManagedLocalLaunchError(
            f"Managed local session started an idle shell instead of Claude ({pane_command or 'empty pane'})",
            status_code=502,
        )

    if "claude" not in pane_command and "node" not in pane_command:
        await _cleanup_tmux_session()
        raise ManagedLocalLaunchError(
            f"Managed local session started an unexpected pane command ({pane_command})",
            status_code=502,
        )

    mark_managed_local_session_launched(db, session=session)
    db.commit()
    db.refresh(session)
    return ManagedLocalLaunchResult(
        session=session,
        attach_command=build_tmux_attach_command(session_name=managed_session_name),
    )


__all__ = [
    "ManagedLocalLaunchError",
    "ManagedLocalLaunchParams",
    "ManagedLocalLaunchResult",
    "launch_managed_local_session",
]
