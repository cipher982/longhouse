from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import replace
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
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
from zerg.services.provider_capability_blob_resolver import factory_blob_key
from zerg.services.provider_capability_proof import LEGACY_PROOF_SCHEMA_VERSION
from zerg.services.provider_capability_proof import AssertionOutcome
from zerg.services.provider_capability_proof import EvidenceClass
from zerg.services.provider_capability_proof import ProviderCapabilityProofRecord
from zerg.services.provider_capability_proof_store import ProofPublication
from zerg.services.provider_capability_proof_store import ProviderCapabilityProofStore


def _blob_digest(label: str) -> str:
    return f"sha256:{hashlib.sha256(label.encode()).hexdigest()}"


_BLOB_CONTENT = {
    _blob_digest(label): label.encode() for label in ("raw", "epoch", "verifier", "census", "compile", "plan", "sandbox", "cleanup")
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


def _plan_projection_bytes(record: dict) -> bytes:
    producer_id, separator, revision = str(record["producer_version"]).rpartition("@")
    assert separator and revision.isdigit()
    projection = {
        "schema_version": 1,
        "artifact_kind": "provider_assurance_plan_cell_projection",
        "plan_digest": record["plan_digest"],
        "epoch_digest": record["accepted_epoch_digest"],
        "subject": {"longhouse_source_sha": record.get("longhouse_git_sha")},
        "command": {
            "subject_kind": record.get("subject_kind") or "provider_release",
            "subject_key": record.get("subject_key"),
            "provider": record.get("provider"),
            "assertion_id": record["assertion_id"],
            "variant": record.get("assertion_variant"),
            "scenario_id": record["scenario_id"],
            "scenario_revision": record["scenario_revision"],
            "producer_id": producer_id,
            "producer_revision": int(revision),
            "provider_contract_digest": record["provider_contract_digest"],
            "adapter_digest": record["adapter_digest"],
            "oracle_digest": record["oracle_digest"],
            "evidence_class": record["evidence_class"],
            "mode": record.get("mode"),
            "worker_platform": record.get("platform"),
            "worker_architecture": record.get("architecture"),
            "longhouse_source_sha": record.get("longhouse_git_sha"),
        },
    }
    return json.dumps(projection, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def _bundle(*records: ProviderCapabilityProofRecord, blob_contents: dict[str, bytes] | None = None) -> dict:
    contents = {**_BLOB_CONTENT, **(blob_contents or {})}
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
            {"digest": digest, "content_base64": base64.b64encode(contents[digest]).decode()}
            for digest in sorted(set().union(*(set(record.referenced_content_digests()) for record in records)))
        ],
        # Publisher claims are deliberately ignored. Trust is derived from the
        # authenticated request and exact records accepted by the Runtime Host.
        "trusted_artifact_ids": ["publisher-controlled-value"],
    }
    payload["bundle_digest"] = routes._bundle_digest(payload)
    return payload


