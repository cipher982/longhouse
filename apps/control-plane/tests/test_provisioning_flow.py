"""Tests for the full provisioning flow: signup -> Stripe -> provision -> health -> deprovision.

Covers:
- Provisioner unit tests (env generation, labels, volume, provision/deprovision)
- Stripe webhook -> auto-provision trigger
- Instances API (create, list, deprovision, reprovision, health check)
- Full signup flow smoke test (signup -> verify -> checkout -> webhook -> provision)
- Provisioning stall detection (duplicate webhook idempotency)
"""
from __future__ import annotations

import os
import sys
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

# Pre-inject a mock stripe module so the lazy `import stripe` inside webhooks.py
# picks up our mock instead of requiring the real package to be patchable at
# module level.
_mock_stripe_module = MagicMock()
_mock_stripe_module.error = MagicMock()
_mock_stripe_module.error.SignatureVerificationError = type("SignatureVerificationError", (Exception,), {})
sys.modules.setdefault("stripe", _mock_stripe_module)

from control_plane.db import Base, get_db  # noqa: E402
from control_plane.main import app  # noqa: E402
from control_plane.models import Instance, User  # noqa: E402
from control_plane.services.provisioner import (  # noqa: E402
    Provisioner,
    ProvisionResult,
    _env_for,
    _generate_password,
    _host_for,
    _labels_for,
    _openai_allowlist,
    _volume_for,
)
from control_plane.routers.auth import _hash_password, _issue_session_token  # noqa: E402
from control_plane.config import settings  # noqa: E402

ADMIN_HEADERS = {"X-Admin-Token": "test-admin"}


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


@pytest.fixture(autouse=True)
def _reset_stripe_mock():
    """Reset the stripe mock before each test so state doesn't leak."""
    _mock_stripe_module.reset_mock()
    _mock_stripe_module.Webhook = MagicMock()
    _mock_stripe_module.error.SignatureVerificationError = type(
        "SignatureVerificationError", (Exception,), {}
    )
    yield


def _make_user(db, email="owner@test.com", verified=True, password="testpass123") -> User:
    user = User(
        email=email,
        password_hash=_hash_password(password),
        email_verified=verified,
    )
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
        "data_path": f"/tmp/test-data/{subdomain}",
    }
    defaults.update(kwargs)
    inst = Instance(**defaults)
    db.add(inst)
    db.commit()
    db.refresh(inst)
    return inst


def _login_cookie(user: User) -> dict[str, str]:
    token = _issue_session_token(user)
    return {"cp_session": token}


def _mock_provisioner():
    """Create a mock Provisioner that simulates successful provisioning."""
    prov = MagicMock(spec=Provisioner)
    prov.provision_instance.return_value = ProvisionResult(
        container_name="longhouse-testuser",
        data_path="/tmp/test-data/testuser",
        password="generated-pass-123",
        password_hash="pbkdf2:sha256:600000$aabb$ccdd",
        image="ghcr.io/test/app:latest",
    )
    prov.deprovision_instance.return_value = None
    prov.wait_for_health.return_value = True
    return prov


def _setup_stripe_webhook(event_type, event_data):
    """Configure the stripe mock to return a specific webhook event."""
    _mock_stripe_module.Webhook.construct_event.return_value = {
        "id": "evt_test",
        "type": event_type,
        "data": {"object": event_data},
    }


def _post_webhook(client):
    """Fire a webhook request with the pre-configured stripe mock."""
    return client.post(
        "/webhooks/stripe",
        content=b'{"type":"test"}',
        headers={"stripe-signature": "t=1,v1=test"},
    )


# ===========================================================================
# Provisioner unit tests
# ===========================================================================


class TestPasswordGeneration:
    def test_generates_unique_passwords(self):
        p1, h1 = _generate_password()
        p2, h2 = _generate_password()
        assert p1 != p2
        assert h1 != h2
        assert p1  # non-empty
        assert h1.startswith("pbkdf2:sha256:600000$")

    def test_hash_format(self):
        _, hash_str = _generate_password()
        parts = hash_str.split("$")
        assert len(parts) == 3
        assert parts[0] == "pbkdf2:sha256:600000"
        assert len(parts[1]) == 32  # 16 bytes hex
        assert len(parts[2]) == 64  # 32 bytes hex


