from __future__ import annotations

import base64
import hashlib
import io
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.testclient import TestClient

from zerg.auth.caller import Caller
from zerg.auth.managed_session_tokens import ManagedSessionToken
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_caller
from zerg.routers import provider_capability_proofs as routes
from zerg.services.provider_capability_blob_resolver import FactoryBlobReference
from zerg.services.provider_capability_blob_resolver import ProviderCapabilityBlobResolver
from zerg.services.provider_capability_blob_resolver import ProviderCapabilityBlobResolverConfig
from zerg.services.provider_capability_blob_resolver import ProviderCapabilityBlobTampered
from zerg.services.provider_capability_blob_resolver import factory_blob_key
from zerg.services.provider_capability_proof import AssertionOutcome
from zerg.services.provider_capability_proof import EvidenceClass
from zerg.services.provider_capability_proof import ProviderCapabilityProofRecord
from zerg.services.provider_capability_proof_store import ProviderCapabilityProofStore


def _digest(label: str) -> str:
    return f"sha256:{hashlib.sha256(label.encode()).hexdigest()}"


def _record(**changes) -> ProviderCapabilityProofRecord:
    record = ProviderCapabilityProofRecord(
        provider="codex",
        provider_version="0.145.0",
        provider_executable_identity=_digest("provider"),
        provider_contract_digest=_digest("contract"),
        adapter_digest=_digest("adapter"),
        scenario_id="codex_helm_interrupt",
        scenario_revision=1,
        oracle_digest=_digest("oracle"),
        assertion_id="interrupt_acknowledged",
        outcome=AssertionOutcome.PASS,
        evidence_class=EvidenceClass.LIVE_NO_TOKEN,
        generated_at="2026-07-22T18:00:00Z",
        producer_class="release_factory",
        producer_version="1",
        invocation_id="factory-run-123",
        run_reference="factory-test/run-1",
        raw_reference_digests=(_digest("raw"),),
        assertion_variant="clean_exit",
        factory_source_sha="f" * 40,
        accepted_epoch_id="helm-resume-v1-test-proof-v4",
        accepted_epoch_digest=_digest("epoch"),
        verifier_bundle_digest=_digest("verifier"),
        compile_report_digest=_digest("compile"),
        plan_digest=_digest("plan"),
        sandbox_receipt_digest=_digest("sandbox"),
        cleanup_receipt_digest=_digest("cleanup"),
        worker_id="factory-worker-1",
        worker_census_digest=_digest("census"),
        acquisition_provenance={"method": "staged_release", "source": "official"},
        auth_mechanism="factory_token_v1",
        observed_activity=("native_resume_command",),
        credential_binding_facts={"provider_token": "admitted"},
    )
    return replace(record, **changes)


def _v4_bundle(record: ProviderCapabilityProofRecord, extra_content: dict[str, bytes] | None = None) -> tuple[dict, dict[str, bytes]]:
    labels = ("raw", "epoch", "verifier", "compile", "plan", "sandbox", "cleanup", "census")
    contents = {_digest(label): label.encode() for label in labels}
    contents.update(extra_content or {})
    contents = {digest: contents[digest] for digest in record.referenced_content_digests()}
    refs = [
        {
            "digest": digest,
            "byte_length": len(content),
            "media_type": "application/json",
            "logical_role": "proof_evidence",
            "key": factory_blob_key(digest),
        }
        for digest, content in sorted(contents.items())
    ]
    bundle = {
        "schema_version": 4,
        "artifact_kind": "provider_capability_proof_bundle",
        "records": [record.serialize()],
        "blobs": refs,
        "publication": {
            "worker_id": record.worker_id,
            "worker_census_digest": record.worker_census_digest,
            "auth_mechanism": record.auth_mechanism,
            "published_at": "2026-07-22T18:01:00Z",
        },
    }
    bundle["bundle_digest"] = routes._bundle_digest(bundle)
    return bundle, contents


