"""Deployment management API for rolling updates across tenant instances."""
from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime
from datetime import timezone

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from control_plane.config import settings
from control_plane.db import get_db
from control_plane.models import Deployment
from control_plane.models import Instance
from control_plane.models import User
from control_plane.routers.instances import require_admin
from control_plane.services.deployer import run_deploy_sync

router = APIRouter(prefix="/api/deployments", tags=["deployments"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class DeploymentCreate(BaseModel):
    image: str
    max_parallel: int = settings.deploy_max_parallel
    failure_threshold: int = settings.deploy_failure_threshold
    rings: list[int] | None = None
    reason: str | None = None
    dry_run: bool = False
    force: bool = False

    def model_post_init(self, __context) -> None:
        if self.max_parallel < 1:
            raise ValueError("max_parallel must be >= 1")
        if self.failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")


class InstanceDeployInfo(BaseModel):
    id: int
    subdomain: str
    ring: int
    deploy_state: str
    skip_reason: str | None = None


class DeploymentOut(BaseModel):
    id: str
    image: str
    image_digest: str | None = None
    status: str
    total_targeted: int
    instances: list[InstanceDeployInfo]


class DeploymentStatus(BaseModel):
    id: str
    image: str
    image_digest: str | None = None
    status: str
    total: int
    succeeded: int
    deploying: int
    failed: int
    pending: int
    rolled_back: int
    skipped: int
    failure_threshold: int
    created_at: datetime | None = None
    completed_at: datetime | None = None
    failed_instances: list[InstanceDeployInfo]


class RollbackCreate(BaseModel):
    scope: str = "failed"  # "failed" or "all"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _generate_deploy_id() -> str:
    now = datetime.now(timezone.utc)
    suffix = secrets.token_hex(3)  # 6 hex chars to avoid second-granularity collisions
    return f"d-{now.strftime('%Y%m%d-%H%M%S')}-{suffix}"


def _get_targeted_instances(
    db: Session,
    rings: list[int] | None,
    target_image: str,
) -> list[tuple[Instance, str | None]]:
    """Return (instance, skip_reason) tuples for all active instances matching ring filter."""
    query = db.query(Instance).filter(Instance.status.in_(["active", "provisioning"]))
    if rings is not None:
        query = query.filter(Instance.deploy_ring.in_(rings))

    instances = query.order_by(Instance.deploy_ring).all()
    results: list[tuple[Instance, str | None]] = []

    for inst in instances:
        skip = None
        if inst.current_image == target_image:
            skip = "already on target image"
        elif inst.deploy_id and inst.deploy_state in ("pending", "deploying"):
            skip = f"active deploy {inst.deploy_id}"
        results.append((inst, skip))

    return results


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("", response_model=DeploymentOut, dependencies=[Depends(require_admin)])
def create_deployment(payload: DeploymentCreate, db: Session = Depends(get_db)):
    """Create a new rolling deployment."""
    # Global concurrency: only one active deployment at a time
    # force=true skips ring prerequisites but NEVER allows concurrent deploys —
    # two deploy threads on the same instances would corrupt state and orphan containers
    active = (
        db.query(Deployment)
        .filter(Deployment.status.in_(["pending", "in_progress"]))
        .first()
    )
    if active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Active deployment {active.id} in progress. Cannot run concurrent deploys.",
        )

    # Ring prerequisite enforcement
    if payload.rings and not payload.force:
        max_ring = max(payload.rings)
        if max_ring > 0:
            # Check that all lower rings have succeeded for this image
            lower_ring_instances = (
                db.query(Instance)
                .filter(
                    Instance.deploy_ring < max_ring,
                    Instance.status.in_(["active", "provisioning"]),
                )
                .all()
            )
            not_on_image = [
                inst for inst in lower_ring_instances if inst.current_image != payload.image
            ]
            if not_on_image:
                subdomains = [inst.subdomain for inst in not_on_image[:5]]
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"Ring prerequisite not met: {len(not_on_image)} instances in lower rings "
                        f"not on target image (e.g. {', '.join(subdomains)}). Use force=true to override."
                    ),
                )

    # Build target list
    targeted = _get_targeted_instances(db, payload.rings, payload.image)
    deployable = [(inst, skip) for inst, skip in targeted if skip is None]

    deploy_id = _generate_deploy_id()
    instance_infos: list[InstanceDeployInfo] = []

    for inst, skip in targeted:
        instance_infos.append(
            InstanceDeployInfo(
                id=inst.id,
                subdomain=inst.subdomain,
                ring=inst.deploy_ring,
                deploy_state="pending" if skip is None else inst.deploy_state,
                skip_reason=skip,
            )
        )

    if payload.dry_run:
        return DeploymentOut(
            id=deploy_id,
            image=payload.image,
            status="dry_run",
            total_targeted=len(deployable),
            instances=instance_infos,
        )

    if not deployable:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No instances to deploy (all skipped or already on target image)",
        )

    # Create deployment record
    deploy = Deployment(
        id=deploy_id,
        image=payload.image,
        status="pending",
        rings=json.dumps(payload.rings) if payload.rings else None,
        max_parallel=payload.max_parallel,
        failure_threshold=payload.failure_threshold,
        reason=payload.reason,
    )
    db.add(deploy)
    db.flush()

    # Post-insert race guard: if another request snuck in, back off
    active_count = (
        db.query(Deployment)
        .filter(Deployment.status.in_(["pending", "in_progress"]))
        .count()
    )
    if active_count > 1:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Another deployment was created concurrently. Retry.",
        )

    # Mark instances as pending
    for inst, _skip in deployable:
        inst.desired_image = payload.image
        inst.deploy_state = "pending"
        inst.deploy_id = deploy_id
        inst.deploy_error = None

    db.commit()

    # Kick off deploy in background thread
    thread = threading.Thread(
        target=run_deploy_sync,
        args=(deploy_id,),
        daemon=True,
        name=f"deploy-{deploy_id}",
    )
    thread.start()

    return DeploymentOut(
        id=deploy_id,
        image=payload.image,
        status="in_progress",
        total_targeted=len(deployable),
        instances=instance_infos,
    )


