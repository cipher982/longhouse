"""Session chat router for live-session drop-in functionality.

Enables interactive chat with Claude Code sessions via turn-by-turn resume.
Each message spawns: claude --resume {id} -p "message" --output-format stream-json

Security:
- Workspace path derived server-side from session metadata (not client)
- Per-session locks prevent concurrent resumes
- Process cancellation on client disconnect
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import AsyncIterator
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.routers.oikos_auth import get_current_oikos_user
from zerg.services.agents_store import AgentsStore
from zerg.services.session_continuity import prepare_session_for_resume
from zerg.services.session_continuity import session_lock_manager
from zerg.services.session_continuity import ship_session_to_zerg
from zerg.services.session_continuity import workspace_resolver

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions", tags=["session-chat"])

SESSION_CHAT_BACKEND_ENV = "SESSION_CHAT_BACKEND"
SESSION_CHAT_MODEL_ENV = "SESSION_CHAT_MODEL"
SESSION_CHAT_ZAI_BASE_URL_ENV = "SESSION_CHAT_ZAI_BASE_URL"
SESSION_CHAT_AWS_PROFILE_ENV = "SESSION_CHAT_AWS_PROFILE"
SESSION_CHAT_AWS_REGION_ENV = "SESSION_CHAT_AWS_REGION"
SESSION_CHAT_BACKEND_AMBIENT = "ambient"
SESSION_CHAT_BACKEND_ZAI = "zai"
SESSION_CHAT_BACKEND_BEDROCK = "bedrock"
DEFAULT_SESSION_CHAT_ZAI_BASE_URL = "https://api.z.ai/api/anthropic"
DEFAULT_SESSION_CHAT_ZAI_MODEL = "glm-5"
SUPPORTED_SESSION_CHAT_BACKENDS = {
    SESSION_CHAT_BACKEND_AMBIENT,
    SESSION_CHAT_BACKEND_ZAI,
    SESSION_CHAT_BACKEND_BEDROCK,
}


def _truthy_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _get_session_chat_backend() -> str:
    backend = os.getenv(SESSION_CHAT_BACKEND_ENV, SESSION_CHAT_BACKEND_AMBIENT).strip().lower()
    if not backend:
        return SESSION_CHAT_BACKEND_AMBIENT
    if backend not in SUPPORTED_SESSION_CHAT_BACKENDS:
        raise RuntimeError(f"{SESSION_CHAT_BACKEND_ENV} must be one of {sorted(SUPPORTED_SESSION_CHAT_BACKENDS)} (got {backend!r})")
    return backend


@dataclass(frozen=True)
class ClaudeResumeRuntime:
    backend: str
    cmd: list[str]
    env_updates: dict[str, str]
    env_unset: tuple[str, ...] = ()


def _build_claude_resume_runtime(*, provider_session_id: str, message: str) -> ClaudeResumeRuntime:
    cmd = [
        "claude",
        "--resume",
        provider_session_id,
        "-p",
        message,
        "--output-format",
        "stream-json",
        "--verbose",
        "--print",
    ]
    backend = _get_session_chat_backend()
    if backend == SESSION_CHAT_BACKEND_AMBIENT:
        return ClaudeResumeRuntime(backend=backend, cmd=cmd, env_updates={})

    model = os.getenv(SESSION_CHAT_MODEL_ENV, "").strip()
    if backend == SESSION_CHAT_BACKEND_ZAI:
        api_key = os.getenv("ZAI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(f"{SESSION_CHAT_BACKEND_ENV}=zai requires ZAI_API_KEY")
        env_updates = {
            "ANTHROPIC_BASE_URL": os.getenv(SESSION_CHAT_ZAI_BASE_URL_ENV, DEFAULT_SESSION_CHAT_ZAI_BASE_URL).strip()
            or DEFAULT_SESSION_CHAT_ZAI_BASE_URL,
            "ANTHROPIC_AUTH_TOKEN": api_key,
            "ANTHROPIC_MODEL": model or DEFAULT_SESSION_CHAT_ZAI_MODEL,
        }
        return ClaudeResumeRuntime(
            backend=backend,
            cmd=cmd,
            env_updates=env_updates,
            env_unset=("CLAUDE_CODE_USE_BEDROCK", "ANTHROPIC_API_KEY"),
        )

    env_updates = {"CLAUDE_CODE_USE_BEDROCK": "1"}
    aws_profile = os.getenv(SESSION_CHAT_AWS_PROFILE_ENV, "").strip()
    aws_region = os.getenv(SESSION_CHAT_AWS_REGION_ENV, "").strip()
    if aws_profile:
        env_updates["AWS_PROFILE"] = aws_profile
    if aws_region:
        env_updates["AWS_REGION"] = aws_region
    if model:
        env_updates["ANTHROPIC_MODEL"] = model
    return ClaudeResumeRuntime(
        backend=backend,
        cmd=cmd,
        env_updates=env_updates,
        env_unset=("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"),
    )


# ---------------------------------------------------------------------------
# Request/Response Models
# ---------------------------------------------------------------------------


class SessionChatRequest(BaseModel):
    """Request to chat with a session."""

    message: str = Field(..., min_length=1, max_length=10000, description="User message")


class SessionLockInfo(BaseModel):
    """Information about a session lock."""

    locked: bool
    holder: str | None = None
    time_remaining_seconds: float | None = None
    fork_available: bool = False


class SessionChatError(BaseModel):
    """Error response for session chat."""

    error: str
    code: str
    lock_info: SessionLockInfo | None = None


def _lock_scope_id_for_session(db: Session, session_id: str) -> str:
    try:
        session_uuid = UUID(session_id)
    except ValueError:
        return session_id
    session = AgentsStore(db).get_session(session_uuid)
    if session is None:
        return session_id
    return str(session.thread_root_session_id or session.id)


# ---------------------------------------------------------------------------
# SSE Event Types
# ---------------------------------------------------------------------------


@dataclass
class SSEEvent:
    """Server-sent event."""

    event: str
    data: str

    def encode(self) -> str:
        """Encode as SSE format."""
        return f"event: {self.event}\ndata: {self.data}\n\n"


async def _stream_fake_claude_output(
    *,
    source_session_id: str,
    target_session_id: str,
    thread_root_session_id: str,
    continued_from_session_id: str | None,
    created_continuation: bool,
    branched_from_event_id: int | None,
    provider_session_id: str,
    workspace_path: Path,
    message: str,
) -> AsyncIterator[str]:
    timestamp = datetime.now(timezone.utc).isoformat()
    assistant_text = f"Test continuation reply to: {message}"

    yield SSEEvent(
        event="system",
        data=json.dumps(
            {
                "type": "session_started",
                "session_id": target_session_id,
                "source_session_id": source_session_id,
                "thread_root_session_id": thread_root_session_id,
                "continued_from_session_id": continued_from_session_id,
                "created_continuation": created_continuation,
                "provider_session_id": provider_session_id,
                "workspace": str(workspace_path),
                "timestamp": timestamp,
            }
        ),
    ).encode()
    yield SSEEvent(
        event="assistant_delta",
        data=json.dumps({"text": assistant_text, "accumulated": assistant_text}),
    ).encode()
    yield SSEEvent(
        event="done",
        data=json.dumps(
            {
                "session_id": target_session_id,
                "source_session_id": source_session_id,
                "shipped_session_id": target_session_id,
                "created_continuation": created_continuation,
                "branched_from_event_id": branched_from_event_id,
                "exit_code": 0,
                "total_text_length": len(assistant_text),
                "timestamp": timestamp,
            }
        ),
    ).encode()


async def stream_claude_output(
    *,
    source_session_id: str,
    target_session_id: str,
    thread_root_session_id: str,
    continued_from_session_id: str | None,
    created_continuation: bool,
    branched_from_event_id: int | None,
    provider_session_id: str,
    workspace_path: Path,
    message: str,
    request_id: str,
) -> AsyncIterator[str]:
    """Stream Claude Code output as SSE events.

    Yields SSE events:
    - system: Session info, status updates
    - assistant_delta: Streaming text from Claude
    - tool_use: Tool calls
    - error: Error messages
    - done: Completion signal
    """
    proc = None
    try:
        if _truthy_env("TESTING") and _truthy_env("E2E_FAKE_SESSION_CHAT"):
            async for event in _stream_fake_claude_output(
                source_session_id=source_session_id,
                target_session_id=target_session_id,
                thread_root_session_id=thread_root_session_id,
                continued_from_session_id=continued_from_session_id,
                created_continuation=created_continuation,
                branched_from_event_id=branched_from_event_id,
                provider_session_id=provider_session_id,
                workspace_path=workspace_path,
                message=message,
            ):
                yield event
            return

        runtime = _build_claude_resume_runtime(provider_session_id=provider_session_id, message=message)

        yield SSEEvent(
            event="system",
            data=json.dumps(
                {
                    "type": "session_started",
                    "session_id": target_session_id,
                    "source_session_id": source_session_id,
                    "thread_root_session_id": thread_root_session_id,
                    "continued_from_session_id": continued_from_session_id,
                    "created_continuation": created_continuation,
                    "provider_session_id": provider_session_id,
                    "workspace": str(workspace_path),
                    "execution_backend": runtime.backend,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            ),
        ).encode()

        proc_env = os.environ.copy()
        proc_env.update(runtime.env_updates)
        for env_name in runtime.env_unset:
            proc_env.pop(env_name, None)

        logger.info(
            "[%s] Starting Claude-compatible continuation: backend=%s cwd=%s",
            request_id,
            runtime.backend,
            workspace_path,
        )

        proc = await asyncio.create_subprocess_exec(
            *runtime.cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=workspace_path,
            env=proc_env,
        )

        assistant_text = ""
        async for line in proc.stdout:
            line = line.decode().strip()
            if not line:
                continue

            try:
                event = json.loads(line)
                event_type = event.get("type", "unknown")

                if event_type == "assistant":
                    msg = event.get("message", {})
                    content = msg.get("content", [])
                    for block in content:
                        if isinstance(block, dict):
                            if block.get("type") == "text":
                                text_content = block.get("text", "")
                                assistant_text += text_content
                                yield SSEEvent(
                                    event="assistant_delta",
                                    data=json.dumps(
                                        {
                                            "text": text_content,
                                            "accumulated": assistant_text,
                                        }
                                    ),
                                ).encode()
                            elif block.get("type") == "tool_use":
                                yield SSEEvent(
                                    event="tool_use",
                                    data=json.dumps(
                                        {
                                            "name": block.get("name"),
                                            "id": block.get("id"),
                                        }
                                    ),
                                ).encode()

                elif event_type == "result":
                    yield SSEEvent(
                        event="tool_result",
                        data=json.dumps(
                            {
                                "result": str(event.get("result", ""))[:500],
                            }
                        ),
                    ).encode()

                elif event_type == "system":
                    yield SSEEvent(
                        event="system",
                        data=json.dumps(
                            {
                                "type": "claude_system",
                                "session_id": event.get("session_id"),
                            }
                        ),
                    ).encode()

            except json.JSONDecodeError:
                logger.debug(f"[{request_id}] Non-JSON output: {line[:100]}")

        await proc.wait()

        shipped_id: str | None = None
        if proc.returncode != 0:
            logger.error(f"[{request_id}] Claude exited with code {proc.returncode}")
            yield SSEEvent(
                event="error",
                data=json.dumps(
                    {
                        "error": f"Claude exited with code {proc.returncode}",
                        "details": "Process exited with non-zero status",
                    }
                ),
            ).encode()
        else:
            try:
                shipped_id = await ship_session_to_zerg(
                    workspace_path=workspace_path,
                    commis_id=request_id,
                    session_id=target_session_id,
                    thread_root_session_id=thread_root_session_id,
                    continued_from_session_id=continued_from_session_id,
                    continuation_kind="cloud",
                    origin_label="Cloud",
                    branched_from_event_id=branched_from_event_id,
                )
                if shipped_id:
                    logger.info(f"[{request_id}] Shipped session to Longhouse: {shipped_id}")
            except Exception as e:
                logger.warning(f"[{request_id}] Failed to ship session to Longhouse: {e}")

        yield SSEEvent(
            event="done",
            data=json.dumps(
                {
                    "session_id": target_session_id,
                    "source_session_id": source_session_id,
                    "shipped_session_id": shipped_id or target_session_id,
                    "created_continuation": created_continuation,
                    "branched_from_event_id": branched_from_event_id,
                    "exit_code": proc.returncode,
                    "execution_backend": runtime.backend if "runtime" in locals() else SESSION_CHAT_BACKEND_AMBIENT,
                    "total_text_length": len(assistant_text),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            ),
        ).encode()

    except asyncio.CancelledError:
        logger.info(f"[{request_id}] Request cancelled by client")
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
        yield SSEEvent(
            event="error",
            data=json.dumps({"error": "Request cancelled"}),
        ).encode()
        raise

    except Exception as e:
        logger.exception(f"[{request_id}] Error streaming Claude output")
        yield SSEEvent(
            event="error",
            data=json.dumps({"error": str(e)[:500]}),
        ).encode()

    finally:
        if proc and proc.returncode is None:
            proc.terminate()


@router.post("/{session_id}/chat")
async def chat_with_session(
    session_id: str,
    body: SessionChatRequest,
    db: Session = Depends(get_db),
    _current_user=Depends(get_current_oikos_user),
):
    """Chat with a Claude Code session.

    Resumes an existing session and streams the response via SSE.
    """
    request_id = str(uuid.uuid4())[:8]
    logger.info(f"[{request_id}] Chat request for session {session_id}")

    try:
        source_session_uuid = UUID(session_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid session id: {session_id}",
        ) from exc

    store = AgentsStore(db)
    source_session = store.get_session(source_session_uuid)
    if not source_session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found",
        )

    if source_session.provider != "claude":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Only Claude sessions can be resumed (got {source_session.provider})",
        )

    lock_scope_id = str(source_session.thread_root_session_id or source_session.id)
    lock = await session_lock_manager.acquire(
        session_id=lock_scope_id,
        holder=request_id,
        ttl_seconds=300,
    )

    if not lock:
        existing_lock = await session_lock_manager.get_lock_info(lock_scope_id)
        lock_info = SessionLockInfo(
            locked=True,
            holder=existing_lock.holder if existing_lock else None,
            time_remaining_seconds=existing_lock.time_remaining if existing_lock else None,
            fork_available=True,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "Session is currently in use",
                "code": "SESSION_LOCKED",
                "lock_info": lock_info.model_dump(),
            },
        )

    resolved_workspace = None

    try:
        target_session, created_continuation = store.ensure_cloud_continuation_target(source_session.id)
        db.commit()

        resolved_workspace = await workspace_resolver.resolve(
            original_cwd=source_session.cwd,
            git_repo=source_session.git_repo,
            git_branch=source_session.git_branch,
            session_id=str(target_session.id),
        )

        if resolved_workspace.error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot resolve workspace: {resolved_workspace.error}",
            )

        provider_session_id = await prepare_session_for_resume(
            session_id=str(source_session.id),
            workspace_path=resolved_workspace.path,
            db=db,
        )

        logger.info(
            f"[{request_id}] Prepared source session {source_session.id} -> {provider_session_id[:20]}... "
            f"target={target_session.id} workspace={resolved_workspace.path} is_temp={resolved_workspace.is_temp}"
        )

        async def generate():
            try:
                async for event in stream_claude_output(
                    source_session_id=str(source_session.id),
                    target_session_id=str(target_session.id),
                    thread_root_session_id=str(target_session.thread_root_session_id or target_session.id),
                    continued_from_session_id=(
                        str(target_session.continued_from_session_id) if target_session.continued_from_session_id else None
                    ),
                    created_continuation=created_continuation,
                    branched_from_event_id=target_session.branched_from_event_id,
                    provider_session_id=provider_session_id,
                    workspace_path=resolved_workspace.path,
                    message=body.message,
                    request_id=request_id,
                ):
                    yield event
            finally:
                await session_lock_manager.release(lock_scope_id, request_id)
                if resolved_workspace and resolved_workspace.is_temp:
                    resolved_workspace.cleanup()
                logger.info(f"[{request_id}] Session chat complete, lock released")

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    except HTTPException:
        await session_lock_manager.release(lock_scope_id, request_id)
        if resolved_workspace and resolved_workspace.is_temp:
            resolved_workspace.cleanup()
        raise

    except Exception as e:
        await session_lock_manager.release(lock_scope_id, request_id)
        if resolved_workspace and resolved_workspace.is_temp:
            resolved_workspace.cleanup()
        logger.exception(f"[{request_id}] Error in chat_with_session")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal error: {str(e)[:200]}",
        )


@router.get("/{session_id}/lock")
async def get_session_lock_status(
    session_id: str,
    db: Session = Depends(get_db),
    _current_user=Depends(get_current_oikos_user),
) -> SessionLockInfo:
    """Check if a session is currently locked.

    Used by UI to show lock status before attempting to chat.
    """
    lock_scope_id = _lock_scope_id_for_session(db, session_id)
    lock = await session_lock_manager.get_lock_info(lock_scope_id)

    if lock:
        return SessionLockInfo(
            locked=True,
            holder=lock.holder,
            time_remaining_seconds=lock.time_remaining,
            fork_available=True,
        )
    else:
        return SessionLockInfo(
            locked=False,
            fork_available=False,
        )


@router.delete("/{session_id}/lock")
async def force_release_lock(
    session_id: str,
    db: Session = Depends(get_db),
    _current_user=Depends(get_current_oikos_user),
) -> dict:
    """Force release a session lock (admin operation).

    Use with caution - may cause issues if a chat is in progress.
    """
    lock_scope_id = _lock_scope_id_for_session(db, session_id)
    released = await session_lock_manager.release(lock_scope_id)
    return {
        "released": released,
        "session_id": session_id,
        "lock_session_id": lock_scope_id,
    }
