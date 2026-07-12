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
from typing import Literal
from typing import Optional
from uuid import UUID
from uuid import uuid4

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.orm import Session

from zerg.config import get_settings
from zerg.database import archive_database_is_read_only
from zerg.database import catalog_db_dependency
from zerg.database import live_store_configured
from zerg.dependencies.auth import get_current_user
from zerg.models.apns_device_registration import APNSDeviceRegistration
from zerg.models.apns_live_activity_registration import APNSLiveActivityRegistration
from zerg.models.device_token import DeviceToken
from zerg.services.write_serializer import get_catalog_write_serializer
from zerg.utils.time import UTCBaseModel

logger = logging.getLogger(__name__)
_catalog_db_dependency = catalog_db_dependency()

# Preserve the established patch seam while routing it to the catalog owner.
get_write_serializer = get_catalog_write_serializer

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


class APNSRegisterRequest(BaseModel):
    """Register or refresh an iOS APNs device token for the current user."""

    device_token: str = Field(..., min_length=16, max_length=255, pattern=r"^[A-Fa-f0-9]+$")
    platform: Literal["ios", "ios_widget"] = "ios"
    app_build_id: Optional[str] = Field(None, max_length=255)
    push_environment: Literal["sandbox", "production"] = "sandbox"


class APNSRegisterResponse(UTCBaseModel):
    """Response for APNs device registration upsert."""

    id: str
    platform: str
    device_token_suffix: str
    push_environment: str
    app_build_id: Optional[str] = None
    last_seen_at: datetime


class APNSLiveActivityRegisterRequest(BaseModel):
    """Register or refresh one ActivityKit push token for a watched session."""

    session_id: str = Field(..., min_length=1, max_length=64)
    activity_id: str = Field(..., min_length=1, max_length=255)
    push_token: str = Field(..., min_length=16, max_length=255, pattern=r"^[A-Fa-f0-9]+$")
    app_build_id: Optional[str] = Field(None, max_length=255)
    push_environment: Literal["sandbox", "production"] = "sandbox"


class APNSLiveActivityRegisterResponse(UTCBaseModel):
    """Response for ActivityKit push-token registration upsert."""

    id: str
    session_id: str
    activity_id: str
    push_token_suffix: str
    push_environment: str
    app_build_id: Optional[str] = None
    last_seen_at: datetime


class APNSLiveActivityEndRequest(BaseModel):
    """Mark one ActivityKit registration as ended by the current user."""

    activity_id: str = Field(..., min_length=1, max_length=255)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/tokens", response_model=CreateTokenResponse, status_code=status.HTTP_201_CREATED)