@router.get("/{deploy_id}", response_model=DeploymentStatus, dependencies=[Depends(require_admin)])
def get_deployment(deploy_id: str, db: Session = Depends(get_db)):
    """Get deployment status with per-instance details."""
    deploy = db.query(Deployment).filter(Deployment.id == deploy_id).first()
    if not deploy:
        raise HTTPException(status_code=404, detail="Deployment not found")

    # Count instance states for this deployment
    instances = db.query(Instance).filter(Instance.deploy_id == deploy_id).all()

    # Also include instances that were part of this deploy but rolled back/completed
    # (deploy_id may have been cleared on pause)
    state_counts = {"succeeded": 0, "deploying": 0, "failed": 0, "pending": 0, "rolled_back": 0, "skipped": 0}
    failed_instances: list[InstanceDeployInfo] = []

    for inst in instances:
        state = inst.deploy_state
        if state in state_counts:
            state_counts[state] += 1
        if state in ("failed", "rolled_back"):
            failed_instances.append(
                InstanceDeployInfo(
                    id=inst.id,
                    subdomain=inst.subdomain,
                    ring=inst.deploy_ring,
                    deploy_state=state,
                    skip_reason=inst.deploy_error,
                )
            )

    return DeploymentStatus(
        id=deploy.id,
        image=deploy.image,
        image_digest=deploy.image_digest,
        status=deploy.status,
        total=len(instances),
        succeeded=state_counts["succeeded"],
        deploying=state_counts["deploying"],
        failed=state_counts["failed"],
        pending=state_counts["pending"],
        rolled_back=state_counts["rolled_back"],
        skipped=state_counts["skipped"],
        failure_threshold=deploy.failure_threshold,
        created_at=deploy.created_at,
        completed_at=deploy.completed_at,
        failed_instances=failed_instances,
    )


@router.get("", dependencies=[Depends(require_admin)])
def list_deployments(db: Session = Depends(get_db)):
    """List recent deployments."""
    deploys = (
        db.query(Deployment)
        .order_by(Deployment.created_at.desc())
        .limit(20)
        .all()
    )
    return {
        "deployments": [
            {
                "id": d.id,
                "image": d.image,
                "status": d.status,
                "failure_count": d.failure_count,
                "created_at": d.created_at.isoformat() if d.created_at else None,
                "completed_at": d.completed_at.isoformat() if d.completed_at else None,
                "reason": d.reason,
            }
            for d in deploys
        ]
    }


@router.post("/{deploy_id}/rollback", response_model=DeploymentOut, dependencies=[Depends(require_admin)])
def rollback_deployment(deploy_id: str, payload: RollbackCreate, db: Session = Depends(get_db)):
    """Roll back a deployment by creating a new deployment targeting last_healthy_image."""
    original = db.query(Deployment).filter(Deployment.id == deploy_id).first()
    if not original:
        raise HTTPException(status_code=404, detail="Deployment not found")

    # Prevent rollback while deploy is still running
    if original.status in ("pending", "in_progress"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Deployment {deploy_id} is still {original.status}. Wait for completion or pause first.",
        )

    # Block if another deployment is already running
    active = (
        db.query(Deployment)
        .filter(Deployment.status.in_(["pending", "in_progress"]))
        .first()
    )
    if active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Active deployment {active.id} in progress. Wait for it to complete first.",
        )

    # Find instances to roll back
    scope_filter = [Instance.deploy_id == deploy_id]
    if payload.scope == "failed":
        scope_filter.append(Instance.deploy_state.in_(["failed", "rolled_back"]))

    instances = db.query(Instance).filter(*scope_filter).all()
    if not instances:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No instances to roll back",
        )

    # Check all have a last_healthy_image to roll back to
    no_rollback = [inst for inst in instances if not inst.last_healthy_image]
    if no_rollback:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{len(no_rollback)} instances have no last_healthy_image to roll back to",
        )

    # Verify all instances agree on the rollback target
    unique_images = {inst.last_healthy_image for inst in instances}
    if len(unique_images) > 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Instances have {len(unique_images)} different last_healthy_image values: "
                f"{', '.join(sorted(unique_images))}. Roll back specific instances manually."
            ),
        )
    rollback_image = unique_images.pop()
    rollback_id = _generate_deploy_id()

    deploy = Deployment(
        id=rollback_id,
        image=rollback_image,
        status="pending",
        max_parallel=original.max_parallel,
        failure_threshold=original.failure_threshold,
        reason=f"Rollback of {deploy_id}",
    )
    db.add(deploy)

    instance_infos: list[InstanceDeployInfo] = []
    for inst in instances:
        inst.desired_image = rollback_image
        inst.deploy_state = "pending"
        inst.deploy_id = rollback_id
        inst.deploy_error = None
        instance_infos.append(
            InstanceDeployInfo(
                id=inst.id,
                subdomain=inst.subdomain,
                ring=inst.deploy_ring,
                deploy_state="pending",
            )
        )

    db.commit()

    # Kick off rollback in background
    thread = threading.Thread(
        target=run_deploy_sync,
        args=(rollback_id,),
        daemon=True,
        name=f"deploy-{rollback_id}",
    )
    thread.start()

    return DeploymentOut(
        id=rollback_id,
        image=rollback_image,
        status="in_progress",
        total_targeted=len(instances),
        instances=instance_infos,
    )
