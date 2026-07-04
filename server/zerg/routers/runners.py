"""Runners API.

REST endpoints for managing runners - user-owned execution infrastructure:
- Create enrollment tokens for registering new runners
- Register runners using enrollment tokens
- List, update, and revoke runners
- View runner jobs (audit trail)

Runners enable secure command execution without backend access to user SSH keys.
"""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Path
from fastapi import Query
from fastapi import Request
from fastapi import Response
from fastapi import WebSocket
from fastapi import WebSocketDisconnect
from fastapi import status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from zerg.crud import runner_crud
from zerg.database import get_db
from zerg.database import get_session_factory
from zerg.database import reset_test_worker_id
from zerg.database import set_test_worker_id
from zerg.dependencies.auth import get_current_user
from zerg.models.models import User
from zerg.request_urls import get_request_public_base_url
from zerg.schemas.runner_schemas import EnrollTokenResponse
from zerg.schemas.runner_schemas import RunnerDoctorResponse
from zerg.schemas.runner_schemas import RunnerJobListResponse
from zerg.schemas.runner_schemas import RunnerJobResponse
from zerg.schemas.runner_schemas import RunnerListResponse
from zerg.schemas.runner_schemas import RunnerPreflightRequest
from zerg.schemas.runner_schemas import RunnerPreflightResponse
from zerg.schemas.runner_schemas import RunnerRegisterRequest
from zerg.schemas.runner_schemas import RunnerRegisterResponse
from zerg.schemas.runner_schemas import RunnerResponse
from zerg.schemas.runner_schemas import RunnerRotateSecretResponse
from zerg.schemas.runner_schemas import RunnerStatusItem
from zerg.schemas.runner_schemas import RunnerStatusResponse
from zerg.schemas.runner_schemas import RunnerSuccessResponse
from zerg.schemas.runner_schemas import RunnerUpdate
from zerg.services.runner_auth import authenticate_runner_identity
from zerg.services.runner_connection_manager import get_runner_connection_manager
from zerg.services.runner_doctor import diagnose_runner
from zerg.services.runner_health import build_runner_response
from zerg.services.runner_health import normalize_runner_binary_tag
from zerg.services.runner_heartbeat_cache import mark_runner_heartbeat
from zerg.services.runner_job_dispatcher import get_runner_job_dispatcher
from zerg.services.write_serializer import get_write_serializer
from zerg.utils.server_timing import ServerTimingRecorder
from zerg.utils.time import utc_now_naive

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/runners",
    tags=["runners"],
)


# ---------------------------------------------------------------------------
# WebSocket helpers
# ---------------------------------------------------------------------------


async def _safe_close_runner_websocket(
    websocket: WebSocket,
    *,
    code: int | None = None,
    reason: str | None = None,
) -> None:
    """Best-effort websocket close for runner routes.

    Fast reconnects and boot-time races can leave Starlette/FastAPI thinking the
    socket is already closed. That should not cascade into noisy secondary
    exceptions in the handler.
    """
    try:
        if code is None:
            await websocket.close()
        else:
            await websocket.close(code=code, reason=reason)
    except Exception as exc:
        logger.debug("Ignoring runner websocket close race: %s", exc)


def _rollback_after_write_failure(db: Session, *, operation: str) -> None:
    """Rollback helper for write-path failures that should not crash cleanup paths."""
    try:
        db.rollback()
    except Exception as rollback_exc:
        logger.warning("Rollback failed after %s: %s", operation, rollback_exc)


