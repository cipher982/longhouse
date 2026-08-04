from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import replace
from datetime import UTC
from datetime import datetime
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
from zerg.services.provider_capability_proof import LEGACY_PROOF_SCHEMA_VERSION
from zerg.services.provider_capability_proof import AssertionOutcome
from zerg.services.provider_capability_proof import EvidenceClass
from zerg.services.provider_capability_proof import ProviderCapabilityProofRecord
from zerg.services.provider_capability_proof_store import ProofPublication
from zerg.services.provider_capability_proof_store import ProviderCapabilityProofStore


def _blob_digest(label: str) -> str:
    return f"sha256:{hashlib.sha256(label.encode()).hexdigest()}"


_BLOB_CONTENT = {
    _blob_digest(label): label.encode()
    for label in ("raw", "epoch", "verifier", "census", "compile", "plan", "sandbox", "cleanup")
}


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
        run_reference="github-actions://cipher982/longhouse/actions/runs/12345/attempts/2",
        raw_reference_digests=(_blob_digest("raw"),),
        assertion_variant="clean_exit",
        factory_source_sha="f" * 40,
        accepted_epoch_id="helm-resume-v1-test",
        accepted_epoch_digest=_blob_digest("epoch"),
        verifier_bundle_digest=_blob_digest("verifier"),
        compile_report_digest=_blob_digest("compile"),
        plan_digest=_blob_digest("plan"),
        sandbox_receipt_digest=_blob_digest("sandbox"),
        cleanup_receipt_digest=_blob_digest("cleanup"),
        worker_id="factory-worker-1",
        worker_census_digest=_blob_digest("census"),
        acquisition_provenance={"method": "staged_release", "source": "official"},
        auth_mechanism="factory_token_v1",
        observed_activity=("native_resume_command", "post_resume_provider_activity"),
        credential_binding_facts={"codex_provider_token": "admitted"},
    )
    return replace(record, **changes)


def _bundle(*records: ProviderCapabilityProofRecord) -> dict:
    payload = {
        "schema_version": 3,
        "artifact_kind": "provider_capability_proof_bundle",
        "records": [record.serialize() for record in records],
        "publication": {
            "worker_id": "factory-worker-1",
            "worker_census_digest": _blob_digest("census"),
            "auth_mechanism": "factory_token_v1",
            "published_at": "2026-07-22T18:01:00Z",
        },
        "blobs": [
            {"digest": digest, "content_base64": base64.b64encode(_BLOB_CONTENT[digest]).decode()}
            for digest in sorted(set().union(*(set(record.referenced_content_digests()) for record in records)))
        ],
        # Publisher claims are deliberately ignored. Trust is derived from the
        # authenticated request and exact records accepted by the Runtime Host.
        "trusted_artifact_ids": ["publisher-controlled-value"],
    }
    payload["bundle_digest"] = routes._bundle_digest(payload)
    return payload


def _client(monkeypatch, tmp_path: Path, *, factory_token: str | None = "factory-secret") -> TestClient:
    store = ProviderCapabilityProofStore(tmp_path / "proofs", require_authenticated_publication=True)
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


def _write_trusted(store: ProviderCapabilityProofStore, record: ProviderCapabilityProofRecord) -> None:
    for digest in record.referenced_content_digests():
        store.write_blob(_BLOB_CONTENT[digest], expected_digest=digest)
    store.write(
        record,
        publication=ProofPublication(
            worker_id="factory-worker-1",
            worker_census_digest=_blob_digest("census"),
            auth_mechanism="factory_token_v1",
            published_at="2026-07-22T18:01:00Z",
            bundle_digest=_blob_digest("published-bundle"),
        ),
    )


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
            "schema_version": 2,
            "accepted": 1,
            "trusted_artifact_ids": [record.artifact_id],
        }
    )
    assert fetched.status_code == 200
    assert fetched.json()["artifact_kind"] == "trusted_provider_capability_proof_bundle"
    assert fetched.json()["trusted_artifact_ids"] == [record.artifact_id]
    assert {key: fetched.json()["records"][0][key] for key in record.serialize()} == record.serialize()
    assert fetched.json()["records"][0]["store_integrity"] == {"admissible": True, "reason_codes": []}
    assert fetched.json()["records"][0]["run_reference"] == record.run_reference
    assert fetched.json()["total_records"] == 1
    assert fetched.json()["truncated"] is False


