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
from zerg.schemas.runner_schemas import RunnerStatusItem
from zerg.schemas.runner_schemas import RunnerStatusResponse
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
    swarmlet_url: str | None = None,  # Deprecated: use longhouse_url instead
    longhouse_url: str | None = None,
    mode: str | None = None,  # Reserved for future: user|system
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
    4. Installs as a launchd (macOS) or systemd (Linux) service
    5. Starts the runner automatically

    No authentication required - this is for bootstrapping new runners.
    """
    import re
    import shlex

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

    # Prefer longhouse_url, fall back to swarmlet_url (deprecated), then settings
    # Note: We don't accept arbitrary URLs from query params for security
    api_url = swarmlet_url  # Only accept deprecated param for backwards compat
    if longhouse_url:
        # Validate URL format to prevent injection
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

    # GitHub releases URL for binaries (hardcoded, not user-provided)
    binary_url = "https://github.com/cipher982/longhouse/releases/latest/download"

    # Shell-escape all user-provided values to prevent command injection
    safe_enroll_token = shlex.quote(enroll_token)
    safe_runner_name = shlex.quote(runner_name) if runner_name else ""
    safe_api_url = shlex.quote(api_url)
    safe_binary_url = shlex.quote(binary_url)

    # Generate the shell script
    # Note: Values are shell-quoted to prevent command injection
    default_runner_name_expr = safe_runner_name if runner_name else "$(hostname)"
    script = f"""#!/bin/bash
# Longhouse Runner - Universal Installer
# Detects OS, registers runner, and installs as native service
#
# macOS: launchd LaunchAgent
# Linux: systemd user service

set -e

# Configuration (can be overridden via env vars)
# Values are pre-validated and shell-escaped by the server
ENROLL_TOKEN="${{ENROLL_TOKEN:-{safe_enroll_token}}}"
RUNNER_NAME="${{RUNNER_NAME:-{default_runner_name_expr}}}"
LONGHOUSE_URL="${{LONGHOUSE_URL:-{safe_api_url}}}"
BINARY_URL="${{BINARY_URL:-{safe_binary_url}}}"

# Validate required vars
if [ -z "$ENROLL_TOKEN" ]; then
  echo "Error: ENROLL_TOKEN is required" >&2
  exit 1
fi

if [ -z "$LONGHOUSE_URL" ]; then
  echo "Error: LONGHOUSE_URL is required" >&2
  exit 1
fi

echo "======================================"
echo "Longhouse Runner Installer"
echo "======================================"
echo ""
echo "Runner Name: $RUNNER_NAME"
echo "API URL: $LONGHOUSE_URL"
echo ""

# Detect OS
OS="$(uname -s)"
case "$OS" in
  Darwin) OS_TYPE="macos" ;;
  Linux) OS_TYPE="linux" ;;
  MINGW*|MSYS*|CYGWIN*)
    echo "Error: Windows is not yet supported. Coming soon!" >&2
    exit 1
    ;;
  *)
    echo "Error: Unsupported operating system: $OS" >&2
    exit 1
    ;;
esac

echo "Detected OS: $OS_TYPE"
echo ""

# Check for required tools
if ! command -v curl >/dev/null 2>&1; then
  echo "Error: curl is required but not installed" >&2
  exit 1
fi

# JSON parsing helper
parse_json() {{
  local json="$1"
  local field="$2"

  if command -v python3 >/dev/null 2>&1; then
    echo "$json" | python3 -c "import sys, json; print(json.load(sys.stdin).get('$field', ''))"
  elif command -v node >/dev/null 2>&1; then
    echo "$json" | node -e "const d=JSON.parse(require('fs').readFileSync(0,'utf-8'));console.log(d['$field']||'')"
  else
    echo ""
  fi
}}

# Register runner with backend
echo "Registering runner '$RUNNER_NAME' with Longhouse..."