def _product_bundle(*, projected_plan: bool = False) -> tuple[dict, str]:
    record = _record().serialize()
    record.pop("artifact_id")
    for name in (
        "provider",
        "provider_version",
        "provider_executable_identity",
        "provider_build_identity",
        "provider_build_granularity",
    ):
        record.pop(name, None)
    record.update(
        subject_kind="longhouse_product",
        subject_key="longhouse_product:sha256:" + "a" * 64,
        longhouse_git_sha="l" * 40,
        provenance_extension={
            "subject_kind": "longhouse_product",
            "subject_key": "longhouse_product:sha256:" + "a" * 64,
        },
    )
    contents = dict(_BLOB_CONTENT)
    if projected_plan:
        record["producer_version"] = "longhouse.test.v1@1"
        projection = _plan_projection_bytes(record)
        projection_digest = f"sha256:{hashlib.sha256(projection).hexdigest()}"
        record["provenance_extension"]["plan_projection_digest"] = projection_digest
        contents[projection_digest] = projection
    artifact_id = hashlib.sha256(json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()).hexdigest()
    record = {"artifact_id": artifact_id, **record}
    referenced = {
        *record["raw_reference_digests"],
        record["accepted_epoch_digest"],
        record["verifier_bundle_digest"],
        record["worker_census_digest"],
        record["compile_report_digest"],
        record["provenance_extension"].get("plan_projection_digest", record["plan_digest"]),
        record["sandbox_receipt_digest"],
        record["cleanup_receipt_digest"],
    }
    payload = {
        "schema_version": 3,
        "artifact_kind": "provider_capability_proof_bundle",
        "records": [record],
        "publication": {
            "worker_id": record["worker_id"],
            "worker_census_digest": record["worker_census_digest"],
            "auth_mechanism": record["auth_mechanism"],
            "published_at": "2026-07-22T18:01:00Z",
        },
        "blobs": [{"digest": digest, "content_base64": base64.b64encode(contents[digest]).decode()} for digest in sorted(referenced)],
    }
    payload["bundle_digest"] = routes._bundle_digest(payload)
    return payload, artifact_id


def _client(monkeypatch, tmp_path: Path, *, factory_token: str | None = "factory-secret") -> TestClient:
    store = ProviderCapabilityProofStore(tmp_path / "proofs", require_authenticated_publication=True)
    monkeypatch.setattr(routes, "_proof_store", lambda: store)
    monkeypatch.setattr(
        routes,
        "get_settings",
        lambda: SimpleNamespace(provider_capability_factory_token=factory_token),
    )
    api_app.dependency_overrides[verify_agents_token] = lambda: SimpleNamespace(device_id="machine-1", owner_id=1)
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    return TestClient(app, backend="asyncio")


def _factory_headers() -> dict[str, str]:
    return {"X-Provider-Capability-Factory-Token": "factory-secret"}


def _write_trusted(
    store: ProviderCapabilityProofStore,
    record: ProviderCapabilityProofRecord,
    *,
    blob_contents: dict[str, bytes] | None = None,
) -> None:
    contents = {**_BLOB_CONTENT, **(blob_contents or {})}
    for digest in record.referenced_content_digests():
        store.write_blob(contents[digest], expected_digest=digest)
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


def _write_reference_trusted(store: ProviderCapabilityProofStore, record: ProviderCapabilityProofRecord) -> None:
    refs = tuple(
        {
            "digest": digest,
            "byte_length": 0,
            "media_type": "application/octet-stream",
            "logical_role": "proof_evidence",
            "key": factory_blob_key(digest),
        }
        for digest in record.referenced_content_digests()
    )
    verification = tuple(
        {
            "digest": ref["digest"],
            "key": ref["key"],
            "content_length": ref["byte_length"],
            "content_type": ref["media_type"],
            "metadata_sha256": ref["digest"].removeprefix("sha256:"),
            "checksum_sha256": base64.b64encode(bytes.fromhex(ref["digest"].removeprefix("sha256:"))).decode("ascii"),
            "body_sha256": ref["digest"].removeprefix("sha256:"),
            "complete": True,
        }
        for ref in refs
    )
    publication_payload = {
        "worker_id": "factory-worker-1",
        "worker_census_digest": _blob_digest("census"),
        "auth_mechanism": "factory_token_v1",
        "published_at": "2026-07-22T18:01:00Z",
    }
    bundle = {
        "schema_version": 4,
        "artifact_kind": "provider_capability_proof_bundle",
        "records": [record.serialize()],
        "blobs": list(refs),
        "publication": publication_payload,
    }
    publication = ProofPublication(
        **publication_payload,
        bundle_digest=routes._bundle_digest(bundle),
        bundle_schema_version=4,
    )
    metadata = store.build_reference_metadata(
        record,
        bundle_digest=publication.bundle_digest,
        publication=publication,
        refs=refs,
        verification=verification,
    )
    store.write_reference_metadata(record, metadata)
    store.write(record, publication=publication)


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


