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
from fastapi import Request
from fastapi import Response
from fastapi import WebSocket
from fastapi import WebSocketDisconnect
from fastapi import status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from zerg.crud import runner_crud
from zerg.database import get_db
from zerg.dependencies.auth import get_current_user
from zerg.models.models import User
from zerg.schemas.runner_schemas import EnrollTokenResponse
from zerg.schemas.runner_schemas import RunnerDoctorResponse
from zerg.schemas.runner_schemas import RunnerListResponse
from zerg.schemas.runner_schemas import RunnerRegisterRequest
from zerg.schemas.runner_schemas import RunnerRegisterResponse
from zerg.schemas.runner_schemas import RunnerResponse
from zerg.schemas.runner_schemas import RunnerRotateSecretResponse
from zerg.schemas.runner_schemas import RunnerStatusItem
from zerg.schemas.runner_schemas import RunnerStatusResponse
from zerg.schemas.runner_schemas import RunnerSuccessResponse
from zerg.schemas.runner_schemas import RunnerUpdate
from zerg.services.runner_connection_manager import get_runner_connection_manager
from zerg.services.runner_doctor import diagnose_runner
from zerg.services.runner_job_dispatcher import get_runner_job_dispatcher
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


async def _handle_exec_chunk(
    db: Session,
    message: dict,
    runner_id: int,
    owner_id: int,
) -> None:
    """Process an exec_chunk message from a runner.

    Appends output to the job record, feeds the commis output buffer, and
    publishes a live SSE chunk for any waiting run subscribers.
    """
    import time

    from zerg.events import EventType
    from zerg.events.event_bus import event_bus
    from zerg.models.models import CommisJob
    from zerg.services.commis_output_buffer import get_commis_output_buffer

    job_id = message.get("job_id")
    stream = message.get("stream")
    data = message.get("data")
    logger.debug(f"Exec chunk from runner {runner_id}, job {job_id}, stream {stream}")

    if not (job_id and stream and data):
        return

    job = runner_crud.get_job(db, job_id)
    if not job or job.runner_id != runner_id or job.owner_id != owner_id:
        logger.warning(f"Ignoring exec_chunk for invalid job {job_id} from runner {runner_id}")
        return

    updated_job = runner_crud.update_job_output(db, job_id, stream, data)
    if not updated_job or not updated_job.commis_id:
        return

    output_buffer = get_commis_output_buffer()

    # Resolve commis job metadata once (cached in buffer)
    commis_job_id = None
    trace_id = None
    meta = output_buffer.get_meta(updated_job.commis_id)
    last_resolved_at = 0
    if meta:
        commis_job_id = meta.job_id
        trace_id = meta.trace_id
        last_resolved_at = meta.last_resolved_at

    # Throttle DB lookup to once per 5 seconds if not yet resolved
    if commis_job_id is None and (time.time() - last_resolved_at) > 5.0:
        commis_job = (
            db.query(CommisJob)
            .filter(
                CommisJob.commis_id == updated_job.commis_id,
                CommisJob.owner_id == owner_id,
            )
            .order_by(CommisJob.id.desc())
            .first()
        )
        if commis_job:
            commis_job_id = commis_job.id
            trace_id = str(commis_job.trace_id) if commis_job.trace_id else None

        # Mark as resolved (even if not found, to trigger throttling)
        output_buffer.append_output(
            commis_id=updated_job.commis_id,
            stream=stream,
            data="",  # Don't append data here, just updating meta
            job_id=commis_job_id,
            trace_id=trace_id,
            owner_id=owner_id,
            resolved=True,
        )

    run_id_int = None
    if updated_job.run_id is not None:
        try:
            run_id_int = int(updated_job.run_id)
        except (TypeError, ValueError):
            run_id_int = None

    output_buffer.append_output(
        commis_id=updated_job.commis_id,
        stream=stream,
        data=data,
        runner_job_id=job_id,
        job_id=commis_job_id,
        run_id=run_id_int,
        trace_id=trace_id,
        owner_id=owner_id,
    )

    # Publish live output chunk (ephemeral SSE only; not persisted)
    if run_id_int:
        MAX_CHUNK_CHARS = 4000
        payload = {
            "job_id": commis_job_id,
            "commis_id": updated_job.commis_id,
            "runner_job_id": job_id,
            "stream": stream,
            "data": data[-MAX_CHUNK_CHARS:] if len(data) > MAX_CHUNK_CHARS else data,
            "run_id": run_id_int,
            "trace_id": trace_id,
            "owner_id": owner_id,
        }
        await event_bus.publish(EventType.COMMIS_OUTPUT_CHUNK, payload)


# ---------------------------------------------------------------------------
# Enrollment Endpoints
# ---------------------------------------------------------------------------


