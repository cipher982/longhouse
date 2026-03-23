"""Device tokens API for per-device authentication.

Provides endpoints for:
- POST /api/devices/tokens - Create a new device token
- GET /api/devices/tokens - List user's device tokens
- DELETE /api/devices/tokens/{id} - Revoke a token
"""

import hashlib
import logging
import secrets
from datetime import datetime
from datetime import timezone
from typing import List
from typing import Optional
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.dependencies.auth import get_current_user
from zerg.models.device_token import DeviceToken
from zerg.utils.time import UTCBaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/devices", tags=["devices"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def hash_token(token: str) -> str:
    """Create SHA-256 hash of a token for storage."""
    return hashlib.sha256(token.encode()).hexdigest()


def generate_device_token() -> str:
    """Generate a cryptographically secure device token.

    Format: zdt_<random-base64-urlsafe>
    Prefix helps identify token type in logs/debugging.
    """
    random_bytes = secrets.token_urlsafe(32)  # 256 bits
    return f"zdt_{random_bytes}"


# ---------------------------------------------------------------------------
# Request/Response Models
# ---------------------------------------------------------------------------


class CreateTokenRequest(BaseModel):
    """Request to create a new device token."""

    device_id: str = Field(..., min_length=1, max_length=255, description="Device identifier (hostname or custom name)")


class CreateTokenResponse(UTCBaseModel):
    """Response containing the newly created token.

    NOTE: The plain token is only returned once during creation.
    """

    id: str = Field(..., description="Token UUID (for management)")
    device_id: str = Field(..., description="Device identifier")
    token: str = Field(..., description="The plain token (shown only once)")
    created_at: datetime = Field(..., description="When the token was created")


class TokenResponse(UTCBaseModel):
    """Response for a single device token (without the plain token)."""

    id: str = Field(..., description="Token UUID")
    device_id: str = Field(..., description="Device identifier")
    created_at: datetime = Field(..., description="When the token was created")
    last_used_at: Optional[datetime] = Field(None, description="When the token was last used")
    revoked_at: Optional[datetime] = Field(None, description="When the token was revoked (if revoked)")
    is_valid: bool = Field(..., description="Whether the token is currently valid")


class TokenListResponse(BaseModel):
    """Response for listing device tokens."""

    tokens: List[TokenResponse]
    total: int


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/tokens", response_model=CreateTokenResponse, status_code=status.HTTP_201_CREATED)
def create_device_token(
    request: CreateTokenRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
) -> CreateTokenResponse:
    """Create a new device token.

    The plain token is returned only once during creation. Store it securely.
    Subsequent API calls will use this token in the X-Agents-Token header.
    """
    # Generate token
    plain_token = generate_device_token()
    token_hash = hash_token(plain_token)

    # Create database record
    device_token = DeviceToken(
        owner_id=current_user.id,
        device_id=request.device_id,
        token_hash=token_hash,
    )

    db.add(device_token)
    db.commit()
    db.refresh(device_token)

    logger.info(f"Created device token for user {current_user.id} device {request.device_id}")

    return CreateTokenResponse(
        id=str(device_token.id),
        device_id=device_token.device_id,
        token=plain_token,
        created_at=device_token.created_at,
    )


@router.get("/tokens", response_model=TokenListResponse)
def list_device_tokens(
    include_revoked: bool = False,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
) -> TokenListResponse:
    """List all device tokens for the current user.

    By default, only shows valid (non-revoked) tokens.
    Use include_revoked=true to see revoked tokens as well.
    """
    query = db.query(DeviceToken).filter(DeviceToken.owner_id == current_user.id)

    if not include_revoked:
        query = query.filter(DeviceToken.revoked_at.is_(None))

    query = query.order_by(DeviceToken.created_at.desc())
    tokens = query.all()

    return TokenListResponse(
        tokens=[
            TokenResponse(
                id=str(t.id),
                device_id=t.device_id,
                created_at=t.created_at,
                last_used_at=t.last_used_at,
                revoked_at=t.revoked_at,
                is_valid=t.is_valid,
            )
            for t in tokens
        ],
        total=len(tokens),
    )


@router.delete("/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_device_token(
    token_id: UUID,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
) -> None:
    """Revoke a device token.

    A revoked token can no longer be used for authentication.
    This action cannot be undone.
    """
    # Find the token
    token = db.query(DeviceToken).filter(DeviceToken.id == token_id, DeviceToken.owner_id == current_user.id).first()

    if not token:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Token {token_id} not found",
        )

    if token.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token is already revoked",
        )

    # Revoke the token
    token.revoked_at = datetime.now(timezone.utc)
    db.commit()

    logger.info(f"Revoked device token {token_id} for user {current_user.id}")


@router.get("/tokens/{token_id}", response_model=TokenResponse)
def get_device_token(
    token_id: UUID,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
) -> TokenResponse:
    """Get details of a specific device token."""
    token = db.query(DeviceToken).filter(DeviceToken.id == token_id, DeviceToken.owner_id == current_user.id).first()

    if not token:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Token {token_id} not found",
        )

    return TokenResponse(
        id=str(token.id),
        device_id=token.device_id,
        created_at=token.created_at,
        last_used_at=token.last_used_at,
        revoked_at=token.revoked_at,
        is_valid=token.is_valid,
    )


# ---------------------------------------------------------------------------
# Token Validation Helper (used by agents router)
# ---------------------------------------------------------------------------


def validate_device_token(token: str, db: Session) -> DeviceToken | None:
    """Validate a device token and return the DeviceToken if valid.

    Updates last_used_at on successful validation.

    Security: Uses constant-time comparison to prevent timing attacks.
    The DB lookup by hash is O(1) via index, and we add an explicit
    secrets.compare_digest() call to normalize any Python-level timing.

    Args:
        token: The plain token to validate
        db: Database session

    Returns:
        DeviceToken if valid, None if invalid or revoked
    """
    token_hash = hash_token(token)

    device_token = db.query(DeviceToken).filter(DeviceToken.token_hash == token_hash).first()

    if not device_token:
        # Constant-time comparison against dummy to normalize timing
        # even when token doesn't exist in DB
        secrets.compare_digest(token_hash, "0" * 64)
        return None

    # Constant-time comparison of the hash we computed vs stored hash
    # This prevents timing leaks at the Python comparison level
    if not secrets.compare_digest(token_hash, device_token.token_hash):
        return None

    if device_token.is_revoked:
        return None

    # Update last_used_at
    device_token.last_used_at = datetime.now(timezone.utc)
    db.commit()

    return device_token
