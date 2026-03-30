"""Managed local session launcher.

Phase 2 keeps this intentionally small:
- resolve a reachable runner owned by the current user
- create a managed-local AgentSession row
- launch the provider CLI on that runner using the provider's managed transport
- verify the managed runtime is reachable

No chat routing or Loop behavior changes live here yet.
"""

from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy.orm import Session

from zerg.auth.managed_local_hook_tokens import issue_managed_local_hook_token
from zerg.crud import runner_crud
from zerg.models.agents import AgentSession
from zerg.services.managed_local_runtime import mark_managed_local_session_launched
from zerg.services.managed_local_tmux import build_managed_local_shell_prelude
from zerg.services.managed_local_tmux import normalize_tmux_session_name
from zerg.services.managed_local_transport import build_managed_local_attach_command
from zerg.services.managed_local_transport import build_managed_local_launch_transport_plan
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
    provider: str = "claude"
    project: str | None = None
    git_repo: str | None = None
    git_branch: str | None = None
    display_name: str | None = None
    loop_mode: str = "manual"
    hook_url: str | None = None
    hook_token: str | None = None
    machine_name: str | None = None
    native_claude_channels_available: bool | None = None
    claude_launch_env: dict[str, str] | None = None


@dataclass(frozen=True)
class ManagedLocalLaunchResult:
    session: AgentSession
    attach_command: str


_MANAGED_LOCAL_TMUX_TMPDIR_MARKER = "__LONGHOUSE_TMUX_TMPDIR__="
_MANAGED_LOCAL_SHELL_COMMANDS = {"", "bash", "sh", "zsh", "fish"}
_MANAGED_LOCAL_RUNTIME_FAILURE_STATUS = 424
_MANAGED_LOCAL_CAPTURE_ERROR_SNIPPETS = (
    "command not found: claude-code",
    "command not found: claude",
    "command not found: codex",
    "aws auth refresh timed out",
    "not logged in",
    "pane is dead",
    "permission denied",
)
_MANAGED_LOCAL_CAPTURE_READY_SNIPPETS = {
    "claude": ("claude",),
    "codex": ("codex",),
}
_MANAGED_LOCAL_VERIFY_ATTEMPTS = 40
_MANAGED_LOCAL_VERIFY_INTERVAL_SECS = 0.25
_VALID_PROVIDERS = {"claude", "codex"}
_PROVIDER_DISPLAY_NAMES = {"claude": "Claude", "codex": "Codex"}
_PROVIDER_PANE_COMMANDS = {
    "claude": {"claude", "node"},
    "codex": {"codex", "node"},
}
_PROVIDER_HOOK_FILES = {
    "claude": {
        "script": "${HOME}/.claude/hooks/longhouse-hook.sh",
        "config": "${HOME}/.claude/settings.json",
        "marker": "longhouse-hook.sh",
    },
    "codex": {
        "script": "${HOME}/.codex/hooks/longhouse-codex-hook.sh",
        "config": "${HOME}/.codex/hooks.json",
        "marker": "longhouse-codex-hook.sh",
    },
}
_ALLOWED_CLAUDE_LAUNCH_ENV_KEYS = frozenset(
    {
        "CLAUDE_CODE_USE_BEDROCK",
        "AWS_PROFILE",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "ANTHROPIC_MODEL",
    }
)


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


def _sanitize_claude_launch_env(raw: dict[str, str] | None) -> dict[str, str]:
    sanitized: dict[str, str] = {}
    if not raw:
        return sanitized
    for key, value in raw.items():
        normalized_key = str(key or "").strip()
        if normalized_key not in _ALLOWED_CLAUDE_LAUNCH_ENV_KEYS:
            continue
        normalized_value = str(value or "").strip()
        if not normalized_value:
            continue
        sanitized[normalized_key] = normalized_value
    return sanitized


