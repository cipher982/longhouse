from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Header
from fastapi import HTTPException
from fastapi import status
from sqlalchemy.orm import Session

from control_plane.config import settings
from control_plane.db import get_db
from control_plane.models import Instance
from control_plane.models import User
from control_plane.schemas import InstanceCreate
from control_plane.schemas import InstanceList
from control_plane.schemas import InstanceOut
from control_plane.schemas import TokenOut
from control_plane.services.provisioner import Provisioner

router = APIRouter(prefix="/api/instances", tags=["instances"])


# ---------------------------------------------------------------------------
# Admin auth
# ---------------------------------------------------------------------------


def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    if not x_admin_token or not hmac.compare_digest(x_admin_token, settings.admin_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin token required")


# ---------------------------------------------------------------------------
# JWT helper (HS256)
# ---------------------------------------------------------------------------


def _b64url(data: bytes) -> bytes:
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def _encode_jwt(payload: dict[str, Any], secret: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    header_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = header_b64 + b"." + payload_b64
    signature = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    sig_b64 = _b64url(signature)
    return (signing_input + b"." + sig_b64).decode()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", response_model=InstanceList, dependencies=[Depends(require_admin)])
def list_instances(db: Session = Depends(get_db)):
    rows = db.query(Instance, User).join(User, Instance.user_id == User.id).all()
    items: list[InstanceOut] = []
    for inst, user in rows:
        items.append(
            InstanceOut(
                id=inst.id,
                email=user.email,
                subdomain=inst.subdomain,
                container_name=inst.container_name,
                status=inst.status,
                created_at=inst.created_at,
                last_health_at=inst.last_health_at,
            )
        )
    return InstanceList(instances=items)


@router.post("", response_model=InstanceOut, dependencies=[Depends(require_admin)])
def create_instance(payload: InstanceCreate, db: Session = Depends(get_db)):
    email = payload.email.strip().lower()
    subdomain = payload.subdomain.strip().lower()

    if not email or not subdomain:
        raise HTTPException(status_code=400, detail="email and subdomain are required")

    # Get or create user
    user = db.query(User).filter(User.email == email).first()
    if not user:
        user = User(email=email)
        db.add(user)
        db.commit()
        db.refresh(user)

    # Idempotent: if user already has instance, return it
    existing = db.query(Instance).filter(Instance.user_id == user.id).first()
    if existing:
        return InstanceOut(
            id=existing.id,
            email=email,
            subdomain=existing.subdomain,
            container_name=existing.container_name,
            status=existing.status,
            created_at=existing.created_at,
            last_health_at=existing.last_health_at,
        )

    provisioner = Provisioner()
    result = provisioner.provision_instance(subdomain, owner_email=email)

    instance = Instance(
        user_id=user.id,
        subdomain=subdomain,
        container_name=result.container_name,
        data_path=result.data_path,
        status="provisioning",
    )
    db.add(instance)
    db.commit()
    db.refresh(instance)

    return InstanceOut(
        id=instance.id,
        email=email,
        subdomain=instance.subdomain,
        container_name=instance.container_name,
        status=instance.status,
        created_at=instance.created_at,
        last_health_at=instance.last_health_at,
    )


@router.get("/{instance_id}", response_model=InstanceOut, dependencies=[Depends(require_admin)])
def get_instance(instance_id: int, db: Session = Depends(get_db)):
    row = db.query(Instance, User).join(User, Instance.user_id == User.id).filter(Instance.id == instance_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="instance not found")
    inst, user = row
    return InstanceOut(
        id=inst.id,
        email=user.email,
        subdomain=inst.subdomain,
        container_name=inst.container_name,
        status=inst.status,
        created_at=inst.created_at,
        last_health_at=inst.last_health_at,
    )


@router.post("/{instance_id}/deprovision", dependencies=[Depends(require_admin)])
def deprovision_instance(instance_id: int, db: Session = Depends(get_db)):
    inst = db.query(Instance).filter(Instance.id == instance_id).first()
    if not inst:
        raise HTTPException(status_code=404, detail="instance not found")

    provisioner = Provisioner()
    provisioner.deprovision_instance(inst.container_name)

    inst.status = "deprovisioned"
    db.commit()
    return {"ok": True}


@router.post("/{instance_id}/reprovision", response_model=InstanceOut, dependencies=[Depends(require_admin)])
def reprovision_instance(instance_id: int, db: Session = Depends(get_db)):
    """Reprovision a stopped/deprovisioned instance."""
    row = db.query(Instance, User).join(User, Instance.user_id == User.id).filter(Instance.id == instance_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="instance not found")
    inst, user = row

    provisioner = Provisioner()
    result = provisioner.provision_instance(inst.subdomain, owner_email=user.email)

    inst.status = "provisioning"
    inst.container_name = result.container_name
    db.commit()
    db.refresh(inst)

    return InstanceOut(
        id=inst.id,
        email=user.email,
        subdomain=inst.subdomain,
        container_name=inst.container_name,
        status=inst.status,
        created_at=inst.created_at,
        last_health_at=inst.last_health_at,
    )


@router.post("/{instance_id}/login-token", response_model=TokenOut, dependencies=[Depends(require_admin)])
def issue_login_token(instance_id: int, db: Session = Depends(get_db)):
    inst = db.query(Instance).filter(Instance.id == instance_id).first()
    if not inst:
        raise HTTPException(status_code=404, detail="instance not found")

    user = db.query(User).filter(User.id == inst.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="user not found")

    expires_in = 5 * 60
    payload = {
        "sub": user.email,
        "instance": inst.subdomain,
        "exp": int(time.time()) + expires_in,
    }
    token = _encode_jwt(payload, settings.jwt_secret)
    return TokenOut(token=token, expires_in=expires_in)
