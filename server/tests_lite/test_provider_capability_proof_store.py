from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from zerg.services.provider_capability_proof import AssertionOutcome
from zerg.services.provider_capability_proof import EvidenceClass
from zerg.services.provider_capability_proof import ProviderCapabilityProofRecord
from zerg.services.provider_capability_proof_store import ProofPublication
from zerg.services.provider_capability_proof_store import ProviderCapabilityProofStore


def _record(**changes) -> ProviderCapabilityProofRecord:
    record = ProviderCapabilityProofRecord(
        provider="claude",
        provider_version="2.1.0",
        provider_executable_identity="sha256:provider",
        provider_contract_digest="sha256:contract",
        adapter_digest="sha256:adapter",
        scenario_id="coordination_awareness_create",
        scenario_revision=1,
        oracle_digest="sha256:oracle",
        assertion_id="peer_tool_visible",
        outcome=AssertionOutcome.PASS,
        evidence_class=EvidenceClass.HERMETIC,
        generated_at="2026-07-22T16:00:00Z",
        producer_class="local_machine",
        producer_version="2",
        invocation_id="run-123",
    )
    return replace(record, **changes)


def test_store_is_append_only_and_idempotent(tmp_path: Path) -> None:
    store = ProviderCapabilityProofStore(tmp_path)
    first = _record()
    second = _record(
        generated_at="2026-07-22T16:01:00Z",
        outcome=AssertionOutcome.INFRASTRUCTURE_ERROR,
        invocation_id="run-456",
    )

    first_path = store.write(first)
    assert store.write(first) == first_path
    store.write(second)

    assert store.records("claude") == (first, second)
    assert first_path.name == f"{first.artifact_id}.json"
    index = json.loads((tmp_path / "claude" / "index.json").read_text())
    assert index["artifact_ids"] == [first.artifact_id, second.artifact_id]


def test_store_reads_records_without_trusting_index(tmp_path: Path) -> None:
    store = ProviderCapabilityProofStore(tmp_path)
    record = _record()
    store.write(record)
    (tmp_path / "claude" / "index.json").write_text('{"artifact_ids": []}\n')

    assert store.records("claude") == (record,)
    store.rebuild_index("claude")
    index = json.loads((tmp_path / "claude" / "index.json").read_text())
    assert index["artifact_ids"] == [record.artifact_id]


def test_concurrent_identical_writers_are_idempotent(tmp_path: Path) -> None:
    store = ProviderCapabilityProofStore(tmp_path)
    record = _record()

    with ThreadPoolExecutor(max_workers=8) as pool:
        paths = list(pool.map(lambda _: store.write(record), range(16)))

    assert len(set(paths)) == 1
    assert store.records("claude") == (record,)


def test_store_rejects_tampered_record(tmp_path: Path) -> None:
    store = ProviderCapabilityProofStore(tmp_path)
    path = store.write(_record())
    payload = json.loads(path.read_text())
    payload["provider_version"] = "tampered"
    path.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="artifact_id does not match"):
        store.records("claude")


def test_trusted_store_requires_publication_and_detects_missing_blob(tmp_path: Path) -> None:
    content = b"retained evidence"
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    epoch = b"accepted epoch"
    epoch_digest = f"sha256:{hashlib.sha256(epoch).hexdigest()}"
    census = b"worker census"
    census_digest = f"sha256:{hashlib.sha256(census).hexdigest()}"
    record = _record(
        raw_reference_digests=(digest,),
        assertion_variant="clean_exit",
        accepted_epoch_id="epoch-1",
        accepted_epoch_digest=epoch_digest,
        worker_id="worker-1",
        worker_census_digest=census_digest,
        auth_mechanism="factory_token_v1",
    )
    store = ProviderCapabilityProofStore(tmp_path, require_authenticated_publication=True)
    with pytest.raises(ValueError, match="requires authenticated"):
        store.write(record)
    store.write_blob(content, expected_digest=digest)
    store.write_blob(epoch, expected_digest=epoch_digest)
    store.write_blob(census, expected_digest=census_digest)
    store.write(
        record,
        publication=ProofPublication(
            worker_id="worker-1",
            worker_census_digest=record.worker_census_digest or "",
            auth_mechanism="factory_token_v1",
            published_at="2026-08-03T00:00:00Z",
            bundle_digest="sha256:" + "c" * 64,
        ),
    )
    assert store.integrity_report("claude").admissible_artifact_ids == {record.artifact_id}

    (tmp_path / "_blobs" / "sha256" / digest.removeprefix("sha256:")).unlink()

    report = store.integrity_report("claude")
    assert not report.admissible_artifact_ids
    assert report.artifacts[0].reason_codes == ("proof_referenced_content_missing",)


def test_trusted_store_accepts_many_proofs_for_one_epoch_and_rejects_tampered_event(tmp_path: Path) -> None:
    epoch = b"accepted epoch"
    epoch_digest = f"sha256:{hashlib.sha256(epoch).hexdigest()}"
    census = b"worker census"
    census_digest = f"sha256:{hashlib.sha256(census).hexdigest()}"
    record = _record(
        assertion_variant="clean_exit",
        accepted_epoch_id="epoch-1",
        accepted_epoch_digest=epoch_digest,
        worker_id="worker-1",
        worker_census_digest=census_digest,
        auth_mechanism="factory_token_v1",
    )
    second = replace(record, assertion_variant="process_loss", invocation_id="run-456")
    publication = ProofPublication(
        worker_id="worker-1",
        worker_census_digest=record.worker_census_digest or "",
        auth_mechanism="factory_token_v1",
        published_at="2026-08-03T00:00:00Z",
        bundle_digest="sha256:" + "c" * 64,
    )
    store = ProviderCapabilityProofStore(tmp_path, require_authenticated_publication=True)
    store.write_blob(epoch, expected_digest=epoch_digest)
    store.write_blob(census, expected_digest=census_digest)

    store.write(record, publication=publication)
    store.write(second, publication=publication)

    assert store.integrity_report("claude").admissible_artifact_ids == {
        record.artifact_id,
        second.artifact_id,
    }
    event_path = next((tmp_path / "_events" / "claude").glob("*.json"))
    event = json.loads(event_path.read_text())
    event["bundle_digest"] = "tampered"
    event_path.write_text(json.dumps(event))
    report = store.integrity_report("claude")
    by_id = {item.artifact_id: item for item in report.artifacts}
    assert "proof_authenticated_publication_missing" in by_id[event["artifact_id"]].reason_codes


def test_orphan_scan_is_global_instead_of_mislabeling_another_provider_blob(tmp_path: Path) -> None:
    store = ProviderCapabilityProofStore(tmp_path)
    content = b"cursor evidence"
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    store.write_blob(content, expected_digest=digest)
    store.write(_record(provider="cursor", raw_reference_digests=(digest,)))

    assert store.integrity_report("claude").orphan_blob_digests == ()


@pytest.mark.parametrize("provider", ["../claude", "Claude", "", "a/b"])
def test_store_rejects_unsafe_provider_paths(tmp_path: Path, provider: str) -> None:
    store = ProviderCapabilityProofStore(tmp_path)

    with pytest.raises(ValueError, match="invalid provider"):
        store.records(provider)