def _build_entry_command(
    *,
    provider: str,
    provider_session_id: str,
    display_name: str | None,
    managed_session_name: str | None = None,
    hook_url: str | None = None,
    hook_token: str | None = None,
    claude_launch_env: dict[str, str] | None = None,
) -> str:
    env_exports = [f"export LONGHOUSE_SESSION_ID={shlex.quote(provider_session_id)}"]
    if hook_url and hook_url.strip():
        env_exports.append(f"export LONGHOUSE_HOOK_URL={shlex.quote(hook_url.strip())}")
    if hook_token and hook_token.strip():
        env_exports.append(f"export LONGHOUSE_HOOK_TOKEN={shlex.quote(hook_token.strip())}")

    if provider == "codex":
        return _build_codex_entry_command(
            managed_session_id=provider_session_id,
            managed_session_name=managed_session_name or provider_session_id,
            env_exports=env_exports,
        )
    for key, value in _sanitize_claude_launch_env(claude_launch_env).items():
        env_exports.append(f"export {key}={shlex.quote(value)}")
    parts = ["claude", "--session-id", provider_session_id]
    if display_name and display_name.strip():
        parts.extend(["-n", display_name.strip()])
    inner = "; ".join(
        [
            *env_exports,
            build_managed_local_shell_prelude(
                require_tmux=False,
                required_commands=("claude",),
            ),
            "exec " + " ".join(shlex.quote(part) for part in parts),
        ]
    )
    return f"zsh -lc {shlex.quote(inner)}"


def _build_codex_entry_command(
    *,
    managed_session_id: str,
    managed_session_name: str,
    env_exports: list[str] | None = None,
) -> str:
    """Build the entry command for a managed-local Codex session.

    Codex still starts as a local TUI on the runner, but Longhouse can later
    drive it through the codex bridge once the managed session is up.
    """
    del managed_session_name
    exports = list(env_exports or [f"export LONGHOUSE_SESSION_ID={shlex.quote(managed_session_id)}"])
    inner = "; ".join(
        [
            *exports,
            build_managed_local_shell_prelude(
                require_tmux=False,
                required_commands=("codex",),
            ),
            "exec codex --enable codex_hooks --no-alt-screen",
        ]
    )
    return f"zsh -lc {shlex.quote(inner)}"


def _build_preflight_command(
    *,
    provider: str,
    cwd: str,
    require_tmux: bool = True,
) -> str:
    quoted_cwd = shlex.quote(cwd)
    cli_name = "codex" if provider == "codex" else "claude"
    checks = [
        build_managed_local_shell_prelude(
            require_tmux=require_tmux,
            required_commands=(cli_name,),
        ),
        f"command -v {cli_name} >/dev/null 2>&1 || {{ echo '{cli_name} is not available' >&2; exit 12; }}",
        f"test -d {quoted_cwd} || {{ echo 'working directory does not exist' >&2; exit 13; }}",
        f'printf {shlex.quote(_MANAGED_LOCAL_TMUX_TMPDIR_MARKER + "%s\\n")} "${{TMUX_TMPDIR:-}}"',
    ]
    return f"zsh -lc {shlex.quote('; '.join(checks))}"


def _build_hooks_ensure_command(*, provider: str) -> str:
    hook_files = _PROVIDER_HOOK_FILES.get(provider, _PROVIDER_HOOK_FILES["claude"])
    shell_script_path = hook_files["script"]
    shell_config_path = hook_files["config"]
    quoted_marker = shlex.quote(hook_files["marker"])
    hook_present_checks = [
        f'test -x "{shell_script_path}"',
        f'test -f "{shell_config_path}"',
        f'grep -q {quoted_marker} "{shell_config_path}"',
    ]
    install_checks = [
        build_managed_local_shell_prelude(
            require_tmux=False,
            required_commands=("longhouse",),
        ),
        "command -v longhouse >/dev/null 2>&1 || { echo 'longhouse is not available' >&2; exit 14; }",
        "longhouse connect --hooks-only >/dev/null 2>&1 || { echo 'failed to install Longhouse hooks' >&2; exit 15; }",
        f"test -x \"{shell_script_path}\" || {{ echo 'Longhouse hook script missing after install' >&2; exit 16; }}",
        f"test -f \"{shell_config_path}\" || {{ echo 'Longhouse hook config missing after install' >&2; exit 17; }}",
        (
            f'grep -q {quoted_marker} "{shell_config_path}" '
            "|| { echo 'Longhouse hook config is missing the expected hook entry' >&2; exit 18; }"
        ),
    ]
    command = " && ".join(hook_present_checks) + f" || {{ {'; '.join(install_checks)}; }}"
    return f"zsh -lc {shlex.quote(command)}"


def _extract_tmux_tmpdir(preflight_stdout: str | None) -> str | None:
    for line in str(preflight_stdout or "").splitlines():
        if not line.startswith(_MANAGED_LOCAL_TMUX_TMPDIR_MARKER):
            continue
        raw = line[len(_MANAGED_LOCAL_TMUX_TMPDIR_MARKER) :].strip()
        return raw or None
    return None


