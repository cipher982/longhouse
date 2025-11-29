"""Agent Connector Credentials API.

REST endpoints for managing per-agent connector credentials:
- List all connector types and their configuration status
- Configure (create/update) credentials for a connector
- Test credentials before or after saving
- Delete connector credentials

All endpoints are scoped to agents owned by the authenticated user.
Credentials are encrypted at rest and never returned in responses.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status
from sqlalchemy.orm import Session

from zerg.connectors.registry import CONNECTOR_REGISTRY, ConnectorType, get_required_fields
from zerg.connectors.testers import test_connector
from zerg.database import get_db
from zerg.dependencies.auth import get_current_user
from zerg.models.models import Agent, ConnectorCredential
from zerg.schemas.connector_schemas import (
    ConnectorConfigureRequest,
    ConnectorStatusResponse,
    ConnectorSuccessResponse,
    ConnectorTestRequest,
    ConnectorTestResponse,
    CredentialFieldSchema,
)
from zerg.utils.crypto import decrypt, encrypt

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/agents/{agent_id}/connectors",
    tags=["agent-connectors"],
)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _get_agent_or_404(db: Session, agent_id: int, current_user: Any) -> Agent:
    """Get agent and verify ownership, raise 404 if not found/owned."""
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent or agent.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


def _get_credential_or_404(db: Session, agent_id: int, connector_type: str) -> ConnectorCredential:
    """Get credential and raise 404 if not found."""
    cred = (
        db.query(ConnectorCredential)
        .filter(
            ConnectorCredential.agent_id == agent_id,
            ConnectorCredential.connector_type == connector_type,
        )
        .first()
    )
    if not cred:
        raise HTTPException(status_code=404, detail="Connector not configured")
    return cred


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/", response_model=list[ConnectorStatusResponse])
def list_agent_connectors(
    agent_id: int = Path(..., gt=0),
    db: Session = Depends(get_db),
    current_user: Any = Depends(get_current_user),
) -> list[ConnectorStatusResponse]:
    """List all connector types and their configuration status for an agent.

    Returns all available connector types with:
    - Metadata (name, description, required fields)
    - Whether credentials are configured for this agent
    - Test status and metadata from last test
    """
    _get_agent_or_404(db, agent_id, current_user)

    # Get all configured credentials for this agent
    configured_creds = {
        c.connector_type: c
        for c in db.query(ConnectorCredential).filter(ConnectorCredential.agent_id == agent_id).all()
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
            ConnectorStatusResponse(
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
def configure_connector(
    request: ConnectorConfigureRequest,
    agent_id: int = Path(..., gt=0),
    db: Session = Depends(get_db),
    current_user: Any = Depends(get_current_user),
) -> ConnectorSuccessResponse:
    """Configure (create or update) connector credentials for an agent.

    If credentials already exist for this connector type, they are updated.
    Otherwise, new credentials are created.

    Test status is reset to 'untested' when credentials are updated.
    """
    _get_agent_or_404(db, agent_id, current_user)

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
        db.query(ConnectorCredential)
        .filter(
            ConnectorCredential.agent_id == agent_id,
            ConnectorCredential.connector_type == conn_type.value,
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
        logger.info("Updated %s credentials for agent %d", conn_type.value, agent_id)
    else:
        # Create new
        cred = ConnectorCredential(
            agent_id=agent_id,
            connector_type=conn_type.value,
            encrypted_value=encrypted,
            display_name=request.display_name,
        )
        db.add(cred)
        logger.info("Created %s credentials for agent %d", conn_type.value, agent_id)

    db.commit()
    return ConnectorSuccessResponse(success=True)


@router.post("/test", response_model=ConnectorTestResponse)
def test_credentials_before_save(
    request: ConnectorTestRequest,
    agent_id: int = Path(..., gt=0),
    db: Session = Depends(get_db),
    current_user: Any = Depends(get_current_user),
) -> ConnectorTestResponse:
    """Test credentials before saving them.

    This endpoint allows testing credentials without persisting them.
    Useful for validating credentials in the UI before committing.
    """
    _get_agent_or_404(db, agent_id, current_user)

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
    agent_id: int = Path(..., gt=0),
    db: Session = Depends(get_db),
    current_user: Any = Depends(get_current_user),
) -> ConnectorTestResponse:
    """Test already-configured connector credentials.

    Tests the stored credentials and updates the test_status and metadata.
    """
    _get_agent_or_404(db, agent_id, current_user)

    cred = _get_credential_or_404(db, agent_id, connector_type)

    # Decrypt credentials
    try:
        decrypted = json.loads(decrypt(cred.encrypted_value))
    except Exception as e:
        logger.exception("Failed to decrypt credentials for agent %d connector %s", agent_id, connector_type)
        raise HTTPException(status_code=500, detail="Failed to decrypt credentials")

    # Test credentials
    result = test_connector(connector_type, decrypted)

    # Update test status
    cred.test_status = "success" if result["success"] else "failed"
    cred.last_tested_at = datetime.utcnow()
    if result.get("metadata"):
        cred.connector_metadata = result["metadata"]

    db.commit()

    return ConnectorTestResponse(
        success=result["success"],
        message=result["message"],
        metadata=result.get("metadata"),
    )


@router.delete("/{connector_type}", status_code=status.HTTP_204_NO_CONTENT)
def delete_connector(
    connector_type: str = Path(...),
    agent_id: int = Path(..., gt=0),
    db: Session = Depends(get_db),
    current_user: Any = Depends(get_current_user),
) -> Response:
    """Remove connector credentials from an agent.

    This deletes the stored credentials permanently.
    """
    _get_agent_or_404(db, agent_id, current_user)

    cred = _get_credential_or_404(db, agent_id, connector_type)

    db.delete(cred)
    db.commit()

    logger.info("Deleted %s credentials for agent %d", connector_type, agent_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
