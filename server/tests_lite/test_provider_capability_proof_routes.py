from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi import status
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.main import api_app
from zerg.main import app
from zerg.routers import provider_capability_proofs as routes
from zerg.services.provider_capability_proof import AssertionOutcome
from zerg.services.provider_capability_proof import EvidenceClass
from zerg.services.provider_capability_proof import ProviderCapabilityProofRecord
from zerg.services.provider_capability_proof_store import ProviderCapabilityProofStore


def _record(**changes) -> ProviderCapabilityProofRecord:
    record = ProviderCapabilityProofRecord(
        provider="codex",
        provider_version="0.145.0",
        provider_executable_identity="sha256:provider",
        provider_contract_digest="sha256:contract",
        adapter_digest="sha256:adapter",
        scenario_id="codex_helm_interrupt",
        scenario_revision=1,
        oracle_digest="sha256:oracle",
        assertion_id="interrupt_acknowledged",
        outcome=AssertionOutcome.PASS,
        evidence_class=EvidenceClass.LIVE_NO_TOKEN,
        generated_at="2026-07-22T18:00:00Z",
        producer_class="release_factory",
        producer_version="1",
        invocation_id="factory-run-123",
        mode="helm",
        platform="darwin",
        architecture="arm64",
        run_reference="factory://runs/factory-run-123",
        raw_reference_digests=("sha256:raw",),
    )
    return replace(record, **changes)


def _bundle(*records: ProviderCapabilityProofRecord) -> dict:
    return {
        "schema_version": 1,
        "artifact_kind": "provider_capability_proof_bundle",
        "records": [record.serialize() for record in records],
        # Publisher claims are deliberately ignored. Trust is derived from the
        # authenticated request and exact records accepted by the Runtime Host.
        "trusted_artifact_ids": ["publisher-controlled-value"],
    }


def _client(monkeypatch, tmp_path: Path, *, factory_token: str | None = "factory-secret") -> TestClient:
    store = ProviderCapabilityProofStore(tmp_path / "proofs")
    monkeypatch.setattr(routes, "_proof_store", lambda: store)
    monkeypatch.setattr(
        routes,
        "get_settings",
        lambda: SimpleNamespace(provider_capability_factory_token=factory_token),
    )
    api_app.dependency_overrides[verify_agents_token] = lambda: SimpleNamespace(device_id="machine-1")
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    return TestClient(app, backend="asyncio")


def _factory_headers() -> dict[str, str]:
    return {"X-Provider-Capability-Factory-Token": "factory-secret"}


def test_factory_publish_is_authenticated_idempotent_and_machine_read_is_server_derived(monkeypatch, tmp_path: Path) -> None:
    client = _client(monkeypatch, tmp_path)
    record = _record()
    try:
        first = client.post("/api/internal/provider-capability-proofs", headers=_factory_headers(), json=_bundle(record))
        second = client.post("/api/internal/provider-capability-proofs", headers=_factory_headers(), json=_bundle(record))
        fetched = client.get("/api/agents/provider-capability-proofs")
    finally:
        api_app.dependency_overrides.clear()

    assert first.status_code == 201
    assert second.status_code == 201
    assert (
        first.json()
        == second.json()
        == {
            "schema_version": 1,
            "accepted": 1,
            "trusted_artifact_ids": [record.artifact_id],
        }
    )
    assert fetched.status_code == 200
    assert fetched.json()["artifact_kind"] == "trusted_provider_capability_proof_bundle"
    assert fetched.json()["trusted_artifact_ids"] == [record.artifact_id]
    assert fetched.json()["records"] == [record.serialize()]


def test_factory_publish_is_absent_when_token_is_unconfigured(monkeypatch, tmp_path: Path) -> None:
    client = _client(monkeypatch, tmp_path, factory_token=None)
    try:
        response = client.post("/api/internal/provider-capability-proofs", json=_bundle(_record()))
    finally:
        api_app.dependency_overrides.clear()

    assert response.status_code == 404


