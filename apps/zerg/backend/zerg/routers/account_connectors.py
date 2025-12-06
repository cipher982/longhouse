"""Account Connector Credentials API.

REST endpoints for managing account-level connector credentials:
- List all connector types and their configuration status
- Configure (create/update) credentials for a connector
- Test credentials before or after saving
- Delete connector credentials

Account-level credentials are shared across all agents owned by the user.
Individual agents can still override with per-agent credentials.

Credentials are encrypted at rest and never returned in responses.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Path
from fastapi import Response
from fastapi import status
from sqlalchemy.orm import Session

from zerg.connectors.registry import CONNECTOR_REGISTRY
from zerg.connectors.registry import ConnectorType
from zerg.connectors.registry import get_required_fields
from zerg.connectors.testers import test_connector
from zerg.database import get_db
from zerg.dependencies.auth import get_current_user
from zerg.models.models import AccountConnectorCredential
from zerg.models.models import User
from zerg.schemas.connector_schemas import AccountConnectorStatusResponse
from zerg.schemas.connector_schemas import ConnectorConfigureRequest
from zerg.schemas.connector_schemas import ConnectorSuccessResponse
from zerg.schemas.connector_schemas import ConnectorTestRequest
from zerg.schemas.connector_schemas import ConnectorTestResponse
from zerg.schemas.connector_schemas import CredentialFieldSchema
from zerg.utils.crypto import decrypt
from zerg.utils.crypto import encrypt

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/account/connectors",
    tags=["account-connectors"],
)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/", response_model=list[AccountConnectorStatusResponse])
def list_account_connectors(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[AccountConnectorStatusResponse]:
    """List all connector types and their configuration status for the user's account.

    Returns all available connector types with:
    - Metadata (name, description, required fields)
    - Whether credentials are configured at account level
    - Test status and metadata from last test
    """
    # Get all configured credentials for this user
    configured_creds = {
        c.connector_type: c
        for c in db.query(AccountConnectorCredential)
        .filter(AccountConnectorCredential.owner_id == current_user.id)
        .all()
    }

    result = []
    for conn_type, definition in CONNECTOR_REGISTRY.items():
        cred = configured_creds.get(conn_type.value)

        # Convert field definitions to schema objects
        fields = [
            CredentialFieldSchema(
                key=f["key"],
                label=f["label"],
                type=f["type"],
                placeholder=f["placeholder"],
                required=f["required"],
            )
            for f in definition["fields"]
        ]

        result.append(
            AccountConnectorStatusResponse(
                type=conn_type.value,
                name=definition["name"],
                description=definition["description"],
                category=definition["category"],
                icon=definition["icon"],
                docs_url=definition["docs_url"],
                fields=fields,
                configured=cred is not None,
                display_name=cred.display_name if cred else None,
                test_status=cred.test_status if cred else "untested",
                last_tested_at=cred.last_tested_at if cred else None,
                metadata=cred.connector_metadata if cred else None,
            )
        )

    return result


@router.post("/", response_model=ConnectorSuccessResponse, status_code=status.HTTP_201_CREATED)
def configure_account_connector(
    request: ConnectorConfigureRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ConnectorSuccessResponse:
    """Configure (create or update) account-level connector credentials.

    If credentials already exist for this connector type, they are updated.
    Otherwise, new credentials are created.

    Test status is reset to 'untested' when credentials are updated.
    """
    # Validate connector type
    try:
        conn_type = ConnectorType(request.connector_type)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown connector type: {request.connector_type}",
        )

    # Validate required fields
    required_fields = get_required_fields(conn_type)
    for field in required_fields:
        if field not in request.credentials or not request.credentials[field]:
            raise HTTPException(
                status_code=400,
                detail=f"Missing required field: {field}",
            )

    # Encrypt credentials as JSON
    encrypted = encrypt(json.dumps(request.credentials))

    # Upsert: check if credential exists
    existing = (
        db.query(AccountConnectorCredential)
        .filter(
            AccountConnectorCredential.owner_id == current_user.id,
            AccountConnectorCredential.connector_type == conn_type.value,
        )
        .first()
    )

    if existing:
        # Update existing
        existing.encrypted_value = encrypted
        existing.display_name = request.display_name
        existing.test_status = "untested"
        existing.last_tested_at = None
        existing.connector_metadata = None
        logger.info(
            "Updated account %s credentials for user %d",
            conn_type.value,
            current_user.id,
        )
    else:
        # Create new
        cred = AccountConnectorCredential(
            owner_id=current_user.id,
            connector_type=conn_type.value,
            encrypted_value=encrypted,
            display_name=request.display_name,
        )
        db.add(cred)
        logger.info(
            "Created account %s credentials for user %d",
            conn_type.value,
            current_user.id,
        )

    db.commit()
    return ConnectorSuccessResponse(success=True)


@router.post("/test", response_model=ConnectorTestResponse)
def test_credentials_before_save(
    request: ConnectorTestRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ConnectorTestResponse:
    """Test credentials before saving them.

    This endpoint allows testing credentials without persisting them.
    Useful for validating credentials in the UI before committing.
    """
    # Validate connector type
    try:
        conn_type = ConnectorType(request.connector_type)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown connector type: {request.connector_type}",
        )

    # Validate required fields
    required_fields = get_required_fields(conn_type)
    for field in required_fields:
        if field not in request.credentials or not request.credentials[field]:
            return ConnectorTestResponse(
                success=False,
                message=f"Missing required field: {field}",
            )

    # Test credentials
    result = test_connector(conn_type, request.credentials)

    return ConnectorTestResponse(
        success=result["success"],
        message=result["message"],
        metadata=result.get("metadata"),
    )


@router.post("/{connector_type}/test", response_model=ConnectorTestResponse)
def test_configured_connector(
    connector_type: str = Path(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ConnectorTestResponse:
    """Test already-configured account-level connector credentials.

    Tests the stored credentials and updates the test_status and metadata.
    """
    cred = (
        db.query(AccountConnectorCredential)
        .filter(
            AccountConnectorCredential.owner_id == current_user.id,
            AccountConnectorCredential.connector_type == connector_type,
        )
        .first()
    )

    if not cred:
        raise HTTPException(status_code=404, detail="Connector not configured")

    # Decrypt credentials
    try:
        decrypted = json.loads(decrypt(cred.encrypted_value))
    except Exception:
        logger.exception(
            "Failed to decrypt account credentials for user %d connector %s",
            current_user.id,
            connector_type,
        )
        raise HTTPException(status_code=500, detail="Failed to decrypt credentials")

    # Test credentials
    result = test_connector(connector_type, decrypted)

    # Update test status
    cred.test_status = "success" if result["success"] else "failed"
    cred.last_tested_at = datetime.utcnow()

    # Always update metadata: if test failed or returned no metadata, clear it.
    cred.connector_metadata = result.get("metadata")

    db.commit()

    return ConnectorTestResponse(
        success=result["success"],
        message=result["message"],
        metadata=result.get("metadata"),
    )


@router.delete("/{connector_type}", status_code=status.HTTP_204_NO_CONTENT)
def delete_account_connector(
    connector_type: str = Path(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    """Remove account-level connector credentials.

    This deletes the stored credentials permanently.
    Note: Any agents with per-agent overrides will still work.
    """
    cred = (
        db.query(AccountConnectorCredential)
        .filter(
            AccountConnectorCredential.owner_id == current_user.id,
            AccountConnectorCredential.connector_type == connector_type,
        )
        .first()
    )

    if not cred:
        raise HTTPException(status_code=404, detail="Connector not configured")

    db.delete(cred)
    db.commit()

    logger.info(
        "Deleted account %s credentials for user %d",
        connector_type,
        current_user.id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