class TestHostHelper:
    def test_host_for_subdomain(self):
        host = _host_for("testuser")
        assert host == f"testuser.{settings.root_domain}"


class TestLabelsForSubdomain:
    def test_caddy_labels(self):
        with patch.object(settings, "proxy_provider", "caddy"):
            labels = _labels_for("testuser")
            assert "caddy" in labels
            assert f"testuser.{settings.root_domain}" in labels["caddy"]
            assert "caddy.reverse_proxy" in labels

    def test_traefik_labels(self):
        with patch.object(settings, "proxy_provider", "traefik"):
            labels = _labels_for("testuser")
            assert "traefik.enable" in labels
            assert labels["traefik.enable"] == "true"
            assert "traefik.http.routers.testuser.rule" in labels


class TestEnvGeneration:
    def test_required_env_vars(self):
        env = _env_for("testuser", "owner@test.com", password="secret123")
        assert env["INSTANCE_ID"] == "testuser"
        assert env["OWNER_EMAIL"] == "owner@test.com"
        assert env["ADMIN_EMAILS"] == "owner@test.com"
        assert env["SINGLE_TENANT"] == "1"
        assert env["DATABASE_URL"] == "sqlite:////data/longhouse.db"
        assert env["LONGHOUSE_PASSWORD"] == "secret123"
        assert env["CONTROL_PLANE_URL"] == f"https://control.{settings.root_domain}"

    def test_no_password_env_when_not_provided(self):
        env = _env_for("testuser", "owner@test.com")
        assert "LONGHOUSE_PASSWORD" not in env

    def test_ses_env_injected_when_configured(self):
        with (
            patch.object(settings, "instance_aws_ses_access_key_id", "AKIATEST"),
            patch.object(settings, "instance_aws_ses_secret_access_key", "secret"),
            patch.object(settings, "instance_aws_ses_region", "us-west-2"),
            patch.object(settings, "instance_from_email", "noreply@test.com"),
        ):
            env = _env_for("testuser", "owner@test.com")
            assert env["AWS_SES_ACCESS_KEY_ID"] == "AKIATEST"
            assert env["AWS_SES_SECRET_ACCESS_KEY"] == "secret"
            assert env["AWS_SES_REGION"] == "us-west-2"
            assert env["FROM_EMAIL"] == "noreply@test.com"
            assert env["NOTIFY_EMAIL"] == "owner@test.com"


class TestOpenAIAllowlist:
    def test_empty_allowlist(self):
        with patch.object(settings, "instance_openai_allowlist", None):
            assert _openai_allowlist() == set()

    def test_wildcard_allowlist(self):
        with patch.object(settings, "instance_openai_allowlist", "*"):
            assert "*" in _openai_allowlist()

    def test_specific_allowlist(self):
        with patch.object(settings, "instance_openai_allowlist", "user1, user2@test.com"):
            al = _openai_allowlist()
            assert "user1" in al
            assert "user2@test.com" in al

    def test_allowed_by_subdomain(self):
        with (
            patch.object(settings, "instance_openai_allowlist", "testuser"),
            patch.object(settings, "instance_openai_api_key", "sk-test"),
            patch.object(settings, "instance_openai_base_url", "https://llm.test"),
        ):
            env = _env_for("testuser", "other@test.com")
            assert env.get("OPENAI_API_KEY") == "sk-test"
            assert env.get("OPENAI_BASE_URL") == "https://llm.test"

    def test_not_allowed(self):
        with (
            patch.object(settings, "instance_openai_allowlist", "otheruser"),
            patch.object(settings, "instance_openai_api_key", "sk-test"),
        ):
            env = _env_for("testuser", "owner@test.com")
            assert "OPENAI_API_KEY" not in env


class TestVolumeCreation:
    def test_creates_directory(self, tmp_path):
        with patch.object(settings, "instance_data_root", str(tmp_path)):
            data_path, volumes = _volume_for("testuser")
            assert os.path.isdir(data_path)
            assert data_path == str(tmp_path / "testuser")
            assert data_path in volumes
            assert volumes[data_path]["bind"] == "/data"
            assert volumes[data_path]["mode"] == "rw"


# ===========================================================================
# Instances API tests
# ===========================================================================


