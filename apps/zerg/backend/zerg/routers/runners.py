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
import os
import secrets
import threading
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Path
from fastapi import Response
from fastapi import status
from fastapi import WebSocket
from fastapi import WebSocketDisconnect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from zerg.crud import runner_crud
from zerg.database import get_db
from zerg.dependencies.auth import get_current_user
from zerg.models.models import Runner
from zerg.models.models import User
from zerg.schemas.runner_schemas import EnrollTokenResponse
from zerg.schemas.runner_schemas import RunnerListResponse
from zerg.schemas.runner_schemas import RunnerRegisterRequest
from zerg.schemas.runner_schemas import RunnerRegisterResponse
from zerg.schemas.runner_schemas import RunnerResponse
from zerg.schemas.runner_schemas import RunnerRotateSecretResponse
from zerg.schemas.runner_schemas import RunnerSuccessResponse
from zerg.schemas.runner_schemas import RunnerUpdate
from zerg.services.runner_connection_manager import get_runner_connection_manager
from zerg.services.runner_job_dispatcher import get_runner_job_dispatcher
from zerg.utils.time import utc_now_naive

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/runners",
    tags=["runners"],
)

_REGISTER_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Enrollment Endpoints
# ---------------------------------------------------------------------------


@router.post("/enroll-token", response_model=EnrollTokenResponse)
def create_enroll_token(
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

    # Get Swarmlet API URL from environment (default to localhost for dev)
    swarmlet_url = os.getenv("SWARMLET_API_URL", "http://localhost:47300")

    # Generate two-step setup instructions
    docker_command = (
        f"# Step 1: Register runner (one-time)\n"
        f"curl -X POST {swarmlet_url}/api/runners/register \\\n"
        f"  -H 'Content-Type: application/json' \\\n"
        f"  -d '{{\"enroll_token\": \"{plaintext_token}\", \"name\": \"my-runner\"}}'\n\n"
        f"# Step 2: Run with credentials from step 1\n"
        f"docker run -d --name swarmlet-runner \\\n"
        f"  -e SWARMLET_URL={swarmlet_url} \\\n"
        f"  -e RUNNER_ID=<id_from_step_1> \\\n"
        f"  -e RUNNER_SECRET=<secret_from_step_1> \\\n"
        f"  swarmlet/runner:latest"
    )

    return EnrollTokenResponse(
        enroll_token=plaintext_token,
        expires_at=token_record.expires_at,
        swarmlet_url=swarmlet_url,
        docker_command=docker_command,
    )


@router.post("/register", response_model=RunnerRegisterResponse)
def register_runner(
    request: RunnerRegisterRequest,
    db: Session = Depends(get_db),
) -> RunnerRegisterResponse:
    """Register a new runner using an enrollment token.

    This endpoint is called by the runner daemon during initial setup.
    The enrollment token is consumed and cannot be reused.

    Token consumption is committed BEFORE runner creation to prevent
    token reuse even if runner creation fails.
    """
    # NOTE: Unit tests override the DB dependency to return a shared Session
    # instance across concurrent requests. SQLAlchemy Sessions are not safe for
    # concurrent use, so we serialize registration to avoid invalid session state.
    with _REGISTER_LOCK:
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

        # Check for name conflicts
        existing = runner_crud.get_runner_by_name(
            db=db,
            owner_id=token_record.owner_id,
            name=request.name,
        )
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Runner with name '{request.name}' already exists",
            )

        # Generate auth secret
        auth_secret = runner_crud.generate_token()

        # Create runner (if this fails, token is already consumed - that's intentional)
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
    )


# ---------------------------------------------------------------------------
# Runner Management Endpoints
# ---------------------------------------------------------------------------


