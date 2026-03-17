from __future__ import annotations

import os

from fastapi.testclient import TestClient

os.environ.setdefault("CONTROL_PLANE_ADMIN_TOKEN", "test-admin")
os.environ.setdefault("CONTROL_PLANE_JWT_SECRET", "test-jwt-secret-for-tests")
os.environ.setdefault("CONTROL_PLANE_DATABASE_URL", "sqlite:///")
os.environ.setdefault("CONTROL_PLANE_INSTANCE_JWT_SECRET", "test-instance-jwt")
os.environ.setdefault("CONTROL_PLANE_INSTANCE_INTERNAL_API_SECRET", "test-internal")
os.environ.setdefault("CONTROL_PLANE_INSTANCE_FERNET_SECRET", "test-fernet")
os.environ.setdefault("CONTROL_PLANE_INSTANCE_TRIGGER_SIGNING_SECRET", "test-trigger")

from control_plane.main import app


def test_health_endpoint_returns_ok():
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