def test_factory_rejects_new_v2_publication_but_keeps_old_history_non_admissible(monkeypatch, tmp_path: Path) -> None:
    client = _client(monkeypatch, tmp_path)
    legacy = _record(
        assertion_id="coordination_instructions_model_visible",
        assertion_variant=None,
        scenario_id="codex_coordination_awareness_create",
        evidence_class=EvidenceClass.LIVE_TOKEN,
        generated_at=datetime.now(UTC).isoformat(),
        schema_version=LEGACY_PROOF_SCHEMA_VERSION,
    )
    payload = {
        "schema_version": LEGACY_PROOF_SCHEMA_VERSION,
        "artifact_kind": "provider_capability_proof_bundle",
        "records": [legacy.serialize()],
    }
    routes._legacy_proof_store().write(legacy)
    try:
        published = client.post(
            "/api/internal/provider-capability-proofs",
            headers=_factory_headers(),
            json=payload,
        )
        fetched = client.get("/api/agents/provider-capability-proofs")
        projection = routes.build_capability_projection_payload()
    finally:
        api_app.dependency_overrides.clear()

    assert published.status_code == 422
    assert published.json()["detail"] == "historical schema-v2 proofs are read-only and cannot be published"
    visible = next(record for record in fetched.json()["records"] if record["artifact_id"] == legacy.artifact_id)
    assert visible["schema_version"] == 2
    assert visible["store_integrity"] == {
        "admissible": False,
        "reason_codes": ["proof_schema_legacy", "historical_schema_v2"],
    }
    assert legacy.artifact_id not in fetched.json()["trusted_artifact_ids"]
    row = next(
        item
        for item in projection["capabilities"]
        if item["provider"] == "codex" and item["assertion_id"] == legacy.assertion_id
    )
    assert row["proof_status"] == "unacceptable_evidence"
    assert row["proof_artifact_id"] is None
    assert row["latest_proof_artifact_id"] == legacy.artifact_id
    assert "proof_schema_legacy" in row["admissibility_reasons"]


def test_factory_publication_timestamp_requires_timezone(monkeypatch, tmp_path: Path) -> None:
    client = _client(monkeypatch, tmp_path)
    payload = _bundle(_record())
    payload["publication"]["published_at"] = "2026-07-22T18:01:00"
    payload["bundle_digest"] = routes._bundle_digest(payload)
    try:
        response = client.post("/api/internal/provider-capability-proofs", headers=_factory_headers(), json=payload)
    finally:
        api_app.dependency_overrides.clear()

    assert response.status_code == 422
    assert response.json()["detail"] == "proof bundle publication timestamp must include a timezone"


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
    payload["bundle_digest"] = routes._bundle_digest(payload)
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
    invalid_schema["schema_version"] = 1
    invalid_schema["bundle_digest"] = routes._bundle_digest(invalid_schema)
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


def test_machine_read_is_bounded_and_provider_fair(monkeypatch, tmp_path: Path) -> None:
    store = ProviderCapabilityProofStore(tmp_path / "proofs")
    monkeypatch.setattr(routes, "_proof_store", lambda: store)
    monkeypatch.setattr(routes, "managed_provider_names", lambda: frozenset({"codex", "claude"}))
    monkeypatch.setattr(routes, "_MAX_RECORDS", 4)
    for provider in ("codex", "claude"):
        for number in range(3):
            store.write(
                _record(
                    provider=provider,
                    invocation_id=f"{provider}-{number}",
                    generated_at=f"2026-07-22T18:00:0{number}Z",
                )
            )
    client = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(routes, "_proof_store", lambda: store)
    try:
        response = client.get("/api/agents/provider-capability-proofs")
    finally:
        api_app.dependency_overrides.clear()

    payload = response.json()
    assert response.status_code == 200
    assert len(payload["records"]) == 4
    assert [record["provider"] for record in payload["records"]] == ["claude", "codex", "claude", "codex"]
    assert payload["total_records"] == 6
    assert payload["truncated"] is True