REGISTER_URL="${{LONGHOUSE_URL}}/api/runners/register"
RESPONSE=$(curl -sf -X POST "$REGISTER_URL" \\
  -H "Content-Type: application/json" \\
  -d "{{\\"enroll_token\\": \\"$ENROLL_TOKEN\\", \\"name\\": \\"$RUNNER_NAME\\"}}" 2>&1) || {{
  echo "Error: Failed to register runner. Check your enrollment token." >&2
  # Don't print response - may contain secrets on partial success
  exit 1
}}

# Parse response (don't print raw response - contains runner_secret)
RUNNER_SECRET=$(parse_json "$RESPONSE" "runner_secret")
RUNNER_NAME=$(parse_json "$RESPONSE" "name")

if [ -z "$RUNNER_SECRET" ]; then
  if command -v python3 >/dev/null 2>&1 || command -v node >/dev/null 2>&1; then
    echo "Error: Failed to parse runner credentials from response" >&2
    echo "Hint: Server may have returned an error. Check your enrollment token." >&2
  else
    echo "Error: Please install python3 or node to parse JSON response" >&2
  fi
  exit 1
fi

echo "Runner registered successfully!"
echo ""

# Platform-specific installation
case "$OS_TYPE" in
  macos)
    ARCH=$(uname -m)
    case "$ARCH" in
      arm64|aarch64) PLATFORM="darwin-arm64" ;;
      x86_64) PLATFORM="darwin-x64" ;;
      *) echo "Error: Unsupported architecture: $ARCH" >&2; exit 1 ;;
    esac

    BIN_DIR="$HOME/.local/bin"
    CONFIG_DIR="$HOME/.config/longhouse"
    STATE_DIR="$HOME/.local/state/longhouse"
    LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"

    mkdir -p "$BIN_DIR" "$CONFIG_DIR" "$STATE_DIR" "$LAUNCH_AGENTS_DIR"
    # Restrict state dir permissions (logs may contain sensitive output)
    chmod 700 "$STATE_DIR"
    touch "$STATE_DIR/runner.log"
    chmod 600 "$STATE_DIR/runner.log"

    BINARY_PATH="$BIN_DIR/longhouse-runner"
    DOWNLOAD_URL="${{BINARY_URL}}/longhouse-runner-${{PLATFORM}}"

    echo "Downloading runner binary ($PLATFORM)..."
    if ! curl -fsSL "$DOWNLOAD_URL" -o "$BINARY_PATH"; then
      echo "Error: Failed to download runner binary from $DOWNLOAD_URL" >&2
      exit 1
    fi
    chmod +x "$BINARY_PATH"

    ENV_FILE="$CONFIG_DIR/runner.env"
    cat > "$ENV_FILE" <<EOF
LONGHOUSE_URL=$LONGHOUSE_URL
RUNNER_NAME=$RUNNER_NAME
RUNNER_SECRET=$RUNNER_SECRET
EOF
    chmod 600 "$ENV_FILE"
    echo "Credentials saved to $ENV_FILE"

    PLIST_FILE="$LAUNCH_AGENTS_DIR/com.longhouse.runner.plist"
    cat > "$PLIST_FILE" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.longhouse.runner</string>
    <key>ProgramArguments</key>
    <array>
        <string>$BINARY_PATH</string>
        <string>--envfile</string>
        <string>$ENV_FILE</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>WorkingDirectory</key>
    <string>$HOME</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>$STATE_DIR/runner.log</string>
    <key>StandardErrorPath</key>
    <string>$STATE_DIR/runner.log</string>
