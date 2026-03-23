"""Managed-local Codex exec helpers."""

from __future__ import annotations

import re
import shlex

from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.services.managed_local_control import ManagedLocalSendResult
from zerg.services.managed_local_runtime import mark_managed_local_input_sent
from zerg.services.runner_job_dispatcher import get_runner_job_dispatcher
from zerg.session_execution_home import SessionExecutionHome

_CODEX_EXEC_SUCCESS_MARKER = "__LONGHOUSE_CODEX_EXEC_SHIPPED__"


def _safe_transcript_stem(provider_session_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(provider_session_id or "").strip())
    return safe or "codex-session"


def build_codex_exec_resume_command(
    *,
    session_id: str,
    provider_session_id: str,
    cwd: str,
    prompt: str,
) -> str:
    transcript_stem = _safe_transcript_stem(provider_session_id)
    script = "\n".join(
        [
            "set -euo pipefail",
            "source ~/.zshrc >/dev/null 2>&1 || true",
            "command -v codex >/dev/null 2>&1 || { echo 'codex is not available' >&2; exit 12; }",
            "command -v longhouse-engine >/dev/null 2>&1 || { echo 'longhouse-engine is not available' >&2; exit 13; }",
            f"cd {shlex.quote(cwd)} || {{ echo 'working directory does not exist' >&2; exit 14; }}",
            f"export LONGHOUSE_SESSION_ID={shlex.quote(session_id)}",
            'WORKDIR=$(mktemp -d "${TMPDIR:-/tmp}/longhouse-codex-exec.XXXXXX")',
            'cleanup() { rm -rf "$WORKDIR"; }',
            "trap cleanup EXIT",
            f'TRANSCRIPT="$WORKDIR/{transcript_stem}.jsonl"',
            "set +e",
            f'codex exec resume --json --skip-git-repo-check --full-auto {shlex.quote(provider_session_id)} {shlex.quote(prompt)} > "$TRANSCRIPT"',
            "STATUS=$?",
            "set -e",
            'if [[ ! -s "$TRANSCRIPT" ]]; then',
            '  echo "codex exec produced no transcript output" >&2',
            '  exit "${STATUS:-16}"',
            "fi",
            'if ! longhouse-engine ship --file "$TRANSCRIPT" --provider codex --session-id "$LONGHOUSE_SESSION_ID" >/dev/null 2>&1; then',
            '  echo "longhouse-engine ship failed" >&2',
            "  exit 15",
            "fi",
            f"echo {shlex.quote(_CODEX_EXEC_SUCCESS_MARKER)}",
        ]
    )
    return f"zsh -lc {shlex.quote(script)}"


async def run_codex_exec_resume_for_managed_local_session(
    *,
    db: Session,
    owner_id: int,
    session: AgentSession,
    text: str,
    commis_id: str | None = None,
    timeout_secs: int = 300,
) -> ManagedLocalSendResult:
    if str(getattr(session, "execution_home", "") or "").strip() != SessionExecutionHome.MANAGED_LOCAL.value:
        return ManagedLocalSendResult(ok=False, error="Session is not managed_local")
    if str(getattr(session, "provider", "") or "").strip().lower() != "codex":
        return ManagedLocalSendResult(ok=False, error="Session is not a managed-local Codex session")
    if not getattr(session, "source_runner_id", None):
        return ManagedLocalSendResult(ok=False, error="Managed local session is missing source runner metadata")
    if not getattr(session, "provider_session_id", None):
        return ManagedLocalSendResult(ok=False, error="Managed local Codex session is missing provider session id")
    if not getattr(session, "cwd", None):
        return ManagedLocalSendResult(ok=False, error="Managed local Codex session is missing working directory")

    dispatcher = get_runner_job_dispatcher()
    result = await dispatcher.dispatch_job(
        db=db,
        owner_id=owner_id,
        runner_id=int(session.source_runner_id),
        command=build_codex_exec_resume_command(
            session_id=str(session.id),
            provider_session_id=str(session.provider_session_id),
            cwd=str(session.cwd),
            prompt=text,
        ),
        timeout_secs=timeout_secs,
        commis_id=commis_id,
        run_id=None,
    )

    if not result.get("ok"):
        return ManagedLocalSendResult(
            ok=False,
            error=str(result.get("error", {}).get("message", "Failed to run Codex exec resume")),
        )

    data = result.get("data", {})
    exit_code = int(data.get("exit_code", 1))
    stdout = str(data.get("stdout") or "")
    stderr = str(data.get("stderr") or "").strip()
    if exit_code != 0 or _CODEX_EXEC_SUCCESS_MARKER not in stdout:
        detail = stderr or stdout.strip() or "Managed local Codex exec resume failed"
        return ManagedLocalSendResult(
            ok=False,
            exit_code=exit_code,
            error=detail,
        )

    mark_managed_local_input_sent(
        db,
        session=session,
        dedupe_suffix=str(commis_id or ""),
    )
    return ManagedLocalSendResult(ok=True, exit_code=0)


__all__ = [
    "build_codex_exec_resume_command",
    "run_codex_exec_resume_for_managed_local_session",
]