def test_device_or_wrong_factory_token_cannot_publish(monkeypatch, tmp_path: Path) -> None:
    client = _client(monkeypatch, tmp_path)
    try:
        device_only = client.post(
            "/api/internal/provider-capability-proofs",
            headers={"X-Agents-Token": "device-token"},
            json=_bundle(_record()),
        )
        wrong_factory = client.post(
            "/api/internal/provider-capability-proofs",
            headers={"X-Provider-Capability-Factory-Token": "wrong"},
            json=_bundle(_record()),
        )
    finally:
        api_app.dependency_overrides.clear()

    assert device_only.status_code == 403
    assert wrong_factory.status_code == 403


def test_machine_read_requires_agents_auth(monkeypatch, tmp_path: Path) -> None:
    client = _client(monkeypatch, tmp_path)

    def reject_machine():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing machine token")

    api_app.dependency_overrides[verify_agents_token] = reject_machine
    try:
        response = client.get("/api/agents/provider-capability-proofs")
    finally:
        api_app.dependency_overrides.clear()

    assert response.status_code == 401


def test_factory_rejects_tampering_before_any_record_is_written(monkeypatch, tmp_path: Path) -> None:
    client = _client(monkeypatch, tmp_path)
    valid = _record()
    tampered = _record(assertion_id="tampered").serialize()
    tampered["artifact_id"] = "0" * 64
    payload = _bundle(valid)
    payload["records"].append(tampered)
    try:
        response = client.post("/api/internal/provider-capability-proofs", headers=_factory_headers(), json=payload)
        fetched = client.get("/api/agents/provider-capability-proofs")
    finally:
        api_app.dependency_overrides.clear()

    assert response.status_code == 422
    assert "artifact_id does not match" in response.json()["detail"]
    assert fetched.json()["records"] == []


def test_factory_rejects_untrusted_producer_and_mixed_invocations(monkeypatch, tmp_path: Path) -> None:
    client = _client(monkeypatch, tmp_path)
    try:
        wrong_producer = client.post(
            "/api/internal/provider-capability-proofs",
            headers=_factory_headers(),
            json=_bundle(_record(producer_class="local_diagnostic")),
        )
        mixed_invocation = client.post(
            "/api/internal/provider-capability-proofs",
            headers=_factory_headers(),
            json=_bundle(_record(), _record(assertion_id="second", invocation_id="factory-run-456")),
        )
    finally:
        api_app.dependency_overrides.clear()

    assert wrong_producer.status_code == 422
    assert "producer_class" in wrong_producer.json()["detail"]
    assert mixed_invocation.status_code == 422
    assert "share one invocation" in mixed_invocation.json()["detail"]


def test_factory_rejects_unknown_provider_and_bundle_schema(monkeypatch, tmp_path: Path) -> None:
    client = _client(monkeypatch, tmp_path)
    invalid_schema = _bundle(_record())
    invalid_schema["schema_version"] = 2
    try:
        unknown_provider = client.post(
            "/api/internal/provider-capability-proofs",
            headers=_factory_headers(),
            json=_bundle(_record(provider="unknown-provider")),
        )
        wrong_schema = client.post(
            "/api/internal/provider-capability-proofs",
            headers=_factory_headers(),
            json=invalid_schema,
        )
    finally:
        api_app.dependency_overrides.clear()

    assert unknown_provider.status_code == 422
    assert "unsupported managed provider" in unknown_provider.json()["detail"]
    assert wrong_schema.status_code == 422
    assert "schema_version" in wrong_schema.json()["detail"]


def test_factory_rejects_oversized_body(monkeypatch, tmp_path: Path) -> None:
    client = _client(monkeypatch, tmp_path)
    try:
        response = client.post(
            "/api/internal/provider-capability-proofs",
            headers={**_factory_headers(), "content-type": "application/json"},
            content=b"{" + b" " * routes._MAX_BODY_BYTES + b"}",
        )
    finally:
        api_app.dependency_overrides.clear()

    assert response.status_code == 413