class _MemoryResolver(ProviderCapabilityBlobResolver):
    def __init__(self, contents: dict[str, bytes]) -> None:
        self.contents = dict(contents)
        self.calls: list[str] = []
        config = ProviderCapabilityBlobResolverConfig("https://objects.example", "factory", "us-east-1", "fixture-id", "fixture-only")
        super().__init__(config, client=SimpleNamespace(get_object=self._get))

    def _get(self, *, Bucket, Key, ChecksumMode):
        digest = f"sha256:{Key.rsplit('/', 1)[-1]}"
        self.calls.append(digest)
        if digest not in self.contents:
            raise ClientError({"Error": {"Code": "NoSuchKey"}, "ResponseMetadata": {"HTTPStatusCode": 404}}, "GetObject")
        content = self.contents[digest]
        raw = digest.removeprefix("sha256:")
        return {
            "ContentLength": len(content),
            "ContentType": "application/json",
            "Metadata": {"sha256": raw},
            "ChecksumSHA256": base64.b64encode(bytes.fromhex(raw)).decode(),
            "Body": io.BytesIO(content),
        }


def _client(monkeypatch, tmp_path: Path, resolver: _MemoryResolver) -> TestClient:
    store = ProviderCapabilityProofStore(tmp_path / "proofs", require_authenticated_publication=True)
    monkeypatch.setattr(routes, "_proof_store", lambda: store)
    monkeypatch.setattr(routes, "get_settings", lambda: SimpleNamespace(provider_capability_factory_token="fixture-factory-token"))
    monkeypatch.setattr(routes, "_blob_resolver", lambda: resolver)
    api_app = FastAPI()
    api_app.include_router(routes.router, prefix="/api")
    api_app.dependency_overrides[verify_agents_caller] = lambda: Caller(
        owner_id=1, principal=SimpleNamespace(owner_id=1, device_id="fixture-machine")
    )
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    return TestClient(api_app)


def _publish(client, bundle):
    return client.post(
        "/api/internal/provider-capability-proofs", headers={"X-Provider-Capability-Factory-Token": "fixture-factory-token"}, json=bundle
    )


def _evidence_url(digest):
    return f"/api/agents/provider-capability-proofs/blobs/{digest.removeprefix('sha256:')}"


def test_large_reference_proof_verifies_bytes_without_copying_them_to_runtime(monkeypatch, tmp_path):
    large = json.dumps({"manifest": "x" * (2 * 1024 * 1024 + 1)}).encode()
    digest = f"sha256:{hashlib.sha256(large).hexdigest()}"
    record = _record(provider_build_identity=digest, provider_build_granularity="full_installed_tree")
    bundle, contents = _v4_bundle(record, {digest: large})
    resolver = _MemoryResolver(contents)
    with _client(monkeypatch, tmp_path, resolver) as client:
        response = _publish(client, bundle)
        assert response.status_code == 201, response.text
        verified_reads = list(resolver.calls)
        listed = client.get("/api/agents/provider-capability-proofs")
        assert record.artifact_id in listed.json()["trusted_artifact_ids"]
        assert resolver.calls == verified_reads, "metadata reads must not download remote evidence again"
        fetched = client.get(_evidence_url(digest))
        assert fetched.status_code == 200, fetched.text
        assert fetched.content == large
        assert fetched.headers["content-disposition"].startswith("attachment;")
        assert fetched.headers["x-content-type-options"] == "nosniff"
        assert fetched.headers["x-provider-capability-sha256"] == digest
    assert set(verified_reads) == set(contents)
    assert not (tmp_path / "proofs" / "_blobs").exists()