def _capture_text_indicates_provider_ready(*, provider: str, capture_text: str) -> bool:
    capture_lower = str(capture_text or "").strip().lower()
    if not capture_lower:
        return False
    return any(marker in capture_lower for marker in _MANAGED_LOCAL_CAPTURE_READY_SNIPPETS.get(provider, ()))


def _pane_command_matches_provider(*, pane_command: str, expected_pane_commands: set[str]) -> bool:
    normalized = str(pane_command or "").strip().lower()
    if not normalized:
        return False
    return normalized in expected_pane_commands


def _pane_command_is_bootstrap_script(*, pane_command: str, managed_session_name: str) -> bool:
    normalized = str(pane_command or "").strip().lower()
    if not normalized:
        return False
    bootstrap_name = f"longhouse-managed-{managed_session_name}.zsh".lower()
    return normalized == bootstrap_name


async def launch_managed_local_session(db: Session, params: ManagedLocalLaunchParams) -> ManagedLocalLaunchResult:
    provider = params.provider or "claude"
    if provider not in _VALID_PROVIDERS:
        raise ManagedLocalLaunchError(f"Unsupported provider '{provider}' for managed local", status_code=400)
    transport = ManagedSessionTransport.for_provider(
        provider,
        machine_name=params.machine_name,
        native_claude_channels_available=params.native_claude_channels_available,
    )
    provider_name = _PROVIDER_DISPLAY_NAMES.get(provider, provider)

    cwd = params.cwd.strip()
    if not cwd:
        raise ManagedLocalLaunchError("cwd is required", status_code=400)

    # Native local transports start on the caller's device — no runner
    # dispatch needed up front. Only tmux transport launches via the runner.
    runner = None
    managed_tmux_tmpdir = None
    if transport == ManagedSessionTransport.TMUX:
        runner = _resolve_runner(db, params.owner_id, params.runner_target)
        _require_runner_ready(runner, owner_id=params.owner_id)
        dispatcher = get_runner_job_dispatcher()

        preflight_result = await dispatcher.dispatch_job(
            db=db,
            owner_id=params.owner_id,
            runner_id=runner.id,
            command=_build_preflight_command(
                provider=provider,
                cwd=cwd,
                require_tmux=True,
            ),
            timeout_secs=10,
            commis_id=None,
            run_id=None,
        )
        if not preflight_result.get("ok"):
            detail = preflight_result.get("error", {}).get("message", "Managed local preflight failed")
            raise ManagedLocalLaunchError(detail, status_code=_MANAGED_LOCAL_RUNTIME_FAILURE_STATUS)

        preflight_data = preflight_result.get("data", {})
        if int(preflight_data.get("exit_code", 1)) != 0:
            stderr = (preflight_data.get("stderr") or "").strip()
            stdout = (preflight_data.get("stdout") or "").strip()
            detail = stderr or stdout or "Managed local preflight failed"
            raise ManagedLocalLaunchError(detail, status_code=400)
        managed_tmux_tmpdir = _extract_tmux_tmpdir(preflight_data.get("stdout"))

        hooks_ensure_result = await dispatcher.dispatch_job(
            db=db,
            owner_id=params.owner_id,
            runner_id=runner.id,
            command=_build_hooks_ensure_command(provider=provider),
            timeout_secs=30,
            commis_id=None,
            run_id=None,
        )
        if not hooks_ensure_result.get("ok"):
            detail = hooks_ensure_result.get("error", {}).get("message", "Managed local hook installation failed")
            raise ManagedLocalLaunchError(detail, status_code=_MANAGED_LOCAL_RUNTIME_FAILURE_STATUS)

        hooks_ensure_data = hooks_ensure_result.get("data", {})
        if int(hooks_ensure_data.get("exit_code", 1)) != 0:
            stderr = (hooks_ensure_data.get("stderr") or "").strip()
            stdout = (hooks_ensure_data.get("stdout") or "").strip()
            detail = stderr or stdout or "Managed local hook installation failed"
            raise ManagedLocalLaunchError(detail, status_code=_MANAGED_LOCAL_RUNTIME_FAILURE_STATUS)
    else:
        # Native local transports launch on the caller's device, so
        # `/this-device` can continue even before that device has a persisted
        # Runner row. The generic `/managed-local` route still takes an
        # explicit runner target and should keep failing if it cannot be
        # resolved.
        if params.machine_name:
            try:
                runner = _resolve_runner(db, params.owner_id, params.runner_target)
            except ManagedLocalLaunchError as exc:
                if exc.status_code != 404:
                    raise
        else:
            runner = _resolve_runner(db, params.owner_id, params.runner_target)

    session_uuid = uuid4()
    provider_session_id = str(session_uuid)
    project = _derive_project(cwd, params.project)
    display_name = (params.display_name or project).strip() or project
    managed_session_name = normalize_tmux_session_name(f"{display_name}-{session_uuid.hex[:8]}")
    runner_name = runner.name if runner else (params.machine_name or "unknown")
    runner_id = runner.id if runner else None
    hook_token = params.hook_token
    if not hook_token and params.hook_url:
        hook_token = issue_managed_local_hook_token(
            owner_id=params.owner_id,
            session_id=provider_session_id,
            project=project,
            device_id=runner_name,
        )

    entry_command = _build_entry_command(
        provider=provider,
        provider_session_id=provider_session_id,
        display_name=params.display_name,
        managed_session_name=managed_session_name,
        hook_url=params.hook_url,
        hook_token=hook_token,
        claude_launch_env=params.claude_launch_env,
    )
    session = AgentSession(
        id=session_uuid,
        provider=provider,
        environment="development",
        project=project,
        device_id=runner_name,
        cwd=cwd,
        git_repo=params.git_repo,
        git_branch=params.git_branch,
        started_at=datetime.now(timezone.utc),
        ended_at=None,
        provider_session_id=provider_session_id,
        thread_root_session_id=session_uuid,
        continued_from_session_id=None,
        continuation_kind="local",
        origin_label=runner_name,
        user_messages=0,
        assistant_messages=0,
        tool_calls=0,
        is_writable_head=1,
        is_sidechain=0,
        loop_mode=params.loop_mode,
        execution_home=SessionExecutionHome.MANAGED_LOCAL.value,
        managed_transport=transport.value,
        source_runner_id=runner_id,
        source_runner_name=runner_name,
        managed_session_name=managed_session_name,
        managed_tmux_tmpdir=managed_tmux_tmpdir,
    )
    db.add(session)
    db.flush()

    if transport == ManagedSessionTransport.CODEX_APP_SERVER:
        mark_managed_local_session_launched(db, session=session)
        db.commit()
        db.refresh(session)
        return ManagedLocalLaunchResult(session=session, attach_command="")

    if transport == ManagedSessionTransport.CLAUDE_CHANNEL_BRIDGE:
        mark_managed_local_session_launched(db, session=session)
        db.commit()
        db.refresh(session)
        return ManagedLocalLaunchResult(
            session=session,
            attach_command=str(build_managed_local_attach_command(session=session) or ""),
        )

    transport_plan = build_managed_local_launch_transport_plan(
        session_name=managed_session_name,
        cwd=cwd,
        entry_command=entry_command,
        tmux_tmpdir=managed_tmux_tmpdir,
    )

    async def _cleanup_tmux_session() -> None:
        cleanup_command = transport_plan.cleanup_command
        if not cleanup_command:
            return
        try:
            await dispatcher.dispatch_job(
                db=db,
                owner_id=params.owner_id,
                runner_id=runner.id,
                command=cleanup_command,
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
        command=transport_plan.launch_command,
        timeout_secs=20,
        commis_id=None,
        run_id=None,
    )
    if not launch_result.get("ok"):
        detail = launch_result.get("error", {}).get("message", "Managed local launch failed")
        raise ManagedLocalLaunchError(detail, status_code=_MANAGED_LOCAL_RUNTIME_FAILURE_STATUS)

    launch_data = launch_result.get("data", {})
    if int(launch_data.get("exit_code", 1)) != 0:
        stderr = (launch_data.get("stderr") or "").strip()
        stdout = (launch_data.get("stdout") or "").strip()
        detail = stderr or stdout or "Managed local launcher exited non-zero"
        raise ManagedLocalLaunchError(detail, status_code=_MANAGED_LOCAL_RUNTIME_FAILURE_STATUS)

    verify_session_result = await dispatcher.dispatch_job(
        db=db,
        owner_id=params.owner_id,
        runner_id=runner.id,
        command=transport_plan.verify_session_command,
        timeout_secs=10,
        commis_id=None,
        run_id=None,
    )
    if not verify_session_result.get("ok"):
        detail = verify_session_result.get("error", {}).get("message", "Managed local session verification failed")
        await _cleanup_tmux_session()
        raise ManagedLocalLaunchError(detail, status_code=_MANAGED_LOCAL_RUNTIME_FAILURE_STATUS)

    verify_session_data = verify_session_result.get("data", {})
    if int(verify_session_data.get("exit_code", 1)) != 0:
        stderr = (verify_session_data.get("stderr") or "").strip()
        stdout = (verify_session_data.get("stdout") or "").strip()
        detail = stderr or stdout or "tmux session did not start successfully"
        await _cleanup_tmux_session()
        raise ManagedLocalLaunchError(detail, status_code=_MANAGED_LOCAL_RUNTIME_FAILURE_STATUS)

    def _finish_launch() -> ManagedLocalLaunchResult:
        mark_managed_local_session_launched(db, session=session)
        db.commit()
        db.refresh(session)
        return ManagedLocalLaunchResult(
            session=session,
            attach_command=str(transport_plan.attach_command or ""),
        )

    if provider == "codex":
        # Managed-local Codex is terminal-first. Waiting for the full Codex UI
        # to finish booting also waits on user MCP startup, which can add
        # double-digit seconds before the attach command is returned.
        return _finish_launch()

    expected_pane_commands = _PROVIDER_PANE_COMMANDS.get(provider, _PROVIDER_PANE_COMMANDS["claude"])

    for attempt in range(_MANAGED_LOCAL_VERIFY_ATTEMPTS):
        verify_result = await dispatcher.dispatch_job(
            db=db,
            owner_id=params.owner_id,
            runner_id=runner.id,
            command=str(transport_plan.verify_command or ""),
            timeout_secs=10,
            commis_id=None,
            run_id=None,
        )
        if not verify_result.get("ok"):
            detail = verify_result.get("error", {}).get("message", "Managed local session verification failed")
            await _cleanup_tmux_session()
            raise ManagedLocalLaunchError(detail, status_code=_MANAGED_LOCAL_RUNTIME_FAILURE_STATUS)

        verify_data = verify_result.get("data", {})
        if int(verify_data.get("exit_code", 1)) != 0:
            stderr = (verify_data.get("stderr") or "").strip()
            stdout = (verify_data.get("stdout") or "").strip()
            detail = stderr or stdout or "tmux session did not start successfully"
            await _cleanup_tmux_session()
            raise ManagedLocalLaunchError(detail, status_code=_MANAGED_LOCAL_RUNTIME_FAILURE_STATUS)

        pane_command = (verify_data.get("stdout") or "").strip().lower()
        if _pane_command_matches_provider(pane_command=pane_command, expected_pane_commands=expected_pane_commands):
            break

        if pane_command not in _MANAGED_LOCAL_SHELL_COMMANDS and not _pane_command_is_bootstrap_script(
            pane_command=pane_command,
            managed_session_name=managed_session_name,
        ):
            await _cleanup_tmux_session()
            raise ManagedLocalLaunchError(
                f"Managed local session started an unexpected pane command ({pane_command})",
                status_code=_MANAGED_LOCAL_RUNTIME_FAILURE_STATUS,
            )

        capture_result = await dispatcher.dispatch_job(
            db=db,
            owner_id=params.owner_id,
            runner_id=runner.id,
            command=str(transport_plan.capture_command or ""),
            timeout_secs=10,
            commis_id=None,
            run_id=None,
        )
        capture_data = capture_result.get("data", {}) if capture_result.get("ok") else {}
        capture_text = ((capture_data.get("stdout") or "") + "\n" + (capture_data.get("stderr") or "")).strip()
        capture_lower = capture_text.lower()

        for marker in _MANAGED_LOCAL_CAPTURE_ERROR_SNIPPETS:
            if marker in capture_lower:
                await _cleanup_tmux_session()
                raise ManagedLocalLaunchError(
                    f"Managed local session failed to start {provider_name}: {capture_text or marker}",
                    status_code=_MANAGED_LOCAL_RUNTIME_FAILURE_STATUS,
                )

        if _capture_text_indicates_provider_ready(provider=provider, capture_text=capture_text):
            break

        if attempt == _MANAGED_LOCAL_VERIFY_ATTEMPTS - 1:
            await _cleanup_tmux_session()
            pane_label = pane_command or "empty pane"
            raise ManagedLocalLaunchError(
                f"Managed local session started an idle shell instead of {provider_name} ({pane_label})",
                status_code=_MANAGED_LOCAL_RUNTIME_FAILURE_STATUS,
            )
        await asyncio.sleep(_MANAGED_LOCAL_VERIFY_INTERVAL_SECS)

    return _finish_launch()


__all__ = [
    "ManagedLocalLaunchError",
    "ManagedLocalLaunchParams",
    "ManagedLocalLaunchResult",
    "launch_managed_local_session",
]