class TestInstancesAPI:
    @patch("control_plane.routers.instances.Provisioner")
    def test_create_instance(self, MockProv, client, db_session):
        prov = _mock_provisioner()
        MockProv.return_value = prov

        resp = client.post(
            "/api/instances",
            json={"email": "new@test.com", "subdomain": "newuser"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["subdomain"] == "newuser"
        assert data["status"] == "provisioning"
        assert data["password"] == "generated-pass-123"
        prov.provision_instance.assert_called_once_with("newuser", owner_email="new@test.com")

    @patch("control_plane.routers.instances.Provisioner")
    def test_create_instance_idempotent(self, MockProv, client, db_session):
        user = _make_user(db_session, email="existing@test.com")
        _make_instance(db_session, user, subdomain="existing")

        resp = client.post(
            "/api/instances",
            json={"email": "existing@test.com", "subdomain": "existing2"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["subdomain"] == "existing"
        MockProv.assert_not_called()

    def test_create_instance_requires_admin(self, client):
        resp = client.post(
            "/api/instances",
            json={"email": "new@test.com", "subdomain": "newuser"},
        )
        assert resp.status_code == 403

    def test_list_instances(self, client, db_session):
        user = _make_user(db_session)
        _make_instance(db_session, user)

        resp = client.get("/api/instances", headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["instances"]) == 1
        assert data["instances"][0]["email"] == "owner@test.com"

    def test_get_instance(self, client, db_session):
        user = _make_user(db_session)
        inst = _make_instance(db_session, user)

        resp = client.get(f"/api/instances/{inst.id}", headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        assert resp.json()["subdomain"] == "inst1"

    def test_get_instance_not_found(self, client, db_session):
        resp = client.get("/api/instances/999", headers=ADMIN_HEADERS)
        assert resp.status_code == 404

    @patch("control_plane.routers.instances.Provisioner")
    def test_deprovision_instance(self, MockProv, client, db_session):
        prov = _mock_provisioner()
        MockProv.return_value = prov

        user = _make_user(db_session)
        inst = _make_instance(db_session, user)

        resp = client.post(f"/api/instances/{inst.id}/deprovision", headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        prov.deprovision_instance.assert_called_once_with("longhouse-inst1")

        db_session.refresh(inst)
        assert inst.status == "deprovisioned"

    @patch("control_plane.routers.instances.Provisioner")
    def test_reprovision_instance(self, MockProv, client, db_session):
        prov = _mock_provisioner()
        MockProv.return_value = prov

        user = _make_user(db_session)
        inst = _make_instance(db_session, user, status="deprovisioned")

        resp = client.post(f"/api/instances/{inst.id}/reprovision", headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "provisioning"
        prov.deprovision_instance.assert_called_once()
        prov.provision_instance.assert_called_once()

    @patch("control_plane.routers.instances.Provisioner")
    def test_reprovision_blocked_during_deploy(self, MockProv, client, db_session):
        user = _make_user(db_session)
        inst = _make_instance(
            db_session, user,
            deploy_id="d-active",
            deploy_state="deploying",
        )

        resp = client.post(f"/api/instances/{inst.id}/reprovision", headers=ADMIN_HEADERS)
        assert resp.status_code == 409

    @patch("control_plane.routers.instances.Provisioner")
    def test_regenerate_password(self, MockProv, client, db_session):
        prov = _mock_provisioner()
        MockProv.return_value = prov

        user = _make_user(db_session)
        inst = _make_instance(db_session, user)

        resp = client.post(f"/api/instances/{inst.id}/regenerate-password", headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        assert resp.json()["password"] is not None
        prov.deprovision_instance.assert_called_once()
        prov.provision_instance.assert_called_once()


class TestInstanceHealthCheck:
    def test_my_instance_health_active(self, client, db_session):
        user = _make_user(db_session)
        _make_instance(db_session, user, status="active")
        client.cookies.update(_login_cookie(user))

        resp = client.get("/api/instances/me/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "active"
        assert resp.json()["ready"] is True

    @patch("control_plane.routers.instances.httpx")
    def test_my_instance_health_provisioning_checks_real(self, mock_httpx, client, db_session):
        user = _make_user(db_session)
        _make_instance(db_session, user, status="provisioning")
        client.cookies.update(_login_cookie(user))

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_httpx.get.return_value = mock_resp

        resp = client.get("/api/instances/me/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "active"
        assert resp.json()["ready"] is True

    def test_my_instance_not_found(self, client, db_session):
        user = _make_user(db_session)
        client.cookies.update(_login_cookie(user))

        resp = client.get("/api/instances/me/health")
        assert resp.status_code == 404

    def test_my_instance_requires_auth(self, client):
        resp = client.get("/api/instances/me/health")
        assert resp.status_code in (302, 401, 403)


class TestSSOKeysEndpoint:
    def test_valid_instance_gets_keys(self, client, db_session):
        user = _make_user(db_session)
        _make_instance(db_session, user, subdomain="myinst")

        resp = client.get(
            "/api/instances/sso-keys",
            headers={
                "X-Instance-Id": "myinst",
                "X-Internal-Secret": "test-internal",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "keys" in data
        assert len(data["keys"]) > 0

    def test_wrong_secret_rejected(self, client, db_session):
        user = _make_user(db_session)
        _make_instance(db_session, user, subdomain="myinst")

        resp = client.get(
            "/api/instances/sso-keys",
            headers={
                "X-Instance-Id": "myinst",
                "X-Internal-Secret": "wrong-secret",
            },
        )
        assert resp.status_code == 403

    def test_unknown_instance_rejected(self, client, db_session):
        resp = client.get(
            "/api/instances/sso-keys",
            headers={
                "X-Instance-Id": "nonexistent",
                "X-Internal-Secret": "test-internal",
            },
        )
        assert resp.status_code == 403


# ===========================================================================
# Stripe webhook -> provisioning tests
# ===========================================================================


class TestStripeWebhookProvisioning:
    def test_webhook_not_configured(self, client):
        with (
            patch.object(settings, "stripe_secret_key", None),
            patch.object(settings, "stripe_webhook_secret", None),
        ):
            resp = client.post("/webhooks/stripe", content=b"{}")
            assert resp.status_code == 503

    @patch("control_plane.services.provisioner.Provisioner")
    def test_checkout_completed_provisions_instance(self, MockProv, client, db_session):
        user = _make_user(db_session, email="checkout@test.com")

        prov = _mock_provisioner()
        prov.provision_instance.return_value = ProvisionResult(
            container_name="longhouse-checkout",
            data_path="/tmp/test-data/checkout",
            password="pass123",
            password_hash="pbkdf2:sha256:600000$aa$bb",
        )
        MockProv.return_value = prov

        with (
            patch.object(settings, "stripe_secret_key", "sk_test"),
            patch.object(settings, "stripe_webhook_secret", "whsec_test"),
        ):
            _setup_stripe_webhook("checkout.session.completed", {
                "client_reference_id": str(user.id),
                "subscription": "sub_test",
                "customer": "cus_test",
            })
            resp = _post_webhook(client)

        assert resp.status_code == 200

        db_session.refresh(user)
        assert user.subscription_status == "active"
        assert user.stripe_customer_id == "cus_test"

        inst = db_session.query(Instance).filter(Instance.user_id == user.id).first()
        assert inst is not None
        assert inst.status == "provisioning"

    def test_checkout_completed_idempotent(self, client, db_session):
        user = _make_user(db_session, email="existing@test.com")
        user.subscription_status = "active"
        db_session.commit()
        _make_instance(db_session, user, subdomain="existing")

        with (
            patch.object(settings, "stripe_secret_key", "sk_test"),
            patch.object(settings, "stripe_webhook_secret", "whsec_test"),
        ):
            _setup_stripe_webhook("checkout.session.completed", {
                "client_reference_id": str(user.id),
                "subscription": "sub_test",
                "customer": "cus_test",
            })
            resp = _post_webhook(client)

        assert resp.status_code == 200
        instances = db_session.query(Instance).filter(Instance.user_id == user.id).all()
        assert len(instances) == 1

    @patch("control_plane.services.provisioner.Provisioner")
    def test_checkout_provision_failure_records_failed_instance(self, MockProv, client, db_session):
        user = _make_user(db_session, email="fail@test.com")

        prov = MagicMock()
        prov.provision_instance.side_effect = RuntimeError("Docker daemon unavailable")
        MockProv.return_value = prov

        with (
            patch.object(settings, "stripe_secret_key", "sk_test"),
            patch.object(settings, "stripe_webhook_secret", "whsec_test"),
        ):
            _setup_stripe_webhook("checkout.session.completed", {
                "client_reference_id": str(user.id),
                "subscription": "sub_fail",
                "customer": "cus_fail",
            })
            resp = _post_webhook(client)

        assert resp.status_code == 200  # Webhook always returns 200

        inst = db_session.query(Instance).filter(Instance.user_id == user.id).first()
        assert inst is not None
        assert inst.status == "failed"


class TestStripeSubscriptionEvents:
    def _fire_event(self, client, event_type, event_data):
        with (
            patch.object(settings, "stripe_secret_key", "sk_test"),
            patch.object(settings, "stripe_webhook_secret", "whsec_test"),
        ):
            _setup_stripe_webhook(event_type, event_data)
            return _post_webhook(client)

    def test_subscription_updated(self, client, db_session):
        user = _make_user(db_session, email="sub@test.com")
        user.stripe_customer_id = "cus_sub"
        user.subscription_status = "active"
        db_session.commit()

        resp = self._fire_event(client, "customer.subscription.updated", {
            "customer": "cus_sub", "status": "past_due",
        })
        assert resp.status_code == 200
        db_session.refresh(user)
        assert user.subscription_status == "past_due"

    def test_subscription_deleted(self, client, db_session):
        user = _make_user(db_session, email="cancel@test.com")
        user.stripe_customer_id = "cus_cancel"
        user.subscription_status = "active"
        db_session.commit()

        resp = self._fire_event(client, "customer.subscription.deleted", {"customer": "cus_cancel"})
        assert resp.status_code == 200
        db_session.refresh(user)
        assert user.subscription_status == "canceled"

    def test_invoice_paid_recovers_from_past_due(self, client, db_session):
        user = _make_user(db_session, email="recover@test.com")
        user.stripe_customer_id = "cus_recover"
        user.subscription_status = "past_due"
        db_session.commit()

        resp = self._fire_event(client, "invoice.paid", {"customer": "cus_recover"})
        assert resp.status_code == 200
        db_session.refresh(user)
        assert user.subscription_status == "active"

    def test_payment_failed_marks_past_due(self, client, db_session):
        user = _make_user(db_session, email="payfail@test.com")
        user.stripe_customer_id = "cus_payfail"
        user.subscription_status = "active"
        db_session.commit()

        resp = self._fire_event(client, "invoice.payment_failed", {"customer": "cus_payfail"})
        assert resp.status_code == 200
        db_session.refresh(user)
        assert user.subscription_status == "past_due"


# ===========================================================================
# Subdomain derivation tests
# ===========================================================================


class TestSubdomainDerivation:
    @patch("control_plane.services.provisioner.Provisioner")
    def test_subdomain_derived_from_email(self, MockProv, client, db_session):
        user = _make_user(db_session, email="john.doe@gmail.com")
        prov = _mock_provisioner()
        MockProv.return_value = prov

        with (
            patch.object(settings, "stripe_secret_key", "sk_test"),
            patch.object(settings, "stripe_webhook_secret", "whsec_test"),
        ):
            _setup_stripe_webhook("checkout.session.completed", {
                "client_reference_id": str(user.id),
                "subscription": "sub_test",
                "customer": "cus_test",
            })
            resp = _post_webhook(client)

        assert resp.status_code == 200
        inst = db_session.query(Instance).filter(Instance.user_id == user.id).first()
        assert inst is not None
        # john.doe -> john-doe (dots become dashes via regex)
        assert inst.subdomain == "john-doe"

    @patch("control_plane.services.provisioner.Provisioner")
    def test_subdomain_uniqueness_collision(self, MockProv, client, db_session):
        existing_user = _make_user(db_session, email="other@test.com")
        _make_instance(db_session, existing_user, subdomain="john")

        user = _make_user(db_session, email="john@test.com")
        prov = _mock_provisioner()
        MockProv.return_value = prov

        with (
            patch.object(settings, "stripe_secret_key", "sk_test"),
            patch.object(settings, "stripe_webhook_secret", "whsec_test"),
        ):
            _setup_stripe_webhook("checkout.session.completed", {
                "client_reference_id": str(user.id),
                "subscription": "sub_test",
                "customer": "cus_test2",
            })
            resp = _post_webhook(client)

        assert resp.status_code == 200
        inst = db_session.query(Instance).filter(Instance.user_id == user.id).first()
        assert inst is not None
        assert inst.subdomain == "john-1"


# ===========================================================================
# Billing checkout tests
# ===========================================================================


class TestBillingCheckout:
    def test_checkout_requires_verified_email(self, client, db_session):
        user = _make_user(db_session, email="unverified@test.com", verified=False)
        client.cookies.update(_login_cookie(user))

        resp = client.post("/billing/checkout")
        assert resp.status_code == 403
        assert "not verified" in resp.json()["detail"].lower()

    def test_checkout_requires_auth(self, client):
        resp = client.post("/billing/checkout")
        assert resp.status_code in (302, 401, 403)

    def test_checkout_without_stripe_config(self, client, db_session):
        user = _make_user(db_session, email="verified@test.com", verified=True)
        client.cookies.update(_login_cookie(user))

        resp = client.post("/billing/checkout")
        assert resp.status_code == 503


# ===========================================================================
# Full signup -> provision smoke test
# ===========================================================================


class TestFullSignupFlow:
    @patch("control_plane.routers.auth._send_verification")
    def test_signup_creates_unverified_user(self, mock_send, client, db_session):
        resp = client.post(
            "/auth/signup",
            data={"email": "flow@test.com", "password": "Password123", "password_confirm": "Password123"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        user = db_session.query(User).filter(User.email == "flow@test.com").first()
        assert user is not None
        assert user.email_verified is False

    @patch("control_plane.services.provisioner.Provisioner")
    @patch("control_plane.routers.auth._send_verification")
    def test_full_flow_signup_verify_provision(self, mock_send, MockProv, client, db_session):
        """Simulates: signup -> email verify -> Stripe checkout -> webhook -> provision."""
        # Step 1: Signup
        resp = client.post(
            "/auth/signup",
            data={"email": "fullflow@test.com", "password": "Password123", "password_confirm": "Password123"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        user = db_session.query(User).filter(User.email == "fullflow@test.com").first()
        assert user is not None

        # Step 2: Verify email
        from control_plane.routers.auth import _issue_verify_token

        verify_token = _issue_verify_token(user)
        resp = client.get(f"/auth/verify?token={verify_token}", follow_redirects=False)
        assert resp.status_code == 302

        db_session.refresh(user)
        assert user.email_verified is True

        # Step 3: Simulate Stripe webhook (checkout complete)
        prov = _mock_provisioner()
        prov.provision_instance.return_value = ProvisionResult(
            container_name="longhouse-fullflow",
            data_path="/tmp/test-data/fullflow",
            password="gen-pass",
            password_hash="pbkdf2:sha256:600000$aa$bb",
        )
        MockProv.return_value = prov

        with (
            patch.object(settings, "stripe_secret_key", "sk_test"),
            patch.object(settings, "stripe_webhook_secret", "whsec_test"),
        ):
            _setup_stripe_webhook("checkout.session.completed", {
                "client_reference_id": str(user.id),
                "subscription": "sub_flow",
                "customer": "cus_flow",
            })
            resp = _post_webhook(client)

        assert resp.status_code == 200

        db_session.refresh(user)
        assert user.subscription_status == "active"

        inst = db_session.query(Instance).filter(Instance.user_id == user.id).first()
        assert inst is not None
        assert inst.status == "provisioning"
        assert inst.subdomain == "fullflow"


# ===========================================================================
# Provisioner class tests (mock Docker)
# ===========================================================================


class TestProvisionerClass:
    @patch("control_plane.services.provisioner.docker.DockerClient")
    def test_provision_new_instance(self, MockDockerClient, tmp_path):
        import docker.errors

        mock_client = MagicMock()
        MockDockerClient.return_value = mock_client
        mock_client.containers.get.side_effect = docker.errors.NotFound("not found")

        mock_image = MagicMock()
        mock_image.attrs = {"RepoDigests": ["ghcr.io/test/app@sha256:abc"]}
        mock_client.images.pull.return_value = mock_image
        mock_client.images.get.return_value = mock_image

        fake_container = MagicMock()
        fake_container.name = "longhouse-newuser"
        mock_client.containers.run.return_value = fake_container

        with patch.object(settings, "instance_data_root", str(tmp_path)):
            provisioner = Provisioner()
            result = provisioner.provision_instance("newuser", owner_email="new@test.com")

        assert result.container_name == "longhouse-newuser"
        assert result.password is not None
        assert result.password_hash is not None
        mock_client.containers.run.assert_called_once()

    @patch("control_plane.services.provisioner.docker.DockerClient")
    def test_provision_existing_container_returns_early(self, MockDockerClient):
        mock_client = MagicMock()
        MockDockerClient.return_value = mock_client

        fake_container = MagicMock()
        fake_container.name = "longhouse-existing"
        mock_client.containers.get.return_value = fake_container

        provisioner = Provisioner()
        result = provisioner.provision_instance("existing", owner_email="e@test.com")

        assert result.container_name == "longhouse-existing"
        assert result.password is None
        mock_client.containers.run.assert_not_called()

    @patch("control_plane.services.provisioner.docker.DockerClient")
    def test_deprovision_stops_and_removes(self, MockDockerClient):
        mock_client = MagicMock()
        MockDockerClient.return_value = mock_client

        fake_container = MagicMock()
        mock_client.containers.get.return_value = fake_container

        provisioner = Provisioner()
        provisioner.deprovision_instance("longhouse-test")

        fake_container.stop.assert_called_once_with(timeout=20)
        fake_container.remove.assert_called_once()

    @patch("control_plane.services.provisioner.docker.DockerClient")
    def test_deprovision_not_found_is_noop(self, MockDockerClient):
        import docker.errors

        mock_client = MagicMock()
        MockDockerClient.return_value = mock_client
        mock_client.containers.get.side_effect = docker.errors.NotFound("not found")

        provisioner = Provisioner()
        provisioner.deprovision_instance("longhouse-missing")  # Should not raise

    @patch("control_plane.services.provisioner.httpx")
    @patch("control_plane.services.provisioner.docker.DockerClient")
    def test_wait_for_health_success(self, MockDockerClient, mock_httpx):
        mock_client = MagicMock()
        MockDockerClient.return_value = mock_client

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_httpx.get.return_value = mock_resp

        with patch.object(settings, "publish_ports", True):
            provisioner = Provisioner()
            result = provisioner.wait_for_health("testuser", timeout=5)
            assert result is True

    @patch("control_plane.services.provisioner.httpx")
    @patch("control_plane.services.provisioner.docker.DockerClient")
    def test_wait_for_health_timeout(self, MockDockerClient, mock_httpx):
        mock_client = MagicMock()
        MockDockerClient.return_value = mock_client

        mock_httpx.get.side_effect = ConnectionError("refused")

        with patch.object(settings, "publish_ports", True):
            provisioner = Provisioner()
            with pytest.raises(RuntimeError, match="Health check failed"):
                provisioner.wait_for_health("testuser", timeout=1)


# ===========================================================================
# Provisioning stall prevention (idempotency under webhook retry)
# ===========================================================================


class TestProvisioningStallPrevention:
    """The webhook handler does sync Docker provisioning inside the HTTP request.

    Stripe expects webhook responses within 20s. If Docker is slow (image pull,
    container start), the webhook times out and Stripe retries, potentially
    causing duplicate provisioning attempts.

    These tests verify the idempotency guards that protect against this.
    """

    @patch("control_plane.services.provisioner.Provisioner")
    def test_duplicate_webhook_does_not_double_provision(self, MockProv, client, db_session):
        """Simulates Stripe retrying the webhook after a timeout."""
        user = _make_user(db_session, email="retry@test.com")
        prov = _mock_provisioner()
        MockProv.return_value = prov

        event_data = {
            "client_reference_id": str(user.id),
            "subscription": "sub_retry",
            "customer": "cus_retry",
        }

        with (
            patch.object(settings, "stripe_secret_key", "sk_test"),
            patch.object(settings, "stripe_webhook_secret", "whsec_test"),
        ):
            # First call
            _setup_stripe_webhook("checkout.session.completed", event_data)
            resp1 = _post_webhook(client)
            assert resp1.status_code == 200

            # Second call (Stripe retry)
            _setup_stripe_webhook("checkout.session.completed", event_data)
            resp2 = _post_webhook(client)
            assert resp2.status_code == 200

        instances = db_session.query(Instance).filter(Instance.user_id == user.id).all()
        assert len(instances) == 1