@pytest.mark.parametrize("damage", ["delete_metadata", "delete_both_markers", "tamper_admission", "rewrite_metadata_and_admission"])
def test_reference_metadata_damage_cannot_preserve_admissibility(monkeypatch, tmp_path, damage):
    record = _record()
    bundle, contents = _v4_bundle(record)
    with _client(monkeypatch, tmp_path, _MemoryResolver(contents)) as client:
        response = _publish(client, bundle)
        assert response.status_code == 201, response.text
        metadata_path = tmp_path / "proofs" / "_references" / record.provider / f"{record.artifact_id}.json"
        admission_path = tmp_path / "proofs" / "_reference_admissions" / record.provider / f"{record.artifact_id}.json"
        if damage in {"delete_metadata", "delete_both_markers"}:
            metadata_path.unlink()
            if damage == "delete_both_markers":
                admission_path.unlink()
                # Local bytes do not turn an attested v4 record back into v3.
                store = routes._proof_store()
                for digest, content in contents.items():
                    store.write_blob(content, expected_digest=digest)
        else:
            admission = json.loads(admission_path.read_text())
            if damage == "tamper_admission":
                admission["metadata_digest"] = _digest("tampered")
            else:
                metadata = json.loads(metadata_path.read_text())
                ref = metadata["refs"][0]
                ref["byte_length"] += 1
                next(item for item in metadata["verification"] if item["digest"] == ref["digest"])["content_length"] += 1
                unsigned = {key: value for key, value in metadata.items() if key != "metadata_digest"}
                metadata["metadata_digest"] = (
                    f"sha256:{hashlib.sha256(json.dumps(unsigned, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode()).hexdigest()}"
                )
                metadata_path.write_text(json.dumps(metadata))
                admission["metadata_digest"] = metadata["metadata_digest"]
            admission_path.write_text(json.dumps(admission))
        listed = client.get("/api/agents/provider-capability-proofs")
        assert record.artifact_id not in listed.json()["trusted_artifact_ids"]
        assert client.get(_evidence_url(_digest("raw"))).status_code == 404


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_bad_remote_evidence_never_creates_a_trusted_record(monkeypatch, tmp_path, damage):
    record = _record()
    bundle, contents = _v4_bundle(record)
    resolver = _MemoryResolver(contents)
    if damage == "missing":
        del resolver.contents[_digest("cleanup")]
    else:
        resolver.contents[_digest("cleanup")] = b"x" * len(contents[_digest("cleanup")])
    with _client(monkeypatch, tmp_path, resolver) as client:
        response = _publish(client, bundle)
        assert response.status_code == 422, response.text
        assert not routes._proof_store().records(record.provider)


@pytest.mark.parametrize("damage", ["too_many", "too_large", "compression", "encryption", "duplicate_record", "inline_record"])
def test_invalid_v4_metadata_is_rejected_before_storage_io(monkeypatch, tmp_path, damage):
    bundle, contents = _v4_bundle(_record())
    if damage == "too_many":
        bundle["blobs"] *= 100
    elif damage == "too_large":
        for ref in bundle["blobs"]:
            ref["byte_length"] = 8 * 1024 * 1024
    elif damage in {"compression", "encryption"}:
        bundle["blobs"][0][damage] = {"unsupported": True}
    elif damage == "duplicate_record":
        bundle["records"] *= 2
    else:
        bundle["records"][0]["provenance_extension"] = {"content_base64": "eA=="}
    bundle["bundle_digest"] = routes._bundle_digest(bundle)
    resolver = _MemoryResolver(contents)
    with _client(monkeypatch, tmp_path, resolver) as client:
        response = _publish(client, bundle)
        assert response.status_code == 422, response.text
        assert not resolver.calls


def test_scoped_managed_session_cannot_read_owner_wide_evidence(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path, _MemoryResolver({})) as client:
        client.app.dependency_overrides[verify_agents_caller] = lambda: Caller(
            owner_id=1, principal=ManagedSessionToken(owner_id=1, session_id="session", scope="hook")
        )
        assert client.get(_evidence_url(_digest("raw"))).status_code == 403


def test_v4_cannot_reencode_a_published_v3_artifact(monkeypatch, tmp_path):
    record = _record()
    v4, contents = _v4_bundle(record)
    v3 = {
        **v4,
        "schema_version": 3,
        "blobs": [{"digest": digest, "content_base64": base64.b64encode(content).decode()} for digest, content in sorted(contents.items())],
    }
    v3["bundle_digest"] = routes._bundle_digest(v3)
    with _client(monkeypatch, tmp_path, _MemoryResolver(contents)) as client:
        assert _publish(client, v3).status_code == 201
        assert _publish(client, v4).status_code == 422
        report = routes._proof_store().integrity_report(record.provider)
        assert record.artifact_id in report.admissible_artifact_ids


def test_resolver_rejects_digest_corruption_even_with_correct_headers():
    content = b"expected evidence"
    digest = _digest("expected evidence")
    reference = FactoryBlobReference(digest, len(content), "application/json", "proof_evidence", factory_blob_key(digest))
    resolver = _MemoryResolver({digest: b"x" * len(content)})
    with pytest.raises(ProviderCapabilityBlobTampered, match="digest"):
        resolver.resolve(reference, collect=True)
