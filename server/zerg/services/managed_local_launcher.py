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
from zerg.services.managed_local_runtime import mark_managed_local_session_launched
from zerg.services.managed_local_transport import build_managed_local_attach_command
from zerg.services.managed_provider_contracts import managed_provider_names
from zerg.services.managed_provider_contracts import require_contract_for_provider
from zerg.services.runner_connection_manager import get_runner_connection_manager
from zerg.session_loop_mode import coerce_session_loop_mode

_VALID_PROVIDERS = managed_provider_names()
_MANAGED_LOCAL_NAME_SAFE_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")
_MANAGED_LOCAL_NAME_MAX = 64

# Providers whose Machine Agent emits managed-control lease snapshots in the
# heartbeat, so the server reconciler observes channel readiness and promotes
# the launcher's birth connection detached -> attached once the bridge is up.
# Engine truth: only codex and claude leases are shipped (see
# engine/src/daemon.rs payload.managed_sessions = leases_from_observations
# (codex) + leases_from_claude_channel_observations (claude)). For these, the
# launcher births the connection ``detached`` so liveness reflects an observed
# ready channel, not a birth-time assertion. Providers WITHOUT a lease observer
# (opencode, antigravity) have no promotion path, so they must be born
# ``attached`` with a fresh health stamp — there is no later signal to flip
# them live, and the read-time freshness clamp still degrades them after the
# lease TTL if no further evidence arrives.
_HEARTBEAT_LEASE_OBSERVED_PROVIDERS = frozenset({"claude", "codex"})


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
    permission_mode: str = "bypass"


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
    label = Path(cwd).name.strip()
    if label and label != "workspace":
        return label
    return "managed-local"


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
    session_uuid = uuid4()
    project = _derive_project(cwd, params.project)
    display_name = (params.display_name or project).strip() or project
    contract = require_contract_for_provider(provider)
    managed_session_name = _build_managed_session_name(display_name, fallback=f"{provider}-{session_uuid.hex[:8]}")

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
        user_messages=0,
        assistant_messages=0,
        tool_calls=0,
        loop_mode=coerce_session_loop_mode(params.loop_mode).value,
        permission_mode="remote_approve" if str(params.permission_mode).strip() == "remote_approve" else "bypass",
    )
    db.add(session)
    db.flush()

    # Phase 2 dual-write: materialize kernel rows alongside legacy launch path.
    primary_thread = ensure_primary_thread(db, session)
    run = record_run(
        db,
        thread=primary_thread,
        provider=provider,
        host_id=source_name,
        cwd=cwd,
        launch_origin="longhouse_spawned",
    )
    connection_capabilities = contract.connection_capabilities
    # Liveness honesty: ``live_control_available`` must mean an observer
    # measured a ready control channel recently, not that the launcher
    # asserted it at row birth.
    #
    # For lease-observed providers (claude/codex) the connection is born
    # ``detached`` (reattach-available, not live). The heartbeat reconciler
    # (``upsert_managed_control_leases`` -> ``_mirror_connection_state``)
    # promotes THIS connection to ``attached`` ~1-2s later once it observes
    # the bridge ready, flipping ``live_control_available`` to true. Matching
    # on (run_id, control_plane) keeps promotion on this same row.
    #
    # For providers with no lease observer (opencode/antigravity) there is no
    # later promotion signal, so the launch IS the only readiness evidence we
    # get; birth ``attached`` with a fresh health stamp, and let the read-time
    # freshness clamp degrade it after the lease TTL if nothing else arrives.
    #
    # ``device_id`` is stamped to ``source_name`` (== the device-token id the
    # heartbeat reconciler uses) so both promotion and
    # ``mark_missing_managed_control_leases`` target this row instead of
    # leaving a NULL-device, durably-false-live orphan.
    lease_observed = provider in _HEARTBEAT_LEASE_OBSERVED_PROVIDERS
    birth_state = "detached" if lease_observed else "attached"
    connection = record_connection(
        db,
        run=run,
        control_plane=contract.control_plane,
        acquisition_kind="spawned_control",
        state=birth_state,
        external_name=managed_session_name,
        device_id=source_name or None,
        can_send_input=connection_capabilities["can_send_input"],
        can_interrupt=connection_capabilities["can_interrupt"],
        can_terminate=connection_capabilities["can_terminate"],
        can_tail_output=connection_capabilities["can_tail_output"],
        can_resume=connection_capabilities["can_resume"],
    )
    if not lease_observed:
        connection.last_health_at = datetime.now(timezone.utc)

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