@router.get("/", response_model=RunnerListResponse)
def list_runners(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> RunnerListResponse:
    """List all runners for the authenticated user."""
    runners = runner_crud.get_runners(db=db, owner_id=current_user.id)

    return RunnerListResponse(
        runners=[RunnerResponse.model_validate(r) for r in runners]
    )


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
    runner.status = "offline"
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
        except Exception as e:
            logger.error(f"Failed to receive hello message: {e}")
            await websocket.close(code=1008, reason="Invalid hello message")
            return

        # Validate hello message
        if hello_data.get("type") != "hello":
            logger.warning(f"Expected hello message, got: {hello_data.get('type')}")
            await websocket.close(code=1008, reason="Expected hello message")
            return

        runner_id = hello_data.get("runner_id")
        secret = hello_data.get("secret")
        metadata = hello_data.get("metadata", {})

        if not runner_id or not secret:
            logger.warning("Hello message missing runner_id or secret")
            await websocket.close(code=1008, reason="Missing runner_id or secret")
            return

        # Validate credentials
        runner = runner_crud.get_runner(db, runner_id)
        if not runner:
            logger.warning(f"Runner not found: {runner_id}")
            await websocket.close(code=1008, reason="Invalid runner_id")
            return

        # Check secret using constant-time comparison
        computed_hash = runner_crud.hash_token(secret)
        if not secrets.compare_digest(computed_hash, runner.auth_secret_hash):
            logger.warning(f"Invalid secret for runner {runner_id}")
            await websocket.close(code=1008, reason="Invalid secret")
            return

        # Check if runner is revoked
        if runner.status == "revoked":
            logger.warning(f"Revoked runner attempted to connect: {runner_id}")
            await websocket.close(code=1008, reason="Runner has been revoked")
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
                logger.warning(
                    f"Runner {runner_id} capability mismatch: "
                    f"DB={runner.capabilities}, reported={reported_caps}"
                )

        db.commit()

        logger.info(f"Runner {runner_id} (owner {owner_id}) connected")

        # Enter message loop
        while True:
            try:
                message = await websocket.receive_json()
                message_type = message.get("type")

                if message_type == "heartbeat":
                    # Update last_seen_at
                    runner.last_seen_at = utc_now_naive()
                    db.commit()
                    logger.debug(f"Heartbeat from runner {runner_id}")

                elif message_type == "exec_chunk":
                    # Handle output streaming
                    job_id = message.get("job_id")
                    stream = message.get("stream")
                    data = message.get("data")
                    logger.debug(
                        f"Exec chunk from runner {runner_id}, job {job_id}, stream {stream}"
                    )

                    # Update job output in database
                    if job_id and stream and data:
                        job = runner_crud.get_job(db, job_id)
                        if not job or job.runner_id != runner_id or job.owner_id != owner_id:
                            logger.warning(
                                f"Ignoring exec_chunk for invalid job {job_id} from runner {runner_id}"
                            )
                        else:
                            runner_crud.update_job_output(db, job_id, stream, data)

                elif message_type == "exec_done":
                    # Handle job completion
                    job_id = message.get("job_id")
                    exit_code = message.get("exit_code")
                    duration_ms = message.get("duration_ms")
                    logger.info(
                        f"Exec done from runner {runner_id}, job {job_id}, exit_code {exit_code}"
                    )

                    # Update job status in database
                    if job_id is not None and exit_code is not None:
                        job = runner_crud.get_job(db, job_id)
                        if not job or job.runner_id != runner_id or job.owner_id != owner_id:
                            logger.warning(
                                f"Ignoring exec_done for invalid job {job_id} from runner {runner_id}"
                            )
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
                    logger.error(
                        f"Exec error from runner {runner_id}, job {job_id}: {error}"
                    )

                    # Update job status in database
                    if job_id and error:
                        job = runner_crud.get_job(db, job_id)
                        if not job or job.runner_id != runner_id or job.owner_id != owner_id:
                            logger.warning(
                                f"Ignoring exec_error for invalid job {job_id} from runner {runner_id}"
                            )
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
                    logger.warning(
                        f"Unknown message type from runner {runner_id}: {message_type}"
                    )

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
                runner = runner_crud.get_runner(db, runner_id)
                if runner:
                    runner.status = "offline"
                    db.commit()
                    logger.info(f"Runner {runner_id} marked offline")

        try:
            await websocket.close()
        except Exception:
            pass  # Already closed