async def _handle_exec_chunk(
    db: Session,
    message: dict,
    runner_id: int,
    owner_id: int,
) -> None:
    """Process an exec_chunk message from a runner.

    Appends output to the job record and publishes a live SSE chunk for
    any waiting run subscribers.
    """
    from zerg.events import EventType
    from zerg.events.event_bus import event_bus

    job_id = message.get("job_id")
    stream = message.get("stream")
    data = message.get("data")
    logger.debug(f"Exec chunk from runner {runner_id}, job {job_id}, stream {stream}")

    if not (job_id and stream and data):
        return

    def _write_chunk(wdb: Session) -> dict | None:
        job = runner_crud.get_job(wdb, job_id)
        if not job or job.runner_id != runner_id or job.owner_id != owner_id:
            return None
        updated_job = runner_crud.update_job_output(wdb, job_id, stream, data)
        if not updated_job:
            return None
        return {
            "run_id": updated_job.run_id,
        }

    try:
        ws = get_write_serializer()
        if ws.is_configured:
            updated_job = await ws.execute(_write_chunk, label="runner-output", auto_commit=False)
        else:
            updated_job = _write_chunk(db)
    except Exception as exc:
        _rollback_after_write_failure(db, operation="exec_chunk persistence")
        logger.error("Failed to persist exec_chunk for runner %s, job %s: %s", runner_id, job_id, exc)
        return

    if not updated_job:
        logger.warning(f"Ignoring exec_chunk for invalid job {job_id} from runner {runner_id}")
        return

    run_id_int = None
    run_id = updated_job.get("run_id")
    if run_id is not None:
        try:
            run_id_int = int(run_id)
        except (TypeError, ValueError):
            run_id_int = None

    # Publish live output chunk (ephemeral SSE only; not persisted)
    if run_id_int:
        MAX_CHUNK_CHARS = 4000
        payload = {
            "runner_job_id": job_id,
            "stream": stream,
            "data": data[-MAX_CHUNK_CHARS:] if len(data) > MAX_CHUNK_CHARS else data,
            "run_id": run_id_int,
            "owner_id": owner_id,
        }
        await event_bus.publish(EventType.COMMIS_OUTPUT_CHUNK, payload)


async def _handle_exec_done(
    db: Session,
    message: dict,
    runner_id: int,
    owner_id: int,
    job_dispatcher,
) -> None:
    """Process an exec_done message from a runner."""

    job_id = message.get("job_id")
    exit_code = message.get("exit_code")
    duration_ms = message.get("duration_ms")
    logger.info(f"Exec done from runner {runner_id}, job {job_id}, exit_code {exit_code}")

    if job_id is None or exit_code is None:
        return

    def _persist_completion(wdb: Session) -> dict | None:
        job = runner_crud.get_job(wdb, job_id)
        if not job or job.runner_id != runner_id or job.owner_id != owner_id:
            return None
        updated_job = runner_crud.update_job_completed(wdb, job_id, exit_code, duration_ms or 0)
        if not updated_job:
            return None
        return {
            "stdout": updated_job.stdout_trunc or "",
            "stderr": updated_job.stderr_trunc or "",
        }

    try:
        ws = get_write_serializer()
        if ws.is_configured:
            persisted = await ws.execute(_persist_completion, label="runner-job-complete", auto_commit=False)
        else:
            persisted = _persist_completion(db)
    except Exception as exc:
        _rollback_after_write_failure(db, operation="exec_done persistence")
        logger.error("Failed to persist exec_done from runner %s, job %s: %s", runner_id, job_id, exc)
        job_dispatcher.complete_job(
            job_id,
            {
                "ok": False,
                "error": {
                    "type": "execution_error",
                    "message": f"Failed to persist runner completion: {exc}",
                },
            },
            runner_id,
        )
        return

    if not persisted:
        logger.warning(f"Ignoring exec_done for invalid job {job_id} from runner {runner_id}")
        return

    result = {
        "ok": True,
        "data": {
            "job_id": job_id,
            "exit_code": exit_code,
            "stdout": persisted["stdout"],
            "stderr": persisted["stderr"],
            "duration_ms": duration_ms or 0,
        },
    }
    job_dispatcher.complete_job(job_id, result, runner_id)


async def _handle_exec_error(
    db: Session,
    message: dict,
    runner_id: int,
    owner_id: int,
    job_dispatcher,
) -> None:
    """Process an exec_error message from a runner."""

    job_id = message.get("job_id")
    error = message.get("error")
    logger.error(f"Exec error from runner {runner_id}, job {job_id}: {error}")

    if not job_id or not error:
        return

    def _persist_error(wdb: Session) -> bool:
        job = runner_crud.get_job(wdb, job_id)
        if not job or job.runner_id != runner_id or job.owner_id != owner_id:
            return False
        updated_job = runner_crud.update_job_error(wdb, job_id, error)
        return updated_job is not None

    try:
        ws = get_write_serializer()
        if ws.is_configured:
            persisted = await ws.execute(_persist_error, label="runner-job-error", auto_commit=False)
        else:
            persisted = _persist_error(db)
    except Exception as exc:
        _rollback_after_write_failure(db, operation="exec_error persistence")
        logger.error("Failed to persist exec_error from runner %s, job %s: %s", runner_id, job_id, exc)
        job_dispatcher.complete_job(
            job_id,
            {
                "ok": False,
                "error": {
                    "type": "execution_error",
                    "message": f"Failed to persist runner error state: {exc}",
                },
            },
            runner_id,
        )
        return

    if not persisted:
        logger.warning(f"Ignoring exec_error for invalid job {job_id} from runner {runner_id}")
        return

    result = {
        "ok": False,
        "error": {
            "type": "execution_error",
            "message": error,
        },
    }
    job_dispatcher.complete_job(job_id, result, runner_id)