</dict>
</plist>
EOF

    if launchctl list 2>/dev/null | grep -q "com.longhouse.runner"; then
      echo "Stopping existing runner..."
      launchctl unload "$PLIST_FILE" 2>/dev/null || true
    fi

    echo "Starting runner service..."
    launchctl load "$PLIST_FILE"

    echo ""
    echo "Runner installed and started successfully!"
    echo ""
    echo "Management commands:"
    echo "  launchctl stop com.longhouse.runner      # Stop"
    echo "  launchctl start com.longhouse.runner     # Start"
    echo "  tail -f $STATE_DIR/runner.log            # View logs"
    ;;

  linux)
    ARCH=$(uname -m)
    case "$ARCH" in
      aarch64|arm64) PLATFORM="linux-arm64" ;;
      x86_64) PLATFORM="linux-x64" ;;
      *) echo "Error: Unsupported architecture: $ARCH" >&2; exit 1 ;;
    esac

    BIN_DIR="$HOME/.local/bin"
    CONFIG_DIR="$HOME/.config/longhouse"
    SYSTEMD_USER_DIR="$HOME/.config/systemd/user"

    mkdir -p "$BIN_DIR" "$CONFIG_DIR" "$SYSTEMD_USER_DIR"
    # Restrict config dir permissions (contains secrets)
    chmod 700 "$CONFIG_DIR"

    BINARY_PATH="$BIN_DIR/longhouse-runner"
    DOWNLOAD_URL="${{BINARY_URL}}/longhouse-runner-${{PLATFORM}}"

    echo "Downloading runner binary ($PLATFORM)..."
    if ! curl -fsSL "$DOWNLOAD_URL" -o "$BINARY_PATH"; then
      echo "Error: Failed to download runner binary from $DOWNLOAD_URL" >&2
      exit 1
    fi
    chmod +x "$BINARY_PATH"

    ENV_FILE="$CONFIG_DIR/runner.env"
    cat > "$ENV_FILE" <<EOF
LONGHOUSE_URL=$LONGHOUSE_URL
RUNNER_NAME=$RUNNER_NAME
RUNNER_SECRET=$RUNNER_SECRET
EOF
    chmod 600 "$ENV_FILE"
    echo "Credentials saved to $ENV_FILE"

    SERVICE_FILE="$SYSTEMD_USER_DIR/longhouse-runner.service"
    cat > "$SERVICE_FILE" <<'SERVICEEOF'
[Unit]
Description=Longhouse Runner
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=%h/.local/bin/longhouse-runner --envfile %h/.config/longhouse/runner.env
Restart=always
RestartSec=5
StartLimitIntervalSec=60
StartLimitBurst=3

[Install]
WantedBy=default.target
SERVICEEOF

    systemctl --user daemon-reload

    if systemctl --user is-active --quiet longhouse-runner 2>/dev/null; then
      echo "Stopping existing runner..."
      systemctl --user stop longhouse-runner
    fi

    echo "Starting runner service..."
    systemctl --user enable longhouse-runner
    systemctl --user start longhouse-runner

    echo ""
    echo "Runner installed and started successfully!"
    echo ""
    echo "Management commands:"
    echo "  systemctl --user stop longhouse-runner   # Stop"
    echo "  systemctl --user start longhouse-runner  # Start"
    echo "  journalctl --user -u longhouse-runner -f # View logs"
    echo ""
    echo "Note: User services only run while you are logged in."
    echo "For always-on servers: loginctl enable-linger \\$USER"
    ;;
esac

echo ""
echo "To uninstall:"
echo "  curl -fsSL ${{LONGHOUSE_URL}}/api/runners/uninstall.sh | bash"
"""

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
    script = """#!/bin/bash
# Longhouse Runner - Universal Uninstaller
# Removes runner service and all associated files

set -e

echo "======================================"
echo "Longhouse Runner Uninstaller"
echo "======================================"
echo ""

# Detect OS
OS="$(uname -s)"
case "$OS" in
  Darwin) OS_TYPE="macos" ;;
  Linux) OS_TYPE="linux" ;;
  MINGW*|MSYS*|CYGWIN*)
    echo "Error: Windows uninstaller not yet supported" >&2
    exit 1
    ;;
  *)
    echo "Error: Unsupported operating system: $OS" >&2
    exit 1
    ;;
esac

