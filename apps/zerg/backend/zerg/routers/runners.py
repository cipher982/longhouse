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
import threading

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Path
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


@router.get("/install.sh")
def get_install_script(
    enroll_token: str,
    runner_name: str | None = None,
    swarmlet_url: str | None = None,
    runner_image: str | None = None,
) -> Response:
    """Return shell script for one-liner runner installation.

    This endpoint is designed to be used with curl:
        curl -fsSL https://api.swarmlet.com/api/runners/install.sh?enroll_token=xxx | bash

    Or with environment variables:
        curl -fsSL https://api.swarmlet.com/api/runners/install.sh | \
            ENROLL_TOKEN=xxx RUNNER_NAME=my-runner bash

    The script:
    1. Registers the runner using the enroll token
    2. Saves credentials to ~/.config/swarmlet/runner.env
    3. Starts the runner container with docker run

    No authentication required - this is for bootstrapping new runners.
    """
    from zerg.config import get_settings

    settings = get_settings()

    # Default values from query params or settings
    if not swarmlet_url:
        if not settings.app_public_url:
            if settings.testing:
                swarmlet_url = "http://localhost:30080"
            else:
                return Response(
                    content="Error: APP_PUBLIC_URL not configured on server",
                    media_type="text/plain",
                    status_code=500,
                )
        else:
            swarmlet_url = settings.app_public_url

    if not runner_image:
        runner_image = settings.runner_docker_image

    # Generate the shell script
    default_runner_name = runner_name or "$(hostname)"
    script = f"""#!/bin/bash
set -e

# Swarmlet Runner Installer
# This script registers a runner and starts it with Docker

# Configuration (can be overridden via env vars)
ENROLL_TOKEN="${{ENROLL_TOKEN:-{enroll_token}}}"
RUNNER_NAME="${{RUNNER_NAME:-{default_runner_name}}}"
SWARMLET_URL="${{SWARMLET_URL:-{swarmlet_url}}}"
RUNNER_IMAGE="${{RUNNER_IMAGE:-{runner_image}}}"

# Validate required vars
if [ -z "$ENROLL_TOKEN" ]; then
  echo "Error: ENROLL_TOKEN is required" >&2
  exit 1
fi

if [ -z "$SWARMLET_URL" ]; then
  echo "Error: SWARMLET_URL is required" >&2
  exit 1
fi

echo "Registering runner '$RUNNER_NAME' with Swarmlet..."

# Register runner and get credentials
REGISTER_URL="${{SWARMLET_URL}}/api/runners/register"
RESPONSE=$(curl -sf -X POST "$REGISTER_URL" \\
  -H "Content-Type: application/json" \\
  -d "{{\\\"enroll_token\\\": \\\"$ENROLL_TOKEN\\\", \\\"name\\\": \\\"$RUNNER_NAME\\\"}}")

if [ $? -ne 0 ]; then
  echo "Error: Failed to register runner. Check your enrollment token." >&2
  exit 1
fi

# Parse JSON response
# Try python3 first, fallback to node, otherwise error
if command -v python3 >/dev/null 2>&1; then
  RUNNER_SECRET=$(echo "$RESPONSE" | python3 -c "import sys, json; print(json.load(sys.stdin)['runner_secret'])")
  RUNNER_NAME=$(echo "$RESPONSE" | python3 -c "import sys, json; print(json.load(sys.stdin)['name'])")
elif command -v node >/dev/null 2>&1; then
  RUNNER_SECRET=$(echo "$RESPONSE" | node -e "console.log(JSON.parse(require('fs').readFileSync(0, 'utf-8')).runner_secret)")
  RUNNER_NAME=$(echo "$RESPONSE" | node -e "console.log(JSON.parse(require('fs').readFileSync(0, 'utf-8')).name)")
else
  echo "Error: Please install python3 or node to parse JSON response" >&2
  exit 1
fi

if [ -z "$RUNNER_SECRET" ]; then
  echo "Error: Failed to parse runner credentials from response" >&2
  exit 1
fi

echo "Runner registered successfully: $RUNNER_NAME"

# Save credentials to config file
CONFIG_DIR="$HOME/.config/swarmlet"
CONFIG_FILE="$CONFIG_DIR/runner.env"

mkdir -p "$CONFIG_DIR"
cat > "$CONFIG_FILE" <<EOF
SWARMLET_URL=$SWARMLET_URL
RUNNER_NAME=$RUNNER_NAME
RUNNER_SECRET=$RUNNER_SECRET
EOF

chmod 600 "$CONFIG_FILE"

echo "Credentials saved to $CONFIG_FILE"

# Start runner container
echo "Starting runner container..."

docker run -d --name swarmlet-runner \\
  --env-file "$CONFIG_FILE" \\
  --restart unless-stopped \\
  "$RUNNER_IMAGE"

if [ $? -eq 0 ]; then
  echo "Runner container started successfully!"
  echo "Container name: swarmlet-runner"
  echo ""
  echo "Check status with: docker logs swarmlet-runner"
else
  echo "Error: Failed to start runner container" >&2
  exit 1
fi
"""

    return Response(
        content=script,
        media_type="text/plain",
        headers={
            "Content-Disposition": "inline; filename=install.sh",
            "Cache-Control": "no-store",
        },
    )


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

    # Get Swarmlet API URL from settings (required in all environments)
    from zerg.config import get_settings

    settings = get_settings()
    # In test mode, use a placeholder URL
    if not settings.app_public_url:
        if settings.testing:
            swarmlet_url = "http://localhost:30080"
        else:
            raise HTTPException(
                status_code=500,
                detail="APP_PUBLIC_URL not configured. Set this in your environment.",
            )
    else:
        swarmlet_url = settings.app_public_url

    runner_image = settings.runner_docker_image

    # Generate two-step setup instructions (legacy, for manual setup)
    docker_command = (
        f"# Step 1: Register runner (one-time)\n"
        f"curl -X POST {swarmlet_url}/api/runners/register \\\n"
        f"  -H 'Content-Type: application/json' \\\n"
        f'  -d \'{{"enroll_token": "{plaintext_token}", "name": "my-runner"}}\'\n\n'
        f"# Step 2: Save the runner_secret from the response, then run:\n"
        f"docker run -d --name swarmlet-runner \\\n"
        f"  -e SWARMLET_URL={swarmlet_url} \\\n"
        f"  -e RUNNER_NAME=my-runner \\\n"
        f"  -e RUNNER_SECRET=<secret_from_step_1> \\\n"
        f"  {runner_image}"
    )

    # Generate one-liner install command (recommended method)
    one_liner_install_command = f"curl -fsSL {swarmlet_url}/api/runners/install.sh?enroll_token={plaintext_token} | bash"

    return EnrollTokenResponse(
        enroll_token=plaintext_token,
        expires_at=token_record.expires_at,
        swarmlet_url=swarmlet_url,
        docker_command=docker_command,
        one_liner_install_command=one_liner_install_command,
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
        runner_name = hello_data.get("runner_name")
        secret = hello_data.get("secret")
        metadata = hello_data.get("metadata", {})

        if not secret:
            logger.warning("Hello message missing secret")
            await websocket.close(code=1008, reason="Missing secret")
            return

        if not runner_id and not runner_name:
            logger.warning("Hello message missing runner_id or runner_name")
            await websocket.close(code=1008, reason="Missing runner_id or runner_name")
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
            stmt = select(RunnerModel).where(RunnerModel.name == runner_name, RunnerModel.auth_secret_hash == computed_hash)
            runner = db.execute(stmt).scalar_one_or_none()
            if not runner:
                logger.warning(f"Runner not found by name: {runner_name}")
                await websocket.close(code=1008, reason="Invalid runner_name or secret")
                return

        if not runner:
            logger.warning(f"Runner not found: {runner_id}")
            await websocket.close(code=1008, reason="Invalid runner_id")
            return

        runner_id = runner.id  # Ensure runner_id is set for name-based auth

        # Check secret using constant-time comparison
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
                logger.warning(f"Runner {runner_id} capability mismatch: DB={runner.capabilities}, reported={reported_caps}")

        try:
            db.commit()
        except Exception as e:
            # If DB commit fails, the session is poisoned until rollback.
            # Close the websocket so the runner will reconnect cleanly.
            db.rollback()
            logger.error(f"Failed to mark runner {runner_id} online: {e}")
            await websocket.close(code=1011, reason="Server DB error")
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
                            await websocket.close(code=1008, reason="Runner not found")
                            break
                        db.commit()
                    except StaleDataError as e:
                        db.rollback()
                        logger.warning(f"Runner {runner_id} stale during heartbeat: {e}")
                        await websocket.close(code=1011, reason="Stale runner state")
                        break
                    except Exception as e:
                        db.rollback()
                        logger.error(f"DB error during heartbeat for runner {runner_id}: {e}")
                        await websocket.close(code=1011, reason="Server DB error")
                        break

                elif message_type == "exec_chunk":
                    # Handle output streaming
                    job_id = message.get("job_id")
                    stream = message.get("stream")
                    data = message.get("data")
                    logger.debug(f"Exec chunk from runner {runner_id}, job {job_id}, stream {stream}")

                    # Update job output in database
                    if job_id and stream and data:
                        job = runner_crud.get_job(db, job_id)
                        if not job or job.runner_id != runner_id or job.owner_id != owner_id:
                            logger.warning(f"Ignoring exec_chunk for invalid job {job_id} from runner {runner_id}")
                        else:
                            runner_crud.update_job_output(db, job_id, stream, data)

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
            await websocket.close()
        except Exception:
            pass  # Already closed