async def create_device_token(
    request: CreateTokenRequest,
    db: Session = Depends(_catalog_db_dependency),
    current_user=Depends(get_current_user),
) -> CreateTokenResponse:
    """Create a new device token.

    The plain token is returned only once during creation. Store it securely.
    Subsequent API calls will use this token in the X-Agents-Token header.
    """
    # Generate token
    plain_token = generate_device_token()
    token_hash = hash_token(plain_token)

    if live_store_configured() and not get_settings().testing:
        from zerg.catalogd.client import CatalogRemoteError
        from zerg.catalogd.client import CatalogUnavailable
        from zerg.services.catalogd_supervisor import get_catalogd_client

        token_id = str(uuid4())
        client = get_catalogd_client()
        if client is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "catalog_unavailable", "message": "Catalog mutation is temporarily unavailable."},
            )
        try:
            result = await client.call(
                "auth.device.create.v2",
                {
                    "owner_id": int(current_user.id),
                    "token_id": token_id,
                    "device_id": request.device_id,
                    "token_hash": token_hash,
                },
                timeout_seconds=1.0,
            )
        except CatalogUnavailable as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "catalog_unavailable", "message": "Catalog mutation is temporarily unavailable."},
            ) from exc
        except CatalogRemoteError as exc:
            logger.warning("Catalog device-token create failed code=%s retryable=%s", exc.code, exc.retryable)
            if exc.code == "resource_exhausted":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={"code": "device_token_limit_reached", "message": "Device token limit reached."},
                ) from exc
            if exc.retryable:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={"code": "catalog_unavailable", "message": "Catalog mutation is temporarily unavailable."},
                ) from exc
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"code": "catalog_operation_failed", "message": "Catalog mutation failed."},
            ) from exc
        if not (result.get("created") is True or result.get("exact_replay") is True) or result.get("token_id") != token_id:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"code": "catalog_protocol_error", "message": "Catalog returned an invalid create result."},
            )
        try:
            created_at = datetime.fromisoformat(result["created_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"code": "catalog_protocol_error", "message": "Catalog returned an invalid creation time."},
            ) from exc
        logger.info(
            "Created device token for user %s device %s at catalog commit %s",
            current_user.id,
            request.device_id,
            result.get("commit_seq"),
        )
        return CreateTokenResponse(
            id=token_id,
            device_id=request.device_id,
            token=plain_token,
            created_at=created_at,
        )

    ws = get_write_serializer()

    def _create_token(wdb: Session) -> tuple[str, str, datetime]:
        device_token = DeviceToken(
            owner_id=current_user.id,
            device_id=request.device_id,
            token_hash=token_hash,
            created_at=datetime.now(timezone.utc),
        )
        wdb.add(device_token)
        wdb.flush()
        wdb.refresh(device_token)
        return str(device_token.id), device_token.device_id, device_token.created_at

    token_id, device_id, created_at = await ws.execute_or_direct(
        _create_token,
        db,
        label="device-token-create",
    )

    logger.info(f"Created device token for user {current_user.id} device {request.device_id}")

    return CreateTokenResponse(
        id=token_id,
        device_id=device_id,
        token=plain_token,
        created_at=created_at,
    )


@router.get("/tokens", response_model=TokenListResponse)
async def list_device_tokens(
    include_revoked: bool = False,
    db: Session = Depends(_catalog_db_dependency),
    current_user=Depends(get_current_user),
) -> TokenListResponse:
    """List all device tokens for the current user.

    By default, only shows valid (non-revoked) tokens.
    Use include_revoked=true to see revoked tokens as well.
    """
    if live_store_configured() and not get_settings().testing:
        from zerg.catalogd.client import CatalogRemoteError
        from zerg.catalogd.client import CatalogUnavailable
        from zerg.services.catalogd_supervisor import get_catalogd_client

        client = get_catalogd_client()
        if client is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "catalog_unavailable", "message": "Catalog read is temporarily unavailable."},
            )
        try:
            result = await client.call(
                "auth.device.list.v2",
                {
                    "owner_id": int(current_user.id),
                    "include_revoked": include_revoked,
                },
            )
        except CatalogUnavailable as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "catalog_unavailable", "message": "Catalog read is temporarily unavailable."},
            ) from exc
        except CatalogRemoteError as exc:
            logger.warning("Catalog device-token list failed code=%s retryable=%s", exc.code, exc.retryable)
            if exc.code == "resource_exhausted":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={"code": "device_token_limit_exceeded", "message": "Device token list exceeds its bound."},
                ) from exc
            if exc.retryable:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={"code": "catalog_unavailable", "message": "Catalog read is temporarily unavailable."},
                ) from exc
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"code": "catalog_operation_failed", "message": "Catalog read failed."},
            ) from exc
        token_payloads = result.get("tokens")
        if not isinstance(token_payloads, list):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"code": "catalog_protocol_error", "message": "Catalog returned an invalid token list."},
            )
        return TokenListResponse(
            tokens=[TokenResponse.model_validate(payload) for payload in token_payloads],
            total=len(token_payloads),
        )

    query = db.query(DeviceToken).filter(DeviceToken.owner_id == current_user.id)

    if not include_revoked:
        query = query.filter(DeviceToken.revoked_at.is_(None))

    query = query.order_by(DeviceToken.created_at.desc(), DeviceToken.id)
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


