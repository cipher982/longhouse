"""Managed local session launcher.

Phase 2 keeps this intentionally small:
- resolve a reachable runner owned by the current user
- create a managed-local AgentSession row
- launch the provider CLI inside a detached tmux session on that runner
- verify the tmux session exists

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

from zerg.crud import runner_crud
from zerg.models.agents import AgentSession
from zerg.services.managed_local_runtime import mark_managed_local_session_launched
from zerg.services.managed_local_tmux import build_managed_local_shell_prelude
from zerg.services.managed_local_tmux import build_tmux_attach_command
from zerg.services.managed_local_tmux import build_tmux_capture_command
from zerg.services.managed_local_tmux import build_tmux_current_command_command
from zerg.services.managed_local_tmux import build_tmux_has_session_command
from zerg.services.managed_local_tmux import build_tmux_kill_session_command
from zerg.services.managed_local_tmux import build_tmux_launch_command
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
    provider: str = "claude"
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


_MANAGED_LOCAL_TMUX_TMPDIR_MARKER = "__LONGHOUSE_TMUX_TMPDIR__="
_MANAGED_LOCAL_SHELL_COMMANDS = {"", "bash", "sh", "zsh", "fish"}
_MANAGED_LOCAL_RUNTIME_FAILURE_STATUS = 424
_MANAGED_LOCAL_CAPTURE_ERROR_SNIPPETS = (
    "command not found: claude-code",
    "command not found: claude",
    "command not found: codex",
    "command not found: eof",
    "curl is not available",
    "codex app-server exited before ready",
    "codex app-server failed to become ready",
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
_MANAGED_CODEX_SESSION_DIR = "$HOME/.claude/longhouse-managed-sessions"


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


def _build_entry_command(
    *,
    provider: str,
    provider_session_id: str,
    display_name: str | None,
    managed_session_name: str | None = None,
) -> str:
    if provider == "codex":
        return _build_codex_entry_command(
            managed_session_id=provider_session_id,
            managed_session_name=managed_session_name or provider_session_id,
        )
    parts = ["claude-code", "--session-id", provider_session_id]
    if display_name and display_name.strip():
        parts.extend(["-n", display_name.strip()])
    inner = "source ~/.zshrc >/dev/null 2>&1; exec " + " ".join(shlex.quote(part) for part in parts)
    return f"zsh -lc {shlex.quote(inner)}"


def _build_codex_entry_command(*, managed_session_id: str, managed_session_name: str) -> str:
    """Build the tmux entry command for a managed-local Codex session.

    Managed-local Codex is terminal-first: tmux hosts a remote Codex TUI while
    a sibling Codex app-server owns the underlying session/thread. This keeps
    the terminal UX intact without letting Longhouse pretend keystroke
    injection is the control plane.
    """

    safe_name = managed_session_name.strip() or managed_session_id
    inner_lines = [
        "set -euo pipefail",
        f"export LONGHOUSE_SESSION_ID={shlex.quote(managed_session_id)}",
        "source ~/.zshrc >/dev/null 2>&1 || true",
        "command -v codex >/dev/null 2>&1 || { echo 'codex is not available' >&2; exit 12; }",
        "command -v curl >/dev/null 2>&1 || { echo 'curl is not available' >&2; exit 13; }",
        f'MANAGED_DIR="{_MANAGED_CODEX_SESSION_DIR}"',
        'mkdir -p "$MANAGED_DIR"',
        f'APP_SERVER_LOG="$MANAGED_DIR/{safe_name}.app-server.log"',
        'APP_SERVER_META="$MANAGED_DIR/$LONGHOUSE_SESSION_ID.app-server.env"',
        'rm -f "$APP_SERVER_LOG" "$APP_SERVER_META"',
        'touch "$APP_SERVER_LOG"',
        'codex app-server --listen ws://127.0.0.1:0 --session-source cli >/dev/null 2>>"$APP_SERVER_LOG" &',
        "APP_SERVER_PID=$!",
        "cleanup() {",
        '  kill "$APP_SERVER_PID" >/dev/null 2>&1 || true',
        '  wait "$APP_SERVER_PID" 2>/dev/null || true',
        "}",
        "trap cleanup EXIT INT TERM",
        'REMOTE_URL=""',
        'READYZ_URL=""',
        'HEALTHZ_URL=""',
        "for _ in {1..200}; do",
        '  if ! kill -0 "$APP_SERVER_PID" 2>/dev/null; then',
        '    echo "codex app-server exited before ready" >&2',
        '    tail -n 40 "$APP_SERVER_LOG" >&2 || true',
        "    break",
        "  fi",
        "  REMOTE_URL=$(sed -n 's/^  listening on: //p' \"$APP_SERVER_LOG\" | tail -n 1)",
        "  READYZ_URL=$(sed -n 's/^  readyz: //p' \"$APP_SERVER_LOG\" | tail -n 1)",
        "  HEALTHZ_URL=$(sed -n 's/^  healthz: //p' \"$APP_SERVER_LOG\" | tail -n 1)",
        '  if [ -n "$REMOTE_URL" ] && [ -n "$READYZ_URL" ] && curl -fsS "$READYZ_URL" >/dev/null 2>&1; then',
        '    cat > "$APP_SERVER_META" <<EOF',
        'LONGHOUSE_CODEX_APP_SERVER_PID="$APP_SERVER_PID"',
        'LONGHOUSE_CODEX_APP_SERVER_URL="$REMOTE_URL"',
        'LONGHOUSE_CODEX_APP_SERVER_READYZ="$READYZ_URL"',
        'LONGHOUSE_CODEX_APP_SERVER_HEALTHZ="$HEALTHZ_URL"',
        "EOF",
        '    exec codex --enable tui_app_server --remote "$REMOTE_URL"',
        "  fi",
        "  sleep 0.1",
        "done",
        'echo "codex app-server failed to become ready" >&2',
        'tail -n 40 "$APP_SERVER_LOG" >&2 || true',
        "exit 14",
    ]
    return f"zsh -lc {shlex.quote(chr(10).join(inner_lines))}"


def _build_preflight_command(*, provider: str, cwd: str) -> str:
    quoted_cwd = shlex.quote(cwd)
    cli_name = "codex" if provider == "codex" else "claude-code"
    checks = [
        build_managed_local_shell_prelude(),
        f"command -v {cli_name} >/dev/null 2>&1 || {{ echo '{cli_name} is not available' >&2; exit 12; }}",
        f"test -d {quoted_cwd} || {{ echo 'working directory does not exist' >&2; exit 13; }}",
        f"printf {shlex.quote(_MANAGED_LOCAL_TMUX_TMPDIR_MARKER + '%s\\n')} \"${{TMUX_TMPDIR:-}}\"",
    ]
    if provider == "codex":
        checks.insert(2, "command -v curl >/dev/null 2>&1 || { echo 'curl is not available' >&2; exit 14; }")
    return f"zsh -lc {shlex.quote('; '.join(checks))}"


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
    transport = validate_managed_transport(params.managed_transport)
    if transport != ManagedSessionTransport.TMUX.value:
        raise ManagedLocalLaunchError(f"Unsupported managed transport '{transport}'", status_code=400)

    provider = params.provider or "claude"
    if provider not in _VALID_PROVIDERS:
        raise ManagedLocalLaunchError(f"Unsupported provider '{provider}' for managed local", status_code=400)
    provider_name = _PROVIDER_DISPLAY_NAMES.get(provider, provider)

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
        command=_build_preflight_command(provider=provider, cwd=cwd),
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

    session_uuid = uuid4()
    provider_session_id = str(session_uuid)
    project = _derive_project(cwd, params.project)
    display_name = (params.display_name or project).strip() or project
    managed_session_name = normalize_tmux_session_name(f"{display_name}-{session_uuid.hex[:8]}")

    session = AgentSession(
        id=session_uuid,
        provider=provider,
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
        managed_tmux_tmpdir=managed_tmux_tmpdir,
    )
    db.add(session)
    db.flush()

    entry_command = _build_entry_command(
        provider=provider,
        provider_session_id=provider_session_id,
        display_name=params.display_name,
        managed_session_name=managed_session_name,
    )
    launch_command = build_tmux_launch_command(
        session_name=managed_session_name,
        cwd=cwd,
        launch_command=entry_command,
        tmux_tmpdir=managed_tmux_tmpdir,
    )
    verify_session_command = build_tmux_has_session_command(
        session_name=managed_session_name,
        tmux_tmpdir=managed_tmux_tmpdir,
    )
    verify_command = build_tmux_current_command_command(
        session_name=managed_session_name,
        tmux_tmpdir=managed_tmux_tmpdir,
    )
    capture_command = build_tmux_capture_command(
        session_name=managed_session_name,
        lines=80,
        tmux_tmpdir=managed_tmux_tmpdir,
    )

    async def _cleanup_tmux_session() -> None:
        try:
            await dispatcher.dispatch_job(
                db=db,
                owner_id=params.owner_id,
                runner_id=runner.id,
                command=build_tmux_kill_session_command(
                    session_name=managed_session_name,
                    tmux_tmpdir=managed_tmux_tmpdir,
                ),
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
        command=verify_session_command,
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

    expected_pane_commands = _PROVIDER_PANE_COMMANDS.get(provider, _PROVIDER_PANE_COMMANDS["claude"])

    for attempt in range(_MANAGED_LOCAL_VERIFY_ATTEMPTS):
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
            command=capture_command,
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
            raise ManagedLocalLaunchError(
                f"Managed local session started an idle shell instead of {provider_name} ({pane_command or 'empty pane'})",
                status_code=_MANAGED_LOCAL_RUNTIME_FAILURE_STATUS,
            )
        await asyncio.sleep(_MANAGED_LOCAL_VERIFY_INTERVAL_SECS)

    mark_managed_local_session_launched(db, session=session)
    db.commit()
    db.refresh(session)
    return ManagedLocalLaunchResult(
        session=session,
        attach_command=build_tmux_attach_command(
            session_name=managed_session_name,
            tmux_tmpdir=managed_tmux_tmpdir,
        ),
    )


__all__ = [
    "ManagedLocalLaunchError",
    "ManagedLocalLaunchParams",
    "ManagedLocalLaunchResult",
    "launch_managed_local_session",
]
