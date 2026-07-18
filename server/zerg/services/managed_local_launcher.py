"""Managed-local session launcher for native-only managed sessions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL
from uuid import UUID
from uuid import uuid4
from uuid import uuid5

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
from zerg.services.session_launch_provenance import sanitize_launch_provenance
from zerg.session_loop_mode import coerce_session_loop_mode

_VALID_PROVIDERS = managed_provider_names()
_MANAGED_LOCAL_NAME_SAFE_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")
_MANAGED_LOCAL_NAME_MAX = 64

# Providers whose Machine Agent emits managed-control lease snapshots in the
# heartbeat, so the server reconciler observes channel readiness and promotes
# the launcher's birth connection detached -> attached once the bridge is up.
# Engine truth: codex, claude, and opencode leases are shipped (see
# engine/src/daemon.rs payload.managed_sessions = leases_from_observations
# (codex) + leases_from_claude_channel_observations (claude) +
# leases_from_opencode_server_observations (opencode)). For these, the launcher
# births the connection ``detached`` so liveness reflects an observed ready
# channel, not a birth-time assertion — important for OpenCode, where the server
# bridge can fail to start AFTER the API session is created, and a birth-time
# ``attached`` would briefly claim live control with no server. Antigravity has
# no control lease observer. Its typed hook readiness is still
# shadow evidence in Phase 2, so launch must remain detached/send-disabled
# until a later authority cutover explicitly promotes it from fresh hook proof.
_HEARTBEAT_LEASE_OBSERVED_PROVIDERS = frozenset({"claude", "codex", "opencode", "cursor"})


def managed_provider_has_lease_observer(provider: str | None) -> bool:
    return str(provider or "").strip().lower() in _HEARTBEAT_LEASE_OBSERVED_PROVIDERS


def managed_provider_requires_readiness_proof(provider: str | None) -> bool:
    """Whether launch alone is insufficient to grant the send capability."""

    return str(provider or "").strip().lower() == "antigravity"


def managed_local_run_id_for_session(session_id: UUID | str) -> UUID:
    return uuid5(NAMESPACE_URL, f"longhouse:managed-local-run:{session_id}")


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
    launch_actor: str | None = None
    launch_surface: str | None = None
    # Optional client-minted identity for Degraded Helm: retries/convergence
    # must reuse this UUID instead of minting a replacement session.
    session_id: UUID | None = None


@dataclass(frozen=True)
class ManagedLocalLaunchResult:
    session: AgentSession
    attach_command: str


@dataclass(frozen=True)
class ManagedLocalLaunchPlan:
    session_id: UUID
    provider: str
    provider_session_id: str | None
    source_name: str
    source_runner_id: int | None
    cwd: str
    project: str
    display_name: str
    managed_session_name: str
    loop_mode: str
    permission_mode: str
    launch_actor: str | None
    launch_surface: str | None
    managed_transport: str
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


def _initial_provider_session_id_for_spawn(provider: str) -> str | None:
    if provider == "claude":
        return str(uuid4())
    return None


def _build_attach_command_for_plan(plan: ManagedLocalLaunchPlan) -> str:
    session_fixture = SimpleNamespace(
        id=plan.session_id,
        managed_transport=plan.managed_transport,
        provider_session_id=plan.provider_session_id,
        cwd=plan.cwd,
        permission_mode=plan.permission_mode,
    )
    return str(build_managed_local_attach_command(session=session_fixture) or "")


def build_managed_local_launch_plan(
    params: ManagedLocalLaunchParams,
    *,
    runner=None,
    session_id: UUID | None = None,
) -> ManagedLocalLaunchPlan:
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

    source_name = str(getattr(runner, "name", "") or params.runner_target).strip()
    plan_session_id = session_id or params.session_id or uuid4()
    provider_session_id = _initial_provider_session_id_for_spawn(provider)
    project = _derive_project(cwd, params.project)
    display_name = (params.display_name or project).strip() or project
    contract = require_contract_for_provider(provider)
    managed_session_name = _build_managed_session_name(display_name, fallback=f"{provider}-{plan_session_id.hex[:8]}")
    permission_mode = "remote_approve" if str(params.permission_mode).strip() == "remote_approve" else "bypass"
    launch_actor, launch_surface = sanitize_launch_provenance(
        origin_kind=None,
        launch_actor=params.launch_actor,
        launch_surface=params.launch_surface,
    )
    loop_mode = coerce_session_loop_mode(params.loop_mode).value
    plan = ManagedLocalLaunchPlan(
        session_id=plan_session_id,
        provider=provider,
        provider_session_id=provider_session_id,
        source_name=source_name,
        source_runner_id=_runner_remote_control_id(runner),
        cwd=cwd,
        project=project,
        display_name=display_name,
        managed_session_name=managed_session_name,
        loop_mode=loop_mode,
        permission_mode=permission_mode,
        launch_actor=launch_actor,
        launch_surface=launch_surface,
        managed_transport=contract.managed_transport.value,
        attach_command="",
    )
    return replace(plan, attach_command=_build_attach_command_for_plan(plan))


def resolve_managed_local_launch_runner(db: Session, params: ManagedLocalLaunchParams):
    runner = _resolve_runner(db, params.owner_id, params.runner_target, required=params.require_runner_ready)
    if params.require_runner_ready:
        _require_runner_ready(runner, owner_id=params.owner_id)
    return runner


def materialize_managed_local_launch_plan_sync(
    db: Session,
    plan: ManagedLocalLaunchPlan,
    *,
    git_repo: str | None = None,
    git_branch: str | None = None,
    started_at: datetime | None = None,
) -> AgentSession:
    existing = db.get(AgentSession, plan.session_id)
    if existing is not None:
        return existing

    contract = require_contract_for_provider(plan.provider)
    launched_at = started_at or datetime.now(timezone.utc)

    session = AgentSession(
        id=plan.session_id,
        provider=plan.provider,
        environment="development",
        project=plan.project,
        device_id=plan.source_name,
        cwd=plan.cwd,
        git_repo=git_repo,
        git_branch=git_branch,
        started_at=launched_at,
        ended_at=None,
        user_messages=0,
        assistant_messages=0,
        tool_calls=0,
        loop_mode=plan.loop_mode,
        permission_mode=plan.permission_mode,
        launch_actor=plan.launch_actor,
        launch_surface=plan.launch_surface,
    )
    db.add(session)
    db.flush()

    # Phase 2 dual-write: materialize kernel rows alongside legacy launch path.
    primary_thread = ensure_primary_thread(db, session)
    if plan.provider_session_id:
        record_thread_alias(
            db,
            thread=primary_thread,
            provider=plan.provider,
            alias_kind="provider_session_id",
            alias_value=plan.provider_session_id,
        )
    run = record_run(
        db,
        thread=primary_thread,
        provider=plan.provider,
        host_id=plan.source_name,
        cwd=plan.cwd,
        launch_origin="longhouse_spawned",
        run_id=managed_local_run_id_for_session(plan.session_id),
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
    # Antigravity launch proves only that the binary was dispatched. It does
    # not prove the hook inbox can receive input, so Phase 2 keeps that
    # connection detached and send-disabled while typed readiness runs in
    # shadow mode. A later reducer cutover may promote it from fresh hook proof.
    #
    # ``device_id`` is stamped to ``source_name`` (== the device-token id the
    # heartbeat reconciler uses) so both promotion and
    # ``mark_missing_managed_control_leases`` target this row instead of
    # leaving a NULL-device, durably-false-live orphan.
    lease_observed = plan.provider in _HEARTBEAT_LEASE_OBSERVED_PROVIDERS
    requires_hook_readiness = managed_provider_requires_readiness_proof(plan.provider)
    birth_state = "detached" if lease_observed or requires_hook_readiness else "attached"
    connection = record_connection(
        db,
        run=run,
        control_plane=contract.control_plane,
        acquisition_kind="spawned_control",
        state=birth_state,
        external_name=plan.managed_session_name,
        device_id=plan.source_name or None,
        can_send_input=(0 if requires_hook_readiness else connection_capabilities["can_send_input"]),
        can_interrupt=connection_capabilities["can_interrupt"],
        can_terminate=connection_capabilities["can_terminate"],
        can_tail_output=connection_capabilities["can_tail_output"],
        can_resume=connection_capabilities["can_resume"],
    )
    if not lease_observed and not requires_hook_readiness:
        connection.last_health_at = datetime.now(timezone.utc)

    mark_managed_local_session_launched(db, session=session)
    return session


def launch_managed_local_session_sync(db: Session, params: ManagedLocalLaunchParams) -> ManagedLocalLaunchResult:
    runner = resolve_managed_local_launch_runner(db, params)
    plan = build_managed_local_launch_plan(params, runner=runner)
    session = materialize_managed_local_launch_plan_sync(
        db,
        plan,
        git_repo=params.git_repo,
        git_branch=params.git_branch,
    )
    db.commit()
    db.refresh(session)
    return ManagedLocalLaunchResult(session=session, attach_command=plan.attach_command)


async def launch_managed_local_session(db: Session, params: ManagedLocalLaunchParams) -> ManagedLocalLaunchResult:
    return launch_managed_local_session_sync(db, params)


__all__ = [
    "ManagedLocalLaunchError",
    "ManagedLocalLaunchParams",
    "ManagedLocalLaunchPlan",
    "ManagedLocalLaunchResult",
    "build_managed_local_launch_plan",
    "launch_managed_local_session",
    "launch_managed_local_session_sync",
    "materialize_managed_local_launch_plan_sync",
    "resolve_managed_local_launch_runner",
]