@router.post("/apns-register", response_model=APNSRegisterResponse)
async def register_apns_device(
    request: APNSRegisterRequest,
    db: Session = Depends(_catalog_db_dependency),
    current_user=Depends(get_current_user),
) -> APNSRegisterResponse:
    """Register or refresh an APNs device token for the current browser user."""
    normalized_token = str(request.device_token or "").strip().lower()
    build_id = str(request.app_build_id or "").strip() or None
    now = datetime.now(timezone.utc)
    ws = get_write_serializer()

    def _register_device(wdb: Session) -> tuple[str, str, str, str | None, datetime]:
        registration = (
            wdb.query(APNSDeviceRegistration)
            .filter(
                APNSDeviceRegistration.owner_id == current_user.id,
                APNSDeviceRegistration.device_token == normalized_token,
            )
            .first()
        )
        if registration is None:
            registration = APNSDeviceRegistration(
                owner_id=current_user.id,
                platform=request.platform,
                device_token=normalized_token,
                push_environment=request.push_environment,
                app_build_id=build_id,
                last_seen_at=now,
            )
            wdb.add(registration)
            wdb.flush()
        else:
            registration.platform = request.platform
            registration.push_environment = request.push_environment
            registration.app_build_id = build_id
            registration.last_seen_at = now
            registration.revoked_at = None

        wdb.flush()
        wdb.refresh(registration)
        return (
            str(registration.id),
            registration.platform,
            registration.push_environment,
            registration.app_build_id,
            registration.last_seen_at,
        )

    registration_id, platform, push_environment, registration_build_id, last_seen_at = await ws.execute_or_direct(
        _register_device,
        db,
        label="apns-device-register",
    )

    logger.info("Registered APNs device for user %s (%s)", current_user.id, request.push_environment)
    return APNSRegisterResponse(
        id=registration_id,
        platform=platform,
        device_token_suffix=normalized_token[-12:],
        push_environment=push_environment,
        app_build_id=registration_build_id,
        last_seen_at=last_seen_at,
    )


@router.post("/apns-live-activity/register", response_model=APNSLiveActivityRegisterResponse)
async def register_apns_live_activity(
    request: APNSLiveActivityRegisterRequest,
    db: Session = Depends(_catalog_db_dependency),
    current_user=Depends(get_current_user),
) -> APNSLiveActivityRegisterResponse:
    """Register or refresh an ActivityKit update token for one watched session."""

    normalized_token = str(request.push_token or "").strip().lower()
    activity_id = str(request.activity_id or "").strip()
    session_id = str(request.session_id or "").strip()
    build_id = str(request.app_build_id or "").strip() or None
    now = datetime.now(timezone.utc)
    ws = get_write_serializer()

    def _register_live_activity(wdb: Session) -> tuple[str, str, str, str, str | None, datetime]:
        registration = (
            wdb.query(APNSLiveActivityRegistration)
            .filter(
                APNSLiveActivityRegistration.owner_id == current_user.id,
                APNSLiveActivityRegistration.activity_id == activity_id,
            )
            .first()
        )
        if registration is None:
            registration = (
                wdb.query(APNSLiveActivityRegistration)
                .filter(
                    APNSLiveActivityRegistration.owner_id == current_user.id,
                    APNSLiveActivityRegistration.push_token == normalized_token,
                )
                .first()
            )
        if registration is None:
            registration = APNSLiveActivityRegistration(
                owner_id=current_user.id,
                session_id=session_id,
                activity_id=activity_id,
                push_token=normalized_token,
                push_environment=request.push_environment,
                app_build_id=build_id,
                last_seen_at=now,
            )
            wdb.add(registration)
            wdb.flush()
        else:
            registration.session_id = session_id
            registration.activity_id = activity_id
            registration.push_token = normalized_token
            registration.push_environment = request.push_environment
            registration.app_build_id = build_id
            registration.last_seen_at = now
            registration.ended_at = None

        wdb.flush()
        wdb.refresh(registration)
        return (
            str(registration.id),
            registration.session_id,
            registration.activity_id,
            registration.push_environment,
            registration.app_build_id,
            registration.last_seen_at,
        )

    (
        registration_id,
        stored_session_id,
        stored_activity_id,
        push_environment,
        registration_build_id,
        last_seen_at,
    ) = await ws.execute_or_direct(
        _register_live_activity,
        db,
        label="apns-live-activity-register",
    )

    logger.info("Registered APNs Live Activity for user %s session %s", current_user.id, stored_session_id)
    return APNSLiveActivityRegisterResponse(
        id=registration_id,
        session_id=stored_session_id,
        activity_id=stored_activity_id,
        push_token_suffix=normalized_token[-12:],
        push_environment=push_environment,
        app_build_id=registration_build_id,
        last_seen_at=last_seen_at,
    )


