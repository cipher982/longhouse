"""Schema discovery must not authorize a candidate or invent catalog health."""

from types import SimpleNamespace

import pytest


@pytest.fixture
def evidence_runtime(monkeypatch, tmp_path):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'archive.db'}")
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("AUTH_DISABLED", "1")
    monkeypatch.setenv("FERNET_SECRET", Fernet.generate_key().decode())
    monkeypatch.setenv("JWT_SECRET", "runtime-evidence-test-only")
    monkeypatch.setenv("LONGHOUSE_DEPLOYMENT_PENDING", "1")
    monkeypatch.setenv("LONGHOUSE_DEPLOYMENT_GENERATION", "7")

    from fastapi.testclient import TestClient

    from zerg import build_info
    from zerg.catalogd.schema import CATALOG_SCHEMA_GENERATION
    from zerg.catalogd.schema import CATALOG_SCHEMA_VERSION
    from zerg.main import api_app
    from zerg.routers import internal_deployments
    from zerg.services.runtime_admission import RuntimeAdmission

    runtime = RuntimeAdmission()
    runtime.observe_candidate(attempt_id="owned-attempt", generation="7")
    monkeypatch.setattr(internal_deployments, "runtime_admission", lambda: runtime)
    monkeypatch.setattr(internal_deployments, "get_settings", lambda: SimpleNamespace(internal_api_secret="evidence-test-only"))
    monkeypatch.setattr(
        build_info,
        "load",
        lambda: build_info.BuildIdentity("1.0.0", "a" * 40, "a" * 8, False, "2026-09-16T00:00:00Z", "dev"),
    )
    ping = {
        "ready": True,
        "schema_version": CATALOG_SCHEMA_VERSION,
        "schema_generation": CATALOG_SCHEMA_GENERATION,
        "writer_admission": {"depth": 0, "max_depth": 32, "active_label": None},
    }
    monkeypatch.setattr(internal_deployments, "catalogd_paths", lambda: (tmp_path / "catalog.db", tmp_path / "catalog.sock"))
    monkeypatch.setattr(internal_deployments, "call_catalogd_sync", lambda *args, **kwargs: ping)
    with TestClient(api_app) as client:
        yield client, runtime, ping


def test_schema_observation_does_not_authorize_candidate(evidence_runtime):
    client, runtime, ping = evidence_runtime
    assert client.get("/internal/deployments/evidence").status_code == 401
    response = client.get("/internal/deployments/evidence", headers={"X-Internal-Token": "evidence-test-only"})
    assert response.status_code == 200
    assert response.json()["schema_version"] == ping["schema_version"]
    assert response.json()["admission"]["state"] == "closed"
    with pytest.raises(ValueError):
        runtime.mark_candidate_consistent(attempt_id="owned-attempt")

    readiness = client.get(
        "/internal/deployments/owned-attempt/readiness",
        params={"expected_generation": "7", "expected_schema_version": str(ping["schema_version"])},
        headers={"X-Internal-Token": "evidence-test-only"},
    )
    assert readiness.status_code == 200
    runtime.mark_candidate_consistent(attempt_id="owned-attempt")


def test_unknown_catalog_schema_is_not_ready(evidence_runtime):
    client, runtime, ping = evidence_runtime
    ping.pop("schema_version")
    response = client.get("/internal/deployments/evidence", headers={"X-Internal-Token": "evidence-test-only"})
    assert response.status_code == 503
    assert response.json()["outcome"] == "not_ready"
    assert response.json()["schema_version"] is None
    assert runtime.state == "closed"
