"""Tests for the rolling deploy system (deployer service + deployment API)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Set required env vars before importing app code
os.environ.setdefault("CONTROL_PLANE_ADMIN_TOKEN", "test-admin")
os.environ.setdefault("CONTROL_PLANE_JWT_SECRET", "test-jwt-secret-for-tests")
os.environ.setdefault("CONTROL_PLANE_DATABASE_URL", "sqlite:///")
os.environ.setdefault("CONTROL_PLANE_INSTANCE_JWT_SECRET", "test-instance-jwt")
os.environ.setdefault("CONTROL_PLANE_INSTANCE_INTERNAL_API_SECRET", "test-internal")
os.environ.setdefault("CONTROL_PLANE_INSTANCE_FERNET_SECRET", "test-fernet")
os.environ.setdefault("CONTROL_PLANE_INSTANCE_TRIGGER_SIGNING_SECRET", "test-trigger")

from control_plane.db import Base, get_db  # noqa: E402
from control_plane.main import app  # noqa: E402
from control_plane.models import Deployment, Instance, User  # noqa: E402
from control_plane.services.deployer import _deploy_single_instance, _run_deploy  # noqa: E402


ADMIN_HEADERS = {"X-Admin-Token": "test-admin"}


@dataclass
class FakeProvisionResult:
    container_name: str
    data_path: str = ""
    password: str | None = None
    password_hash: str | None = None
    image: str | None = None
    image_digest: str | None = None


@pytest.fixture()
def db_session(tmp_path):
    db_url = f"sqlite:///{tmp_path}/test.db"
    engine = create_engine(db_url)
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = TestSession()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def client(db_session):
    def _override_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = _override_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _make_user(db, email="owner@test.com") -> User:
    user = User(email=email)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_instance(db, user, subdomain="inst1", **kwargs) -> Instance:
    defaults = {
        "user_id": user.id,
        "subdomain": subdomain,
        "container_name": f"longhouse-{subdomain}",
        "status": "active",
        "deploy_ring": 2,
        "deploy_state": "idle",
        "current_image": "ghcr.io/test/app:old",
        "last_healthy_image": "ghcr.io/test/app:old",
    }
    defaults.update(kwargs)
    inst = Instance(**defaults)
    db.add(inst)
    db.commit()
    db.refresh(inst)
    return inst


def _make_deployment(db, deploy_id="d-test-001", **kwargs) -> Deployment:
    defaults = {
        "id": deploy_id,
        "image": "ghcr.io/test/app:new",
        "status": "pending",
        "max_parallel": 5,
        "failure_threshold": 3,
        "failure_count": 0,
    }
    defaults.update(kwargs)
    deploy = Deployment(**defaults)
    db.add(deploy)
    db.commit()
    db.refresh(deploy)
    return deploy


def _mock_provisioner(succeed=True):
    """Create a mock Provisioner. If succeed=False, wait_for_health raises."""
    prov = MagicMock()
    prov.client.images.pull.return_value = None
    mock_image = MagicMock()
    mock_image.attrs = {"RepoDigests": ["ghcr.io/test/app@sha256:abc123"]}
    prov.client.images.get.return_value = mock_image
    # Return a result with the subdomain from the call args
    def _provision_side_effect(subdomain, **kwargs):
        return FakeProvisionResult(
            container_name=f"longhouse-{subdomain}",
            image="ghcr.io/test/app:new",
            image_digest="ghcr.io/test/app@sha256:abc123",
        )
    prov.provision_instance.side_effect = _provision_side_effect
    if not succeed:
        prov.wait_for_health.side_effect = RuntimeError("Health check failed")
    return prov


# ---------------------------------------------------------------------------
# _deploy_single_instance tests
# ---------------------------------------------------------------------------


class TestDeploySingleInstance:
    def test_success(self, db_session):
        user = _make_user(db_session)
        deploy = _make_deployment(db_session)
        inst = _make_instance(db_session, user, deploy_id=deploy.id, deploy_state="pending")
        prov = _mock_provisioner(succeed=True)

        result = _deploy_single_instance(inst, user, deploy, prov, db_session)

        assert result is True
        assert inst.deploy_state == "succeeded"
        assert inst.current_image == deploy.image
        assert inst.last_healthy_image == deploy.image
        assert inst.status == "active"

    def test_failure_triggers_rollback(self, db_session):
        user = _make_user(db_session)
        deploy = _make_deployment(db_session)
        inst = _make_instance(
            db_session, user,
            deploy_id=deploy.id,
            deploy_state="pending",
            last_healthy_image="ghcr.io/test/app:old",
        )
        prov = _mock_provisioner(succeed=True)
        # First health check fails (deploy), second succeeds (rollback)
        prov.wait_for_health.side_effect = [RuntimeError("Health check failed"), True]

        result = _deploy_single_instance(inst, user, deploy, prov, db_session)

        assert result is False
        assert inst.deploy_state == "rolled_back"
        assert inst.current_image == "ghcr.io/test/app:old"
        assert inst.status == "active"
        assert inst.last_health_at is not None
        assert inst.deploy_error is not None

    def test_failure_no_rollback_when_same_image(self, db_session):
        user = _make_user(db_session)
        deploy = _make_deployment(db_session)
        inst = _make_instance(
            db_session, user,
            deploy_id=deploy.id,
            deploy_state="pending",
            last_healthy_image="ghcr.io/test/app:new",  # same as target
        )
        prov = _mock_provisioner(succeed=False)

        result = _deploy_single_instance(inst, user, deploy, prov, db_session)

        assert result is False
        assert inst.deploy_state == "failed"  # no rollback attempted
        assert inst.status == "failed"

    def test_failure_without_last_healthy_image_marks_failed(self, db_session):
        user = _make_user(db_session)
        deploy = _make_deployment(db_session)
        inst = _make_instance(
            db_session, user,
            deploy_id=deploy.id,
            deploy_state="pending",
            last_healthy_image=None,
        )
        prov = _mock_provisioner(succeed=False)

        result = _deploy_single_instance(inst, user, deploy, prov, db_session)

        assert result is False
        assert inst.deploy_state == "failed"
        assert inst.status == "failed"


# ---------------------------------------------------------------------------
# _run_deploy tests
# ---------------------------------------------------------------------------


class TestRunDeploy:
    @patch("control_plane.services.deployer.Provisioner")
    def test_happy_path_all_succeed(self, MockProv, db_session):
        prov = _mock_provisioner(succeed=True)
        MockProv.return_value = prov

        user = _make_user(db_session)
        deploy = _make_deployment(db_session, max_parallel=5, failure_threshold=3)
        inst1 = _make_instance(db_session, user, subdomain="a1", deploy_id=deploy.id, deploy_state="pending")
        inst2 = _make_instance(
            db_session, _make_user(db_session, "u2@test.com"), subdomain="a2",
            deploy_id=deploy.id, deploy_state="pending",
        )

        _run_deploy(deploy.id, db_session)

        db_session.refresh(deploy)
        assert deploy.status == "completed"
        assert deploy.failure_count == 0
        db_session.refresh(inst1)
        db_session.refresh(inst2)
        assert inst1.deploy_state == "succeeded"
        assert inst2.deploy_state == "succeeded"

    @patch("control_plane.services.deployer.Provisioner")
    def test_failure_threshold_pauses(self, MockProv, db_session):
        prov = _mock_provisioner(succeed=False)
        MockProv.return_value = prov

        user1 = _make_user(db_session, "u1@test.com")
        user2 = _make_user(db_session, "u2@test.com")
        user3 = _make_user(db_session, "u3@test.com")
        deploy = _make_deployment(db_session, max_parallel=1, failure_threshold=2)
        inst1 = _make_instance(db_session, user1, subdomain="b1", deploy_id=deploy.id, deploy_state="pending")
        inst2 = _make_instance(db_session, user2, subdomain="b2", deploy_id=deploy.id, deploy_state="pending")
        inst3 = _make_instance(db_session, user3, subdomain="b3", deploy_id=deploy.id, deploy_state="pending")

        _run_deploy(deploy.id, db_session)

        db_session.refresh(deploy)
        assert deploy.status == "paused"
        assert deploy.failure_count == 2
        # Third instance should be skipped (not idle/cleared)
        db_session.refresh(inst3)
        assert inst3.deploy_state == "skipped"
        assert inst3.deploy_id == deploy.id  # still linked

    @patch("control_plane.services.deployer.Provisioner")
    def test_mid_batch_threshold(self, MockProv, db_session):
        """With max_parallel=3 and threshold=1, should stop after first failure in batch."""
        call_count = 0
        prov = _mock_provisioner(succeed=True)

        def fail_on_second(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("Health check failed")

        prov.wait_for_health.side_effect = fail_on_second
        MockProv.return_value = prov

        user1 = _make_user(db_session, "u1@test.com")
        user2 = _make_user(db_session, "u2@test.com")
        user3 = _make_user(db_session, "u3@test.com")
        deploy = _make_deployment(db_session, max_parallel=3, failure_threshold=1)
        _make_instance(db_session, user1, subdomain="c1", deploy_id=deploy.id, deploy_state="pending")
        _make_instance(db_session, user2, subdomain="c2", deploy_id=deploy.id, deploy_state="pending")
        _make_instance(db_session, user3, subdomain="c3", deploy_id=deploy.id, deploy_state="pending")

        _run_deploy(deploy.id, db_session)

        db_session.refresh(deploy)
        # Should pause because threshold was hit mid-batch
        assert deploy.status == "paused"
        assert deploy.failure_count >= 1

    @patch("control_plane.services.deployer.Provisioner")
    def test_image_pull_failure_skips_all(self, MockProv, db_session):
        prov = MagicMock()
        prov.client.images.pull.side_effect = RuntimeError("Pull failed")
        MockProv.return_value = prov

        user = _make_user(db_session)
        deploy = _make_deployment(db_session)
        inst = _make_instance(db_session, user, deploy_id=deploy.id, deploy_state="pending")

        _run_deploy(deploy.id, db_session)

        db_session.refresh(deploy)
        assert deploy.status == "failed"
        db_session.refresh(inst)
        assert inst.deploy_state == "skipped"
        assert inst.deploy_id == deploy.id


# ---------------------------------------------------------------------------
# Crash recovery tests
# ---------------------------------------------------------------------------


class TestCrashRecovery:
    def test_stale_deploying_instances_marked_failed(self, db_session):
        from control_plane.main import _recover_stale_deploys

        user = _make_user(db_session)
        deploy = _make_deployment(db_session, deploy_id="d-crash-001", status="in_progress")
        inst = _make_instance(
            db_session, user,
            subdomain="crash1",
            deploy_id=deploy.id,
            deploy_state="deploying",
        )
        inst_id = inst.id
        deploy_id = deploy.id

        # _recover_stale_deploys creates its own session and calls close(),
        # so we mock SessionLocal to return a non-closing wrapper
        mock_session = MagicMock(wraps=db_session)
        mock_session.close = MagicMock()  # prevent actual close
        with patch("control_plane.main.SessionLocal", return_value=mock_session):
            _recover_stale_deploys()

        # Re-query to get fresh state
        inst = db_session.query(Instance).filter(Instance.id == inst_id).first()
        assert inst.deploy_state == "failed"
        assert inst.deploy_error == "Control plane restarted during deploy"

        deploy = db_session.query(Deployment).filter(Deployment.id == deploy_id).first()
        assert deploy.status == "paused"


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


class TestDeploymentAPI:
    @patch("control_plane.routers.deployments.run_deploy_sync")
    def test_create_deployment(self, mock_run, client, db_session):
        user = _make_user(db_session)
        _make_instance(db_session, user, current_image="ghcr.io/test/app:old")

        resp = client.post(
            "/api/deployments",
            json={"image": "ghcr.io/test/app:new"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "in_progress"
        assert data["total_targeted"] == 1
        mock_run.assert_called_once()

    @patch("control_plane.routers.deployments.run_deploy_sync")
    def test_concurrent_deploy_rejected(self, mock_run, client, db_session):
        user = _make_user(db_session)
        _make_instance(db_session, user, current_image="ghcr.io/test/app:old")

        # Create first deployment
        _make_deployment(db_session, deploy_id="d-existing", status="in_progress")

        resp = client.post(
            "/api/deployments",
            json={"image": "ghcr.io/test/app:new"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 409

    def test_get_deployment_status(self, client, db_session):
        user = _make_user(db_session)
        deploy = _make_deployment(db_session, status="completed")
        _make_instance(
            db_session, user,
            deploy_id=deploy.id,
            deploy_state="succeeded",
        )

        resp = client.get(f"/api/deployments/{deploy.id}", headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert data["succeeded"] == 1
        assert data["status"] == "completed"

    def test_get_deployment_status_includes_skipped(self, client, db_session):
        user = _make_user(db_session)
        deploy = _make_deployment(db_session, status="paused")
        _make_instance(
            db_session, user,
            deploy_id=deploy.id,
            deploy_state="skipped",
        )

        resp = client.get(f"/api/deployments/{deploy.id}", headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert data["skipped"] == 1

    def test_list_deployments(self, client, db_session):
        _make_deployment(db_session)

        resp = client.get("/api/deployments", headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        assert len(resp.json()["deployments"]) == 1

    @patch("control_plane.routers.deployments.run_deploy_sync")
    def test_rollback_image_mismatch_rejected(self, mock_run, client, db_session):
        user1 = _make_user(db_session, "u1@test.com")
        user2 = _make_user(db_session, "u2@test.com")
        deploy = _make_deployment(db_session, status="failed")
        _make_instance(
            db_session, user1, subdomain="r1",
            deploy_id=deploy.id,
            deploy_state="failed",
            last_healthy_image="ghcr.io/test/app:v1",
        )
        _make_instance(
            db_session, user2, subdomain="r2",
            deploy_id=deploy.id,
            deploy_state="failed",
            last_healthy_image="ghcr.io/test/app:v2",  # different!
        )

        resp = client.post(
            f"/api/deployments/{deploy.id}/rollback",
            json={"scope": "failed"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400
        assert "different" in resp.json()["detail"]

    @patch("control_plane.routers.deployments.run_deploy_sync")
    def test_rollback_success(self, mock_run, client, db_session):
        user = _make_user(db_session)
        deploy = _make_deployment(db_session, status="failed")
        _make_instance(
            db_session, user,
            deploy_id=deploy.id,
            deploy_state="failed",
            last_healthy_image="ghcr.io/test/app:old",
        )

        resp = client.post(
            f"/api/deployments/{deploy.id}/rollback",
            json={"scope": "failed"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["image"] == "ghcr.io/test/app:old"
        mock_run.assert_called_once()

    def test_dry_run(self, client, db_session):
        user = _make_user(db_session)
        _make_instance(db_session, user, current_image="ghcr.io/test/app:old")

        resp = client.post(
            "/api/deployments",
            json={"image": "ghcr.io/test/app:new", "dry_run": True},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "dry_run"

    def test_validation_max_parallel(self, client, db_session):
        resp = client.post(
            "/api/deployments",
            json={"image": "ghcr.io/test/app:new", "max_parallel": 0},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 422

    @patch("control_plane.routers.deployments.run_deploy_sync")
    def test_force_still_blocks_concurrent_deploy(self, mock_run, client, db_session):
        """force=true must NOT allow concurrent deploys — two threads on same instances would corrupt state."""
        user = _make_user(db_session)
        _make_instance(db_session, user, current_image="ghcr.io/test/app:old")
        _make_deployment(db_session, deploy_id="d-existing", status="in_progress")

        resp = client.post(
            "/api/deployments",
            json={"image": "ghcr.io/test/app:new", "force": True},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 409
        assert "concurrent" in resp.json()["detail"].lower() or "in progress" in resp.json()["detail"].lower()

    @patch("control_plane.routers.deployments.run_deploy_sync")
    def test_rollback_blocked_during_active_deploy(self, mock_run, client, db_session):
        """Rollback should be rejected if another deployment is already running."""
        user = _make_user(db_session)
        old_deploy = _make_deployment(db_session, deploy_id="d-old", status="failed")
        _make_instance(
            db_session, user,
            deploy_id=old_deploy.id,
            deploy_state="failed",
            last_healthy_image="ghcr.io/test/app:old",
        )
        # Another deploy is in progress
        _make_deployment(db_session, deploy_id="d-active", status="in_progress")

        resp = client.post(
            f"/api/deployments/{old_deploy.id}/rollback",
            json={"scope": "failed"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 409

    def test_deploy_id_uniqueness(self, client, db_session):
        """Deploy IDs should include random suffix to avoid collisions."""
        from control_plane.routers.deployments import _generate_deploy_id
        ids = {_generate_deploy_id() for _ in range(100)}
        assert len(ids) == 100  # all unique


# ---------------------------------------------------------------------------
# Deprovision concurrency guard tests
# ---------------------------------------------------------------------------


class TestDeprovisionGuard:
    @patch("control_plane.routers.instances.Provisioner")
    def test_deprovision_blocked_during_deploy(self, MockProv, client, db_session):
        """Deprovision should be rejected if instance is part of an active deployment."""
        user = _make_user(db_session)
        inst = _make_instance(
            db_session, user,
            deploy_id="d-active",
            deploy_state="deploying",
        )

        resp = client.post(
            f"/api/instances/{inst.id}/deprovision",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 409
        assert "active deployment" in resp.json()["detail"]

    @patch("control_plane.routers.instances.Provisioner")
    def test_deprovision_allowed_after_deploy(self, MockProv, client, db_session):
        """Deprovision should work fine if instance's deploy is complete."""
        user = _make_user(db_session)
        inst = _make_instance(
            db_session, user,
            deploy_id="d-done",
            deploy_state="succeeded",
        )

        resp = client.post(
            f"/api/instances/{inst.id}/deprovision",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Double failure (deploy + rollback both fail) tests
# ---------------------------------------------------------------------------


class TestDoubleFailure:
    def test_deploy_and_rollback_both_fail_sets_instance_down(self, db_session):
        """When both deploy and rollback fail, instance.status should reflect it's down."""
        user = _make_user(db_session)
        deploy = _make_deployment(db_session)
        inst = _make_instance(
            db_session, user,
            deploy_id=deploy.id,
            deploy_state="pending",
            last_healthy_image="ghcr.io/test/app:old",
        )

        prov = MagicMock()
        # Health check fails (deploy fails)
        prov.wait_for_health.side_effect = RuntimeError("Health check failed")
        # Rollback provision also fails
        prov.provision_instance.side_effect = RuntimeError("Docker daemon error")

        result = _deploy_single_instance(inst, user, deploy, prov, db_session)

        assert result is False
        assert inst.status == "failed"  # reflects instance is DOWN
        assert "Rollback also failed" in inst.deploy_error