def test_capability_projection_joins_a_real_proof_and_labels_the_unproven_rest(monkeypatch, tmp_path: Path) -> None:
    # coordination_instructions_model_visible is a real assertion_id from
    # schemas/managed_providers.yml's codex coordination.awareness.create
    # capability -- an end-to-end check that the join actually resolves a
    # real schema entry, not a fixture invented for this test alone. Uses a
    # live-relative timestamp, not a fixed date: the route calls
    # project_capabilities() with now=None (real time), so a hardcoded past
    # date eventually ages past the assertion's real max_age_seconds and the
    # test starts asserting "stale" instead of "pass".
    generated_at = datetime.now(UTC).isoformat()
    store = ProviderCapabilityProofStore(tmp_path / "proofs", require_authenticated_publication=True)
    monkeypatch.setattr(routes, "_proof_store", lambda: store)
    proof = _record(
            assertion_id="coordination_instructions_model_visible",
            assertion_variant=None,
            scenario_id="codex_coordination_awareness_create",
            outcome=AssertionOutcome.PASS,
            generated_at=generated_at,
            # The schema's real acceptable_evidence for this assertion is
            # live_token only (schemas/managed_providers.yml) -- the fixture
            # must use a genuinely valid evidence class now that
            # project_capabilities() checks it (review 2026-07-29), or this
            # "proven" row silently becomes unacceptable_evidence instead.
            evidence_class=EvidenceClass.LIVE_TOKEN,
        )
    _write_trusted(store, proof)
    client = _client(monkeypatch, tmp_path)
    try:
        response = client.get("/api/agents/provider-capabilities")
    finally:
        api_app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["artifact_kind"] == "provider_capability_projection"
    # assertion_id is not globally unique across providers -- e.g. both codex
    # and cursor declare "coordination_instructions_model_visible" for their
    # own coordination.awareness.create capability -- so the join and this
    # assertion must both key on (provider, assertion_id), not assertion_id
    # alone.
    by_key = {(row["provider"], row["assertion_id"]): row for row in payload["capabilities"]}
    proven = by_key[("codex", "coordination_instructions_model_visible")]
    assert proven["capability"] == "coordination.awareness.create"
    assert proven["proof_status"] == "pass"
    assert proven["generated_at"] == generated_at
    other_codex_proven = by_key.get(("cursor", "coordination_instructions_model_visible"))
    if other_codex_proven is not None:
        assert other_codex_proven["proof_status"] == "never_proven"
    # Every other declared assertion has no proof in this store at all --
    # the row must still exist, labeled, not silently dropped.
    unproven = [row for key, row in by_key.items() if key != ("codex", "coordination_instructions_model_visible")]
    assert unproven
    assert all(row["proof_status"] == "never_proven" for row in unproven)
    assert all(row["generated_at"] is None for row in unproven)


def test_admin_provider_capabilities_mirrors_the_agents_surface(monkeypatch, tmp_path: Path) -> None:
    # docs/specs/provider-factory-coherence.md, Phase 5 UI: browsers
    # authenticate with the session cookie, never a device token, so the
    # web app cannot call GET /agents/provider-capabilities directly.
    # GET /admin/provider-capabilities is the cookie-authenticated mirror
    # that calls the exact same build_capability_projection_payload() --
    # this proves both surfaces return identical data from one proof store,
    # not two projection code paths that can drift apart.
    from zerg.dependencies.auth import get_current_user
    from zerg.dependencies.auth import require_admin

    generated_at = datetime.now(UTC).isoformat()
    store = ProviderCapabilityProofStore(tmp_path / "proofs", require_authenticated_publication=True)
    monkeypatch.setattr(routes, "_proof_store", lambda: store)
    proof = _record(
            assertion_id="coordination_instructions_model_visible",
            assertion_variant=None,
            scenario_id="codex_coordination_awareness_create",
            outcome=AssertionOutcome.PASS,
            generated_at=generated_at,
        )
    _write_trusted(store, proof)
    api_app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1, is_admin=True)
    api_app.dependency_overrides[require_admin] = lambda: None
    client = TestClient(app, backend="asyncio")
    try:
        agents_response = client.get(
            "/api/agents/provider-capabilities",
            headers={"X-Agents-Token": "irrelevant-in-auth-disabled-tests"},
        )
        admin_response = client.get("/api/admin/provider-capabilities")
    finally:
        api_app.dependency_overrides.clear()

    assert admin_response.status_code == 200
    payload = admin_response.json()
    assert payload["artifact_kind"] == "provider_capability_projection"
    assert payload["capabilities"]
    assert agents_response.status_code == 200
    assert admin_response.json() == agents_response.json()


def test_capability_projection_translates_malformed_schema_to_a_clean_500(monkeypatch, tmp_path: Path) -> None:
    # Review 2026-07-29: provider_capability_schema._load_schema() raises
    # SystemExit for a malformed schema -- correct for the Makefile-driven
    # CLI callers it predates, wrong for this endpoint, which is now a live
    # Runtime Host request path. SystemExit is a BaseException; left
    # untranslated it can take the worker down instead of returning a 5xx.
    client = _client(monkeypatch, tmp_path)

    def broken_load_capability_assertions():
        raise SystemExit("schemas/managed_providers.yml must contain a YAML mapping with a top-level 'providers' list")

    monkeypatch.setattr(routes, "load_capability_assertions", broken_load_capability_assertions)
    try:
        response = client.get("/api/agents/provider-capabilities")
    finally:
        api_app.dependency_overrides.clear()

    assert response.status_code == 500
    assert "providers" in response.json()["detail"]


def test_capability_projection_requires_agents_auth(monkeypatch, tmp_path: Path) -> None:
    client = _client(monkeypatch, tmp_path)

    def reject_machine():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing machine token")

    api_app.dependency_overrides[verify_agents_token] = reject_machine
    try:
        response = client.get("/api/agents/provider-capabilities")
    finally:
        api_app.dependency_overrides.clear()

    assert response.status_code == 401