# ---------------------------------------------------------------------------
# Enrollment Endpoints
# ---------------------------------------------------------------------------


@router.get("/install.sh")
def get_install_script(
    enroll_token: str | None = None,
    runner_name: str | None = None,
    longhouse_url: str | None = None,
    mode: str | None = None,
) -> Response:
    """Return shell script for one-liner runner installation.

    This endpoint is designed to be used with curl:
        curl -fsSL https://api.longhouse.ai/api/runners/install.sh?enroll_token=xxx | bash

    Or with environment variables (preferred - avoids token in shell history):
        ENROLL_TOKEN=xxx curl -fsSL https://api.longhouse.ai/api/runners/install.sh | bash

    The script:
    1. Detects OS (macOS/Linux)
    2. Registers the runner using the enroll token
    3. Downloads the native binary from GitHub Releases
    4. Installs as a launchd (macOS), systemd user service (`desktop`), or Linux system service (`server`)
    5. Starts the runner automatically

    No authentication required - this is for bootstrapping new runners.
    """
    import re
    import shlex
    from pathlib import Path

    from zerg.config import get_settings

    settings = get_settings()

    # Validate enroll_token format (alphanumeric + dash/underscore only).
    # It may also be omitted entirely so callers can provide ENROLL_TOKEN via env.
    if enroll_token is not None and not re.match(r"^[A-Za-z0-9_-]+$", enroll_token):
        return Response(
            content="Error: Invalid enroll_token format",
            media_type="text/plain",
            status_code=400,
        )

    # Validate runner_name if provided (alphanumeric + dash/underscore/dot only)
    if runner_name and not re.match(r"^[A-Za-z0-9_.-]+$", runner_name):
        return Response(
            content="Error: Invalid runner_name format (use alphanumeric, dash, underscore, dot)",
            media_type="text/plain",
            status_code=400,
        )

    if mode and mode not in {"desktop", "server"}:
        return Response(
            content="Error: Invalid mode (use desktop or server)",
            media_type="text/plain",
            status_code=400,
        )

    # Resolve API URL
    api_url = None
    if longhouse_url:
        if not re.match(r"^https?://[A-Za-z0-9._-]+(:[0-9]+)?(/.*)?$", longhouse_url):
            return Response(
                content="Error: Invalid longhouse_url format",
                media_type="text/plain",
                status_code=400,
            )
        api_url = longhouse_url
    if not api_url:
        if not settings.app_public_url:
            if settings.testing:
                api_url = "http://localhost:30080"
            else:
                return Response(
                    content="Error: APP_PUBLIC_URL not configured on server",
                    media_type="text/plain",
                    status_code=500,
                )
        else:
            api_url = settings.app_public_url

    binary_url = f"https://github.com/cipher982/longhouse/releases/download/{settings.runner_binary_tag}"
    update_manifest_url = "https://github.com/cipher982/longhouse/releases/latest/download/longhouse-runner-manifest.json"
    runner_binary_version = normalize_runner_binary_tag(settings.runner_binary_tag) or settings.runner_binary_tag

    # Shell-escape all substituted values to prevent injection
    safe_enroll_token = shlex.quote(enroll_token) if enroll_token else ""
    safe_runner_name_expr = shlex.quote(runner_name) if runner_name else "$(hostname)"
    safe_api_url = shlex.quote(api_url)
    safe_binary_url = shlex.quote(binary_url)
    safe_update_manifest_url = shlex.quote(update_manifest_url)
    safe_runner_binary_version = shlex.quote(runner_binary_version)

    safe_install_mode = mode or "desktop"
    safe_requested_capabilities = shlex.quote("exec.full")

    template_path = Path(__file__).parent / "templates" / "install.sh"
    script = template_path.read_text()
    # Single-pass replacement via regex to prevent placeholder-collision: if a
    # substituted value itself contains another placeholder string, chained
    # .replace() calls would corrupt it. re.sub processes each position once.
    import re as _re

    _substitutions = {
        "__ENROLL_TOKEN__": safe_enroll_token,
        "__RUNNER_NAME_EXPR__": safe_runner_name_expr,
        "__API_URL__": safe_api_url,
        "__BINARY_URL__": safe_binary_url,
        "__INSTALL_MODE__": safe_install_mode,
        "__REQUESTED_CAPABILITIES__": safe_requested_capabilities,
        "__RUNNER_BINARY_VERSION__": safe_runner_binary_version,
        "__UPDATE_MANIFEST_URL__": safe_update_manifest_url,
    }
    _pattern = _re.compile("|".join(_re.escape(k) for k in _substitutions))
    script = _pattern.sub(lambda m: _substitutions[m.group()], script)

    return Response(
        content=script,
        media_type="text/plain",
        headers={
            "Content-Disposition": "inline; filename=install.sh",
            "Cache-Control": "no-store",
        },
    )


