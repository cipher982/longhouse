from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
import time
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter
from fastapi import Depends
from fastapi import Header
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from sqlalchemy.orm import Session

from control_plane.config import settings
from control_plane.db import get_db
from control_plane.models import Instance
from control_plane.models import User
from control_plane.schemas import InstanceCreate
from control_plane.schemas import InstanceCustomEnvPayload
from control_plane.schemas import InstanceList
from control_plane.schemas import MigrationStatusOut
from control_plane.schemas import InstanceOut
from control_plane.schemas import TokenOut
from control_plane.services.provisioner import Provisioner
from control_plane.services.provisioner import _generate_password
from control_plane.services.provisioner import normalize_custom_env_overrides
from control_plane.services.provisioner import parse_custom_env_json
from control_plane.services.provisioner import resolve_instance_data_path

router = APIRouter(prefix="/api/instances", tags=["instances"])

_HEAVY_MIGRATION_EVENTS = "20260304_events_branch_backfill"
_HEAVY_MIGRATION_SOURCE_LINES = "20260304_source_lines_branch_revision_rebuild"
_HEAVY_MIGRATION_ORDER = (_HEAVY_MIGRATION_EVENTS, _HEAVY_MIGRATION_SOURCE_LINES)


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


def _instance_url(subdomain: str) -> str:
    return f"https://{subdomain}.{settings.root_domain}"


def _instance_health_url(inst: Instance) -> str:
    return f"{_instance_url(inst.subdomain)}/api/health"


def _probe_instance_health(inst: Instance, timeout_seconds: float = 5.0) -> tuple[bool, str | None]:
    """Probe tenant instance health endpoint."""
    try:
        resp = httpx.get(_instance_health_url(inst), timeout=timeout_seconds, follow_redirects=True)
        if resp.status_code == 200:
            return True, None
        return False, f"status={resp.status_code}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _refresh_instance_health_if_ready(
    db: Session,
    inst: Instance,
    timeout_seconds: float = 5.0,
) -> bool:
    """Promote provisioning instances to active when their health endpoint is ready."""
    if inst.status != "provisioning":
        return inst.status == "active"

    healthy, _ = _probe_instance_health(inst, timeout_seconds=timeout_seconds)
    if healthy:
        inst.status = "active"
        inst.last_health_at = datetime.now(timezone.utc)
        return True
    return False


def _instance_db_path(inst: Instance) -> Path:
    return Path(resolve_instance_data_path(inst.subdomain, data_path=inst.data_path)) / "longhouse.db"


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def _normalized_table_sql(conn: sqlite3.Connection, table_name: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table_name,),
    ).fetchone()
    if not row or not row[0]:
        return ""
    sql = str(row[0]).lower()
    return "".join(ch for ch in sql if not ch.isspace() and ch not in {'"', "`", "[", "]"})


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    if not _table_exists(conn, table_name):
        return set()
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row[1]) for row in rows}


def _build_migration_status(inst: Instance) -> MigrationStatusOut:
    db_path = _instance_db_path(inst)
    if not db_path.exists():
        return MigrationStatusOut(state="unknown", detail=f"db missing at {db_path}")

    pending: set[str] = set()
    failed: set[str] = set()

    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            if _table_exists(conn, "migration_runs"):
                rows = conn.execute("SELECT migration_name, status FROM migration_runs").fetchall()
                for migration_name, migration_status in rows:
                    if str(migration_status) == "failed":
                        failed.add(str(migration_name))

            event_columns = _table_columns(conn, "events")
            if event_columns:
                if "branch_id" not in event_columns:
                    pending.add(_HEAVY_MIGRATION_EVENTS)
                else:
                    null_count_row = conn.execute("SELECT COUNT(*) FROM events WHERE branch_id IS NULL").fetchone()
                    null_count = int(null_count_row[0]) if null_count_row else 0
                    if null_count > 0:
                        pending.add(_HEAVY_MIGRATION_EVENTS)

            source_columns = _table_columns(conn, "source_lines")
            if source_columns:
                if "branch_id" not in source_columns or "revision" not in source_columns:
                    pending.add(_HEAVY_MIGRATION_SOURCE_LINES)
                normalized_sql = _normalized_table_sql(conn, "source_lines")
                if "unique(session_id,source_path,source_offset)" in normalized_sql:
                    pending.add(_HEAVY_MIGRATION_SOURCE_LINES)
    except Exception as exc:  # noqa: BLE001
        return MigrationStatusOut(state="error", detail=str(exc))

    ordered_pending = [name for name in _HEAVY_MIGRATION_ORDER if name in pending]
    ordered_failed = sorted(failed)

    state = "ok"
    if ordered_pending:
        state = "pending"
    elif ordered_failed:
        state = "failed"

    return MigrationStatusOut(
        state=state,
        pending_count=len(ordered_pending),
        pending_names=ordered_pending,
        failed_names=ordered_failed,
    )


