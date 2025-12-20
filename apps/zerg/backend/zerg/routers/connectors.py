"""Connectors API (minimal, connector-first email integration).

Exposes read and delete operations for the authenticated user's connectors.
Secrets in the `config` field are redacted by default.
"""

from __future__ import annotations

from typing import Any
from typing import Dict
from typing import List

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Path
from fastapi import status
from sqlalchemy.orm import Session

from zerg.crud import crud
from zerg.database import get_db
from zerg.dependencies.auth import get_current_user

router = APIRouter(prefix="/connectors", tags=["connectors"], dependencies=[Depends(get_current_user)])


def _redact_config(cfg: Dict[str, Any] | None) -> Dict[str, Any] | None:
    if cfg is None:
        return None
    redacted = dict(cfg)
    # Known secret keys to redact; extend as new providers are added
    for key in ("refresh_token", "client_secret", "password", "secret"):
        if key in redacted:
            redacted[key] = "***"
    return redacted


@router.get("/", status_code=status.HTTP_200_OK)
def list_connectors(db: Session = Depends(get_db), current_user: Any = Depends(get_current_user)) -> List[Dict[str, Any]]:
    rows = crud.get_connectors(db, owner_id=current_user.id)
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "id": r.id,
                "owner_id": r.owner_id,
                "type": r.type,
                "provider": r.provider,
                "config": _redact_config(r.config),
                "created_at": getattr(r, "created_at", None),
                "updated_at": getattr(r, "updated_at", None),
            }
        )
    return out


@router.delete("/{connector_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_connector(
    *,
    connector_id: int = Path(..., gt=0),
    db: Session = Depends(get_db),
    current_user: Any = Depends(get_current_user),
):
    conn = crud.get_connector(db, connector_id)
    if conn is None or conn.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Connector not found")
    crud.delete_connector(db, connector_id)