echo "Detected OS: $OS_TYPE"
echo ""

case "$OS_TYPE" in
  macos)
    PLIST_FILE="$HOME/Library/LaunchAgents/com.longhouse.runner.plist"

    # Stop and unload service
    if [ -f "$PLIST_FILE" ]; then
      echo "Stopping and removing launchd service..."
      launchctl unload "$PLIST_FILE" 2>/dev/null || true
      rm -f "$PLIST_FILE"
      echo "LaunchAgent removed"
    else
      echo "LaunchAgent not found (already removed?)"
    fi

    # Remove binary
    BINARY_PATH="$HOME/.local/bin/longhouse-runner"
    if [ -f "$BINARY_PATH" ]; then
      rm -f "$BINARY_PATH"
      echo "Binary removed: $BINARY_PATH"
    fi

    # Remove config directory
    CONFIG_DIR="$HOME/.config/longhouse"
    if [ -d "$CONFIG_DIR" ]; then
      rm -rf "$CONFIG_DIR"
      echo "Config removed: $CONFIG_DIR"
    fi

    # Remove state directory (logs)
    STATE_DIR="$HOME/.local/state/longhouse"
    if [ -d "$STATE_DIR" ]; then
      rm -rf "$STATE_DIR"
      echo "State/logs removed: $STATE_DIR"
    fi
    ;;

  linux)
    SERVICE_NAME="longhouse-runner"
    SERVICE_FILE="$HOME/.config/systemd/user/${SERVICE_NAME}.service"

    # Stop and disable service
    if systemctl --user is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
      echo "Stopping systemd service..."
      systemctl --user stop "$SERVICE_NAME"
    fi

    if systemctl --user is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
      echo "Disabling systemd service..."
      systemctl --user disable "$SERVICE_NAME"
    fi

    # Remove service file
    if [ -f "$SERVICE_FILE" ]; then
      rm -f "$SERVICE_FILE"
      systemctl --user daemon-reload
      echo "Systemd service removed"
    else
      echo "Systemd service not found (already removed?)"
    fi

    # Remove binary
    BINARY_PATH="$HOME/.local/bin/longhouse-runner"
    if [ -f "$BINARY_PATH" ]; then
      rm -f "$BINARY_PATH"
      echo "Binary removed: $BINARY_PATH"
    fi

    # Remove config directory
    CONFIG_DIR="$HOME/.config/longhouse"
    if [ -d "$CONFIG_DIR" ]; then
      rm -rf "$CONFIG_DIR"
      echo "Config removed: $CONFIG_DIR"
    fi
    ;;
esac

echo ""
echo "Longhouse Runner uninstalled successfully!"
echo ""
echo "Note: The runner registration still exists on the server."
echo "To fully remove, revoke the runner from the Longhouse web UI."
"""

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
    # In test mode, use a placeholder URL
    if not settings.app_public_url:
        if settings.testing:
            api_url = "http://localhost:30080"
        else:
            raise HTTPException(
                status_code=500,
                detail="APP_PUBLIC_URL not configured. Set this in your environment.",
            )
    else:
        api_url = settings.app_public_url

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
            # Note: if two owners have runners with same name AND same secret hash,
            # this returns the first match. This is a config error but shouldn't crash.
            stmt = select(RunnerModel).where(RunnerModel.name == runner_name, RunnerModel.auth_secret_hash == computed_hash)
            results = db.execute(stmt).scalars().all()
            if len(results) > 1:
                logger.warning(f"Multiple runners found with name '{runner_name}' and same secret hash - using first match")
            runner = results[0] if results else None
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
                            updated_job = runner_crud.update_job_output(db, job_id, stream, data)
                            if updated_job and updated_job.commis_id:
                                from zerg.events import EventType
                                from zerg.events.event_bus import event_bus
                                from zerg.models.models import CommisJob
                                from zerg.services.commis_output_buffer import get_commis_output_buffer

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
                                import time

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