def _instance_out(inst: Instance, email: str, *, password: str | None = None) -> InstanceOut:
    return InstanceOut(
        id=inst.id,
        email=email,
        subdomain=inst.subdomain,
        url=_instance_url(inst.subdomain),
        container_name=inst.container_name,
        status=inst.status,
        password=password,
        created_at=inst.created_at,
        last_health_at=inst.last_health_at,
        migration=_build_migration_status(inst),
    )


def _recreate_instance(
    inst: Instance,
    user: User,
    provisioner: Provisioner,
    *,
    password: str | None = None,
) -> Any:
    provisioner.deprovision_instance(inst.container_name)
    try:
        custom_env = parse_custom_env_json(inst.custom_env_json)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    result = provisioner.provision_instance(
        inst.subdomain,
        owner_email=user.email,
        password=password,
        custom_env=custom_env,
        data_path=resolve_instance_data_path(inst.subdomain, data_path=inst.data_path),
    )
    inst.container_name = result.container_name
    inst.data_path = result.data_path
    inst.status = "provisioning"
    inst.last_health_at = None
    if result.password_hash:
        inst.password_hash = result.password_hash
    if result.image:
        inst.current_image = result.image
    return result


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/sso-keys")
def get_sso_keys(
    x_instance_id: str = Header(default=""),
    x_internal_secret: str = Header(default=""),
    db: Session = Depends(get_db),
):
    """Return SSO signing keys for an instance.

    Authenticated via instance ID + internal API secret headers.
    Instances call this at runtime to stay in sync with the control plane's
    JWT secrets, eliminating stale-secret drift after rotations.
    """
    if not x_instance_id or not x_internal_secret:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing auth headers")

    if not hmac.compare_digest(x_internal_secret, settings.instance_internal_api_secret):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid internal secret")

    # Validate that the requesting instance exists in DB
    inst = db.query(Instance).filter(Instance.subdomain == x_instance_id).first()
    if not inst:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Unknown instance")

    # Only return the instance SSO signing key — never the CP's own session key
    keys = [settings.instance_jwt_secret]

    return {"keys": keys, "ttl_seconds": 300}


@router.get("/me", response_model=InstanceOut)
def my_instance(request: Request, db: Session = Depends(get_db)):
    """Get the current user's instance (session auth, not admin)."""
    from control_plane.routers.auth import get_current_user

    user = get_current_user(request, db)
    inst = db.query(Instance).filter(Instance.user_id == user.id).first()
    if not inst:
        raise HTTPException(status_code=404, detail="No instance found")

    return _instance_out(inst, user.email)


@router.get("/me/health")
def my_instance_health(request: Request, db: Session = Depends(get_db)):
    """Server-side health check for the current user's instance.

    Makes a real HTTPS request to the instance, verifying both SSL cert
    and a 200 response from /api/health. Updates instance status to "active"
    on first successful check.
    """
    from control_plane.routers.auth import get_current_user

    user = get_current_user(request, db)
    inst = db.query(Instance).filter(Instance.user_id == user.id).first()
    if not inst:
        raise HTTPException(status_code=404, detail="No instance found")

    if inst.status == "active":
        return {"status": "active", "ready": True}

    healthy, detail = _probe_instance_health(inst, timeout_seconds=5.0)
    if healthy:
        inst.status = "active"
        inst.last_health_at = datetime.now(timezone.utc)
        db.commit()
        return {"status": "active", "ready": True}

    payload: dict[str, Any] = {"status": "provisioning", "ready": False}
    if detail:
        payload["detail"] = detail
    return payload


@router.get("", response_model=InstanceList, dependencies=[Depends(require_admin)])
def list_instances(db: Session = Depends(get_db)):
    rows = db.query(Instance, User).join(User, Instance.user_id == User.id).all()

    changed = False
    for inst, _ in rows:
        if _refresh_instance_health_if_ready(db, inst, timeout_seconds=2.0):
            changed = True
    if changed:
        db.commit()

    return InstanceList(instances=[_instance_out(inst, user.email) for inst, user in rows])


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
        return _instance_out(existing, email)

    provisioner = Provisioner()
    result = provisioner.provision_instance(subdomain, owner_email=email)

    instance = Instance(
        user_id=user.id,
        subdomain=subdomain,
        container_name=result.container_name,
        data_path=result.data_path,
        password_hash=result.password_hash,
        status="provisioning",
    )
    db.add(instance)
    db.commit()
    db.refresh(instance)

    return _instance_out(instance, email, password=result.password)


