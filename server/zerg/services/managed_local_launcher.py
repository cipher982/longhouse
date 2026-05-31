"""Managed-local session launcher for native-only managed sessions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy.orm import Session

from zerg.crud import runner_crud
from zerg.models.agents import AgentSession
from zerg.services.agents.kernel_writes import ensure_primary_thread
from zerg.services.agents.kernel_writes import record_connection
from zerg.services.agents.kernel_writes import record_run
from zerg.services.agents.kernel_writes import record_thread_alias
from zerg.services.managed_local_runtime import mark_managed_local_session_launched
from zerg.services.managed_local_transport import build_managed_local_attach_command
from zerg.services.managed_provider_contracts import managed_provider_names
from zerg.services.managed_provider_contracts import require_contract_for_provider
from zerg.services.runner_connection_manager import get_runner_connection_manager
from zerg.session_execution_home import SessionExecutionHome
from zerg.session_loop_mode import coerce_session_loop_mode

_VALID_PROVIDERS = managed_provider_names()
_MANAGED_LOCAL_NAME_SAFE_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")
_MANAGED_LOCAL_NAME_MAX = 64


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
    provider: str = "claude"
    project: str | None = None
    git_repo: str | None = None
    git_branch: str | None = None
    display_name: str | None = None
    loop_mode: str = "assist"
    machine_name: str | None = None
    native_claude_channels_available: bool | None = None
    claude_launch_env: dict[str, str] | None = None
    require_runner_ready: bool = False


@dataclass(frozen=True)
class ManagedLocalLaunchResult:
    session: AgentSession
    attach_command: str


def _resolve_runner(db: Session, owner_id: int, target: str, *, required: bool = True):
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
            if not required:
                return None
            raise ManagedLocalLaunchError(f"Runner '{target}' not found", status_code=404)
        return runner

    runner = runner_crud.get_runner_by_name(db, owner_id, target)
    if runner is None:
        if not required:
            return None
        raise ManagedLocalLaunchError(f"Runner '{target}' not found", status_code=404)
    return runner


def _require_runner_ready(runner, *, owner_id: int) -> None:
    if runner.status == "revoked":
        raise ManagedLocalLaunchError(
            f"Remote command Runner '{runner.name}' has been revoked. This is separate from the Machine Agent " "that ships transcripts.",
            status_code=409,
        )

    connection_manager = get_runner_connection_manager()
    if not connection_manager.is_online(owner_id, runner.id):
        raise ManagedLocalLaunchError(
            f"Remote command Runner '{runner.name}' is offline. This blocks browser-launched remote execution, "
            "not local transcript shipping. Start the Runner or launch from the target machine.",
            status_code=409,
        )

    capabilities = runner.capabilities or []
    if "exec.full" not in capabilities:
        raise ManagedLocalLaunchError(
            f"Remote command Runner '{runner.name}' must have exec.full capability for " "browser-launched managed sessions",
            status_code=400,
        )


def _runner_remote_control_id(runner) -> int | None:
    if runner is None:
        return None
    if runner.status == "revoked":
        return None
    if "exec.full" not in (runner.capabilities or []):
        return None
    return int(runner.id)


def _derive_project(cwd: str, project: str | None) -> str:
    if project and project.strip():
        return project.strip()
    return Path(cwd).name or "managed-local"


def _build_managed_session_name(seed: str, *, fallback: str) -> str:
    cleaned = _MANAGED_LOCAL_NAME_SAFE_CHARS.sub("-", str(seed or "").strip()).strip("-")
    if not cleaned:
        cleaned = fallback
    return cleaned[:_MANAGED_LOCAL_NAME_MAX].rstrip("-")


def launch_managed_local_session_sync(db: Session, params: ManagedLocalLaunchParams) -> ManagedLocalLaunchResult:
    provider = params.provider or "claude"
    if provider not in _VALID_PROVIDERS:
        raise ManagedLocalLaunchError(f"Unsupported provider '{provider}' for managed local", status_code=400)

    if not str(params.machine_name or "").strip():
        raise ManagedLocalLaunchError(
            "Browser-launched managed-local sessions were removed. Launch from the target machine with "
            "`longhouse claude`, `longhouse codex`, `longhouse opencode`, or `longhouse agy`.",
            status_code=410,
        )

    if provider == "claude" and params.native_claude_channels_available is False:
        raise ManagedLocalLaunchError(
            "Native Claude channels are unavailable on this machine. Longhouse now requires " "the local Claude channel bridge.",
            status_code=412,
        )

    cwd = params.cwd.strip()
    if not cwd:
        raise ManagedLocalLaunchError("cwd is required", status_code=400)

    runner = _resolve_runner(db, params.owner_id, params.runner_target, required=params.require_runner_ready)
    if params.require_runner_ready:
        _require_runner_ready(runner, owner_id=params.owner_id)
    source_name = str(getattr(runner, "name", "") or params.runner_target).strip()
    # Codex managed control is owned by the Machine Agent channel. Keep Runner
    # association only for legacy transports that still dispatch through Runner.
    source_runner_id = None if provider == "codex" else _runner_remote_control_id(runner)

    session_uuid = uuid4()
    provider_session_id = str(session_uuid)
    project = _derive_project(cwd, params.project)
    display_name = (params.display_name or project).strip() or project
    contract = require_contract_for_provider(provider)
    transport = contract.managed_transport

    session = AgentSession(
        id=session_uuid,
        provider=provider,
        environment="development",
        project=project,
        device_id=source_name,
        cwd=cwd,
        git_repo=params.git_repo,
        git_branch=params.git_branch,
        started_at=datetime.now(timezone.utc),
        ended_at=None,
        provider_session_id=provider_session_id,
        thread_root_session_id=session_uuid,
        continued_from_session_id=None,
        continuation_kind="local",
        origin_label=source_name,
        user_messages=0,
        assistant_messages=0,
        tool_calls=0,
        is_writable_head=1,
        is_sidechain=0,
        loop_mode=coerce_session_loop_mode(params.loop_mode).value,
        execution_home=SessionExecutionHome.MANAGED_LOCAL.value,
        managed_transport=transport.value,
        source_runner_id=source_runner_id,
        source_runner_name=source_name,
        managed_session_name=_build_managed_session_name(display_name, fallback=f"{provider}-{session_uuid.hex[:8]}"),
    )
    db.add(session)
    db.flush()

    # Phase 2 dual-write: materialize kernel rows alongside legacy launch path.
    primary_thread = ensure_primary_thread(db, session)
    record_thread_alias(
        db,
        thread=primary_thread,
        provider=provider,
        alias_kind="provider_session_id",
        alias_value=provider_session_id,
    )
    run = record_run(
        db,
        thread=primary_thread,
        provider=provider,
        host_id=source_name,
        cwd=cwd,
        launch_origin="longhouse_spawned",
    )
    connection_capabilities = contract.connection_capabilities
    record_connection(
        db,
        run=run,
        control_plane=contract.control_plane,
        acquisition_kind="spawned_control",
        state="attached",
        external_name=session.managed_session_name,
        can_send_input=connection_capabilities["can_send_input"],
        can_interrupt=connection_capabilities["can_interrupt"],
        can_terminate=connection_capabilities["can_terminate"],
        can_tail_output=connection_capabilities["can_tail_output"],
        can_resume=connection_capabilities["can_resume"],
    )

    mark_managed_local_session_launched(db, session=session)
    attach_command = str(build_managed_local_attach_command(session=session) or "")
    db.commit()
    db.refresh(session)
    return ManagedLocalLaunchResult(session=session, attach_command=attach_command)


async def launch_managed_local_session(db: Session, params: ManagedLocalLaunchParams) -> ManagedLocalLaunchResult:
    return launch_managed_local_session_sync(db, params)


__all__ = [
    "ManagedLocalLaunchError",
    "ManagedLocalLaunchParams",
    "ManagedLocalLaunchResult",
    "launch_managed_local_session",
    "launch_managed_local_session_sync",
]
