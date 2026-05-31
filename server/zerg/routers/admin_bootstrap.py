"""Admin bootstrap API endpoints.

These endpoints allow seeding runners and credentials via API instead of
relying on file mounts, which are brittle in container environments.

All endpoints require admin privileges and optionally support token-based auth
for CLI/automation use cases.
"""

import json
import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Header
from fastapi import HTTPException
from sqlalchemy.orm import Session

from zerg.config import get_settings
from zerg.crud import runner_crud
from zerg.database import get_db
from zerg.dependencies.auth import get_current_user
from zerg.models.models import AccountConnectorCredential
from zerg.models.models import User
from zerg.schemas.bootstrap import BootstrapStatusItem
from zerg.schemas.bootstrap import BootstrapStatusResponse
from zerg.schemas.bootstrap import BootstrapSuccessResponse
from zerg.schemas.bootstrap import CredentialsSeedRequest
from zerg.schemas.bootstrap import RunnersSeedRequest
from zerg.utils.crypto import encrypt

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/bootstrap",
    tags=["admin", "bootstrap"],
)


# ---------------------------------------------------------------------------
# Bootstrap Token Auth (alternative to session auth for CLI use)
# ---------------------------------------------------------------------------


def require_bootstrap_auth(
    authorization: str | None = Header(default=None),
    current_user=Depends(get_current_user),
):
    """Dependency that accepts either bootstrap token or session auth.

    For CLI/automation use cases, accepts Authorization: Bearer <BOOTSTRAP_TOKEN>.
    Falls back to session auth if no token provided.

    Returns the authenticated admin user.
    """
    settings = get_settings()

    # If token provided, validate it
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]  # Strip "Bearer " prefix
        bootstrap_token = settings.bootstrap_token

        if not bootstrap_token:
            raise HTTPException(
                status_code=500,
                detail="Bootstrap token not configured. Set BOOTSTRAP_TOKEN env var.",
            )

        if token != bootstrap_token:
            raise HTTPException(
                status_code=401,
                detail="Invalid bootstrap token",
            )

        # Token is valid - find admin user to associate with the operation
        # We don't have a user context with token auth, so we'll use the first admin
        return None  # Signal that we're using token auth

    # No token - rely on session auth (current_user already validated)
    if current_user is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    # Ensure user is admin
    if getattr(current_user, "role", "USER") != "ADMIN":
        raise HTTPException(status_code=403, detail="Admin privileges required")

    return current_user


def get_admin_user(db: Session, auth_user) -> User:
    """Get the admin user for bootstrap operations.

    If auth_user is provided (session auth), returns that user.
    If auth_user is None (token auth), finds the first admin user.
    """
    if auth_user is not None:
        return auth_user

    # Token auth - find first admin user
    admin_user = db.query(User).filter(User.role == "ADMIN").order_by(User.id).first()
    if not admin_user:
        raise HTTPException(
            status_code=404,
            detail="No admin user found. Create a user first.",
        )
    return admin_user


# ---------------------------------------------------------------------------
# Bootstrap Endpoints
# ---------------------------------------------------------------------------


@router.post("/runners", response_model=BootstrapSuccessResponse)
async def seed_runners(
    request: RunnersSeedRequest,
    db: Session = Depends(get_db),
    auth_user=Depends(require_bootstrap_auth),
):
    """Seed runners for the admin user.

    This replaces file-based seeding from ~/.config/zerg/runners.json.
    Idempotent - skips runners that already exist.
    """
    admin_user = get_admin_user(db, auth_user)

    seeded_count = 0
    skipped_count = 0

    for runner_config in request.runners:
        # Check if runner already exists (idempotent)
        existing = runner_crud.get_runner_by_name(db, admin_user.id, runner_config.name)
        if existing:
            skipped_count += 1
            continue

        # Create the runner with the known secret
        runner_crud.create_runner(
            db=db,
            owner_id=admin_user.id,
            name=runner_config.name,
            auth_secret=runner_config.secret,
            labels=runner_config.labels,
            capabilities=runner_config.capabilities,
        )
        seeded_count += 1
        logger.info(f"Seeded runner '{runner_config.name}' for {admin_user.email}")

    db.commit()

    return BootstrapSuccessResponse(
        success=True,
        message=f"Seeded {seeded_count} runner(s) ({skipped_count} already existed)",
        details={
            "seeded_count": seeded_count,
            "skipped_count": skipped_count,
        },
    )


@router.post("/credentials", response_model=BootstrapSuccessResponse)
async def seed_credentials(
    request: CredentialsSeedRequest,
    db: Session = Depends(get_db),
    auth_user=Depends(require_bootstrap_auth),
):
    """Seed connector credentials for the admin user.

    All credentials are Fernet-encrypted before storage.
    Idempotent - skips credentials that already exist.
    """
    admin_user = get_admin_user(db, auth_user)

    # Convert request to dict for iteration
    creds_dict = request.model_dump(exclude_none=True)

    seeded_count = 0
    skipped_count = 0

    for connector_type, creds in creds_dict.items():
        if creds is None:
            continue

        # Check for existing credential
        existing = (
            db.query(AccountConnectorCredential)
            .filter(
                AccountConnectorCredential.owner_id == admin_user.id,
                AccountConnectorCredential.connector_type == connector_type,
            )
            .first()
        )

        if existing:
            skipped_count += 1
            logger.debug(f"Credential for {connector_type} already exists - skipping")
            continue

        # Encrypt and store
        encrypted_value = encrypt(json.dumps(creds))
        credential = AccountConnectorCredential(
            owner_id=admin_user.id,
            connector_type=connector_type,
            encrypted_value=encrypted_value,
            display_name=f"Personal {connector_type.title()}",
            test_status="untested",
        )
        db.add(credential)
        seeded_count += 1
        logger.info(f"Seeded credential for {connector_type}")

    db.commit()

    return BootstrapSuccessResponse(
        success=True,
        message=f"Seeded {seeded_count} credential(s) ({skipped_count} already existed)",
        details={
            "seeded_count": seeded_count,
            "skipped_count": skipped_count,
        },
    )


@router.get("/status", response_model=BootstrapStatusResponse)
async def get_bootstrap_status(
    db: Session = Depends(get_db),
    auth_user=Depends(require_bootstrap_auth),
):
    """Get status of what's configured vs missing."""
    admin_user = get_admin_user(db, auth_user)

    # Check runners
    runners = runner_crud.get_runners_by_owner(db, admin_user.id)
    runners_configured = len(runners) > 0
    runners_details = f"{len(runners)} runner(s)" if runners_configured else "not configured"

    # Check credentials
    creds = db.query(AccountConnectorCredential).filter(AccountConnectorCredential.owner_id == admin_user.id).all()
    creds_configured = len(creds) > 0
    cred_types = [c.connector_type for c in creds]
    creds_details = f"{len(creds)} configured: {', '.join(cred_types)}" if creds_configured else "not configured"

    return BootstrapStatusResponse(
        runners=BootstrapStatusItem(configured=runners_configured, details=runners_details),
        credentials=BootstrapStatusItem(configured=creds_configured, details=creds_details),
    )
