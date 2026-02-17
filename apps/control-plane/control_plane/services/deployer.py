"""Batch deployment service for rolling updates across tenant instances.

DB-driven reconcile loop: reads Deployment + Instance state from the database,
so it can resume after control plane restarts.
"""
from __future__ import annotations

import logging
from datetime import datetime
from datetime import timezone

import docker
from sqlalchemy.orm import Session

from control_plane.db import SessionLocal
from control_plane.models import Deployment
from control_plane.models import Instance
from control_plane.models import User
from control_plane.services.provisioner import Provisioner

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _chunked(items: list, size: int):
    """Yield successive chunks of `size` from `items`."""
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _deploy_single_instance(
    inst: Instance,
    user: User,
    deploy: Deployment,
    provisioner: Provisioner,
    db: Session,
) -> bool:
    """Deploy a single instance. Returns True on success, False on failure."""
    inst.deploy_state = "deploying"
    inst.deploy_started_at = _utcnow()
    inst.deploy_error = None
    db.commit()

    try:
        provisioner.deprovision_instance(inst.container_name)
        # skip_pull=True: batch deploy pre-pulls once to avoid tag drift between instances
        result = provisioner.provision_instance(
            inst.subdomain, owner_email=user.email, image=deploy.image, skip_pull=True
        )
        inst.container_name = result.container_name

        # wait_for_health raises RuntimeError on timeout (never returns False)
        provisioner.wait_for_health(inst.subdomain, timeout=120)

        inst.deploy_state = "succeeded"
        inst.current_image = deploy.image
        inst.last_healthy_image = deploy.image
        inst.status = "active"
        inst.last_health_at = _utcnow()
        db.commit()
        logger.info(f"Deploy succeeded for {inst.subdomain}")
        return True

    except Exception as exc:
        inst.deploy_state = "failed"
        inst.deploy_error = str(exc)[:500]
        inst.status = "failed"
        logger.error(f"Deploy failed for {inst.subdomain}: {exc}")

        # Attempt rollback to last known good image
        if inst.last_healthy_image and inst.last_healthy_image != deploy.image:
            try:
                provisioner.deprovision_instance(inst.container_name)
                result = provisioner.provision_instance(
                    inst.subdomain, owner_email=user.email, image=inst.last_healthy_image
                )
                inst.container_name = result.container_name
                provisioner.wait_for_health(inst.subdomain, timeout=120)
                inst.deploy_state = "rolled_back"
                inst.current_image = inst.last_healthy_image
                inst.status = "active"
                inst.last_health_at = _utcnow()
                logger.info(f"Rolled back {inst.subdomain} to {inst.last_healthy_image}")
            except Exception as rb_exc:
                # Deploy failed AND rollback failed — instance has no running container
                inst.status = "failed"
                inst.deploy_error = f"Deploy failed: {str(exc)[:200]}; Rollback also failed: {str(rb_exc)[:200]}"
                logger.error(f"CRITICAL: Rollback failed for {inst.subdomain}: {rb_exc}. Instance is DOWN.")

        db.commit()
        return False


def run_deploy_sync(deploy_id: str) -> None:
    """Run the deployment reconcile loop synchronously.

    This is the main entry point — called from a background thread.
    Uses its own DB session to avoid threading issues.
    """
    db = SessionLocal()
    try:
        _run_deploy(deploy_id, db)
    except Exception:
        logger.exception(f"Deployment {deploy_id} failed with unhandled error")
        deploy = db.query(Deployment).filter(Deployment.id == deploy_id).first()
        if deploy and deploy.status == "in_progress":
            deploy.status = "failed"
            deploy.completed_at = _utcnow()
            db.commit()
    finally:
        db.close()


def _run_deploy(deploy_id: str, db: Session) -> None:
    deploy = db.query(Deployment).filter(Deployment.id == deploy_id).first()
    if not deploy or deploy.status not in ("pending", "in_progress"):
        return

    deploy.status = "in_progress"
    db.commit()

    provisioner = Provisioner()

    # Pre-pull image once — all instances use this cached image (no per-instance pulls)
    try:
        provisioner.client.images.pull(deploy.image)
    except docker.errors.ImageNotFound:
        try:
            provisioner.client.images.get(deploy.image)
            logger.warning("Image %s not found in registry; using local image", deploy.image)
        except docker.errors.ImageNotFound as exc:
            logger.error(f"Failed to pull image {deploy.image}: {exc}")
            deploy.status = "failed"
            deploy.completed_at = _utcnow()
            # Mark pending instances as skipped (keep deploy_id for status visibility)
            db.query(Instance).filter(
                Instance.deploy_id == deploy_id,
                Instance.deploy_state == "pending",
            ).update({"deploy_state": "skipped"})
            db.commit()
            return
    except Exception as exc:
        logger.error(f"Failed to pull image {deploy.image}: {exc}")
        deploy.status = "failed"
        deploy.completed_at = _utcnow()
        # Mark pending instances as skipped (keep deploy_id for status visibility)
        db.query(Instance).filter(
            Instance.deploy_id == deploy_id,
            Instance.deploy_state == "pending",
        ).update({"deploy_state": "skipped"})
        db.commit()
        return

    try:
        pulled = provisioner.client.images.get(deploy.image)
        digests = pulled.attrs.get("RepoDigests", [])
        if digests and not deploy.image_digest:
            deploy.image_digest = digests[0]
            db.commit()
    except Exception:  # noqa: BLE001
        pass

    # Get all instances for this deploy that haven't succeeded
    instances_with_users = (
        db.query(Instance, User)
        .join(User, Instance.user_id == User.id)
        .filter(
            Instance.deploy_id == deploy_id,
            Instance.deploy_state.in_(["pending", "failed"]),
        )
        .order_by(Instance.deploy_ring)
        .all()
    )

    for batch in _chunked(instances_with_users, deploy.max_parallel):
        # Check failure threshold BEFORE starting batch
        if deploy.failure_count >= deploy.failure_threshold:
            deploy.status = "paused"
            # Mark remaining pending instances as skipped (keep deploy_id for status visibility)
            db.query(Instance).filter(
                Instance.deploy_id == deploy_id,
                Instance.deploy_state == "pending",
            ).update({"deploy_state": "skipped"})
            db.commit()
            logger.warning(
                f"Deployment {deploy_id} paused: {deploy.failure_count} failures "
                f"reached threshold {deploy.failure_threshold}"
            )
            return

        for inst, user in batch:
            success = _deploy_single_instance(inst, user, deploy, provisioner, db)
            if not success:
                deploy.failure_count += 1
                db.commit()
                # Check threshold mid-batch to avoid exceeding it
                if deploy.failure_count >= deploy.failure_threshold:
                    break

    # Check if threshold was hit in the final batch
    if deploy.failure_count >= deploy.failure_threshold:
        deploy.status = "paused"
        db.query(Instance).filter(
            Instance.deploy_id == deploy_id,
            Instance.deploy_state == "pending",
        ).update({"deploy_state": "skipped"})
        db.commit()
        logger.warning(
            f"Deployment {deploy_id} paused: {deploy.failure_count} failures "
            f"reached threshold {deploy.failure_threshold}"
        )
        return

    # Finalize
    deploy.status = "completed" if deploy.failure_count == 0 else "failed"
    deploy.completed_at = _utcnow()
    db.commit()
    logger.info(
        f"Deployment {deploy_id} {deploy.status}: "
        f"{deploy.failure_count} failures out of {len(instances_with_users)} instances"
    )
