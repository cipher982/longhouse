"""Automation connector credentials API.

REST endpoints for managing automation-specific connector credentials:
- List all connector types and their configuration status
- Configure (create/update) credentials for a connector
- Test credentials before or after saving
- Delete connector credentials

All endpoints are scoped to automations owned by the authenticated user.
Credentials are encrypted at rest and never returned in responses.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from datetime import timezone
from typing import Any

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
from zerg.models.models import ConnectorCredential
from zerg.models.models import Fiche as AutomationProfile
from zerg.schemas.connector_schemas import ConnectorConfigureRequest
from zerg.schemas.connector_schemas import ConnectorStatusResponse
from zerg.schemas.connector_schemas import ConnectorSuccessResponse
from zerg.schemas.connector_schemas import ConnectorTestRequest
from zerg.schemas.connector_schemas import ConnectorTestResponse
from zerg.schemas.connector_schemas import CredentialFieldSchema
from zerg.utils.crypto import decrypt
from zerg.utils.crypto import encrypt

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/automations/{automation_id}/connectors",
    tags=["automation-connectors"],
)

legacy_router = APIRouter(
    prefix="/fiches/{fiche_id}/connectors",
    tags=["automation-connectors"],
)


def _get_automation_or_404(db: Session, automation_id: int, current_user: Any) -> AutomationProfile:
    """Get an automation and verify ownership."""
    automation = db.query(AutomationProfile).filter(AutomationProfile.id == automation_id).first()
    if not automation or automation.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Automation not found")
    return automation


def _get_connector_credential_or_404(db: Session, automation_id: int, connector_type: str) -> ConnectorCredential:
    """Get connector credentials for an automation."""
    credential = (
        db.query(ConnectorCredential)
        .filter(
            ConnectorCredential.fiche_id == automation_id,
            ConnectorCredential.connector_type == connector_type,
        )
        .first()
    )
    if not credential:
        raise HTTPException(status_code=404, detail="Connector not configured")
    return credential


@router.get("/", response_model=list[ConnectorStatusResponse])
def list_automation_connectors(
    automation_id: int = Path(..., gt=0),
    db: Session = Depends(get_db),
    current_user: Any = Depends(get_current_user),
) -> list[ConnectorStatusResponse]:
    """List all connector types and their configuration status for an automation.

    Returns all available connector types with:
    - Metadata (name, description, required fields)
    - Whether credentials are configured for this automation
    - Test status and metadata from the last test
    """
    _get_automation_or_404(db, automation_id, current_user)

    configured_credentials = {
        credential.connector_type: credential
        for credential in db.query(ConnectorCredential).filter(ConnectorCredential.fiche_id == automation_id).all()
    }

    result = []
    for conn_type, definition in CONNECTOR_REGISTRY.items():
        credential = configured_credentials.get(conn_type.value)
        fields = [
            CredentialFieldSchema(
                key=field["key"],
                label=field["label"],
                type=field["type"],
                placeholder=field["placeholder"],
                required=field["required"],
            )
            for field in definition["fields"]
        ]

        result.append(
            ConnectorStatusResponse(
                type=conn_type.value,
                name=definition["name"],
                description=definition["description"],
                category=definition["category"],
                icon=definition["icon"],
                docs_url=definition["docs_url"],
                fields=fields,
                configured=credential is not None,
                display_name=credential.display_name if credential else None,
                test_status=credential.test_status if credential else "untested",
                last_tested_at=credential.last_tested_at if credential else None,
                metadata=credential.connector_metadata if credential else None,
            )
        )

    return result


@router.post("/", response_model=ConnectorSuccessResponse, status_code=status.HTTP_201_CREATED)
def configure_automation_connector(
    request: ConnectorConfigureRequest,
    automation_id: int = Path(..., gt=0),
    db: Session = Depends(get_db),
    current_user: Any = Depends(get_current_user),
) -> ConnectorSuccessResponse:
    """Configure connector credentials for an automation."""
    _get_automation_or_404(db, automation_id, current_user)

    try:
        conn_type = ConnectorType(request.connector_type)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown connector type: {request.connector_type}",
        )

    required_fields = get_required_fields(conn_type)
    for field in required_fields:
        if field not in request.credentials or not request.credentials[field]:
            raise HTTPException(
                status_code=400,
                detail=f"Missing required field: {field}",
            )

    encrypted = encrypt(json.dumps(request.credentials))
    existing = (
        db.query(ConnectorCredential)
        .filter(
            ConnectorCredential.fiche_id == automation_id,
            ConnectorCredential.connector_type == conn_type.value,
        )
        .first()
    )

    if existing:
        existing.encrypted_value = encrypted
        existing.display_name = request.display_name
        existing.test_status = "untested"
        existing.last_tested_at = None
        existing.connector_metadata = None
        logger.info("Updated %s credentials for automation %d", conn_type.value, automation_id)
    else:
        credential = ConnectorCredential(
            fiche_id=automation_id,
            connector_type=conn_type.value,
            encrypted_value=encrypted,
            display_name=request.display_name,
        )
        db.add(credential)
        logger.info("Created %s credentials for automation %d", conn_type.value, automation_id)

    db.commit()
    return ConnectorSuccessResponse(success=True)


@router.post("/test", response_model=ConnectorTestResponse)
def test_automation_credentials_before_save(
    request: ConnectorTestRequest,
    automation_id: int = Path(..., gt=0),
    db: Session = Depends(get_db),
    current_user: Any = Depends(get_current_user),
) -> ConnectorTestResponse:
    """Test automation connector credentials before saving them."""
    _get_automation_or_404(db, automation_id, current_user)

    try:
        conn_type = ConnectorType(request.connector_type)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown connector type: {request.connector_type}",
        )

    required_fields = get_required_fields(conn_type)
    for field in required_fields:
        if field not in request.credentials or not request.credentials[field]:
            return ConnectorTestResponse(
                success=False,
                message=f"Missing required field: {field}",
            )

    result = test_connector(conn_type, request.credentials)
    return ConnectorTestResponse(
        success=result["success"],
        message=result["message"],
        metadata=result.get("metadata"),
    )


@router.post("/{connector_type}/test", response_model=ConnectorTestResponse)
def test_configured_automation_connector(
    connector_type: str = Path(...),
    automation_id: int = Path(..., gt=0),
    db: Session = Depends(get_db),
    current_user: Any = Depends(get_current_user),
) -> ConnectorTestResponse:
    """Test already-configured connector credentials for an automation."""
    _get_automation_or_404(db, automation_id, current_user)

    credential = _get_connector_credential_or_404(db, automation_id, connector_type)

    try:
        decrypted = json.loads(decrypt(credential.encrypted_value))
    except Exception:
        logger.exception("Failed to decrypt credentials for automation %d connector %s", automation_id, connector_type)
        raise HTTPException(status_code=500, detail="Failed to decrypt credentials")

    result = test_connector(connector_type, decrypted)
    credential.test_status = "success" if result["success"] else "failed"
    credential.last_tested_at = datetime.now(timezone.utc)
    credential.connector_metadata = result.get("metadata")
    db.commit()

    return ConnectorTestResponse(
        success=result["success"],
        message=result["message"],
        metadata=result.get("metadata"),
    )


@router.delete("/{connector_type}", status_code=status.HTTP_204_NO_CONTENT)
def delete_automation_connector(
    connector_type: str = Path(...),
    automation_id: int = Path(..., gt=0),
    db: Session = Depends(get_db),
    current_user: Any = Depends(get_current_user),
) -> Response:
    """Remove stored connector credentials from an automation."""
    _get_automation_or_404(db, automation_id, current_user)

    credential = _get_connector_credential_or_404(db, automation_id, connector_type)
    db.delete(credential)
    db.commit()

    logger.info("Deleted %s credentials for automation %d", connector_type, automation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@legacy_router.get("/", response_model=list[ConnectorStatusResponse])
def list_legacy_fiche_connectors(
    fiche_id: int = Path(..., gt=0),
    db: Session = Depends(get_db),
    current_user: Any = Depends(get_current_user),
) -> list[ConnectorStatusResponse]:
    return list_automation_connectors(automation_id=fiche_id, db=db, current_user=current_user)


@legacy_router.post("/", response_model=ConnectorSuccessResponse, status_code=status.HTTP_201_CREATED)
def configure_legacy_fiche_connector(
    request: ConnectorConfigureRequest,
    fiche_id: int = Path(..., gt=0),
    db: Session = Depends(get_db),
    current_user: Any = Depends(get_current_user),
) -> ConnectorSuccessResponse:
    return configure_automation_connector(request=request, automation_id=fiche_id, db=db, current_user=current_user)


@legacy_router.post("/test", response_model=ConnectorTestResponse)
def test_legacy_fiche_credentials_before_save(
    request: ConnectorTestRequest,
    fiche_id: int = Path(..., gt=0),
    db: Session = Depends(get_db),
    current_user: Any = Depends(get_current_user),
) -> ConnectorTestResponse:
    return test_automation_credentials_before_save(request=request, automation_id=fiche_id, db=db, current_user=current_user)


@legacy_router.post("/{connector_type}/test", response_model=ConnectorTestResponse)
def test_legacy_fiche_connector(
    connector_type: str = Path(...),
    fiche_id: int = Path(..., gt=0),
    db: Session = Depends(get_db),
    current_user: Any = Depends(get_current_user),
) -> ConnectorTestResponse:
    return test_configured_automation_connector(
        connector_type=connector_type,
        automation_id=fiche_id,
        db=db,
        current_user=current_user,
    )


@legacy_router.delete("/{connector_type}", status_code=status.HTTP_204_NO_CONTENT)
def delete_legacy_fiche_connector(
    connector_type: str = Path(...),
    fiche_id: int = Path(..., gt=0),
    db: Session = Depends(get_db),
    current_user: Any = Depends(get_current_user),
) -> Response:
    return delete_automation_connector(connector_type=connector_type, automation_id=fiche_id, db=db, current_user=current_user)