def test_factory_publish_accepts_a_digest_bound_plan_cell_projection(monkeypatch, tmp_path: Path) -> None:
    base = _record(
        subject_kind="provider_release",
        subject_key="provider_release:sha256:" + "b" * 64,
        longhouse_git_sha="1" * 40,
        producer_version="codex.native_resume.v1@1",
    )
    projection = _plan_projection_bytes(base.canonical_payload())
    projection_digest = f"sha256:{hashlib.sha256(projection).hexdigest()}"
    record = replace(base, provenance_extension={"plan_projection_digest": projection_digest})
    bundle = _bundle(record, blob_contents={projection_digest: projection})
    declared = {item["digest"] for item in bundle["blobs"]}
    client = _client(monkeypatch, tmp_path)
    try:
        response = client.post("/api/internal/provider-capability-proofs", headers=_factory_headers(), json=bundle)
    finally:
        api_app.dependency_overrides.clear()

    assert response.status_code == 201, response.text
    assert projection_digest in declared
    assert record.plan_digest not in declared


def test_product_assurance_publish_is_archived_but_never_projected_as_a_provider(monkeypatch, tmp_path: Path) -> None:
    client = _client(monkeypatch, tmp_path)
    bundle, artifact_id = _product_bundle()
    try:
        first = client.post("/api/internal/provider-capability-proofs", headers=_factory_headers(), json=bundle)
        second = client.post("/api/internal/provider-capability-proofs", headers=_factory_headers(), json=bundle)
        fetched = client.get("/api/agents/provider-capability-proofs")
        projection = client.get("/api/agents/provider-capabilities")
    finally:
        api_app.dependency_overrides.clear()

    assert first.status_code == 201, first.text
    assert (
        second.json()
        == first.json()
        == {
            "schema_version": 2,
            "accepted": 1,
            "trusted_artifact_ids": [artifact_id],
        }
    )
    archive = tmp_path / "trusted-product-assurance"
    assert len(list((archive / "bundles").glob("*.json"))) == 1
    assert len(list((archive / "events" / artifact_id).glob("*.json"))) == 1
    assert json.loads(next((archive / "bundles").glob("*.json")).read_text()) == bundle
    assert artifact_id not in fetched.json()["trusted_artifact_ids"]
    assert all(item["proof_artifact_id"] != artifact_id for item in projection.json()["capabilities"])

    invalid, _ = _product_bundle()
    invalid["records"][0]["provider"] = "codex"
    invalid["records"][0]["artifact_id"] = hashlib.sha256(
        json.dumps(
            {key: value for key, value in invalid["records"][0].items() if key != "artifact_id"},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    invalid["bundle_digest"] = routes._bundle_digest(invalid)
    rejected = client.post("/api/internal/provider-capability-proofs", headers=_factory_headers(), json=invalid)
    assert rejected.status_code == 422
    assert "provider identity" in rejected.json()["detail"]


def test_product_assurance_accepts_a_digest_bound_plan_cell_projection(monkeypatch, tmp_path: Path) -> None:
    bundle, artifact_id = _product_bundle(projected_plan=True)
    record = bundle["records"][0]
    declared = {item["digest"] for item in bundle["blobs"]}
    projection_digest = record["provenance_extension"]["plan_projection_digest"]
    client = _client(monkeypatch, tmp_path)
    try:
        response = client.post("/api/internal/provider-capability-proofs", headers=_factory_headers(), json=bundle)
    finally:
        api_app.dependency_overrides.clear()

    assert response.status_code == 201, response.text
    assert response.json()["trusted_artifact_ids"] == [artifact_id]
    assert projection_digest in declared
    assert record["plan_digest"] not in declared


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
    row = next(item for item in projection["capabilities"] if item["provider"] == "codex" and item["assertion_id"] == legacy.assertion_id)
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


def test_non_ascii_factory_token_is_403_not_a_server_error(monkeypatch, tmp_path: Path) -> None:
    client = _client(monkeypatch, tmp_path)
    try:
        response = client.post(
            "/api/internal/provider-capability-proofs",
            headers={"X-Provider-Capability-Factory-Token": "caf\u00e9".encode("latin-1")},
            json=_bundle(_record()),
        )
    finally:
        api_app.dependency_overrides.clear()

    assert response.status_code == 403


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
        # Likewise the scenario revision: 496de4902 raised codex
        # coordination.awareness.create / coordination_instructions_model_visible
        # to minimum_scenario_revision 6 in schemas/managed_providers.yml, so a
        # stale revision 1 turns this row into proof_scenario_revision_mismatch
        # (unacceptable_evidence) instead of pass.
        scenario_revision=6,
    )
    _write_trusted(store, proof)
    claude_raw = b"claude-raw"
    claude_raw_digest = _blob_digest("claude-raw")
    claude_proof = _record(
        provider="claude",
        provider_version="2.1.0",
        scenario_id="claude_coordination_awareness_create",
        scenario_revision=4,
        assertion_id="coordination_instructions_model_visible",
        assertion_variant=None,
        outcome=AssertionOutcome.PASS,
        generated_at=generated_at,
        evidence_class=EvidenceClass.LIVE_TOKEN,
        invocation_id="factory-run-claude",
        raw_reference_digests=(claude_raw_digest,),
    )
    _write_trusted(store, claude_proof, blob_contents={claude_raw_digest: claude_raw})
    unrelated = b"unrelated-retained-evidence"
    store.write_blob(unrelated, expected_digest=_blob_digest("unrelated-retained-evidence"))
    client = _client(monkeypatch, tmp_path)
    try:
        response = client.get("/api/agents/provider-capabilities")
    finally:
        api_app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["artifact_kind"] == "provider_capability_projection"
    assert payload["projection_version"] == "assurance-projection-v1"
    assert payload["subject_fence"]["configured"] is False
    # assertion_id is not globally unique across providers -- e.g. both codex
    # and cursor declare "coordination_instructions_model_visible" for their
    # own coordination.awareness.create capability -- so the join and this
    # assertion must both key on (provider, assertion_id), not assertion_id
    # alone.
    by_key = {(row["provider"], row["assertion_id"]): row for row in payload["capabilities"]}
    proven = by_key[("codex", "coordination_instructions_model_visible")]
    assert proven["capability"] == "coordination.awareness.create"
    assert proven["disposition"] == "implemented"
    assert proven["proof_status"] == "pass"
    assert proven["generated_at"] == generated_at
    assert proven["proof_artifact_id"] == proof.artifact_id
    claude_proven = by_key[("claude", "coordination_instructions_model_visible")]
    assert claude_proven["proof_status"] == "pass"
    assert claude_proven["generated_at"] == generated_at
    assert claude_proven["proof_artifact_id"] == claude_proof.artifact_id
    assert claude_proven["proof_artifact_id"] != proven["proof_artifact_id"]
    cursor_proven = by_key.get(("cursor", "coordination_instructions_model_visible"))
    if cursor_proven is not None:
        assert cursor_proven["proof_status"] == "never_proven"
    # Every other declared assertion has no proof in this store at all --
    # the row must still exist, labeled, not silently dropped.
    unproven = [
        row
        for key, row in by_key.items()
        if key
        not in {
            ("codex", "coordination_instructions_model_visible"),
            ("claude", "coordination_instructions_model_visible"),
        }
    ]
    assert unproven
    assert all(row["proof_status"] == "never_proven" for row in unproven)
    assert all(row["generated_at"] is None for row in unproven)


@pytest.mark.parametrize("damage", ["mutate", "delete"])
def test_capability_projection_disqualifies_inline_proof_when_referenced_evidence_is_damaged(
    monkeypatch, tmp_path: Path, damage: str
) -> None:
    generated_at = datetime.now(UTC).isoformat()
    store = ProviderCapabilityProofStore(tmp_path / "proofs", require_authenticated_publication=True)
    monkeypatch.setattr(routes, "_proof_store", lambda: store)
    proof = _record(
        assertion_id="coordination_instructions_model_visible",
        assertion_variant=None,
        scenario_id="codex_coordination_awareness_create",
        scenario_revision=6,
        outcome=AssertionOutcome.PASS,
        evidence_class=EvidenceClass.LIVE_TOKEN,
        generated_at=generated_at,
    )
    _write_trusted(store, proof)
    digest = proof.raw_reference_digests[0]
    evidence_path = tmp_path / "proofs" / "_blobs" / "sha256" / digest.removeprefix("sha256:")
    if damage == "mutate":
        evidence_path.write_bytes(b"tampered evidence")
    else:
        evidence_path.unlink()

    client = _client(monkeypatch, tmp_path)
    try:
        response = client.get("/api/agents/provider-capabilities")
        proofs_response = client.get("/api/agents/provider-capability-proofs")
    finally:
        api_app.dependency_overrides.clear()

    assert response.status_code == 200
    row = next(
        item
        for item in response.json()["capabilities"]
        if (item["provider"], item["assertion_id"]) == ("codex", "coordination_instructions_model_visible")
    )
    assert row["proof_status"] == "unacceptable_evidence"
    assert "proof_referenced_content_missing" in row["admissibility_reasons"]
    assert proofs_response.status_code == 200
    proof_payload = proofs_response.json()
    retained = next(item for item in proof_payload["records"] if item["artifact_id"] == proof.artifact_id)
    assert retained["store_integrity"] == {"admissible": False, "reason_codes": ["proof_referenced_content_missing"]}
    assert proof.artifact_id not in proof_payload["trusted_artifact_ids"]


@pytest.mark.parametrize("digest", ["sha256:not-a-digest", "sha256:../../outside", "sha512:" + "a" * 64])
def test_malformed_retained_reference_is_rejected_without_breaking_metadata_reads(monkeypatch, tmp_path: Path, digest: str) -> None:
    store = ProviderCapabilityProofStore(tmp_path / "proofs", require_authenticated_publication=True)
    monkeypatch.setattr(routes, "_proof_store", lambda: store)
    assertion = routes.load_chip_edge_assertions()["codex"]["interrupt"][0]
    proof = _record(
        assertion_id=assertion.assertion_id,
        assertion_variant=assertion.variant,
        scenario_id=assertion.scenario_id,
        scenario_revision=assertion.minimum_scenario_revision,
        evidence_class=EvidenceClass(assertion.acceptable_evidence[0]),
        generated_at=datetime.now(UTC).isoformat(),
        raw_reference_digests=(digest,),
    )
    for reference in proof.referenced_content_digests():
        if reference in _BLOB_CONTENT:
            store.write_blob(_BLOB_CONTENT[reference], expected_digest=reference)
    store.write(
        proof,
        publication=ProofPublication(
            worker_id=proof.worker_id,
            worker_census_digest=proof.worker_census_digest,
            auth_mechanism=proof.auth_mechanism,
            published_at=proof.generated_at,
            bundle_digest=_blob_digest("malformed-retained-fixture"),
        ),
    )
    client = _client(monkeypatch, tmp_path)
    try:
        capabilities = client.get("/api/agents/provider-capabilities")
        retained = client.get("/api/agents/provider-capability-proofs")
        routes._certification_cache = None
        certification = client.get("/api/public/provider-certification")
    finally:
        api_app.dependency_overrides.clear()
        routes._certification_cache = None

    assert capabilities.status_code == retained.status_code == certification.status_code == 200
    row = next(
        row
        for row in capabilities.json()["capabilities"]
        if (row["provider"], row["assertion_id"], row["variant"]) == ("codex", proof.assertion_id, proof.assertion_variant)
    )
    assert row["proof_status"] == "unacceptable_evidence"
    assert "proof_referenced_content_missing" in row["admissibility_reasons"]
    record = next(record for record in retained.json()["records"] if record["artifact_id"] == proof.artifact_id)
    assert record["store_integrity"] == {"admissible": False, "reason_codes": ["proof_referenced_content_missing"]}
    codex = next(provider for provider in certification.json()["providers"] if provider["provider"] == "codex")
    assert codex["chips"]["interrupt"]["state"] != "certified"


def test_capability_projection_uses_older_intact_pass_when_latest_pass_evidence_is_tampered(monkeypatch, tmp_path: Path) -> None:
    moment = datetime.now(UTC)
    older_generated_at = (moment - timedelta(minutes=5)).isoformat()
    latest_generated_at = moment.isoformat()
    store = ProviderCapabilityProofStore(tmp_path / "proofs", require_authenticated_publication=True)
    monkeypatch.setattr(routes, "_proof_store", lambda: store)
    older = _record(
        assertion_id="coordination_instructions_model_visible",
        assertion_variant=None,
        scenario_id="codex_coordination_awareness_create",
        scenario_revision=6,
        outcome=AssertionOutcome.PASS,
        evidence_class=EvidenceClass.LIVE_TOKEN,
        generated_at=older_generated_at,
        invocation_id="factory-run-older",
    )
    latest_raw = b"latest-raw"
    latest_raw_digest = _blob_digest("latest-raw")
    latest = _record(
        assertion_id="coordination_instructions_model_visible",
        assertion_variant=None,
        scenario_id="codex_coordination_awareness_create",
        scenario_revision=6,
        outcome=AssertionOutcome.PASS,
        evidence_class=EvidenceClass.LIVE_TOKEN,
        generated_at=latest_generated_at,
        invocation_id="factory-run-latest",
        raw_reference_digests=(latest_raw_digest,),
    )
    _write_trusted(store, older)
    _write_trusted(store, latest, blob_contents={latest_raw_digest: latest_raw})
    latest_path = tmp_path / "proofs" / "_blobs" / "sha256" / latest_raw_digest.removeprefix("sha256:")
    latest_path.write_bytes(b"tampered latest evidence")

    client = _client(monkeypatch, tmp_path)
    try:
        response = client.get("/api/agents/provider-capabilities")
    finally:
        api_app.dependency_overrides.clear()

    assert response.status_code == 200
    row = next(
        item
        for item in response.json()["capabilities"]
        if (item["provider"], item["assertion_id"]) == ("codex", "coordination_instructions_model_visible")
    )
    assert row["proof_status"] == "pass"
    assert row["proof_artifact_id"] == older.artifact_id
    assert row["generated_at"] == older_generated_at
    assert row["latest_proof_artifact_id"] == latest.artifact_id
    assert row["latest_outcome"] == "pass"


def test_capability_projection_keeps_v4_reference_proof_without_local_evidence(monkeypatch, tmp_path: Path) -> None:
    generated_at = datetime.now(UTC).isoformat()
    store = ProviderCapabilityProofStore(tmp_path / "proofs", require_authenticated_publication=True)
    monkeypatch.setattr(routes, "_proof_store", lambda: store)
    proof = _record(
        assertion_id="coordination_instructions_model_visible",
        assertion_variant=None,
        scenario_id="codex_coordination_awareness_create",
        scenario_revision=6,
        outcome=AssertionOutcome.PASS,
        evidence_class=EvidenceClass.LIVE_TOKEN,
        generated_at=generated_at,
    )
    _write_reference_trusted(store, proof)
    assert not (tmp_path / "proofs" / "_blobs").exists()

    client = _client(monkeypatch, tmp_path)
    try:
        response = client.get("/api/agents/provider-capabilities")
    finally:
        api_app.dependency_overrides.clear()

    assert response.status_code == 200
    row = next(
        item
        for item in response.json()["capabilities"]
        if (item["provider"], item["assertion_id"]) == ("codex", "coordination_instructions_model_visible")
    )
    assert row["proof_status"] == "pass"
    assert row["proof_artifact_id"] == proof.artifact_id


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