@router.get("/uninstall.sh")
def get_uninstall_script() -> Response:
    """Return shell script for uninstalling the runner.

    This endpoint is designed to be used with curl:
        curl -fsSL https://api.longhouse.ai/api/runners/uninstall.sh | bash

    The script:
    1. Detects OS (macOS/Linux)
    2. Stops and removes the service (launchd/systemd)
    3. Removes binary, config, and state files

    No authentication required.
    """
    from pathlib import Path

    template_path = Path(__file__).parent / "templates" / "uninstall.sh"
    script = template_path.read_text()

    return Response(
        content=script,
        media_type="text/plain",
        headers={
            "Content-Disposition": "inline; filename=uninstall.sh",
            "Cache-Control": "no-store",
        },
    )


@router.post("/enroll-token", response_model=EnrollTokenResponse)
def create_enroll_token(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> EnrollTokenResponse:
    """Create a new enrollment token for registering a runner.

    Returns a one-time token and setup instructions including a complete
    docker run command for easy deployment.
    """
    # Prevent caching of sensitive tokens
    response.headers["Cache-Control"] = "no-store"

    # Create token with 10 minute TTL
    token_record, plaintext_token = runner_crud.create_enroll_token(
        db=db,
        owner_id=current_user.id,
        ttl_minutes=10,
    )

    # Get Longhouse API URL from settings (required in all environments)
    from zerg.config import get_settings

    settings = get_settings()
    if settings.app_public_url:
        api_url = settings.app_public_url.rstrip("/")
    else:
        # In local/demo environments, derive the canonical URL from the current request
        # so runner enrollment still works without APP_PUBLIC_URL.
        api_url = get_request_public_base_url(request)

    runner_image = settings.runner_docker_image
    requested_capabilities = "exec.full"

    # Generate two-step setup instructions (legacy, for manual setup)
    docker_command = (
        f"# Step 1: Register runner (one-time)\n"
        f"curl -X POST {api_url}/api/runners/register \\\n"
        f"  -H 'Content-Type: application/json' \\\n"
        f'  -d \'{{"enroll_token": "{plaintext_token}", "name": "my-runner", "capabilities": ["{requested_capabilities}"]}}\'\n\n'
        f"# Step 2: Save the runner_secret from the response, then run:\n"
        f"docker run -d --name longhouse-runner \\\n"
        f"  -e LONGHOUSE_URL={api_url} \\\n"
        f"  -e RUNNER_NAME=my-runner \\\n"
        f"  -e RUNNER_SECRET=<secret_from_step_1> \\\n"
        f"  {runner_image}"
    )

    # Generate one-liner install command (env var method - avoids token in shell history)
    one_liner_install_command = (
        f"RUNNER_REQUESTED_CAPABILITIES={requested_capabilities} "
        f"ENROLL_TOKEN={plaintext_token} bash -c 'curl -fsSL {api_url}/api/runners/install.sh | bash'"
    )

    return EnrollTokenResponse(
        enroll_token=plaintext_token,
        expires_at=token_record.expires_at,
        longhouse_url=api_url,
        docker_command=docker_command,
        one_liner_install_command=one_liner_install_command,
    )


@router.post("/register", response_model=RunnerRegisterResponse)
async def register_runner(
    request: RunnerRegisterRequest,
    db: Session = Depends(get_db),
) -> RunnerRegisterResponse:
    """Register a new runner using an enrollment token.

    This endpoint is called by the runner daemon during initial setup.
    The enrollment token is consumed and cannot be reused.

    Token consumption is committed BEFORE runner creation to prevent
    token reuse even if runner creation fails.
    """
    # Validate and consume token (commit immediately)
    token_record = runner_crud.validate_and_consume_enroll_token(
        db=db,
        token=request.enroll_token,
    )

    if not token_record:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired enrollment token",
        )

    # Commit token consumption immediately (separate transaction)
    db.commit()

    # Generate runner name if not provided
    if not request.name:
        # Use random suffix to avoid race conditions
        request.name = f"runner-{secrets.token_hex(4)}"

    # Generate auth secret (used for both create and re-enroll)
    auth_secret = runner_crud.generate_token()
    requested_capabilities = runner_crud.normalize_capabilities(request.capabilities)

    # If a runner with this name already exists, rotate its secret (re-enroll path).
    # This handles DB wipes, instance migrations, and lost-credential recovery without
    # requiring delete + re-register.
    existing = runner_crud.get_runner_by_name(
        db=db,
        owner_id=token_record.owner_id,
        name=request.name,
    )
    if existing:
        if existing.status == "revoked":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Runner '{request.name}' is revoked. Delete it before re-enrolling.",
            )
        # Rotate secret in-place — runner ID and history are preserved
        existing.auth_secret_hash = runner_crud.hash_token(auth_secret)
        existing.status = "offline"
        db.commit()
        logger.info(f"Re-enrolled runner '{request.name}' (id={existing.id}, owner={token_record.owner_id})")

        # Kick any currently-connected socket with the old secret so it can't
        # keep executing jobs. The runner must reconnect with the new secret.
        connection_manager = get_runner_connection_manager()
        ws = connection_manager.get_connection(token_record.owner_id, existing.id)
        if ws:
            try:
                await ws.close(code=1008, reason="Runner re-enrolled with new secret")
            except Exception as e:
                logger.warning(f"Failed to close stale socket for runner {existing.id} on re-enroll: {e}")
            connection_manager.unregister(token_record.owner_id, existing.id, ws)

        return RunnerRegisterResponse(
            runner_id=existing.id,
            runner_secret=auth_secret,
            name=existing.name,
            runner_capabilities_csv=",".join(existing.capabilities or ["exec.readonly"]),
        )

    # New runner — create it
    try:
        runner = runner_crud.create_runner(
            db=db,
            owner_id=token_record.owner_id,
            name=request.name,
            auth_secret=auth_secret,
            availability_policy=request.availability_policy,
            labels=request.labels,
            capabilities=requested_capabilities,
            metadata=request.metadata,
        )
    except IntegrityError as e:
        db.rollback()
        logger.error(f"IntegrityError during runner creation: {e}")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Runner with name '{request.name}' already exists",
        )

    return RunnerRegisterResponse(
        runner_id=runner.id,
        runner_secret=auth_secret,
        name=runner.name,
        runner_capabilities_csv=",".join(runner.capabilities or ["exec.readonly"]),
    )