@router.get("/{instance_id}", response_model=InstanceOut, dependencies=[Depends(require_admin)])
def get_instance(instance_id: int, db: Session = Depends(get_db)):
    row = db.query(Instance, User).join(User, Instance.user_id == User.id).filter(Instance.id == instance_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="instance not found")
    inst, user = row

    if _refresh_instance_health_if_ready(db, inst, timeout_seconds=2.0):
        db.commit()

    return _instance_out(inst, user.email)


@router.get("/{instance_id}/custom-env", dependencies=[Depends(require_admin)])
def get_instance_custom_env(instance_id: int, db: Session = Depends(get_db)):
    inst = db.query(Instance).filter(Instance.id == instance_id).first()
    if not inst:
        raise HTTPException(status_code=404, detail="instance not found")
    try:
        custom_env = parse_custom_env_json(inst.custom_env_json)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"custom_env": custom_env}


@router.put("/{instance_id}/custom-env", dependencies=[Depends(require_admin)])
def update_instance_custom_env(instance_id: int, payload: InstanceCustomEnvPayload, db: Session = Depends(get_db)):
    inst = db.query(Instance).filter(Instance.id == instance_id).first()
    if not inst:
        raise HTTPException(status_code=404, detail="instance not found")

    try:
        normalized = normalize_custom_env_overrides(payload.custom_env)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    inst.custom_env_json = json.dumps(normalized, separators=(",", ":"), sort_keys=True) if normalized else None
    db.commit()
    return {"ok": True, "custom_env": normalized}


@router.post("/{instance_id}/deprovision", dependencies=[Depends(require_admin)])
def deprovision_instance(instance_id: int, db: Session = Depends(get_db)):
    inst = db.query(Instance).filter(Instance.id == instance_id).first()
    if not inst:
        raise HTTPException(status_code=404, detail="instance not found")

    # Concurrency guard: reject if instance is part of an active deployment
    if inst.deploy_id and inst.deploy_state in ("pending", "deploying"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Instance is part of active deployment {inst.deploy_id}",
        )

    provisioner = Provisioner()
    provisioner.deprovision_instance(inst.container_name)

    inst.status = "deprovisioned"
    db.commit()
    return {"ok": True}


@router.post("/{instance_id}/regenerate-password", response_model=InstanceOut, dependencies=[Depends(require_admin)])
def regenerate_password(instance_id: int, db: Session = Depends(get_db)):
    """Generate a new password for an instance and update its container env."""
    row = db.query(Instance, User).join(User, Instance.user_id == User.id).filter(Instance.id == instance_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="instance not found")
    inst, user = row

    password, password_hash = _generate_password()
    inst.password_hash = password_hash
    db.commit()

    provisioner = Provisioner()
    result = _recreate_instance(inst, user, provisioner, password=password)
    db.commit()
    db.refresh(inst)

    return _instance_out(inst, user.email, password=password)


@router.post("/{instance_id}/reprovision", response_model=InstanceOut, dependencies=[Depends(require_admin)])
def reprovision_instance(instance_id: int, db: Session = Depends(get_db)):
    """Reprovision a stopped/deprovisioned instance."""
    row = db.query(Instance, User).join(User, Instance.user_id == User.id).filter(Instance.id == instance_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="instance not found")
    inst, user = row

    # Concurrency guard: reject if instance is part of an active deployment
    if inst.deploy_id and inst.deploy_state in ("pending", "deploying"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Instance is part of active deployment {inst.deploy_id}",
        )

    provisioner = Provisioner()
    try:
        provisioner.run_migration_preflight(
            inst.subdomain,
            data_path=resolve_instance_data_path(inst.subdomain, data_path=inst.data_path),
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    result = _recreate_instance(inst, user, provisioner)
    db.commit()
    db.refresh(inst)

    return _instance_out(inst, user.email, password=result.password)


@router.post("/backfill-images", dependencies=[Depends(require_admin)])
def backfill_images(db: Session = Depends(get_db)):
    """One-time backfill: read running container image refs into Instance records."""
    import docker

    client = docker.DockerClient(base_url=settings.docker_host)
    instances = db.query(Instance).filter(Instance.status.in_(["active", "provisioning"])).all()
    updated = 0

    for inst in instances:
        try:
            container = client.containers.get(inst.container_name)
            image_ref = container.image.tags[0] if container.image.tags else None
            if not image_ref:
                digests = container.image.attrs.get("RepoDigests", [])
                image_ref = digests[0] if digests else None
            if image_ref:
                inst.current_image = image_ref
                inst.last_healthy_image = image_ref
                updated += 1
        except Exception:  # noqa: BLE001
            continue

    db.commit()
    return {"ok": True, "updated": updated, "total": len(instances)}


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
        "sub": str(user.id),
        "email": user.email,
        "instance": inst.subdomain,
        "exp": int(time.time()) + expires_in,
    }
    token = _encode_jwt(payload, settings.instance_jwt_secret)
    return TokenOut(token=token, expires_in=expires_in)