@router.get("/install.sh")
def get_install_script(
    enroll_token: str,
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

    # Validate enroll_token format (alphanumeric + dash/underscore only)
    if not re.match(r"^[A-Za-z0-9_-]+$", enroll_token):
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

    # Shell-escape all substituted values to prevent injection
    safe_enroll_token = shlex.quote(enroll_token)
    safe_runner_name_expr = shlex.quote(runner_name) if runner_name else "$(hostname)"
    safe_api_url = shlex.quote(api_url)
    safe_binary_url = shlex.quote(binary_url)

    safe_install_mode = mode or "desktop"

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
        api_url = str(request.base_url).rstrip("/")

    runner_image = settings.runner_docker_image

    # Generate two-step setup instructions (legacy, for manual setup)
    docker_command = (
        f"# Step 1: Register runner (one-time)\n"
        f"curl -X POST {api_url}/api/runners/register \\\n"
        f"  -H 'Content-Type: application/json' \\\n"
        f'  -d \'{{"enroll_token": "{plaintext_token}", "name": "my-runner"}}\'\n\n'
        f"# Step 2: Save the runner_secret from the response, then run:\n"
        f"docker run -d --name longhouse-runner \\\n"
        f"  -e LONGHOUSE_URL={api_url} \\\n"
        f"  -e RUNNER_NAME=my-runner \\\n"
        f"  -e RUNNER_SECRET=<secret_from_step_1> \\\n"
        f"  {runner_image}"
    )

    # Generate one-liner install command (env var method - avoids token in shell history)
    one_liner_install_command = f"ENROLL_TOKEN={plaintext_token} bash -c 'curl -fsSL {api_url}/api/runners/install.sh | bash'"

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
            labels=request.labels,
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
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> RunnerStatusResponse:
    """Get runner health summary for status indicators.

    Returns a lightweight summary of runner status for UI health indicators.
    Useful for detecting broken runner connections early.
    """
    runners = runner_crud.get_runners(db=db, owner_id=current_user.id)

    online_count = sum(1 for r in runners if r.status == "online")
    offline_count = sum(1 for r in runners if r.status in ("offline", "revoked"))

    return RunnerStatusResponse(
        total=len(runners),
        online=online_count,
        offline=offline_count,
        runners=[RunnerStatusItem(name=r.name, status=r.status) for r in runners],
    )


@router.get("/", response_model=RunnerListResponse)
def list_runners(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> RunnerListResponse:
    """List all runners for the authenticated user."""
    runners = runner_crud.get_runners(db=db, owner_id=current_user.id)

    return RunnerListResponse(runners=[RunnerResponse.model_validate(r) for r in runners])


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

    return RunnerResponse.model_validate(runner)


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

    return diagnose_runner(runner)


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

    return RunnerResponse.model_validate(updated_runner)


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


@router.websocket("/ws")
async def runner_websocket(
    websocket: WebSocket,
    db: Session = Depends(get_db),
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

        computed_hash = runner_crud.hash_token(secret)

        # Look up runner by ID or name
        # Name-based auth requires iterating users, but since the secret is unique
        # per runner, we can validate after finding by name across all users
        # Import here for use in heartbeat updates (needed regardless of auth path)
        from sqlalchemy import select
        from sqlalchemy import update

        from zerg.models.models import Runner as RunnerModel

        runner = None
        if runner_id:
            runner = runner_crud.get_runner(db, runner_id)
        elif runner_name:
            # Name-based auth: names are only unique per-owner, so we bind name+secret.
            # Note: if two owners have runners with same name AND same secret hash,
            # this returns the first match. This is a config error but shouldn't crash.
            stmt = select(RunnerModel).where(RunnerModel.name == runner_name, RunnerModel.auth_secret_hash == computed_hash)
            results = db.execute(stmt).scalars().all()
            if len(results) > 1:
                logger.warning(f"Multiple runners found with name '{runner_name}' and same secret hash - using first match")
            runner = results[0] if results else None
            if not runner:
                logger.warning(f"Runner not found by name: {runner_name}")
                await _safe_close_runner_websocket(websocket, code=1008, reason="Invalid runner_name or secret")
                return

        if not runner:
            logger.warning(f"Runner not found: {runner_id}")
            await _safe_close_runner_websocket(websocket, code=1008, reason="Invalid runner_id")
            return

        runner_id = runner.id  # Ensure runner_id is set for name-based auth

        # Check secret using constant-time comparison
        if not secrets.compare_digest(computed_hash, runner.auth_secret_hash):
            logger.warning(f"Invalid secret for runner {runner_id}")
            await _safe_close_runner_websocket(websocket, code=1008, reason="Invalid secret")
            return

        # Check if runner is revoked
        if runner.status == "revoked":
            logger.warning(f"Revoked runner attempted to connect: {runner_id}")
            await _safe_close_runner_websocket(websocket, code=1008, reason="Runner has been revoked")
            return

        owner_id = runner.owner_id

        # Register connection
        connection_manager.register(owner_id, runner_id, websocket)

        # Update runner status to online
        runner.status = "online"
        runner.last_seen_at = utc_now_naive()
        if metadata:
            runner.runner_metadata = metadata

            # Validate runner capabilities match what's in the database
            reported_caps = metadata.get("capabilities", [])
            if reported_caps and set(reported_caps) != set(runner.capabilities):
                logger.warning(f"Runner {runner_id} capability mismatch: DB={runner.capabilities}, reported={reported_caps}")

        try:
            db.commit()
        except Exception as e:
            # If DB commit fails, the session is poisoned until rollback.
            # Close the websocket so the runner will reconnect cleanly.
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
                    # Update last_seen_at (no log - too noisy at 30s intervals)
                    try:
                        stmt = update(RunnerModel).where(RunnerModel.id == runner_id).values(last_seen_at=utc_now_naive())
                        result = db.execute(stmt)
                        if result.rowcount != 1:
                            db.rollback()
                            logger.warning(f"Runner {runner_id} missing during heartbeat (rowcount={result.rowcount})")
                            await _safe_close_runner_websocket(websocket, code=1008, reason="Runner not found")
                            break
                        db.commit()
                    except StaleDataError as e:
                        db.rollback()
                        logger.warning(f"Runner {runner_id} stale during heartbeat: {e}")
                        await _safe_close_runner_websocket(websocket, code=1011, reason="Stale runner state")
                        break
                    except Exception as e:
                        db.rollback()
                        logger.error(f"DB error during heartbeat for runner {runner_id}: {e}")
                        await _safe_close_runner_websocket(websocket, code=1011, reason="Server DB error")
                        break

                elif message_type == "exec_chunk":
                    await _handle_exec_chunk(db, message, runner_id, owner_id)

                elif message_type == "exec_done":
                    # Handle job completion
                    job_id = message.get("job_id")
                    exit_code = message.get("exit_code")
                    duration_ms = message.get("duration_ms")
                    logger.info(f"Exec done from runner {runner_id}, job {job_id}, exit_code {exit_code}")

                    # Update job status in database
                    if job_id is not None and exit_code is not None:
                        job = runner_crud.get_job(db, job_id)
                        if not job or job.runner_id != runner_id or job.owner_id != owner_id:
                            logger.warning(f"Ignoring exec_done for invalid job {job_id} from runner {runner_id}")
                            continue

                        runner_crud.update_job_completed(db, job_id, exit_code, duration_ms or 0)

                        # Get final job state to return
                        job = runner_crud.get_job(db, job_id)
                        if job:
                            result = {
                                "ok": True,
                                "data": {
                                    "job_id": job_id,
                                    "exit_code": exit_code,
                                    "stdout": job.stdout_trunc or "",
                                    "stderr": job.stderr_trunc or "",
                                    "duration_ms": duration_ms or 0,
                                },
                            }
                            job_dispatcher.complete_job(job_id, result, runner_id)

                elif message_type == "exec_error":
                    # Handle job error
                    job_id = message.get("job_id")
                    error = message.get("error")
                    logger.error(f"Exec error from runner {runner_id}, job {job_id}: {error}")

                    # Update job status in database
                    if job_id and error:
                        job = runner_crud.get_job(db, job_id)
                        if not job or job.runner_id != runner_id or job.owner_id != owner_id:
                            logger.warning(f"Ignoring exec_error for invalid job {job_id} from runner {runner_id}")
                            continue

                        runner_crud.update_job_error(db, job_id, error)

                        # Notify waiting dispatcher
                        result = {
                            "ok": False,
                            "error": {
                                "type": "execution_error",
                                "message": error,
                            },
                        }
                        job_dispatcher.complete_job(job_id, result, runner_id)

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
                try:
                    # If the session is in a failed state (e.g. earlier flush error), reset it first.
                    db.rollback()
                except Exception:
                    pass

                try:
                    runner = runner_crud.get_runner(db, runner_id)
                    if runner:
                        runner.status = "offline"
                        db.commit()
                        logger.info(f"Runner {runner_id} marked offline")
                except Exception as e:
                    try:
                        db.rollback()
                    except Exception:
                        pass
                    logger.warning(f"Failed to mark runner {runner_id} offline during cleanup: {e}")

        try:
            await _safe_close_runner_websocket(websocket)
        except Exception:
            pass  # Already closed