@router.post("/apns-live-activity/end", status_code=status.HTTP_204_NO_CONTENT)
async def end_apns_live_activity(
    request: APNSLiveActivityEndRequest,
    db: Session = Depends(_catalog_db_dependency),
    current_user=Depends(get_current_user),
) -> None:
    """Mark an ActivityKit update token as ended after the user stops watching."""

    activity_id = str(request.activity_id or "").strip()
    now = datetime.now(timezone.utc)
    ws = get_write_serializer()

    def _end_live_activity(wdb: Session) -> None:
        registration = (
            wdb.query(APNSLiveActivityRegistration)
            .filter(
                APNSLiveActivityRegistration.owner_id == current_user.id,
                APNSLiveActivityRegistration.activity_id == activity_id,
            )
            .first()
        )
        if registration is not None:
            registration.ended_at = now

    await ws.execute_or_direct(
        _end_live_activity,
        db,
        label="apns-live-activity-end",
    )


@router.delete("/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_device_token(
    token_id: UUID,
    db: Session = Depends(_catalog_db_dependency),
    current_user=Depends(get_current_user),
) -> None:
    """Revoke a device token.

    A revoked token can no longer be used for authentication.
    This action cannot be undone.
    """
    if live_store_configured() and not get_settings().testing:
        from zerg.catalogd.client import CatalogRemoteError
        from zerg.catalogd.client import CatalogUnavailable
        from zerg.services.catalogd_supervisor import get_catalogd_client

        client = get_catalogd_client()
        if client is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "catalog_unavailable", "message": "Catalog mutation is temporarily unavailable."},
            )
        try:
            result = await client.call(
                "auth.device.revoke.v2",
                {
                    "owner_id": int(current_user.id),
                    "token_id": str(token_id),
                },
                timeout_seconds=1.0,
            )
        except CatalogUnavailable as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "catalog_unavailable", "message": "Catalog mutation is temporarily unavailable."},
            ) from exc
        except CatalogRemoteError as exc:
            logger.warning("Catalog device-token revoke failed code=%s retryable=%s", exc.code, exc.retryable)
            if exc.retryable:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={"code": "catalog_unavailable", "message": "Catalog mutation is temporarily unavailable."},
                ) from exc
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"code": "catalog_operation_failed", "message": "Catalog mutation failed."},
            ) from exc
        if result.get("found") is not True:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Token {token_id} not found",
            )
        logger.info(
            "Revoked device token %s for user %s at catalog commit %s",
            token_id,
            current_user.id,
            result.get("commit_seq"),
        )
        return

    ws = get_write_serializer()

    def _revoke_token(wdb: Session) -> None:
        token = wdb.query(DeviceToken).filter(DeviceToken.id == token_id, DeviceToken.owner_id == current_user.id).first()

        if not token:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Token {token_id} not found",
            )

        if token.revoked_at is None:
            token.revoked_at = datetime.now(timezone.utc)

    await ws.execute_or_direct(
        _revoke_token,
        db,
        label="device-token-revoke",
    )

    logger.info(f"Revoked device token {token_id} for user {current_user.id}")


@router.get("/tokens/{token_id}", response_model=TokenResponse)
def get_device_token(
    token_id: UUID,
    db: Session = Depends(_catalog_db_dependency),
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

    Updates last_used_at on successful validation when safe to do so.

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

    # Debounce last_used_at writes — at most once per hour per token.
    # Every-request writes cause SQLite write-lock contention under load.
    # When the write serializer is active, keep auth validation read-only.
    now = datetime.now(timezone.utc)
    last = device_token.last_used_at
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    if (last is None or (now - last).total_seconds() > 3600) and not archive_database_is_read_only():
        if get_write_serializer().is_configured:
            return device_token
        device_token.last_used_at = now
        db.commit()

    return device_token
