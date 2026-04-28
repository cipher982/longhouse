from __future__ import annotations

import os

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("CONTROL_PLANE_ADMIN_TOKEN", "test-admin")
os.environ.setdefault("CONTROL_PLANE_JWT_SECRET", "test-jwt-secret-for-tests")
os.environ.setdefault("CONTROL_PLANE_DATABASE_URL", "sqlite:///")
os.environ.setdefault("CONTROL_PLANE_INSTANCE_JWT_SECRET", "test-instance-jwt")
os.environ.setdefault("CONTROL_PLANE_INSTANCE_INTERNAL_API_SECRET", "test-internal")
os.environ.setdefault("CONTROL_PLANE_INSTANCE_FERNET_SECRET", "test-fernet")
os.environ.setdefault("CONTROL_PLANE_INSTANCE_TRIGGER_SIGNING_SECRET", "test-trigger")

from control_plane.db import Base, get_db  # noqa: E402
from control_plane.main import app  # noqa: E402
from control_plane.models import AcquisitionEvent  # noqa: E402


def test_acquisition_event_redacts_ip_and_summarizes(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = TestSession()

    def _override_db():
        yield db

    app.dependency_overrides[get_db] = _override_db
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/acquisition/events",
                json={
                    "event_name": "install_success",
                    "install_id": "install-1",
                    "source": "installer",
                    "version": "0.1.15",
                    "os_name": "darwin",
                    "arch": "arm64",
                    "command": "record_install",
                    "install_method": "uv",
                    "install_source": "pypi",
                    "props": {"package_ref_kind": "url"},
                },
                headers={"CF-Connecting-IP": "203.0.113.1", "CF-IPCountry": "US"},
            )
            assert response.status_code == 202

            summary = client.get("/api/acquisition/summary", headers={"X-Admin-Token": "test-admin"})
            assert summary.status_code == 200
        event = db.query(AcquisitionEvent).one()
        assert event.ip_hash
        assert event.ip_hash != "203.0.113.1"
        assert event.country == "US"
        assert summary.json()["by_event"] == {"install_success": 1}
        assert summary.json()["unique_install_ids"] == 1
    finally:
        app.dependency_overrides.clear()
        db.close()
        engine.dispose()


def test_tracked_installer_redirect_records_fetch(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = TestSession()

    def _override_db():
        yield db

    app.dependency_overrides[get_db] = _override_db
    try:
        with TestClient(app) as client:
            response = client.get("/install.sh", follow_redirects=False)
        assert response.status_code == 302
        assert "raw.githubusercontent.com" in response.headers["location"]
        event = db.query(AcquisitionEvent).one()
        assert event.event_name == "installer_fetch"
    finally:
        app.dependency_overrides.clear()
        db.close()
        engine.dispose()