# ---------------------------------------------------------------------------
# Runner Management Endpoints
# ---------------------------------------------------------------------------


@router.get("/status", response_model=RunnerStatusResponse)
def get_runner_status(
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> RunnerStatusResponse:
    """Get runner health summary for status indicators.

    Returns a lightweight summary of runner status for UI health indicators.
    Useful for detecting broken runner connections early.
    """
    timing = ServerTimingRecorder()
    response.headers["Cache-Control"] = "private, max-age=15"

    with timing.span("load_runners"):
        runners = runner_crud.get_runners(db=db, owner_id=current_user.id)
    connection_manager = get_runner_connection_manager()
    with timing.span("serialize_status"):
        serialized = [
            build_runner_response(
                runner,
                is_connected=connection_manager.is_online(runner.owner_id, runner.id),
            )
            for runner in runners
        ]

    online_count = sum(1 for runner in serialized if runner.status == "online")
    offline_count = sum(1 for runner in serialized if runner.status in ("offline", "revoked"))

    result = RunnerStatusResponse(
        total=len(runners),
        online=online_count,
        offline=offline_count,
        runners=[
            RunnerStatusItem(
                name=runner.name,
                availability_policy=runner.availability_policy,
                status=runner.status,
                status_reason=runner.status_reason,
                status_summary=runner.status_summary,
            )
            for runner in serialized
        ],
    )
    timing.apply(response)
    return result


@router.get("/", response_model=RunnerListResponse)
def list_runners(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> RunnerListResponse:
    """List all runners for the authenticated user."""
    runners = runner_crud.get_runners(db=db, owner_id=current_user.id)
    connection_manager = get_runner_connection_manager()
    return RunnerListResponse(
        runners=[
            build_runner_response(
                runner,
                is_connected=connection_manager.is_online(runner.owner_id, runner.id),
            )
            for runner in runners
        ]
    )


@router.delete(
    "/{runner_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_runner(
    runner_id: int = Path(..., gt=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    """Delete a stale runner permanently.

    Use this for cleanup of offline or revoked runners. Connected runners must
    be disconnected or revoked first so users do not accidentally remove a live
    machine from Longhouse.
    """
    runner = runner_crud.get_runner(db=db, runner_id=runner_id)
    if not runner or runner.owner_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Runner not found",
        )

    connection_manager = get_runner_connection_manager()
    if connection_manager.is_online(runner.owner_id, runner.id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot delete a connected runner. Wait for it to disconnect or revoke it first.",
        )

    deleted = runner_crud.delete_runner(db=db, runner_id=runner_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete runner",
        )

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{runner_id}", response_model=RunnerResponse)
def get_runner(
    runner_id: int = Path(..., gt=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> RunnerResponse:
    """Get details of a specific runner."""
    runner = runner_crud.get_runner(db=db, runner_id=runner_id)

    if not runner or runner.owner_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Runner not found",
        )

    connection_manager = get_runner_connection_manager()
    return build_runner_response(
        runner,
        is_connected=connection_manager.is_online(runner.owner_id, runner.id),
    )


@router.post("/preflight", response_model=RunnerPreflightResponse)
def runner_preflight(
    request: RunnerPreflightRequest,
    db: Session = Depends(get_db),
) -> RunnerPreflightResponse:
    """Authenticate runner credentials for local doctor flows."""
    auth = authenticate_runner_identity(
        db,
        runner_id=request.runner_id,
        runner_name=request.runner_name,
        secret=request.secret,
    )
    if not auth.authenticated or auth.runner is None:
        return RunnerPreflightResponse(
            authenticated=False,
            reason_code=auth.reason_code,
            summary=auth.summary,
            runner_id=request.runner_id,
            runner_name=request.runner_name,
        )

    connection_manager = get_runner_connection_manager()
    runner_response = build_runner_response(
        auth.runner,
        is_connected=connection_manager.is_online(auth.runner.owner_id, auth.runner.id),
    )
    return RunnerPreflightResponse(
        authenticated=True,
        reason_code=auth.reason_code,
        summary=auth.summary,
        runner_id=runner_response.id,
        runner_name=runner_response.name,
        status=runner_response.status,
        status_reason=runner_response.status_reason,
        status_summary=runner_response.status_summary,
        last_seen_at=runner_response.last_seen_at,
        last_seen_age_seconds=runner_response.last_seen_age_seconds,
        availability_policy=runner_response.availability_policy,
        install_mode=runner_response.install_mode,
        runner_version=runner_response.runner_version,
        latest_runner_version=runner_response.latest_runner_version,
        version_status=runner_response.version_status,
        capabilities_match=runner_response.capabilities_match,
    )


@router.get("/{runner_id}/jobs", response_model=RunnerJobListResponse)
def list_runner_jobs(
    runner_id: int = Path(..., gt=0),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> RunnerJobListResponse:
    """List recent jobs for a specific runner."""
    runner = runner_crud.get_runner(db=db, runner_id=runner_id)
    if not runner or runner.owner_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Runner not found",
        )

    jobs = runner_crud.get_runner_jobs(db=db, runner_id=runner_id, skip=offset, limit=limit)
    return RunnerJobListResponse(jobs=[RunnerJobResponse.model_validate(job) for job in jobs])


@router.get("/{runner_id}/doctor", response_model=RunnerDoctorResponse)
def get_runner_doctor(
    runner_id: int = Path(..., gt=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> RunnerDoctorResponse:
    """Run server-side doctor diagnostics for a specific runner."""
    runner = runner_crud.get_runner(db=db, runner_id=runner_id)

    if not runner or runner.owner_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Runner not found",
        )

    connection_manager = get_runner_connection_manager()
    return diagnose_runner(
        runner,
        is_connected=connection_manager.is_online(runner.owner_id, runner.id),
    )


@router.patch("/{runner_id}", response_model=RunnerResponse)
def update_runner(
    update: RunnerUpdate,
    runner_id: int = Path(..., gt=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> RunnerResponse:
    """Update a runner's configuration (name, labels, capabilities)."""
    # Verify ownership
    runner = runner_crud.get_runner(db=db, runner_id=runner_id)
    if not runner or runner.owner_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Runner not found",
        )

    # Check for name conflicts if name is being changed
    if update.name and update.name != runner.name:
        existing = runner_crud.get_runner_by_name(
            db=db,
            owner_id=current_user.id,
            name=update.name,
        )
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Runner with name '{update.name}' already exists",
            )

    # Update runner
    try:
        updated_runner = runner_crud.update_runner(
            db=db,
            runner_id=runner_id,
            name=update.name,
            availability_policy=update.availability_policy,
            labels=update.labels,
            capabilities=update.capabilities,
        )
    except IntegrityError as e:
        db.rollback()
        logger.error(f"IntegrityError during runner update: {e}")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Runner with name '{update.name}' already exists",
        )

    if not updated_runner:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update runner",
        )

    connection_manager = get_runner_connection_manager()
    return build_runner_response(
        updated_runner,
        is_connected=connection_manager.is_online(updated_runner.owner_id, updated_runner.id),
    )


@router.post("/{runner_id}/revoke", response_model=RunnerSuccessResponse)
def revoke_runner(
    runner_id: int = Path(..., gt=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> RunnerSuccessResponse:
    """Revoke a runner (mark as revoked, prevent reconnection).

    The runner will be disconnected and cannot reconnect. Jobs will no longer
    be routed to this runner.
    """
    # Verify ownership
    runner = runner_crud.get_runner(db=db, runner_id=runner_id)
    if not runner or runner.owner_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Runner not found",
        )

    # Revoke runner
    revoked_runner = runner_crud.revoke_runner(db=db, runner_id=runner_id)
    if not revoked_runner:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to revoke runner",
        )

    return RunnerSuccessResponse(
        success=True,
        message=f"Runner '{runner.name}' has been revoked",
    )


@router.post("/{runner_id}/rotate-secret", response_model=RunnerRotateSecretResponse)
async def rotate_runner_secret(
    response: Response,
    runner_id: int = Path(..., gt=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> RunnerRotateSecretResponse:
    """Rotate a runner's authentication secret.

    Generates a new secret, invalidating the old one immediately.
    The runner will be disconnected and must reconnect with the new secret.

    WARNING: The new secret is returned only once. Store it securely.
    """
    # Prevent caching of sensitive secrets
    response.headers["Cache-Control"] = "no-store"

    # Verify ownership
    runner = runner_crud.get_runner(db=db, runner_id=runner_id)
    if not runner or runner.owner_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Runner not found",
        )

    # Cannot rotate secret for revoked runners
    if runner.status == "revoked":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot rotate secret for a revoked runner",
        )

    # Rotate the secret
    result = runner_crud.rotate_runner_secret(db=db, runner_id=runner_id)
    if not result:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to rotate runner secret",
        )

    updated_runner, new_secret = result

    # Disconnect the runner if currently connected
    # This forces it to reconnect with the new secret
    connection_manager = get_runner_connection_manager()
    ws = connection_manager.get_connection(current_user.id, runner_id)
    if ws:
        try:
            await ws.close(code=1008, reason="Secret rotated")
            logger.info(f"Disconnected runner {runner_id} after secret rotation")
        except Exception as e:
            logger.warning(f"Failed to close WebSocket for runner {runner_id}: {e}")
        # Unregister the connection
        connection_manager.unregister(current_user.id, runner_id, ws)

    # Update runner status to offline since we disconnected it
    updated_runner.status = "offline"
    db.commit()

    return RunnerRotateSecretResponse(
        runner_id=runner_id,
        runner_secret=new_secret,
        message=f"Secret rotated for runner '{updated_runner.name}'. Update your runner configuration and restart.",
    )


# ---------------------------------------------------------------------------
# Runner WebSocket Endpoint
# ---------------------------------------------------------------------------


async def _runner_websocket_with_db(
    websocket: WebSocket,
    db: Session,
) -> None:
    """WebSocket endpoint for runner connections.

    Protocol:
    1. Runner connects and sends hello message with runner_id + secret
    2. Server validates credentials and marks runner as online
    3. Runner sends periodic heartbeats
    4. Server can send exec_request messages
    5. Runner sends exec_chunk/exec_done/exec_error messages
    6. On disconnect, runner is marked offline
    """
    await websocket.accept()
    connection_manager = get_runner_connection_manager()
    job_dispatcher = get_runner_job_dispatcher()

    runner_id: int | None = None
    owner_id: int | None = None

    try:
        # Wait for hello message
        try:
            hello_data = await websocket.receive_json()
        except WebSocketDisconnect as e:
            logger.info(f"Runner disconnected before hello (code={e.code})")
            return
        except Exception as e:
            logger.warning(f"Failed to receive hello message: {e}")
            await _safe_close_runner_websocket(websocket, code=1008, reason="Invalid hello message")
            return

        # Validate hello message
        if hello_data.get("type") != "hello":
            logger.warning(f"Expected hello message, got: {hello_data.get('type')}")
            await _safe_close_runner_websocket(websocket, code=1008, reason="Expected hello message")
            return

        runner_id = hello_data.get("runner_id")
        runner_name = hello_data.get("runner_name")
        secret = hello_data.get("secret")
        metadata = hello_data.get("metadata", {})

        if not secret:
            logger.warning("Hello message missing secret")
            await _safe_close_runner_websocket(websocket, code=1008, reason="Missing secret")
            return

        if not runner_id and not runner_name:
            logger.warning("Hello message missing runner_id or runner_name")
            await _safe_close_runner_websocket(websocket, code=1008, reason="Missing runner_id or runner_name")
            return

        # Import here for use in heartbeat updates

        auth = authenticate_runner_identity(
            db,
            runner_id=runner_id,
            runner_name=runner_name,
            secret=secret,
        )
        runner = auth.runner
        if not auth.authenticated or not runner:
            logger.warning(
                "Runner websocket auth failed for id=%s name=%s: %s",
                runner_id,
                runner_name,
                auth.reason_code,
            )
            await _safe_close_runner_websocket(websocket, code=1008, reason=auth.summary)
            return

        runner_id = runner.id  # Ensure runner_id is set for name-based auth

        owner_id = runner.owner_id

        # Register connection
        connection_manager.register(owner_id, runner_id, websocket)

        # Update runner status to online via serializer
        _rid = runner_id
        _meta = metadata if metadata else None

        def _mark_online(wdb: Session) -> None:
            r = runner_crud.get_runner(wdb, _rid)
            if r:
                r.status = "online"
                r.last_seen_at = utc_now_naive()
                if _meta:
                    r.runner_metadata = _meta

        try:
            ws = get_write_serializer()
            await ws.execute_or_direct(_mark_online, db, label="runner-online")
        except Exception as e:
            db.rollback()
            logger.error(f"Failed to mark runner {runner_id} online: {e}")
            await _safe_close_runner_websocket(websocket, code=1011, reason="Server DB error")
            return

        logger.info(f"Runner {runner_id} (owner {owner_id}) connected")

        # Enter message loop
        while True:
            try:
                message = await websocket.receive_json()
                message_type = message.get("type")

                if message_type == "heartbeat":
                    # Track in memory only — no DB write per heartbeat.
                    # DB is updated on connect/disconnect; health checks
                    # use runner_heartbeat_cache for liveness.
                    mark_runner_heartbeat(runner_id, seen_at=utc_now_naive())

                elif message_type == "exec_chunk":
                    await _handle_exec_chunk(db, message, runner_id, owner_id)

                elif message_type == "exec_done":
                    await _handle_exec_done(db, message, runner_id, owner_id, job_dispatcher)

                elif message_type == "exec_error":
                    await _handle_exec_error(db, message, runner_id, owner_id, job_dispatcher)

                else:
                    logger.warning(f"Unknown message type from runner {runner_id}: {message_type}")

            except WebSocketDisconnect:
                logger.info(f"Runner {runner_id} disconnected")
                break
            except Exception as e:
                logger.error(f"Error processing message from runner {runner_id}: {e}")
                break

    except Exception as e:
        logger.error(f"Error in runner websocket handler: {e}")

    finally:
        # Cleanup: only unregister and mark offline if this is still the registered connection
        if runner_id and owner_id:
            # Only unregister if this websocket is still the current connection
            was_unregistered = connection_manager.unregister(owner_id, runner_id, websocket)

            # Only mark runner offline if we actually unregistered it (wasn't replaced)
            if was_unregistered:
                _rid = runner_id

                def _mark_offline(wdb: Session) -> None:
                    r = runner_crud.get_runner(wdb, _rid)
                    if r:
                        r.status = "offline"

                try:
                    # Roll back any dirty state from websocket message processing.
                    _rollback_after_write_failure(db, operation="runner websocket cleanup")
                    ws = get_write_serializer()
                    await ws.execute_or_direct(_mark_offline, db, label="runner-offline")
                    logger.info(f"Runner {runner_id} marked offline")
                except Exception as e:
                    logger.warning(f"Failed to mark runner {runner_id} offline during cleanup: {e}")

        await _safe_close_runner_websocket(websocket)


@router.websocket("/ws")
async def runner_websocket(
    websocket: WebSocket,
) -> None:
    worker_id = websocket.query_params.get("worker")
    worker_token = set_test_worker_id(worker_id) if worker_id else None
    db = get_session_factory()()

    try:
        await _runner_websocket_with_db(websocket, db)
    finally:
        db.close()
        if worker_token is not None:
            reset_test_worker_id(worker_token)
