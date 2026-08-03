"""Provider-neutral command handoff for ended Helm Resume."""

from __future__ import annotations

import shlex
from typing import Literal

from pydantic import BaseModel
from pydantic import Field

from zerg.services.session_views import SessionResponse


class SessionResumeIntentResponse(BaseModel):
    session_id: str
    provider: str
    machine_id: str | None = None
    machine_label: str | None = None
    cwd: str | None = None
    available: bool
    reason: str | None = None
    argv: list[str] = Field(default_factory=list)
    command: str | None = None
    handoff: Literal["terminal_command"] = "terminal_command"


_RESUME_FLAG = {
    "codex": "--resume-session",
    "claude": "--resume",
    "cursor": "--resume-session",
    "opencode": "--resume-session",
}


def build_session_resume_intent(session: SessionResponse) -> SessionResumeIntentResponse:
    session_control = getattr(session, "control", None)
    machine_label = (
        getattr(session_control, "source_runner_name", None)
        or getattr(session, "origin_label", None)
        or getattr(session, "home_label", None)
        or session.device_id
    )
    action = session.session_state.control.actions.resume if session.session_state.control is not None else None
    if action is None or action.state != "available":
        return SessionResumeIntentResponse(
            session_id=session.id,
            provider=session.provider,
            machine_id=session.device_id,
            machine_label=machine_label,
            cwd=session.cwd,
            available=False,
            reason=action.reason if action is not None else "control_unknown",
        )
    flag = _RESUME_FLAG.get(session.provider)
    if flag is None:
        return SessionResumeIntentResponse(
            session_id=session.id,
            provider=session.provider,
            machine_id=session.device_id,
            machine_label=machine_label,
            cwd=session.cwd,
            available=False,
            reason="unsupported",
        )
    if not session.device_id:
        return SessionResumeIntentResponse(
            session_id=session.id,
            provider=session.provider,
            machine_label=machine_label,
            cwd=session.cwd,
            available=False,
            reason="machine_unknown",
        )
    if not session.cwd:
        return SessionResumeIntentResponse(
            session_id=session.id,
            provider=session.provider,
            machine_id=session.device_id,
            machine_label=machine_label,
            available=False,
            reason="workspace_missing",
        )
    argv = ["longhouse", session.provider, "--cwd", session.cwd, flag, session.id]
    return SessionResumeIntentResponse(
        session_id=session.id,
        provider=session.provider,
        machine_id=session.device_id,
        machine_label=machine_label,
        cwd=session.cwd,
        available=True,
        argv=argv,
        command=shlex.join(argv),
    )


__all__ = ["SessionResumeIntentResponse", "build_session_resume_intent"]
